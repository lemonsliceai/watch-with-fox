"""Podcast STT + player lifecycle.

Bundles the three collaborators that together make up "Fox's ears":

  * `groq.STT` wrapped in a `StreamAdapter` with Silero VAD (Groq STT is
    non-streaming, so the adapter segments the continuous feed).
  * `PodcastPlayer` — an ffmpeg subprocess decoding the YouTube audio URL
    into 16 kHz mono PCM and pushing it into the STT stream.
  * A consumer task that pulls `FINAL_TRANSCRIPT` events and invokes the
    caller's `on_transcript(text)` callback.

Factored out of `ComedianAgent` so the pipeline owns its own startup,
consumer loop, and shutdown — the agent just wires up the callback.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from livekit.agents import stt as stt_mod
from livekit.agents.vad import VAD
from livekit.plugins import groq, silero

from podcast_commentary.agent.podcast_player import PodcastPlayer

logger = logging.getLogger("podcast-commentary.podcast_pipeline")


class PodcastPipeline:
    """STT stream + ffmpeg player + transcript consumer for podcast audio."""

    def __init__(
        self,
        *,
        audio_url: str,
        vad: VAD | None,
        on_transcript: Callable[[str], Awaitable[None]],
        proxy: str | None = None,
    ) -> None:
        self._audio_url = audio_url
        self._vad = vad
        self._on_transcript = on_transcript
        self._proxy = proxy
        self._stt_adapter: stt_mod.StreamAdapter | None = None
        self._stream: stt_mod.RecognizeStream | None = None
        self._consumer_task: asyncio.Task | None = None
        self._player: PodcastPlayer | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Create the STT stream, player, and consumer task.

        Does NOT spawn ffmpeg yet — that happens when the client sends its
        first `{type:"play", t:...}` data packet.
        """
        vad = self._vad or silero.VAD.load(activation_threshold=0.6)
        self._stt_adapter = stt_mod.StreamAdapter(
            stt=groq.STT(model="whisper-large-v3-turbo"),
            vad=vad,
        )
        self._stream = self._stt_adapter.stream()
        self._consumer_task = asyncio.create_task(
            self._consume(), name="podcast_transcripts"
        )
        self._player = PodcastPlayer(self._audio_url, self._stream, proxy=self._proxy)
        logger.info("Podcast pipeline initialised (awaiting client play)")

    async def shutdown(self) -> None:
        """Tear down ffmpeg, STT stream, and consumer task."""
        if self._player is not None:
            await self._player.close()
        if self._stream is not None:
            try:
                self._stream.end_input()
                await self._stream.aclose()
            except Exception:
                logger.debug("Error closing podcast STT stream", exc_info=True)
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Commands (driven by the client's podcast.control data channel)
    # ------------------------------------------------------------------
    async def play(self, start_sec: float) -> None:
        """Start or restart ffmpeg decoding from `start_sec`."""
        if self._player is None:
            logger.warning(
                "Received 'play' but podcast player not initialised"
            )
            return
        logger.info("Dispatching podcast play at t=%.2fs", start_sec)
        await self._player.play(start_sec)

    async def pause(self) -> None:
        """Stop ffmpeg (kills the decode subprocess)."""
        if self._player is None:
            logger.warning(
                "Received 'pause' but podcast player not initialised"
            )
            return
        logger.info("Dispatching podcast pause")
        await self._player.pause()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _consume(self) -> None:
        """Feed podcast STT finals into `on_transcript`.

        Every final triggers the callback; it's the caller's job to gate
        on "is Fox speaking?" and drop the reaction if needed. We only
        dedupe empty / noise events here.
        """
        assert self._stream is not None
        try:
            async for ev in self._stream:
                if ev.type != stt_mod.SpeechEventType.FINAL_TRANSCRIPT:
                    continue
                if not ev.alternatives:
                    continue
                text = (ev.alternatives[0].text or "").strip()
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
            logger.exception("Podcast transcript consumer crashed")
