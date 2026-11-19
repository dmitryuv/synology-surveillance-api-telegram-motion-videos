"""Thin client for the Synology Surveillance Station Web API.

Only the four calls this app needs are implemented: login, Camera.List,
Recording.List and Recording.Download (direct or through CMS Redirect).
Notes verified against a live SS 9.x host:

* events on a recording server (dsId > 0) must be downloaded through
  SYNO.SurveillanceStation.CMS Redirect; a direct Download returns error 117
  even with mountId set (mountId is always 0 for camera events there);
* Recording.List v5 takes dualRecModeList=0, which hides the chunks of a
  continuous dual recording (dual_rec_mode=1). Events with that flag are also
  skipped defensively in code;
* offset/limit pagination works and data.total gives the full count;
* login accepts POST, so the password never has to sit in a URL.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

import requests

AUTH_ERROR_CODES = {105, 106, 107, 119}
DOWNLOAD_MARGIN_SECS = 4
EVENT_PAGE_SIZE = 100
DOWNLOAD_CHUNK_SIZE = 1 << 16
DOWNLOAD_ERROR_MAX = 64 * 1024

ERROR_MESSAGES = {
    105: "no permission or session expired",
    106: "session invalid",
    107: "session expired",
    119: "session expired",
    117: "recording belongs to another recording server",
    400: "invalid parameter",
    401: "camera disabled",
    402: "camera disabled",
    403: "no permission for this action",
    407: "ip temporarily blocked by DSM",
}


class SynologyError(Exception):
    """Base error for Synology calls."""


class SynologyTransientError(SynologyError):
    """Network, 5xx or unparsable response: the same call may succeed later."""


class SynologyApiError(SynologyError):
    """The API answered success=false, or an HTTP 4xx."""

    def __init__(self, code, context=""):
        self.code = code
        message = ERROR_MESSAGES.get(code, "error code %s" % code)
        if context:
            message = "%s (%s)" % (message, context)
        super().__init__(message)


@dataclass(frozen=True)
class Event:
    """One recording as returned by Recording.List."""

    id: int
    camera_id: int
    ds_id: int
    start_time: float
    stop_time: float
    recording: bool
    dual_rec_mode: int

    @property
    def duration(self):
        return self.stop_time - self.start_time

    def is_complete(self, min_duration):
        """True when the recording is finished, or already long enough to cut."""
        return not self.recording or self.duration >= min_duration


def _parse_event(raw):
    try:
        return Event(
            id=int(raw["id"]),
            camera_id=int(raw["cameraId"]),
            ds_id=int(raw.get("dsId") or 0),
            start_time=float(raw.get("startTime") or 0),
            stop_time=float(raw.get("stopTime") or 0),
            recording=bool(raw.get("recording")),
            dual_rec_mode=int(raw.get("dual_rec_mode") or 0),
        )
    except (KeyError, TypeError, ValueError):
        logging.warning("Skipping malformed event record: %r", raw)
        return None


def _json_error_code(body):
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return None
    if isinstance(data, dict):
        return _error_code(data)
    return None


def _error_code(data):
    """Error code of an API reply, tolerating a missing/mistyped error object."""
    error = data.get("error")
    if isinstance(error, dict):
        return error.get("code")
    return None


class SurveillanceClient:
    """One logged-in session, shared by all camera threads.

    requests.Session is not safe for concurrent use, so each thread gets its
    own; the sid and the re-login are shared under a lock.
    """

    def __init__(self, base_url, user, password, *, connect_timeout=10,
                 read_timeout=60, verify=True):
        base_url = base_url.rstrip("/")
        self._auth_url = base_url + "/webapi/auth.cgi"
        self._entry_url = base_url + "/webapi/entry.cgi"
        self._user = user
        self._password = password
        self._timeout = (connect_timeout, read_timeout)
        self._verify = verify
        self._lock = threading.RLock()
        self._local = threading.local()
        self._sid = None

    def _session(self):
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
        return session

    def _ensure_sid(self):
        with self._lock:
            if self._sid is None:
                self.login()
            return self._sid

    def _invalidate_sid(self):
        with self._lock:
            self._sid = None

    def login(self):
        """Authenticate and store the sid. Re-login is safe to call again."""
        with self._lock:
            params = {
                "api": "SYNO.API.Auth",
                "method": "login",
                "version": 6,
                "session": "SurveillanceStation",
                "format": "cookie",
                "account": self._user,
                "passwd": self._password,
            }
            try:
                response = self._session().post(self._auth_url, data=params,
                                                timeout=self._timeout,
                                                verify=self._verify)
            except requests.RequestException as e:
                raise SynologyTransientError("login: %s: %s" % (type(e).__name__, e)) from e
            if response.status_code >= 400:
                raise SynologyApiError(response.status_code, "login HTTP")
            try:
                data = response.json()
            except ValueError:
                raise SynologyTransientError("login returned a non-JSON response")
            if not data.get("success"):
                raise SynologyApiError(_error_code(data), "login")
            self._sid = data["data"]["sid"]
            logging.info("Synology login OK")

    def logout(self):
        with self._lock:
            if self._sid is None:
                return
            try:
                self._session().get(self._auth_url, params={
                    "api": "SYNO.API.Auth", "method": "logout", "version": 6,
                    "session": "SurveillanceStation", "_sid": self._sid,
                }, timeout=self._timeout, verify=self._verify)
            except requests.RequestException as e:
                logging.debug("Logout failed: %s", e)
            self._sid = None

    def _call(self, params):
        """GET entry.cgi and return the parsed JSON on success.

        A stale session (105/106/107/119) triggers exactly one re-login and one
        retry; other API errors are raised to the caller.
        """
        for attempt in (1, 2):
            query = dict(params)
            query["_sid"] = self._ensure_sid()
            try:
                response = self._session().get(self._entry_url, params=query,
                                               timeout=self._timeout,
                                               verify=self._verify)
            except requests.RequestException as e:
                raise SynologyTransientError("%s: %s" % (type(e).__name__, e)) from e
            if response.status_code >= 500:
                raise SynologyTransientError("HTTP %i from Surveillance Station"
                                             % response.status_code)
            if response.status_code >= 400:
                raise SynologyApiError(response.status_code, "HTTP error")
            try:
                data = response.json()
            except ValueError:
                raise SynologyTransientError("Surveillance Station returned non-JSON")
            if data.get("success"):
                return data
            code = _error_code(data)
            if code in AUTH_ERROR_CODES and attempt == 1:
                logging.info("Synology session expired (code %s), logging in again", code)
                self._invalidate_sid()
                continue
            raise SynologyApiError(code)
        raise SynologyTransientError("request failed after re-login")

    @staticmethod
    def _payload(data):
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise SynologyTransientError("response without a data object")
        return payload

    def cameras(self):
        data = self._call({
            "api": "SYNO.SurveillanceStation.Camera",
            "method": "List",
            "version": 1,
        })
        return self._payload(data).get("cameras") or []

    def _list_page(self, camera_id, src_type, src_id, limit, offset):
        data = self._call({
            "api": "SYNO.SurveillanceStation.Recording",
            "method": "List",
            "version": 5,
            "locked": 0,
            "dualRecModeList": 0,
            "limit": limit,
            "offset": offset,
            "cameraIds": camera_id,
            "evtSrcType": src_type,
            "evtSrcId": src_id,
        })
        payload = self._payload(data)
        return payload.get("events") or [], int(payload.get("total") or 0)

    def latest_event(self, camera_id, src_type, src_id):
        """Newest event of a camera, used to seed the checkpoint on first run."""
        events, _ = self._list_page(camera_id, src_type, src_id, 1, 0)
        for raw in events:
            event = _parse_event(raw)
            if event is not None and event.camera_id == camera_id:
                return event
        return None

    def events(self, camera_id, src_type, src_id, since_id):
        """All events newer than since_id, oldest first.

        Recording.List returns newest first, so pages are walked until an event
        at or below the checkpoint shows up.
        """
        collected = []
        offset = 0
        while True:
            page, total = self._list_page(camera_id, src_type, src_id,
                                          EVENT_PAGE_SIZE, offset)
            reached_checkpoint = False
            for raw in page:
                event = _parse_event(raw)
                if event is None:
                    continue
                if event.camera_id != camera_id:
                    continue
                if event.id <= since_id:
                    reached_checkpoint = True
                    break
                collected.append(event)
            if (reached_checkpoint or len(page) < EVENT_PAGE_SIZE
                    or offset + EVENT_PAGE_SIZE >= total):
                break
            offset += EVENT_PAGE_SIZE
        collected.sort(key=lambda event: event.id)
        return collected

    def _download_params(self, event, play_time_ms, sid):
        if event.ds_id > 0:
            web_api = json.dumps({
                "api": "SYNO.SurveillanceStation.Recording",
                "version": 6,
                "method": "Download",
                "id": event.id,
                "offsetTimeMs": 0,
                "playTimeMs": play_time_ms,
            }, separators=(",", ":"))
            return {
                "api": "SYNO.SurveillanceStation.CMS",
                "method": "Redirect",
                "version": 1,
                "dsId": event.ds_id,
                "webAPI": web_api,
                "isDownloadFile": "true",
                "_sid": sid,
            }
        return {
            "api": "SYNO.SurveillanceStation.Recording",
            "method": "Download",
            "version": 6,
            "id": event.id,
            "offsetTimeMs": 0,
            "playTimeMs": play_time_ms,
            "_sid": sid,
        }

    def download(self, event, play_time_ms, out_path):
        """Stream one partial recording into out_path.

        The API answers with JSON (not video) when something is wrong; that is
        detected from the Content-Type and the first byte and raised, so an
        error body never ends up in the .mp4 file.
        """
        for attempt in (1, 2):
            params = self._download_params(event, play_time_ms, self._ensure_sid())
            try:
                response = self._session().get(self._entry_url, params=params,
                                               stream=True, timeout=self._timeout,
                                               verify=self._verify)
            except requests.RequestException as e:
                raise SynologyTransientError("download event %i: %s: %s"
                                             % (event.id, type(e).__name__, e)) from e
            try:
                if response.status_code >= 500:
                    raise SynologyTransientError("download event %i: HTTP %i"
                                                 % (event.id, response.status_code))
                if response.status_code >= 400:
                    raise SynologyApiError(response.status_code, "download event %i" % event.id)

                content_type = (response.headers.get("Content-Type") or "").lower()
                first = b""
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    first = chunk
                    break

                if "json" in content_type or first.lstrip()[:1] == b"{":
                    body = first
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        body += chunk
                        if len(body) > DOWNLOAD_ERROR_MAX:
                            break
                    code = _json_error_code(body)
                    if code in AUTH_ERROR_CODES and attempt == 1:
                        logging.info("Synology session expired during download (code %s)", code)
                        self._invalidate_sid()
                        continue
                    if code is None:
                        raise SynologyTransientError(
                            "download event %i: unparsable error body %r"
                            % (event.id, body[:200]))
                    raise SynologyApiError(code, "download event %i" % event.id)

                written = 0
                with open(out_path, "wb") as out:
                    if first:
                        out.write(first)
                        written += len(first)
                    for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                        out.write(chunk)
                        written += len(chunk)
                if written == 0:
                    raise SynologyTransientError("download event %i: empty body" % event.id)
                logging.info("Downloaded event %i: %i bytes", event.id, written)
                return written
            finally:
                response.close()
        raise SynologyTransientError("download event %i failed after re-login" % event.id)