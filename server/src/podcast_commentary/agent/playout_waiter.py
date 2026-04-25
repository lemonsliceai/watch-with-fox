"""Robust playout wait for SpeechHandle — recovers from missing playback_finished RPCs.

Production reality (livekit/agents #3510, #4315): LemonSlice's *second*
``AvatarSession`` in a multi-avatar room sometimes does not send the
``lk.playback_finished`` RPC back. Without that RPC the framework's
``DataStreamAudioOutput.on_playback_finished`` never fires and
``SpeechHandle.wait_for_playout`` blocks forever.

The waiter polls the handle, watches the inner audio chain's
``_pushed_duration`` for an audio-settle plateau, and falls through to
manual synthesis (and last-resort ``force_listening``) so a stuck handle
can't hang the whole room.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any

from podcast_commentary.agent.comedian import (
    PersonaAgent,
    _deepest_audio_chain,
    _read_pushed_duration,
)

logger = logging.getLogger("podcast-commentary.playout")


class PlayoutWaiter:
    """Stateless helper used by intros + commentary turns.

    Construct once per Director and reuse across personas — the persona
    is passed in per-call so a single waiter handles all of them.
    """

    # Grace window after we synthesize ``playback_finished`` for the
    # framework to resolve the handle and fire our done-callback before
    # we fall back to ``force_listening``. 2s is enough — the resolve
    # path is purely in-process once the event fires.
    _SYNTHESIS_GRACE_S: float = 2.0

    # Audio-settle detection. To synthesize ``playback_finished`` early
    # (recovering from a dropped vendor RPC) we require BOTH:
    #   * ``_pushed_duration`` stopped growing for ``_AUDIO_SETTLE_WINDOW_S``
    #     after at least ``_AUDIO_MIN_PUSHED_S`` was pushed → TTS done
    #     generating audio
    #   * wall-clock time since the first audio frame is at least the
    #     pushed audio length (plus ``_AUDIO_DRAIN_GRACE_S``) → the avatar
    #     has had real time to play the buffered audio at 1× speed
    # The drain gate is load-bearing: LemonSlice buffers an entire intro
    # in <0.5s of wall-clock, so without the gate the settle window trips
    # while the avatar is still mid-sentence and the next persona intros
    # on top of the previous one.
    _AUDIO_SETTLE_WINDOW_S: float = 1.5
    _AUDIO_MIN_PUSHED_S: float = 0.5
    _AUDIO_DRAIN_GRACE_S: float = 0.5
    _PLAYOUT_POLL_INTERVAL_S: float = 0.25

    async def wait(
        self,
        persona: PersonaAgent,
        handle: Any,
        *,
        timeout: float,
        label: str,
    ) -> None:
        """Wait for ``handle``'s playout. See module docstring for the recovery ladder.

        Recovery ladder:
          1. Poll every ``_PLAYOUT_POLL_INTERVAL_S``, racing the handle's
             ``wait_for_playout`` against audio-settle detection.
          2. If ``_pushed_duration`` plateaued for ``_AUDIO_SETTLE_WINDOW_S``
             AND enough wall-clock time has passed since the first audio
             frame for the avatar to have drained it at 1× speed
             (``new_audio + _AUDIO_DRAIN_GRACE_S``), synthesize early —
             audio is done, the RPC just didn't come back.
          3. If neither happens before ``timeout``, hard-synthesize and
             log whether audio actually reached the wire.
          4. After synthesis, ``_SYNTHESIS_GRACE_S`` for the handle to
             resolve via the event we just fired.
          5. Last resort: ``force_listening`` — cuts audio, unblocks phase.

        All log lines tagged ``[<persona>|<label>]`` so a single grep on
        the log can show one turn's full lifecycle.
        """
        tag = f"[{persona.name}|{label}]"
        audio_inner = _deepest_audio_chain(persona._audio_output())
        start = time.monotonic()
        first_audio_ts: float | None = None
        last_growth_ts = start
        # The deepest audio chain's ``_pushed_duration`` accumulates across
        # the session — capture it now so only growth above this baseline
        # counts as new audio for THIS turn. Without the baseline, a slow
        # LLM (Alien's 6-candidate VS often takes >1.5s before TTS starts)
        # means the residual cumulative value reads as if audio is already
        # flowing, no growth happens for the settle window, and we
        # false-positive settle → force_listening → mid-sentence cutoff.
        baseline_pushed = _read_pushed_duration(audio_inner)
        last_pushed = baseline_pushed

        logger.info(
            "%s playout wait begin (timeout=%.1fs, settle_window=%.1fs, "
            "min_pushed=%.1fs, baseline=%.2fs)",
            tag,
            timeout,
            self._AUDIO_SETTLE_WINDOW_S,
            self._AUDIO_MIN_PUSHED_S,
            baseline_pushed,
        )

        # Reuse the wait task across poll iterations — creating a new one
        # each tick would leak pending tasks if the underlying impl
        # doesn't return the same future for repeated calls.
        wait_task = asyncio.create_task(handle.wait_for_playout())
        exit_reason: str
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(wait_task),
                        timeout=self._PLAYOUT_POLL_INTERVAL_S,
                    )
                    elapsed = time.monotonic() - start
                    pushed = _read_pushed_duration(audio_inner)
                    logger.info(
                        "%s playout CONFIRMED by vendor RPC "
                        "(elapsed=%.2fs, pushed=%.2fs, first_audio=%s)",
                        tag,
                        elapsed,
                        pushed,
                        (
                            f"{first_audio_ts - start:.2f}s"
                            if first_audio_ts is not None
                            else "NEVER"
                        ),
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("%s wait_for_playout raised in poll", tag, exc_info=True)

                now = time.monotonic()
                elapsed = now - start
                pushed = _read_pushed_duration(audio_inner)

                if pushed > last_pushed + 0.01:
                    if first_audio_ts is None:
                        first_audio_ts = now
                        logger.info(
                            "%s FIRST AUDIO on wire (elapsed=%.2fs, pushed=%.2fs, new=%.2fs)",
                            tag,
                            elapsed,
                            pushed,
                            pushed - baseline_pushed,
                        )
                    last_pushed = pushed
                    last_growth_ts = now

                idle = now - last_growth_ts
                new_audio = pushed - baseline_pushed
                # Settle requires:
                #   * real growth above the baseline — the cumulative
                #     residual would otherwise trip the idle window before
                #     TTS starts and we'd cut audio that never played.
                #   * pushing-stopped idle window — TTS finished generating.
                #   * audio-drained wall-clock — LemonSlice buffers the
                #     full clip in <0.5s, so pushing-stopped fires while
                #     the avatar is still speaking. Without this gate the
                #     next persona intros on top of the previous one.
                drained = (
                    first_audio_ts is not None
                    and (now - first_audio_ts) >= new_audio + self._AUDIO_DRAIN_GRACE_S
                )
                if (
                    first_audio_ts is not None
                    and new_audio >= self._AUDIO_MIN_PUSHED_S
                    and idle >= self._AUDIO_SETTLE_WINDOW_S
                    and drained
                ):
                    logger.warning(
                        "%s AUDIO SETTLED early — synthesizing "
                        "(elapsed=%.2fs, pushed=%.2fs, new=%.2fs, idle=%.2fs, "
                        "since_first=%.2fs) — vendor RPC likely dropped; "
                        "audio already played through",
                        tag,
                        elapsed,
                        pushed,
                        new_audio,
                        idle,
                        now - first_audio_ts,
                    )
                    exit_reason = "settle"
                    break

                if elapsed >= timeout:
                    if new_audio > 0.01:
                        logger.warning(
                            "%s HARD TIMEOUT — audio flowed but RPC missing "
                            "(elapsed=%.2fs, pushed=%.2fs, new=%.2fs, idle=%.2fs) — "
                            "synthesizing; Alien/Fox should still be audible",
                            tag,
                            elapsed,
                            pushed,
                            new_audio,
                            idle,
                        )
                    else:
                        logger.error(
                            "%s HARD TIMEOUT — NO NEW AUDIO reached the wire "
                            "(elapsed=%.2fs, baseline=%.2fs, pushed=%.2fs) — "
                            "persona will be SILENT. Likely upstream block "
                            "(TTS, TranscriptSynchronizer barrier, avatar "
                            "writer never opened, or session closed before "
                            "first frame)",
                            tag,
                            elapsed,
                            baseline_pushed,
                            pushed,
                        )
                    exit_reason = "timeout"
                    break
        finally:
            if not wait_task.done():
                wait_task.cancel()
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await wait_task

        # --- Recovery path ---
        outer_pushed, inner_pushed = persona.synthesize_playout_complete()
        logger.info(
            "%s synthesize_playout_complete done (reason=%s, sync=%.2fs, wire=%.2fs)",
            tag,
            exit_reason,
            outer_pushed,
            inner_pushed,
        )

        try:
            await asyncio.wait_for(handle.wait_for_playout(), timeout=self._SYNTHESIS_GRACE_S)
            logger.info(
                "%s handle resolved after synthesis (grace=%.1fs)",
                tag,
                self._SYNTHESIS_GRACE_S,
            )
            return
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            pass
        except Exception:
            logger.debug("%s wait_for_playout (post-synthesis) raised", tag, exc_info=True)

        # Nuclear option — synthesis didn't wake the waiter, so something
        # deeper is wedged. Cuts any still-playing audio off.
        logger.error(
            "%s handle STILL not done after synthesis — force_listening "
            "(nuclear option, cuts live audio)",
            tag,
        )
        with contextlib.suppress(Exception):
            persona.force_listening()

    @staticmethod
    def attach_observers(personas: list[PersonaAgent]) -> None:
        """Subscribe to ``playback_finished`` on each persona's audio output.

        Purely observational — lets operators tell vendor RPC confirms
        from our synthesised ones in the logs. Helps triage vendor
        regressions without having to repro.
        """
        for persona in personas:
            audio = persona._audio_output()
            if audio is None:
                continue
            name = persona.name

            def _on_playback_finished(ev: Any, name: str = name) -> None:
                logger.info(
                    "[%s] playback_finished event (position=%.2fs, interrupted=%s)",
                    name,
                    float(getattr(ev, "playback_position", 0.0) or 0.0),
                    getattr(ev, "interrupted", False),
                )

            try:
                audio.on("playback_finished", _on_playback_finished)
            except Exception:
                logger.debug(
                    "[%s] failed to attach playback_finished listener",
                    name,
                    exc_info=True,
                )


__all__ = ["PlayoutWaiter"]
