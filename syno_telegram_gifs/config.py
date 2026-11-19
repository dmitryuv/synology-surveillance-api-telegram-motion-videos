"""Configuration loading and validation.

The config file keeps the keys of the original single-file app, so an existing
config.json keeps working. New keys are optional and default to the previous
behaviour.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    """Raised when the config file is missing, malformed or inconsistent."""


GLOBAL_KEYS = {
    "data_folder", "ffmpeg_working_folder", "format", "video_preset",
    "tele_bot_token", "tele_chat_id", "tele_base_url", "proxy",
    "tele_connect_timeout", "tele_read_timeout", "tele_send_attempts",
    "tele_retry_backoff_secs", "synology_base_api_url", "synology_user",
    "synology_password", "synology_cameras",
    "syno_timeout_connect", "syno_timeout_read", "syno_verify_ssl",
    "poll_interval_secs", "event_backlog_secs", "log_level",
}
CAMERA_KEYS = {
    "id", "skip_first_n_secs", "max_length_secs", "video_max_height", "fps",
    "tele_bot_token", "tele_chat_id", "enabled",
}

FORMATS = {"mp4", "gif"}
LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}


@dataclass(frozen=True)
class CameraConfig:
    """Static per-camera settings from the config file."""

    id: int
    skip_first_n_secs: float
    max_length_secs: float
    video_max_height: int
    fps: int
    tele_bot_token: str | None
    tele_chat_id: int | None
    enabled: bool = True


@dataclass(frozen=True)
class Config:
    synology_base_api_url: str
    synology_user: str
    synology_password: str
    synology_cameras: tuple[CameraConfig, ...]

    data_folder: str
    ffmpeg_working_folder: str
    format: str
    video_preset: str

    tele_bot_token: str | None
    tele_chat_id: int | None
    tele_base_url: str | None
    proxy: str | None
    tele_connect_timeout: float
    tele_read_timeout: float
    tele_send_attempts: int
    tele_retry_backoff_secs: float

    syno_timeout_connect: float
    syno_timeout_read: float
    syno_verify_ssl: bool
    poll_interval_secs: float
    event_backlog_secs: float
    log_level: str


def _number(raw, key, default, *, minimum=None, positive=False):
    """Coerce a config value to float and check its range."""
    if raw is None:
        raw = default
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        raise ConfigError("%s: expected a number, got %r" % (key, raw))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ConfigError("%s: expected a number, got %r" % (key, raw))
    if positive and value <= 0:
        raise ConfigError("%s: must be greater than 0, got %r" % (key, raw))
    if minimum is not None and value < minimum:
        raise ConfigError("%s: must be >= %s, got %r" % (key, minimum, raw))
    return value


def _integer(raw, key, default, *, minimum=None, positive=False):
    value = _number(raw, key, default, minimum=minimum, positive=positive)
    if value != int(value):
        raise ConfigError("%s: expected a whole number, got %r" % (key, raw))
    return int(value)


def _boolean(raw, key, default):
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        if raw.lower() in {"true", "yes", "1"}:
            return True
        if raw.lower() in {"false", "no", "0"}:
            return False
    raise ConfigError("%s: expected a boolean, got %r" % (key, raw))


def _required_string(raw, key):
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError("%s: required non-empty string" % key)
    return raw.strip()


def _chat_id(raw, key):
    if raw is None:
        return None
    value = _integer(raw, key, None)
    if value == 0:
        raise ConfigError("%s: must not be 0" % key)
    return value


def _camera(raw, index, config):
    where = "synology_cameras[%i]" % index
    if not isinstance(raw, dict):
        raise ConfigError("%s: expected an object" % where)
    unknown = set(raw) - CAMERA_KEYS
    if unknown:
        logging.warning("%s: ignoring unknown keys: %s", where, ", ".join(sorted(unknown)))

    camera_id = _integer(raw.get("id"), where + ".id", None, positive=True)

    token = raw.get("tele_bot_token") or config.get("tele_bot_token")
    chat_id = _chat_id(raw.get("tele_chat_id"), where + ".tele_chat_id")
    if chat_id is None:
        chat_id = _chat_id(config.get("tele_chat_id"), "tele_chat_id")

    return CameraConfig(
        id=camera_id,
        skip_first_n_secs=_number(raw.get("skip_first_n_secs"), where + ".skip_first_n_secs",
                                   0, minimum=0),
        max_length_secs=_number(raw.get("max_length_secs"), where + ".max_length_secs",
                                None, positive=True),
        video_max_height=_integer(raw.get("video_max_height"), where + ".video_max_height",
                                  480, positive=True),
        fps=_integer(raw.get("fps"), where + ".fps", 15, positive=True),
        tele_bot_token=token,
        tele_chat_id=chat_id,
        enabled=_boolean(raw.get("enabled"), where + ".enabled", True),
    )


def load(path) -> Config:
    """Read, parse and validate the config file. Raises ConfigError."""
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("config file not found: %s" % path)
    except json.JSONDecodeError as e:
        raise ConfigError("config file is not valid JSON: %s (%s)" % (path, e))
    if not isinstance(raw, dict):
        raise ConfigError("config file must contain a JSON object")

    unknown = set(raw) - GLOBAL_KEYS
    if unknown:
        logging.warning("ignoring unknown config keys: %s", ", ".join(sorted(unknown)))

    cameras_raw = raw.get("synology_cameras")
    if not isinstance(cameras_raw, list):
        raise ConfigError("synology_cameras: required array (use [] to list camera ids)")

    cameras = tuple(_camera(item, i, raw) for i, item in enumerate(cameras_raw))

    seen = set()
    for camera in cameras:
        if camera.id in seen:
            raise ConfigError("synology_cameras: duplicate camera id %i" % camera.id)
        seen.add(camera.id)

    fmt = str(raw.get("format", "mp4")).strip().lower()
    if fmt not in FORMATS:
        raise ConfigError("format: expected one of %s, got %r"
                          % (", ".join(sorted(FORMATS)), raw.get("format")))

    log_level = str(raw.get("log_level", "INFO")).strip().upper()
    if log_level not in LOG_LEVELS:
        raise ConfigError("log_level: expected one of %s, got %r"
                          % (", ".join(sorted(LOG_LEVELS)), raw.get("log_level")))

    attempts = _integer(raw.get("tele_send_attempts"), "tele_send_attempts", 3, positive=True)
    if attempts > 3:
        logging.warning("tele_send_attempts %i is above the hard limit, using 3", attempts)
        attempts = 3

    base_url = _required_string(raw.get("synology_base_api_url"), "synology_base_api_url")
    tele_base_url = raw.get("tele_base_url") or None
    if tele_base_url is not None:
        tele_base_url = _required_string(tele_base_url, "tele_base_url").rstrip("/")

    config = Config(
        synology_base_api_url=base_url.rstrip("/"),
        synology_user=_required_string(raw.get("synology_user"), "synology_user"),
        synology_password=_required_string(raw.get("synology_password"), "synology_password"),
        synology_cameras=cameras,
        data_folder=str(raw.get("data_folder") or "/data"),
        ffmpeg_working_folder=str(raw.get("ffmpeg_working_folder") or "/gifs"),
        format=fmt,
        video_preset=str(raw.get("video_preset") or "veryfast"),
        tele_bot_token=raw.get("tele_bot_token") or None,
        tele_chat_id=_chat_id(raw.get("tele_chat_id"), "tele_chat_id"),
        tele_base_url=tele_base_url,
        proxy=raw.get("proxy") or None,
        tele_connect_timeout=_number(raw.get("tele_connect_timeout"), "tele_connect_timeout",
                                     20, positive=True),
        tele_read_timeout=_number(raw.get("tele_read_timeout"), "tele_read_timeout",
                                  60, positive=True),
        tele_send_attempts=attempts,
        tele_retry_backoff_secs=_number(raw.get("tele_retry_backoff_secs"),
                                        "tele_retry_backoff_secs", 5, positive=True),
        syno_timeout_connect=_number(raw.get("syno_timeout_connect"), "syno_timeout_connect",
                                     10, positive=True),
        syno_timeout_read=_number(raw.get("syno_timeout_read"), "syno_timeout_read",
                                  60, positive=True),
        syno_verify_ssl=_boolean(raw.get("syno_verify_ssl"), "syno_verify_ssl", False),
        poll_interval_secs=_number(raw.get("poll_interval_secs"), "poll_interval_secs",
                                   10, positive=True),
        event_backlog_secs=_number(raw.get("event_backlog_secs"), "event_backlog_secs",
                                   0, minimum=0),
        log_level=log_level,
    )

    for camera in config.synology_cameras:
        if not camera.enabled:
            continue
        if not camera.tele_bot_token:
            raise ConfigError("camera %i: no tele_bot_token (set per camera or globally)" % camera.id)
        if camera.tele_chat_id is None:
            raise ConfigError("camera %i: no tele_chat_id (set per camera or globally)" % camera.id)

    return config