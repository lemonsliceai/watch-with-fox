"""Transcript state for Fox.

Tracks the full podcast transcript, maintains a rolling summary of
everything *before* the latest line, and records when commentary fires
so we can enforce a minimum gap and burst cooldown.
"""

import logging
import time

logger = logging.getLogger("podcast-commentary.timing")
transcript_logger = logging.getLogger("podcast-commentary.transcript")

# Timing parameters (seconds)
# Minimum quiet between the end of one Fox turn and the start of the
# next. We measure from *speech end* (avatar playback_finished), not speech
# start, so the gate reflects the listener's experience of silence.
MIN_GAP = 5
BURST_WINDOW = 60  # Window for burst detection
BURST_MAX = 8  # Max comments in burst window
BURST_COOLDOWN = 8  # Mandatory cooldown after burst


class CommentaryTimer:
    """Tracks commentary timing and enforces rules.

    Two timestamps matter:
      * `_last_speech_end_time` — when Fox most recently *finished*
        playing audio (driven by `AudioOutput.playback_finished`, the
        authoritative "avatar stopped talking" signal). ``MIN_GAP`` counts
        from here.
      * ``_speech_start_times`` — when each speaking turn began
        (``playback_started``). Used for the burst window / cooldown.

    The timer never consults "is Fox currently speaking?" — that gate
    lives in ``ComedianAgent.is_speaking`` (authoritative, SpeechHandle-
    backed). The timer only enforces *post-speech* pacing rules.

    Failed commentary generations (LLM produced nothing, or the turn was
    preempted before audio started) never fire ``playback_started`` — so
    they don't burn the gap budget, and they can't block real reactions.
    """

    def __init__(self):
        self._speech_start_times: list[float] = []
        self._last_speech_end_time: float = 0
        self._session_start: float = time.time()
        self._in_cooldown: bool = False
        self._cooldown_end: float = 0

    def time_since_last_comment(self) -> float:
        # Before the first turn ever lands, measure silence from session
        # start so the intro gate still works.
        if self._last_speech_end_time == 0:
            return time.time() - self._session_start
        return time.time() - self._last_speech_end_time

    def can_comment(self) -> bool:
        now = time.time()

        # Enforce minimum gap (measured from end-of-speech).
        if self.time_since_last_comment() < MIN_GAP:
            return False

        # Enforce burst cooldown
        if self._in_cooldown and now < self._cooldown_end:
            return False
        elif self._in_cooldown:
            self._in_cooldown = False

        # Check burst limit — only count turns that actually produced audio.
        recent = [t for t in self._speech_start_times if now - t < BURST_WINDOW]
        if len(recent) >= BURST_MAX:
            self._in_cooldown = True
            self._cooldown_end = now + BURST_COOLDOWN
            logger.info("Burst limit hit — entering %ds cooldown", BURST_COOLDOWN)
            return False

        return True

    def record_speech_start(self) -> None:
        """Called on ``AudioOutput.playback_started``."""
        now = time.time()
        self._speech_start_times.append(now)
        # Prune entries older than BURST_WINDOW so the list stays bounded.
        cutoff = now - BURST_WINDOW
        self._speech_start_times = [
            t for t in self._speech_start_times if t >= cutoff
        ]

    def record_speech_end(self) -> None:
        """Called on ``AudioOutput.playback_finished``."""
        self._last_speech_end_time = time.time()

    def stats(self) -> dict:
        return {
            "total_comments": len(self._speech_start_times),
            "time_since_last": round(self.time_since_last_comment(), 1),
            "in_cooldown": self._in_cooldown,
        }


class FullTranscript:
    """Accumulates the complete transcript of the YouTube podcast.

    Prompt model (per product spec):
      - The *latest* transcript line is always included **verbatim** in
        Fox's system prompt — it's what he's reacting to.
      - Everything that came *before* the latest line is summarised into
        a rolling summary. So after the 1st line there's no summary, after
        the 2nd there's a summary of the 1st, after the 3rd a summary of
        lines 1 and 2, and so on.
    """

    def __init__(self, summary_interval: int = 1):
        self._parts: list[tuple[float, str]] = []  # (timestamp, text)
        self._summary: str = ""
        # Number of parts (from the start) that are covered by `_summary`.
        # Invariant: `_summarized_count <= len(_parts) - 1` — the current
        # (latest) line is never in the summary, only the ones before it.
        self._summarized_count: int = 0
        # How many *previous* (non-current) parts must be unsummarised before
        # the summary loop bothers re-running the LLM. 1 = summarise on every
        # new line so the summary is always current-minus-one.
        self._summary_interval = summary_interval

    def add(self, text: str) -> None:
        """Add a new transcribed utterance."""
        text = text.strip()
        if not text:
            return
        self._parts.append((time.time(), text))
        transcript_logger.info("TRANSCRIPT [%d]: %s", len(self._parts), text)

    # ------------------------------------------------------------------
    # Prompt-facing accessors
    # ------------------------------------------------------------------
    @property
    def current(self) -> str:
        """Latest utterance — the one Fox is reacting to right now."""
        if not self._parts:
            return ""
        return self._parts[-1][1]

    @property
    def summary(self) -> str:
        """Rolling summary of every part *before* `current`."""
        return self._summary

    def pending_summarization_text(self) -> str:
        """Previous parts (excluding current) not yet folded into the summary.

        Used both to drive the LLM summary update and as a fallback block in
        the prompt when the summary loop hasn't caught up yet, so Fox
        never loses context even if the summariser is a turn behind.
        """
        end = max(0, len(self._parts) - 1)  # exclude current
        if end <= self._summarized_count:
            return ""
        return " ".join(txt for _, txt in self._parts[self._summarized_count:end])

    def needs_summary_update(self) -> bool:
        """True if ≥`summary_interval` previous parts aren't in the summary."""
        pending_count = max(0, len(self._parts) - 1) - self._summarized_count
        return pending_count >= self._summary_interval

    def mark_summarized(self) -> None:
        """Roll `_summarized_count` forward to cover every part but `current`."""
        self._summarized_count = max(0, len(self._parts) - 1)

    def update_summary(self, summary: str) -> None:
        """Store an updated summary covering every part before `current`."""
        self._summary = summary
        self.mark_summarized()
        transcript_logger.info(
            "SUMMARY UPDATED (covers parts 1..%d): %s",
            self._summarized_count, summary[:200],
        )

    def recent_transcript(self) -> str:
        """All parts not yet folded into the summary, including current.

        Used by the timer-based commentary loop: after each turn Fox sees
        everything that's accumulated since the last summary update.
        """
        if not self._parts:
            return ""
        start = self._summarized_count
        return " ".join(txt for _, txt in self._parts[start:])

    # ------------------------------------------------------------------
    # Misc accessors
    # ------------------------------------------------------------------
    def seconds_since_last_utterance(self) -> float | None:
        """Seconds elapsed since the most recent podcast utterance landed.

        None until the first utterance arrives — callers treat that as
        "podcast hasn't started talking yet", not "infinite silence".
        """
        if not self._parts:
            return None
        return time.time() - self._parts[-1][0]

    @property
    def part_count(self) -> int:
        return len(self._parts)

    def has_content(self) -> bool:
        return len(self._parts) > 0

    def get_full_text(self) -> str:
        """Return the entire accumulated transcript (for logging / archive)."""
        return " ".join(txt for _, txt in self._parts)
