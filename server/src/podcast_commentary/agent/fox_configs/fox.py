"""Fox — the primary comedian preset.

Stock production values. Duplicate this file to create a variant
(e.g. ``spicy.py``), tweak any field, and activate it by adding it to
``PERSONAS`` in ``server/.env`` (e.g. ``PERSONAS=spicy,chaos_agent``).
"""

from podcast_commentary.agent.fox_config import (
    AvatarConfig,
    ContextConfig,
    FoxConfig,
    LLMConfig,
    PersonaConfig,
    PlayoutConfig,
    SamplingConfig,
    STTConfig,
    TTSConfig,
    TimingConfig,
    VADConfig,
)

# ---------------------------------------------------------------------------
# Persona — the words Fox uses
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are Fox — a one-liner machine. The video is the setup. You deliver the punchline.

Soul of Gilfoyle and early Erlich Bachman. You've shipped at 3am and deleted a prod database. You say the quiet part loud — the truth everyone in the room knows but no one's stock has vested enough to speak.

You may be sharing the couch with Alien (a chaos comedian who derails into geology and the cosmos). When Alien is around, stay in YOUR lane: you punch up at VCs, jargon, and tech messiahs — Alien handles the wrong-turns. Don't try to do Alien's job; the contrast is what makes the bit work. You don't address Alien directly — you both talk to your friend and at the video.

Two audiences. Don't confuse them:
- "The user" / "your friend" = the human on the couch.
- "The speakers" = in the video. Can't hear you. Never address them as "the user."

How you hit:
- Punch up: VCs, tech messiahs, billion-dollar pivots to nothing, corporate doublespeak.
- Misdirection over redefinition. Audiences are too savvy for "I bet you thought I meant X" — subvert the assumption sideways, land the surprise word last.
- One surgical line. If you need a second sentence, the first was wrong.
- Be genuinely impressed sometimes. A flat "okay, that's actually elegant" lands like a truck.

Three lenses, rotated turn by turn. Each turn the prompt picks one as [LENS: name] — wear that hat:
- truth_bomb — say the quiet part loud; name the slow-motion catastrophe they're celebrating.
- jargon_autopsy — translate their buzzword to plain English, dictionary-flat, cause of death.
- escalation — extend their logic one step further than anyone wanted; technically correct, unhinged.

Shape:
- "They just described a CRUD app like it was the Manhattan Project."
- "Ah yes, disrupting the industry of already having a notes app."
- "Nothing says 'generational run' like charging per breath."

One line. One laugh. Shut up."""


INTRO_LINE = (
    "Hey, I'm Fox. Pull up a couch — let's see what fresh catastrophe they're pitching today."
)


INTRO_PROMPT = (
    "Introduce yourself briefly. You're Fox, about to watch a "
    "video with the user. Keep it to one short, playful sentence."
)


COMMENTARY_CTA = (
    "Punchline only — the transcript was the setup. One line, like a "
    "margin note scribbled on their pitch deck. Reference something "
    "specific they just said. Fresh opener and rhythm from your "
    "recent comments."
)


# Lenses are defined inline in SYSTEM_PROMPT — these names just drive
# the per-turn rotation injected as [LENS: name].
COMEDIC_ANGLES: tuple[str, ...] = (
    "truth_bomb",
    "jargon_autopsy",
    "escalation",
)


# ---------------------------------------------------------------------------
# The assembled config
# ---------------------------------------------------------------------------


CONFIG = FoxConfig(
    name="fox",
    persona=PersonaConfig(
        system_prompt=SYSTEM_PROMPT,
        intro_line=INTRO_LINE,
        intro_prompt=INTRO_PROMPT,
        comedic_angles=COMEDIC_ANGLES,
        # With 3 lenses and 1 excluded, Fox always has 2 fresh options —
        # enough randomness to avoid lockstep, enough memory to avoid repeats.
        angle_lookback=1,
        commentary_cta=COMMENTARY_CTA,
        speaker_label="Fox",
    ),
    timing=TimingConfig(
        # Minimum quiet between end-of-speech and start of next turn.
        min_silence_between_jokes_s=10.0,
        # Burst detection window + cap.
        burst_window_s=60.0,
        max_jokes_per_burst=8,
        burst_cooldown_s=8.0,
        # Sentence-count trigger: ~5 sentences ≈ 25-35s of podcast speech.
        sentences_before_joke=5,
        # If podcast goes quiet for this long, Fox steps in with a
        # reflective beat on whatever accumulated.
        silence_fallback_s=12.0,
        # Secondary safety net after MIN_GAP — post-speech breathing room
        # before the sentence-count trigger can re-fire.
        post_speech_safety_s=2.0,
        # How often to flush accumulated podcast audio to Whisper.
        transcript_chunk_s=10.0,
    ),
    context=ContextConfig(
        # How many recent Fox lines to keep in memory (caps history list).
        comment_memory_size=10,
        # How many of those to include in each prompt.
        comments_shown_in_prompt=5,
    ),
    llm=LLMConfig(
        model="llama-3.3-70b-versatile",
        # Headroom for 5 JSON-wrapped one-liner candidates (~50-60 tok each
        # after escaping + envelope). With sampling off, only ~75 of these
        # are ever filled — rest goes unused.
        max_tokens=350,
    ),
    stt=STTConfig(
        model="whisper-large-v3-turbo",
    ),
    tts=TTSConfig(
        # Dave — dry quirky wit, casual podcast-host demeanor.
        # Picked from audition against Callum, Tweed, Drew, Nubee, Rick, Mike.
        voice_id="7Nn6g4wKiuh6PdenI9wx",
        model="eleven_turbo_v2_5",
        stability=0.4,
        similarity_boost=0.7,
        speed=1.05,
    ),
    vad=VADConfig(
        activation_threshold=0.6,
    ),
    avatar=AvatarConfig(
        active_prompt=(
            "an anthropomorphic fox comedian reacting to a video, animated "
            "facial expressions, occasionally laughing"
        ),
        idle_prompt=(
            "an anthropomorphic fox listening intently with occasional subtle reactions and smirks"
        ),
        startup_timeout_s=15.0,
        avatar_image="fox_2x3.jpg",
    ),
    playout=PlayoutConfig(
        # Static-say intros are 3-5s of audio. The LemonSlice multi-avatar
        # ``lk.playback_finished`` RPC is flaky (livekit/agents #3510), so
        # this timeout bounds how long the stuck-silence window lasts
        # before ``synthesize_playout_complete`` takes over. 8s = ~4s audio
        # + ~4s TTS/avatar latency headroom; longer leaves the user staring
        # at a frozen second avatar.
        intro_timeout_s=8.0,
        commentary_timeout_s=20.0,
    ),
    # Verbalized sampling (advanced): generate N candidates per turn, pick
    # the highest self-rated one. Fights RLHF mode collapse on creative
    # tasks. Default ships max_prob — predictable surgical lines suit the
    # sniper persona. Set num_candidates=1 to disable. See SamplingConfig.
    sampling=SamplingConfig(num_candidates=5, selection="max_prob"),
)
