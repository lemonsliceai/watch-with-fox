"""Podcast STT pipeline.

Bundles the collaborators that together make up "Fox's ears":

  * `groq.STT` — non-streaming Whisper called on fixed-interval audio chunks.
    Podcast speech is near-continuous with few natural pauses, so VAD-based
    segmentation (the previous approach) would buffer 30-60 s before emitting
    a transcript.  Fixed-interval chunking (~10 s) gives predictable,
    frequent delivery regardless of speech patterns.
  * A LiveKit audio track published by the Chrome extension as
    ``podcast-audio`` — it captures tab audio via ``chrome.tabCapture`` and
    publishes it directly. The pipeline subscribes to that track and feeds
    frames into the recognition buffer.

Factored out of `ComedianAgent` so the pipeline owns its own startup,
recognition loop, and shutdown — the agent just wires up the callback.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from livekit import rtc
from livekit.plugins import groq

from podcast_commentary.agent.fox_config import CONFIG

logger = logging.getLogger("podcast-commentary.podcast_pipeline")

# How often to send accumulated audio to Whisper for transcription.
# 10 s ≈ 2-3 sentences at typical speaking pace; after two chunks Fox
# has enough material (~5 sentences) to trigger commentary. Sourced from
# the active FoxConfig preset.
CHUNK_INTERVAL_SECONDS = CONFIG.timing.transcript_chunk_s


def _log_task_exception(task: asyncio.Task) -> None:
    """Done-callback that surfaces exceptions instead of letting GC swallow them."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Task %r failed: %s", task.get_name(), exc, exc_info=exc)


class _FrameBuffer:
    """Collects audio frames for periodic batch STT recognition."""

    def __init__(self) -> None:
        self._frames: list[rtc.AudioFrame] = []

    def push_frame(self, frame: rtc.AudioFrame) -> None:
        self._frames.append(frame)

    def drain(self) -> list[rtc.AudioFrame]:
        """Return all buffered frames and reset."""
        frames = self._frames
        self._frames = []
        return frames


class PodcastPipeline:
    """Fixed-interval STT + LiveKit track consumer + transcript delivery.

    Audio arrives via ``attach_track()`` once the Chrome extension's
    ``podcast-audio`` track is subscribed.
    """

    def __init__(
        self,
        *,
        on_transcript: Callable[[str], Awaitable[None]],
    ) -> None:
        self._on_transcript = on_transcript
        self._stt: groq.STT | None = None
        self._buffer: _FrameBuffer | None = None
        self._recognition_task: asyncio.Task | None = None
        self._audio_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Create the STT, frame buffer, and recognition loop.

        Audio doesn't flow until the extension publishes its
        ``podcast-audio`` track and ``attach_track()`` is called.
        """
        self._stt = groq.STT(model=CONFIG.stt.model)
        self._buffer = _FrameBuffer()
        self._recognition_task = asyncio.create_task(
            self._recognition_loop(), name="podcast_recognition"
        )
        self._recognition_task.add_done_callback(_log_task_exception)
        logger.info("Podcast pipeline initialised (awaiting podcast-audio track from extension)")

    async def shutdown(self) -> None:
        """Tear down the audio consumer and recognition loop."""
        if self._audio_task is not None:
            self._audio_task.cancel()
            try:
                await self._audio_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._recognition_task is not None:
            self._recognition_task.cancel()
            try:
                await self._recognition_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Track attachment — subscribe to the extension's LiveKit audio track
    # ------------------------------------------------------------------
    def attach_track(self, track: rtc.Track) -> None:
        """Start consuming audio frames from a LiveKit audio track.

        Called by the agent when the extension's ``podcast-audio`` track
        is subscribed. If ``start()`` hasn't run yet (no buffer), we log
        and bail out so the failure surfaces in logs instead of asserting
        inside an orphan task.
        """
        if self._buffer is None:
            logger.error(
                "attach_track called before start() — buffer is None; "
                "podcast-audio will not be consumed"
            )
            return
        if self._audio_task is not None:
            self._audio_task.cancel()
        self._audio_task = asyncio.create_task(
            self._consume_audio(track), name="podcast_audio_consumer"
        )
        # Surface task failures — bare create_task swallows exceptions until GC.
        self._audio_task.add_done_callback(_log_task_exception)
        logger.info("Attached podcast-audio track — consuming frames for STT")

    async def _consume_audio(self, track: rtc.Track) -> None:
        """Read audio frames from a LiveKit track and push to the STT buffer."""
        frames_pushed = 0
        try:
            assert self._buffer is not None
            audio_stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
            async for event in audio_stream:
                self._buffer.push_frame(event.frame)
                frames_pushed += 1
                if frames_pushed == 1:
                    logger.info("First podcast audio frame pushed to STT buffer")
                elif frames_pushed % 500 == 0:
                    logger.info(
                        "Podcast audio healthy — %d frames pushed",
                        frames_pushed,
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Podcast audio consumer crashed after %d frames", frames_pushed)

    # ------------------------------------------------------------------
    # Fixed-interval recognition loop
    # ------------------------------------------------------------------
    async def _recognition_loop(self) -> None:
        """Every CHUNK_INTERVAL_SECONDS, send buffered audio to Whisper.

        Podcast audio is continuous speech — VAD can't reliably detect
        segment boundaries, so fixed-interval chunking gives predictable
        transcript delivery (~10 s per chunk ≈ 2-3 sentences).
        """
        assert self._stt is not None
        assert self._buffer is not None
        try:
            while True:
                await asyncio.sleep(CHUNK_INTERVAL_SECONDS)
                frames = self._buffer.drain()
                if not frames:
                    continue

                try:
                    event = await self._stt.recognize(frames)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Podcast STT recognition failed", exc_info=True)
                    continue

                if not event.alternatives:
                    continue
                text = (event.alternatives[0].text or "").strip()
                if not text:
                    continue

                logger.info("Podcast transcript: %s", text[:120])
                try:
                    await self._on_transcript(text)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Podcast transcript callback crashed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Podcast recognition loop crashed")
