"""Telegram delivery with bounded retries.

Download and conversion happen once per event; only the upload is retried.
One TeleBot instance is kept per bot token, and sends for the same token are
serialised: telebot shares a requests.Session per instance, which is not safe
for concurrent uploads through a slow SOCKS proxy.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import urlsplit

import requests
import telebot
from telebot import apihelper
from telebot.apihelper import ApiException, ApiTelegramException

DEFAULT_TELE_RETRY_BACKOFF_SECS = 5
TELEGRAM_RETRY_AFTER_CAP_SECS = 60
TELEGRAM_CHAT_ACTION_TIMEOUT_SECS = 10


def mask_proxy(proxy):
    """Hide proxy credentials before logging the URL."""
    try:
        parsed = urlsplit(proxy)
        port = ":%i" % parsed.port if parsed.port else ""
    except ValueError:
        return "***"
    if parsed.username or parsed.password:
        return "%s://***@%s%s" % (parsed.scheme, parsed.hostname or "", port)
    return proxy


def setup(config):
    """Apply process-wide telebot settings once, before any bot is created."""
    if config.proxy:
        apihelper.proxy = {"https": config.proxy}
        logging.info("Proxy set: %s", mask_proxy(config.proxy))
    if config.tele_base_url:
        apihelper.API_URL = config.tele_base_url + "/bot{0}/{1}"
        logging.info("Telegram base URL set: %s", config.tele_base_url)
    apihelper.CONNECT_TIMEOUT = config.tele_connect_timeout
    apihelper.READ_TIMEOUT = config.tele_read_timeout
    logging.info("Telegram timeouts: connect %ss, read %ss, send attempts %i",
                 config.tele_connect_timeout, config.tele_read_timeout,
                 config.tele_send_attempts)


def _reset_session():
    """Drop telebot's keep-alive pool so a retry opens a fresh connection."""
    try:
        apihelper._get_req_session(reset=True)
    except Exception as e:  # private telebot API, never break a send because of it
        logging.debug("Cannot reset Telegram HTTP session: %s", e)


class Notifier:
    def __init__(self, config):
        self._config = config
        self._bots = {}
        self._locks = {}
        self._registry_lock = threading.Lock()

    def _bot(self, token):
        with self._registry_lock:
            bot = self._bots.get(token)
            if bot is None:
                bot = telebot.TeleBot(token)
                self._bots[token] = bot
                self._locks[token] = threading.Lock()
            return bot, self._locks[token]

    @staticmethod
    def _chat_action(bot, camera_id, chat_id):
        try:
            bot.send_chat_action(chat_id, "upload_video",
                                 timeout=TELEGRAM_CHAT_ACTION_TIMEOUT_SECS)
        except Exception as e:
            logging.warning("Cannot send chat action for camera_id %i: %s: %s",
                            camera_id, type(e).__name__, e)

    def send(self, camera_id, token, chat_id, path, caption, kind):
        """Upload one file, retrying transient Telegram/proxy errors.

        kind is "gif" or "mp4". Returns True when the file was delivered.
        """
        bot, lock = self._bot(token)
        max_attempts = self._config.tele_send_attempts
        backoff = self._config.tele_retry_backoff_secs
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        logging.info("Sending %s %s (%i bytes) for camera_id %i, up to %i attempts",
                     kind, path, size, camera_id, max_attempts)

        for attempt in range(1, max_attempts + 1):
            started = time.monotonic()
            retry_after = None
            if attempt == 1:
                self._chat_action(bot, camera_id, chat_id)

            try:
                with lock:
                    with open(path, "rb") as file:
                        if kind == "gif":
                            bot.send_animation(chat_id, file, disable_notification=True,
                                               caption=caption)
                        else:
                            bot.send_video(chat_id, file, disable_notification=True,
                                           caption=caption)
                logging.info("Telegram %s sent for camera_id %i (attempt %i/%i, %.1fs)",
                             kind, camera_id, attempt, max_attempts,
                             time.monotonic() - started)
                return True

            except ApiTelegramException as e:
                elapsed = time.monotonic() - started
                error_code = e.error_code or 0
                description = (e.result_json or {}).get("description", e)
                if error_code == 429:
                    parameters = (e.result_json or {}).get("parameters") or {}
                    try:
                        retry_after = float(parameters.get("retry_after", backoff))
                    except (TypeError, ValueError):
                        retry_after = backoff
                    retry_after = min(retry_after, TELEGRAM_RETRY_AFTER_CAP_SECS)
                    logging.warning("Telegram rate limit for camera_id %i "
                                    "(attempt %i/%i, %.1fs), retry_after %ss",
                                    camera_id, attempt, max_attempts, elapsed, retry_after)
                elif error_code >= 500:
                    logging.warning("Telegram transient error %i for camera_id %i "
                                    "(attempt %i/%i, %.1fs): %s",
                                    error_code, camera_id, attempt, max_attempts,
                                    elapsed, description)
                else:
                    logging.error("Telegram rejected %s for camera_id %i "
                                  "(HTTP %i, attempt %i/%i, %.1fs): %s; not retrying",
                                  kind, camera_id, error_code, attempt, max_attempts,
                                  elapsed, description)
                    return False

            except requests.exceptions.RequestException as e:
                logging.warning("Telegram %s attempt %i/%i for camera_id %i failed "
                                "after %.1fs: %s: %s",
                                kind, attempt, max_attempts, camera_id,
                                time.monotonic() - started, type(e).__name__, e)

            except ApiException as e:
                status = getattr(e.result, "status_code", None)
                if status is None or status == 200 or status >= 500:
                    logging.warning("Telegram %s attempt %i/%i for camera_id %i failed "
                                    "after %.1fs: %s (HTTP %s, transient)",
                                    kind, attempt, max_attempts, camera_id,
                                    time.monotonic() - started, type(e).__name__, status)
                else:
                    logging.error("Telegram API error for camera_id %i (HTTP %s, "
                                  "attempt %i/%i, %.1fs): %s; not retrying",
                                  camera_id, status, attempt, max_attempts,
                                  time.monotonic() - started, e)
                    return False

            except Exception:
                logging.exception("Unexpected error while sending %s for camera_id %i "
                                  "(attempt %i/%i); not retrying",
                                  kind, camera_id, attempt, max_attempts)
                return False

            if attempt >= max_attempts:
                break
            if retry_after is not None:
                delay = retry_after
            else:
                _reset_session()
                delay = backoff * attempt
            logging.info("Retrying Telegram %s for camera_id %i in %.1fs (attempt %i/%i)",
                         kind, camera_id, delay, attempt + 1, max_attempts)
            time.sleep(delay)

        logging.error("Giving up sending %s for camera_id %i after %i attempts",
                      kind, camera_id, max_attempts)
        return False