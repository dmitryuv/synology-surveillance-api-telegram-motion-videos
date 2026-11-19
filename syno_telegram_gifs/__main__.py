"""Entry point: config, logging, camera discovery and worker lifecycle."""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

from . import media, telegram
from .config import ConfigError, load
from .store import EventStore
from .synology import SynologyError, SurveillanceClient
from .worker import Camera, CameraWorker

LOG_FORMAT = "[%(asctime)s] [%(levelname)s] (%(threadName)-10s) %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="syno_telegram_gifs",
        description="Send Surveillance Station motion events to Telegram.",
    )
    parser.add_argument("config", help="path to config.json")
    parser.add_argument("--once", action="store_true",
                        help="poll every camera once and exit (for checks)")
    parser.add_argument("--dry-run", action="store_true",
                        help="download and convert, but do not send or advance the checkpoint")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="override log_level from the config")
    return parser.parse_args(argv)


def _prepare_folder(folder, label):
    path = Path(folder)
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        raise ConfigError("%s is not writable: %s (%s)" % (label, folder, e))


def _log_cameras(infos, active):
    """Print Camera.List with a tick marking the cameras this instance polls."""
    logging.info("Surveillance Station cameras (✓ = polled by this instance):")
    for info in infos:
        try:
            camera_id = int(info["id"])
        except (KeyError, TypeError, ValueError):
            camera_id = None
        mark = "✓" if camera_id in active else " "
        logging.info("%s id=%s name=%s host=%s ownerDsId=%s",
                     mark, info.get("id"), info.get("name"), info.get("host"),
                     info.get("ownerDsId"))


def _build_cameras(config, infos):
    by_id = {}
    for info in infos:
        try:
            by_id[int(info["id"])] = info
        except (KeyError, TypeError, ValueError):
            continue

    cameras = []
    for settings in config.synology_cameras:
        if not settings.enabled:
            logging.info("camera %i is disabled in the config, skipping", settings.id)
            continue
        info = by_id.get(settings.id)
        if info is None:
            logging.warning("camera %i is not in Camera.List, skipping", settings.id)
            continue
        try:
            ds_id = int(info.get("ownerDsId") or 0)
        except (TypeError, ValueError):
            logging.warning("camera %i has an invalid ownerDsId, skipping", settings.id)
            continue
        cameras.append(Camera(
            config=settings,
            ds_id=ds_id,
            src_type=2 if ds_id > 0 else 0,
            name=str(info.get("name") or ""),
            bot_token=settings.tele_bot_token,
            chat_id=settings.tele_chat_id,
        ))

    _log_cameras(infos, {camera.config.id for camera in cameras})
    return cameras


def _install_signal_handlers(stop_event):
    def handler(signum, _frame):
        logging.info("Received %s, stopping", signal.Signals(signum).name)
        stop_event.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, datefmt=LOG_DATEFMT)

    try:
        config = load(args.config)
    except ConfigError as e:
        logging.error("Config error: %s", e)
        return 2

    logging.getLogger().setLevel(args.log_level or config.log_level)
    # urllib3 logs full request URLs at DEBUG, including the session id in the
    # query string, so keep it quiet even in DEBUG mode.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    if not config.syno_verify_ssl:
        import urllib3
        from urllib3.exceptions import InsecureRequestWarning
        urllib3.disable_warnings(InsecureRequestWarning)

    logging.info("Starting (%s mode)", "dry-run" if args.dry_run else "normal")

    try:
        media.check_ffmpeg()
        _prepare_folder(config.data_folder, "data_folder")
        _prepare_folder(config.ffmpeg_working_folder, "ffmpeg_working_folder")
    except (media.MediaError, ConfigError) as e:
        logging.error("%s", e)
        return 2

    store = EventStore(config.data_folder)
    client = SurveillanceClient(
        config.synology_base_api_url,
        config.synology_user,
        config.synology_password,
        connect_timeout=config.syno_timeout_connect,
        read_timeout=config.syno_timeout_read,
        verify=config.syno_verify_ssl,
    )
    telegram.setup(config)
    notifier = telegram.Notifier(config)
    encoder_lock = threading.Lock()
    stop_event = threading.Event()

    try:
        try:
            infos = client.cameras()
        except SynologyError as e:
            logging.error("Cannot read the camera list: %s", e)
            return 1

        if not config.synology_cameras:
            _log_cameras(infos, set())
            logging.info("synology_cameras is empty; copy the ids above into the config")
            return 0

        cameras = _build_cameras(config, infos)
        if not cameras:
            logging.error("None of the configured cameras exist on this server")
            return 1

        workers = [
            CameraWorker(camera, config=config, client=client, store=store,
                         notifier=notifier, encoder_lock=encoder_lock,
                         stop_event=stop_event, dry_run=args.dry_run)
            for camera in cameras
        ]

        if args.once:
            failures = 0
            try:
                for worker in workers:
                    try:
                        worker.poll_once()
                    except Exception:
                        logging.exception("Poll of %s failed", worker.camera.label())
                        failures += 1
            except KeyboardInterrupt:
                logging.info("Interrupted")
                return 130
            return 1 if failures else 0

        _install_signal_handlers(stop_event)
        for worker in workers:
            worker.start()
        logging.info("%i camera worker(s) started", len(workers))
        while not stop_event.is_set():
            stop_event.wait(1)
        for worker in workers:
            worker.join(timeout=30)
        logging.info("All workers stopped")
        return 0

    finally:
        client.logout()
        store.close()


if __name__ == "__main__":
    sys.exit(main())