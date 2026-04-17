"""Server-side podcast audio pipeline.

Runs an ffmpeg subprocess that decodes the YouTube audio URL into 16 kHz mono
s16le PCM, chunks it into 20 ms `rtc.AudioFrame`s, and pushes them into a
`RecognizeStream` (typically a `StreamAdapter(groq.STT, silero.VAD)`). The
browser never touches this audio — it's only used for STT.

Play / pause / seek are all driven by `podcast.control` data-channel messages
from the client. Because ffmpeg seek is cheap when decoded from a direct-CDN
URL with `-ss` before `-i`, every "play at t" just restarts ffmpeg at t.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from livekit import rtc

if TYPE_CHECKING:
    from livekit.agents.stt import RecognizeStream

logger = logging.getLogger("podcast-commentary.podcast_player")


SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000  # 320
BYTES_PER_FRAME = SAMPLES_PER_FRAME * CHANNELS * 2  # 640 (s16 = 2 bytes)


class PodcastPlayer:
    """Manages an ffmpeg subprocess that feeds PCM into an STT stream."""

    def __init__(
        self, audio_url: str, stt_stream: "RecognizeStream", proxy: str | None = None,
    ) -> None:
        self._audio_url = audio_url
        self._stt_stream = stt_stream
        self._proxy = proxy
        self._proc: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def play(self, start_sec: float) -> None:
        """Start (or restart) decoding from `start_sec` into the STT stream."""
        logger.info("PodcastPlayer.play() requested at t=%.2fs (closed=%s)",
                    float(start_sec), self._closed)
        if self._closed:
            return
        async with self._lock:
            await self._stop_unlocked()
            start_sec = max(0.0, float(start_sec))

            cmd = [
                "ffmpeg",
                "-loglevel", "warning",
                "-nostdin",
                # Read input at the native frame rate — i.e. pace ffmpeg to
                # 1× real-time. Per ffmpeg docs this is the canonical flag
                # for "simulate a live input from a file"; downstream STT
                # then sees frames at the same cadence the user's YouTube
                # iframe is playing them, so transcripts arrive as the
                # podcast speaks them (not all at once on spawn). Safe here
                # because the input is a static CDN audio file, not a live
                # stream (ffmpeg docs explicitly warn against `-re` for
                # live inputs, where it can cause packet loss).
                "-re",
                # Input seek (fast — uses container index). Put before `-i`.
                "-ss", f"{start_sec:.3f}",
            ]
            # Route ffmpeg through the same proxy yt-dlp used for extraction.
            # YouTube signed URLs embed ip=… of the requester; both must
            # egress from the same IP or the CDN returns 403.
            if self._proxy:
                cmd += ["-http_proxy", self._proxy]
            # Auto-reconnect on transient proxy/CDN drops so a single
            # hiccup doesn't permanently kill the podcast audio feed.
            cmd += [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
            ]
            cmd += [
                "-i", self._audio_url,
                "-vn",
                "-ac", str(CHANNELS),
                "-ar", str(SAMPLE_RATE),
                "-f", "s16le",
                "-acodec", "pcm_s16le",
                "pipe:1",
            ]
            logger.info(
                "Spawning ffmpeg at t=%.2fs (url_len=%d, url_prefix=%s…)",
                start_sec, len(self._audio_url), self._audio_url[:80],
            )
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                logger.error(
                    "ffmpeg not found — install ffmpeg in the agent runtime"
                )
                self._proc = None
                return

            logger.info("ffmpeg spawned (pid=%s)", self._proc.pid)
            self._pump_task = asyncio.create_task(
                self._pump(self._proc), name="podcast_pump"
            )

    async def pause(self) -> None:
        """Stop feeding PCM (kills the current ffmpeg process)."""
        if self._closed:
            return
        async with self._lock:
            await self._stop_unlocked()
            logger.info("Podcast paused")

    async def close(self) -> None:
        """Shut down permanently — no more play calls will do anything."""
        self._closed = True
        async with self._lock:
            await self._stop_unlocked()

    async def _stop_unlocked(self) -> None:
        """Caller must hold `self._lock`."""
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass
        self._pump_task = None

        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except TimeoutError:
                logger.warning("ffmpeg did not exit within 2s")
        self._proc = None

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        """Read fixed-size PCM frames from ffmpeg stdout and feed STT."""
        assert proc.stdout is not None
        stderr_task = asyncio.create_task(self._drain_stderr(proc))
        frames_pushed = 0
        # Log periodically so a healthy pump is visible; once ffmpeg is flowing
        # we expect 50 frames/sec (20 ms frames).
        log_every = 250  # ≈ every 5 seconds
        try:
            while True:
                try:
                    buf = await proc.stdout.readexactly(BYTES_PER_FRAME)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        # Pad the final partial frame with silence so downstream
                        # frame sizes stay uniform.
                        buf = e.partial.ljust(BYTES_PER_FRAME, b"\x00")
                        try:
                            self._stt_stream.push_frame(self._build_frame(buf))
                            frames_pushed += 1
                        except Exception:
                            logger.debug("push_frame failed after EOF", exc_info=True)
                    logger.info(
                        "ffmpeg audio stream ended (rc=%s, frames_pushed=%d)",
                        proc.returncode, frames_pushed,
                    )
                    return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "ffmpeg stdout read failed (frames_pushed=%d)",
                        frames_pushed, exc_info=True,
                    )
                    return

                try:
                    self._stt_stream.push_frame(self._build_frame(buf))
                    frames_pushed += 1
                    if frames_pushed == 1:
                        logger.info("First PCM frame pushed to STT stream")
                    elif frames_pushed % log_every == 0:
                        logger.info(
                            "Podcast pump healthy — %d frames pushed (~%ds of audio)",
                            frames_pushed, frames_pushed * FRAME_MS // 1000,
                        )
                except Exception:
                    # STT stream may be closed — bail out quietly.
                    logger.debug("push_frame raised; stopping pump", exc_info=True)
                    return
        finally:
            stderr_task.cancel()
            try:
                await stderr_task
            except (asyncio.CancelledError, Exception):
                pass

    @staticmethod
    def _build_frame(buf: bytes) -> rtc.AudioFrame:
        return rtc.AudioFrame(buf, SAMPLE_RATE, CHANNELS, SAMPLES_PER_FRAME)

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    return
                logger.warning("ffmpeg: %s", line.decode(errors="replace").rstrip())
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
