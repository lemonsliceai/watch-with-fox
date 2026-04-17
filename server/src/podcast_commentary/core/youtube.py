"""Extract direct audio stream URLs from YouTube videos using yt-dlp."""

import asyncio
import base64
import logging
import os
import re
import tempfile
from functools import partial

import yt_dlp

from podcast_commentary.core.config import settings

logger = logging.getLogger("podcast-commentary.youtube")

_YT_PATTERN = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([^&?/]+)"
)

# Resolve YOUTUBE_COOKIES once at import time: the value is either a filesystem
# path to a Netscape cookies.txt, or the base64-encoded *contents* of one.
# When base64 is detected we decode it to a persistent temp file so yt-dlp can
# read it by path.
_cookies_path: str | None = None

if settings.YOUTUBE_COOKIES:
    _raw = settings.YOUTUBE_COOKIES
    if os.path.isfile(_raw):
        _cookies_path = _raw
        logger.info("Using YouTube cookies file at %s", _cookies_path)
    else:
        try:
            decoded = base64.b64decode(_raw)
            # Quick sanity check — Netscape cookie files start with a comment
            # or a domain line; either way they're valid ASCII/UTF-8.
            decoded.decode("utf-8")
            fd, _cookies_path = tempfile.mkstemp(suffix=".txt", prefix="yt_cookies_")
            os.write(fd, decoded)
            os.close(fd)
            logger.info("Decoded base64 YouTube cookies to %s", _cookies_path)
        except Exception:
            logger.warning(
                "YOUTUBE_COOKIES is set but is neither a valid file path nor base64 — ignoring"
            )


def is_youtube_url(url: str) -> bool:
    return bool(_YT_PATTERN.search(url))


def _extract_audio_url_sync(video_url: str, proxy: str | None = None) -> str | None:
    # YouTube client selection (April 2026 reality):
    #   - `web` / `web_safari` are SABR-only now: YouTube strips the `url`
    #     field from adaptiveFormats, so yt-dlp can't retrieve a plain CDN
    #     audio URL → "Requested format is not available".
    #   - `tv` is under a live DRM experiment (yt-dlp#12563) that marks all
    #     https formats as DRM-protected for many videos.
    #   - `ios` requires a GVS PO token for every https/hls format now.
    #   - `android_sdkless` is deprecated.
    # Using player_client=["default"] lets yt-dlp's own resolver try the
    # working set in order (as of commit 309b03f) and fall back to
    # `android_vr` when everything else is blocked — that's what actually
    # returns a usable signed CDN URL today for public non-age-gated videos.
    # This still picks format 140 (m4a / mp4a.40.2) as the best audio.
    #
    # Challenge-solver JS bundles ship via `yt-dlp[default]` (the
    # yt-dlp-ejs extra, see pyproject.toml). yt-dlp auto-detects `deno` on
    # PATH; installing Deno is only required if we ever move off the
    # `default` client set to one that needs nsig decryption.
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["default"],
            },
        },
    }

    if proxy:
        opts["proxy"] = proxy
    elif settings.YOUTUBE_PROXY:
        opts["proxy"] = settings.YOUTUBE_PROXY
    if _cookies_path:
        opts["cookiefile"] = _cookies_path

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=False)
            if not info:
                logger.warning("yt-dlp returned no info for %s", video_url)
                return None
            url = info.get("url")
            chosen_format_id = info.get("format_id")
            chosen_ext = info.get("ext")
            chosen_acodec = info.get("acodec")
            if not url:
                # Some formats return the URL nested differently
                formats = info.get("formats", [])
                if formats:
                    fmt = formats[-1]
                    url = fmt.get("url")
                    chosen_format_id = fmt.get("format_id")
                    chosen_ext = fmt.get("ext")
                    chosen_acodec = fmt.get("acodec")
                    logger.info("Fell back to formats[-1] for audio URL")
            if url:
                host = url.split("/")[2] if "/" in url else "?"
                # Signed URL query string reveals which client yt-dlp ended up using (c=WEB, c=ANDROID_VR, etc.)
                client_tag = "?"
                m = re.search(r"[?&]c=([A-Z_]+)", url)
                if m:
                    client_tag = m.group(1)
                logger.info(
                    "yt-dlp extracted audio URL (host=%s, client=%s, format_id=%s, ext=%s, acodec=%s)",
                    host,
                    client_tag,
                    chosen_format_id,
                    chosen_ext,
                    chosen_acodec,
                )
            else:
                logger.warning(
                    "yt-dlp info has no 'url' field. Keys: %s", list(info.keys())
                )
            return url
    except Exception:
        logger.exception("yt-dlp failed for %s", video_url)
        return None


async def extract_audio_url(video_url: str, proxy: str | None = None) -> str | None:
    """Extract a direct audio stream URL from a YouTube video.

    Runs yt-dlp in a thread to avoid blocking the event loop.
    Returns the best audio-only URL, or None if extraction fails.

    If *proxy* is given it overrides the global ``YOUTUBE_PROXY`` setting.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, partial(_extract_audio_url_sync, video_url, proxy)
    )
