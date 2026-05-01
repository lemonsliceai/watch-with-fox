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

    # Public base URL that hosts the avatar images under ``/static/``.
    # Independent of the API server: by default the FastAPI process
    # serves these itself (so this is typically the API's public URL),
    # but it can be any public host — CDN, object store, etc. — as long
    # as the images live under ``/static/``. Combined with each preset's
    # ``AvatarConfig.avatar_image`` filename to form the final URL.
    # LemonSlice Cloud fetches from its own servers, so localhost won't
    # work. Leave unset to run without avatars.
    AVATAR_BASE_URL: str | None = None

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
    # **first entry is the primary** — its room receives the LiveKit
    # ``RoomAgentDispatch`` (the agent worker is dispatched there and
    # self-joins the rest as secondaries) and its timing values drive
    # shared cadence. Reorder this list to change the primary.
    #
    # The shipping default is ``alien,cat_girl``. Other presets in
    # ``fox_configs/`` (e.g. ``david_sacks``) are opt-in experiments —
    # add them here to play with them, but they are not part of the
    # canonical lineup. When empty, every preset module is auto-discovered
    # (sorted) — useful for local dev when poking at a new preset.
    PERSONAS: str = "alien,cat_girl"

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
