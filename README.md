# synology-surveillance-api-motion-telegram-gifs

Sends the video of Synology Surveillance Station motion events to Telegram: the
script polls recordings, cuts a short clip, re-encodes it and uploads it as an
MP4 (or GIF).

Features kept from the original project, plus a few fixes:

1. Works with DSM 7+ / Surveillance Station 9+.
2. Per-camera Telegram targets, including a different bot token per camera.
3. Supports telegram proxy 
4. CMS support: cameras on a recording server are downloaded through
   `SYNO.SurveillanceStation.CMS` Redirect, as the API requires.
5. The poller waits until a recording is long enough to cut, so a clip is never
   truncated.
6. Continuous dual-recording chunks are ignored (`dualRecModeList=0` plus a
   `dual_rec_mode` check).
7. Every new event is processed, not just the newest one, and events are
   remembered across restarts in SQLite.
8. Uploads are retried on network errors, 429 and 5xx; a failing event can
   never loop forever.

## Requirements

* Docker (recommended), or Python 3.11+ and ffmpeg in `PATH`.
* `pip install -r requirements.txt`.

## Running

```bash
docker run --rm \
  -v "$PWD/config.json:/config/config.json:ro" \
  -v "$PWD/data:/data" \
  -v "$PWD/gifs:/gifs" \
  syno/syno_telegram_gifs:local /config/config.json
```

Or without Docker:

```bash
python3 -m syno_telegram_gifs config.json
```

Flags for checks:

* `--once` — poll every camera once and exit.
* `--dry-run` — download and convert, but do not send anything and do not
  advance the checkpoint. Meant to be combined with `--once`; repeated runs
  process the same events again.
* `--log-level DEBUG|INFO|WARNING|ERROR` — override `log_level`.

Stop it with SIGINT/SIGTERM: workers finish the current event, temp files are
removed and the database is closed.

## Config file

A JSON object; see `config-sample.json`.

```json
{
  "data_folder": "./data",
  "ffmpeg_working_folder": "./gifs",
  "tele_bot_token": "XXX",
  "tele_chat_id": 123,
  "synology_base_api_url": "https://192.168.1.2:5001",
  "synology_user": "admin",
  "synology_password": "password123",
  "synology_cameras": [
    {
      "id": 1,
      "skip_first_n_secs": 5,
      "max_length_secs": 5,
      "video_max_height": 480,
      "fps": 15
    }
  ]
}
```

### Global

| key | default | meaning |
|---|---|---|
| `data_folder` | `/data` | where `processed_events.db` lives |
| `ffmpeg_working_folder` | `/gifs` | working folder for downloads and clips |
| `format` | `mp4` | `mp4` or `gif` |
| `video_preset` | `veryfast` | x264 preset for mp4 |
| `tele_bot_token` | — | default token for all cameras |
| `tele_chat_id` | — | default chat id for all cameras |
| `proxy` | — | e.g. `socks5h://user:pass@host:port` |
| `tele_base_url` | — | custom Telegram API endpoint |
| `tele_connect_timeout` | 20 | Telegram connect timeout, seconds |
| `tele_read_timeout` | 60 | Telegram read timeout, seconds |
| `tele_send_attempts` | 3 | upload attempts per event, capped at 3 |
| `tele_retry_backoff_secs` | 5 | base backoff between upload attempts |
| `poll_interval_secs` | 10 | pause between polls of one camera |
| `event_backlog_secs` | 0 | skip events older than this on start; 0 = no limit |
| `syno_timeout_connect` | 10 | Surveillance Station connect timeout, seconds |
| `syno_timeout_read` | 60 | Surveillance Station read timeout, seconds |
| `syno_verify_ssl` | false | verify the NAS certificate; most NAS use a self-signed one, so `false` is the default |
| `log_level` | INFO | `DEBUG`, `INFO`, `WARNING` or `ERROR` |

### Per camera

| key | default | meaning |
|---|---|---|
| `id` | — | Surveillance Station camera id (required) |
| `skip_first_n_secs` | 0 | seconds skipped at the start of the recording |
| `max_length_secs` | — | clip length in seconds (required) |
| `video_max_height` | 480 | output height in lines; width follows the aspect ratio |
| `fps` | 15 | output frame rate |
| `tele_bot_token` | global | token for this camera |
| `tele_chat_id` | global | chat id for this camera |
| `enabled` | true | set to `false` to keep the entry but stop polling |

If you don't know the camera ids, set `"synology_cameras": []`: the ids are
printed at startup and the process exits.

## How it works

For each configured camera a thread polls `Recording.List`, takes every event
newer than the checkpoint, and processes them oldest first:

```
download partial slice -> ffmpeg -> Telegram -> checkpoint
```

* The slice is `skip_first_n_secs + max_length_secs + 4` seconds, downloaded
  with `offsetTimeMs=0` / `playTimeMs`, so only the needed part is transferred.
* Conversions take a shared lock: only one ffmpeg runs at a time, so adding
  cameras does not multiply CPU load.
* Download and conversion happen once; only the upload is retried.
* An event is marked processed after a delivery attempt that succeeded or ran
  out of retries, and after a broken event has failed 3 times.

On the very first run the checkpoint is seeded with the newest event and
nothing is sent, so an existing archive is not uploaded.

The database is `processed_events`, one row per camera (`camera_id`,
`last_event_id`). It is compatible with the database of the previous
single-file version.

## Development

* `./build.sh` builds `syno/syno_telegram_gifs:local`.

