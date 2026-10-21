"""Bounded, temporary-file FFmpeg operations for the bot's AI Video menu.

These are deterministic format/trim operations, not generative AI.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

MAX_INPUT_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_BYTES = 45 * 1024 * 1024
MAX_SOURCE_DURATION_SECONDS = 10 * 60
MAX_REELS_DURATION_SECONDS = 90
MAX_TRIM_DURATION_SECONDS = 180
MAX_SHORT_CLIP_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 12
PROCESS_TIMEOUT_SECONDS = 150
ALLOWED_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}


class VideoEditError(ValueError):
    """A user-correctable input or processing error."""


def parse_timecode(value: str) -> float:
    """Parse seconds, MM:SS, or HH:MM:SS into seconds."""
    raw = value.strip()
    if not raw:
        raise VideoEditError("Vaqt ko‘rsatilmagan.")

    parts = raw.split(":")
    if not 1 <= len(parts) <= 3:
        raise VideoEditError("Vaqtni SS, MM:SS yoki HH:MM:SS shaklida yozing.")

    try:
        numbers = [float(part) for part in parts]
    except ValueError as exc:
        raise VideoEditError(
            "Vaqtni SS, MM:SS yoki HH:MM:SS shaklida yozing."
        ) from exc

    if any(number < 0 for number in numbers):
        raise VideoEditError("Vaqt manfiy bo‘lishi mumkin emas.")
    if any(number >= 60 for number in numbers[1:]):
        raise VideoEditError("Daqiqa va soniya qismlari 60 dan kichik bo‘lishi kerak.")
    if len(numbers) > 1 and numbers[-1] >= 60:
        raise VideoEditError("Soniya 60 dan kichik bo‘lishi kerak.")
    if len(numbers) == 3 and numbers[1] >= 60:
        raise VideoEditError("Daqiqa 60 dan kichik bo‘lishi kerak.")

    if len(numbers) == 1:
        seconds = numbers[0]
    elif len(numbers) == 2:
        seconds = numbers[0] * 60 + numbers[1]
    else:
        seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]

    if seconds > MAX_SOURCE_DURATION_SECONDS:
        raise VideoEditError("Vaqt 10 daqiqadan oshmasligi kerak.")
    return seconds


def parse_time_range(value: str, *, max_segment_seconds: int) -> tuple[float, float]:
    """Parse a user range such as '00:00:10 - 00:00:25'."""
    parts = re.split(r"\s*[-–—]\s*", value.strip(), maxsplit=1)
    if len(parts) != 2:
        raise VideoEditError(
            "Boshlanish va tugash vaqtini `00:00:10 - 00:00:25` "
            "shaklida yuboring."
        )
    start = parse_timecode(parts[0])
    end = parse_timecode(parts[1])
    if end <= start:
        raise VideoEditError("Tugash vaqti boshlanish vaqtidan keyin bo‘lishi kerak.")
    if end - start > max_segment_seconds:
        raise VideoEditError(
            f"Tanlangan bo‘lak {max_segment_seconds} soniyadan oshmasin."
        )
    return start, end


def validate_uploaded_video(
    *,
    file_name: str | None,
    mime_type: str | None,
    file_size: int | None,
) -> None:
    if file_size is not None and not 0 < file_size <= MAX_INPUT_BYTES:
        raise VideoEditError("Video fayl 20 MB dan kichik bo‘lishi kerak.")

    extension = Path(file_name or "").suffix.lower()
    is_video_mime = bool(mime_type and mime_type.lower().startswith("video/"))
    if not is_video_mime and extension not in ALLOWED_EXTENSIONS:
        raise VideoEditError("MP4, MOV, M4V, MKV yoki WEBM video fayl yuboring.")


async def probe_video(path: str | Path) -> tuple[float, int, int]:
    """Return duration, width, and height using ffprobe with a strict timeout."""
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "format=duration:stream=codec_type,width,height",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise VideoEditError("Video faylini tekshirish vaqti tugadi.") from exc
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-800:]
        raise VideoEditError(f"Video faylini o‘qib bo‘lmadi. {detail}".strip())

    try:
        metadata = json.loads(stdout.decode("utf-8"))
        streams = metadata.get("streams") or []
        video_stream = next(
            stream for stream in streams if stream.get("codec_type") == "video"
        )
        duration = float((metadata.get("format") or {}).get("duration", 0))
        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
    except (ValueError, TypeError, KeyError, StopIteration, json.JSONDecodeError) as exc:
        raise VideoEditError("Yuklangan faylda o‘qiladigan video topilmadi.") from exc

    if duration <= 0 or width <= 0 or height <= 0:
        raise VideoEditError("Video davomiyligi yoki o‘lchami aniqlanmadi.")
    if duration > MAX_SOURCE_DURATION_SECONDS:
        raise VideoEditError("Video davomiyligi 10 daqiqadan oshmasligi kerak.")
    return duration, width, height


async def process_video(
    input_path: str | Path,
    output_path: str | Path,
    *,
    mode: str,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
) -> tuple[float, int, int]:
    """Process one uploaded video and return its duration and dimensions."""
    input_file = Path(input_path)
    output_file = Path(output_path)
    if not input_file.is_file():
        raise VideoEditError("Yuklangan video vaqtinchalik faylda topilmadi.")
    if input_file.stat().st_size > MAX_INPUT_BYTES:
        raise VideoEditError("Video fayl 20 MB dan katta.")

    duration, _width, _height = await probe_video(input_file)
    if mode == "reels":
        if duration > MAX_REELS_DURATION_SECONDS:
            raise VideoEditError(
                "Reels 9:16 formati uchun video 90 soniyadan oshmasin. "
                "Avval “Video kesish” orqali qisqartiring."
            )
        output_duration = duration
    elif mode in {"trim", "clip"}:
        if start_seconds is None or end_seconds is None:
            raise VideoEditError("Boshlanish va tugash vaqtini tanlang.")
        if start_seconds < 0 or end_seconds <= start_seconds:
            raise VideoEditError("Vaqt oralig‘i noto‘g‘ri.")
        if end_seconds > duration + 0.05:
            raise VideoEditError(
                f"Video {duration:.1f} soniya. Tugash vaqti videodan oshmasin."
            )
        output_duration = end_seconds - start_seconds
        segment_limit = (
            MAX_SHORT_CLIP_SECONDS if mode == "clip" else MAX_TRIM_DURATION_SECONDS
        )
        if output_duration > segment_limit:
            raise VideoEditError(
                f"Tanlangan bo‘lak {segment_limit} soniyadan oshmasin."
            )
    else:
        raise VideoEditError("Noma’lum montaj turi.")

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
    ]
    if mode in {"trim", "clip"}:
        command.extend(["-ss", f"{start_seconds:.3f}"])
    command.extend(
        [
            "-i",
            str(input_file),
            "-t",
            f"{output_duration:.3f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
        ]
    )
    if mode == "reels":
        command.extend(
            [
                "-vf",
                "fps=30,scale=720:1280:force_original_aspect_ratio=increase,"
                "crop=720:1280,setsar=1",
            ]
        )
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "27",
            "-threads",
            "2",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-map_metadata",
            "-1",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            str(output_file),
        ]
    )

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise VideoEditError(
            "Montaj 150 soniyadan oshdi va resurs limitiga ko‘ra to‘xtatildi."
        ) from exc
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace")[-1000:]
        raise VideoEditError(
            f"FFmpeg videoni qayta ishlay olmadi. {detail}".strip()
        )
    if not output_file.is_file() or output_file.stat().st_size == 0:
        raise VideoEditError("FFmpeg bo‘sh natija fayli qaytardi.")
    if output_file.stat().st_size > MAX_OUTPUT_BYTES:
        output_file.unlink(missing_ok=True)
        raise VideoEditError(
            "Natija Telegram yuborish hajmi limitidan oshdi. "
            "Qisqaroq bo‘lak tanlab qayta urinib ko‘ring."
        )

    return await probe_video(output_file)
