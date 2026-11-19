"""ffmpeg conversion of a downloaded recording.

The recipes are the ones measured on the previous version: a fast keyframe
seek before -i, an fps + height-capped scale filter, x264/crf30 for mp4 and a
plain one-pass GIF. The palettegen/paletteuse variant was measured too and
produced 3-7x larger files for a marginal gain, so it is not used.
"""
from __future__ import annotations

import logging
import shutil
import subprocess

DEFAULT_VIDEO_MAX_HEIGHT = 480
DEFAULT_VIDEO_PRESET = "veryfast"
X264_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast",
                "medium", "slow", "slower", "veryslow", "placebo"}
FFMPEG_TIMEOUT_SECS = 180
STDERR_TAIL_LINES = 20


class MediaError(Exception):
    """ffmpeg is missing, timed out or returned a non-zero code."""


def check_ffmpeg():
    path = shutil.which("ffmpeg")
    if not path:
        raise MediaError("ffmpeg not found in PATH")
    return path


def normalize_height(value):
    """Keep the height even (yuv420p) and at least 2."""
    try:
        height = int(value)
    except (TypeError, ValueError):
        logging.warning("Invalid video_max_height %r, using %i",
                        value, DEFAULT_VIDEO_MAX_HEIGHT)
        height = DEFAULT_VIDEO_MAX_HEIGHT
    if height <= 0:
        logging.warning("Invalid video_max_height %r, using %i",
                        value, DEFAULT_VIDEO_MAX_HEIGHT)
        height = DEFAULT_VIDEO_MAX_HEIGHT
    return max(2, height // 2 * 2)


def _scale_filter(height, fps):
    # Width follows the aspect ratio (-2), height is capped and never upscaled
    # by min(ih,H). The comma inside min() must be escaped for the ffmpeg
    # filter parser, not for a shell (we never use one).
    return "fps={},scale=-2:min(ih\\,{}):flags=bilinear".format(fps, height)


def build_mp4_args(input_path, output_path, *, skip_secs, length_secs,
                   video_max_height, fps, preset):
    """Arguments for an mp4 delivery file. Returned as a list for easy logging."""
    height = normalize_height(video_max_height)
    if preset not in X264_PRESETS:
        logging.warning("Invalid video_preset %r, using %s", preset, DEFAULT_VIDEO_PRESET)
        preset = DEFAULT_VIDEO_PRESET
    return [
        "ffmpeg", "-loglevel", "warning", "-y",
        "-ss", str(skip_secs),
        "-i", str(input_path),
        "-vf", _scale_filter(height, fps),
        "-c:v", "libx264", "-preset", preset, "-crf", "30",
        "-pix_fmt", "yuv420p", "-an",
        "-movflags", "+faststart",
        "-t", str(length_secs),
        str(output_path),
    ]


def build_gif_args(input_path, output_path, *, skip_secs, length_secs,
                   video_max_height, fps):
    """Arguments for a GIF. -fps_mode replaces the removed -vsync of ffmpeg 4."""
    height = normalize_height(video_max_height)
    return [
        "ffmpeg", "-loglevel", "warning", "-y",
        "-ss", str(skip_secs),
        "-i", str(input_path),
        "-vf", _scale_filter(height, fps),
        "-fps_mode", "vfr",
        "-t", str(length_secs),
        str(output_path),
    ]


def convert(args):
    """Run ffmpeg, raising MediaError with the tail of stderr on failure."""
    logging.debug("ffmpeg %s", " ".join(args[1:]))
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                timeout=FFMPEG_TIMEOUT_SECS)
    except subprocess.TimeoutExpired:
        raise MediaError("ffmpeg timed out after %i s" % FFMPEG_TIMEOUT_SECS)
    except OSError as e:
        raise MediaError("cannot run ffmpeg: %s" % e)
    if result.returncode != 0:
        tail = "\n".join((result.stderr or "").strip().splitlines()[-STDERR_TAIL_LINES:])
        raise MediaError("ffmpeg exited with code %i\n%s" % (result.returncode, tail))
    if result.stderr:
        logging.debug("ffmpeg stderr: %s", result.stderr.strip())