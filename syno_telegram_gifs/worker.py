"""Per-camera polling pipeline: download -> convert -> send -> checkpoint.

One thread per camera does the network work (poll, download) in parallel, while
conversion takes a shared lock so only one ffmpeg runs at a time - the NAS/DVA
CPU is the bottleneck, not the network.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import media
from .config import CameraConfig
from .media import MediaError
from .synology import DOWNLOAD_MARGIN_SECS, SynologyError

MAX_EVENT_ATTEMPTS = 3


@dataclass
class Camera:
    """Static config plus the runtime fields filled from Camera.List."""

    config: CameraConfig
    ds_id: int
    src_type: int
    name: str
    bot_token: str
    chat_id: int

    def label(self):
        return self.name or "camera %i" % self.config.id


def _remove_file(path):
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as e:
        logging.warning("Cannot remove file %s: %s", path, e)


class CameraWorker(threading.Thread):
    def __init__(self, camera, *, config, client, store, notifier, encoder_lock,
                 stop_event, dry_run=False):
        super().__init__(name="cam-%i" % camera.config.id, daemon=True)
        self.camera = camera
        self._config = config
        self._client = client
        self._store = store
        self._notifier = notifier
        self._encoder_lock = encoder_lock
        self._stop_event = stop_event
        self._dry_run = dry_run
        self._failures = {}

    def stop(self):
        self._stop_event.set()

    def _advance(self, event_id):
        """Move the checkpoint, unless this is a dry run."""
        if self._dry_run:
            logging.info("%s: dry-run, not advancing the checkpoint past event %i",
                         self.camera.label(), event_id)
            return
        self._store.set_checkpoint(self.camera.config.id, event_id)

    def run(self):
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                logging.exception("Unhandled error while polling %s; continuing",
                                  self.camera.label())
            self._stop_event.wait(self._config.poll_interval_secs)

    # -- pipeline ---------------------------------------------------------

    def poll_once(self):
        camera = self.camera
        settings = camera.config
        min_duration = settings.skip_first_n_secs + settings.max_length_secs

        checkpoint = self._store.checkpoint(settings.id)
        if checkpoint is None:
            self._seed_checkpoint()
            return

        events = self._client.events(settings.id, camera.src_type, camera.ds_id, checkpoint)
        if not events:
            return

        events = self._drop_stale(events)

        for event in events:
            if self._stop_event.is_set():
                return
            if event.dual_rec_mode:
                logging.info("%s: skipping dual-recording chunk event %i",
                             camera.label(), event.id)
                self._advance(event.id)
                continue
            if not event.is_complete(min_duration):
                logging.info("%s: event %i is still recording (%.1fs < %.1fs), "
                             "waiting for the next poll",
                             camera.label(), event.id, event.duration, min_duration)
                return
            if not self._process(event):
                return
            self._advance(event.id)

    def _seed_checkpoint(self):
        """First run: point the checkpoint at the newest event, send nothing."""
        camera = self.camera
        event = self._client.latest_event(camera.config.id, camera.src_type, camera.ds_id)
        if event is None:
            logging.info("%s: no events yet, checkpoint stays empty", camera.label())
            return
        if self._dry_run:
            logging.info("%s: dry-run, would seed the checkpoint at event %i",
                         camera.label(), event.id)
            return
        self._store.set_checkpoint(camera.config.id, event.id)
        logging.info("%s: first run, checkpoint seeded at event %i (nothing sent)",
                     camera.label(), event.id)

    def _drop_stale(self, events):
        backlog = self._config.event_backlog_secs
        if backlog <= 0:
            return events
        cutoff = time.time() - backlog
        stale = [event for event in events if event.stop_time < cutoff]
        if not stale:
            return events
        self._advance(stale[-1].id)
        logging.warning("%s: skipping %i event(s) older than %.0fs",
                        self.camera.label(), len(stale), backlog)
        return [event for event in events if event.stop_time >= cutoff]

    def _process(self, event):
        """Download, convert and deliver one event.

        Returns True when the checkpoint may advance: delivery succeeded, or it
        failed after the bounded send retries (the same semantics as before).
        Download/convert failures are retried MAX_EVENT_ATTEMPTS times and then
        dropped with an error, so a broken event cannot loop forever.
        """
        camera = self.camera
        settings = camera.config
        workdir = Path(self._config.ffmpeg_working_folder)
        source = workdir / ("cam%i_event%i.mp4" % (settings.id, event.id))
        if self._config.format == "gif":
            output = workdir / ("cam%i_event%i.gif" % (settings.id, event.id))
        else:
            output = workdir / ("cam%i_event%i_video.mp4" % (settings.id, event.id))

        min_duration = settings.skip_first_n_secs + settings.max_length_secs
        play_time_ms = int((min_duration + DOWNLOAD_MARGIN_SECS) * 1000)

        try:
            self._client.download(event, play_time_ms, source)
            if self._config.format == "gif":
                args = media.build_gif_args(
                    source, output,
                    skip_secs=settings.skip_first_n_secs,
                    length_secs=settings.max_length_secs,
                    video_max_height=settings.video_max_height,
                    fps=settings.fps,
                )
            else:
                args = media.build_mp4_args(
                    source, output,
                    skip_secs=settings.skip_first_n_secs,
                    length_secs=settings.max_length_secs,
                    video_max_height=settings.video_max_height,
                    fps=settings.fps,
                    preset=self._config.video_preset,
                )
            with self._encoder_lock:
                media.convert(args)
        except (SynologyError, MediaError, OSError) as e:
            _remove_file(output)
            return self._fail(event, e)
        finally:
            _remove_file(source)

        try:
            if self._dry_run:
                try:
                    size = output.stat().st_size
                except OSError:
                    size = -1
                logging.info("%s: dry-run, would send event %i (%s, %i bytes)",
                             camera.label(), event.id, output.name, size)
                delivered = True
            else:
                delivered = self._notifier.send(
                    settings.id, camera.bot_token, camera.chat_id, output,
                    camera.label(), self._config.format,
                )
        finally:
            _remove_file(output)

        self._failures.pop(event.id, None)
        if delivered:
            logging.info("%s: done with event %i", camera.label(), event.id)
        else:
            logging.error("%s: sending event %i failed after %i attempts; "
                          "event dropped", camera.label(), event.id,
                          self._config.tele_send_attempts)
        return True

    def _fail(self, event, error):
        count = self._failures.get(event.id, 0) + 1
        self._failures[event.id] = count
        logging.error("%s: event %i failed (%i/%i): %s: %s",
                      self.camera.label(), event.id, count, MAX_EVENT_ATTEMPTS,
                      type(error).__name__, error)
        if count >= MAX_EVENT_ATTEMPTS:
            logging.error("%s: giving up on event %i after %i attempts; "
                          "advancing the checkpoint", self.camera.label(),
                          event.id, count)
            self._failures.pop(event.id, None)
            return True
        return False