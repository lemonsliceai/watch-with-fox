from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # LiveKit — required. No defaults: if these are missing the server
    # should fail to boot rather than silently fall back to dev credentials
    # in production.
    LIVEKIT_URL: str | None = None
    LIVEKIT_API_KEY: str | None = None
    LIVEKIT_API_SECRET: str | None = None

    DATABASE_URL: str | None = None

    # Groq — STT (Whisper) and LLM (Llama Scout)
    GROQ_API_KEY: str | None = None

    # ElevenLabs — TTS
    ELEVEN_API_KEY: str | None = None

    # LemonSlice — avatar rendering
    LEMONSLICE_API_KEY: str | None = None

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8080

    # LiveKit agent name. Both the API (job dispatcher) and the agent (worker)
    # must agree on this value. The default matches what's deployed on LiveKit
    # Cloud; override in server/.env for local dev (e.g.
    # "podcast-commentary-agent-local") so LiveKit can't misroute local
    # dispatches to the deployed worker (or vice versa).
    AGENT_NAME: str = "podcast-commentary-agent"

    # Comma-separated list of FoxConfig presets to load. Each preset becomes
    # one on-screen persona; the Director picks who speaks each turn. The
    # first persona in the list is the "primary" — it owns the user mic STT
    # (push-to-talk) and its timing values drive shared cadence.
    # Defaults to Fox + Alien for the dual-avatar experience.
    PERSONAS: str = "fox,chaos_agent"

    # Legacy single-persona selector. If set and PERSONAS is empty, falls
    # back to a single persona using this name. Kept for back-compat.
    FOX_CONFIG: str = "fox"

    # Speaker-selection LLM (Director judge). Cheap + fast wins here — we
    # only need a JSON pick, not creative writing. Same Groq model as the
    # comedians; could be swapped for an even smaller one.
    DIRECTOR_LLM_MODEL: str = "llama-3.3-70b-versatile"
    # Hard cap on consecutive turns from the same persona. The judge can
    # ride a streak up to this number; on the next turn the cap forces a
    # switch (or skip). 2 = "double-tap fine, triple-tap spammy".
    DIRECTOR_MAX_CONSECUTIVE: int = 2

    model_config = {"env_file": (".env", ".env.local"), "extra": "ignore"}


settings = Settings()
