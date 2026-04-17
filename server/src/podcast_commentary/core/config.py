from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    LIVEKIT_URL: str = "ws://localhost:7880"
    LIVEKIT_API_KEY: str = "devkey"
    LIVEKIT_API_SECRET: str = "secret"

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

    # Avatar URL (public URL where fox_2x3.jpg is served)
    AVATAR_URL: str = "http://localhost:8080/static/fox_2x3.jpg"

    # YouTube bot-detection workarounds.
    #
    # YOUTUBE_PROXY — residential/mobile proxy URL (e.g. http://user:pass@host:port).
    # Used by *both* yt-dlp (URL extraction) and ffmpeg (audio streaming) so that
    # YouTube's IP-locked signed URLs stay valid. ffmpeg only supports HTTP
    # CONNECT proxies (not SOCKS5). Fully automated — no manual steps.
    YOUTUBE_PROXY: str | None = None
    #
    # YOUTUBE_COOKIES — base64-encoded Netscape cookies.txt, or a filesystem path.
    # Requires a one-time manual export from a logged-in browser:
    #   yt-dlp --cookies-from-browser chrome --cookies cookies.txt ...
    #   base64 -i cookies.txt  → paste into env var
    YOUTUBE_COOKIES: str | None = None

    # LiveKit agent name. Both the API (job dispatcher) and the agent (worker)
    # must agree on this value. The default matches what's deployed on LiveKit
    # Cloud; override in server/.env for local dev (e.g.
    # "podcast-commentary-agent-local") so LiveKit can't misroute local
    # dispatches to the deployed worker (or vice versa).
    AGENT_NAME: str = "podcast-commentary-agent"

    model_config = {"env_file": (".env", ".env.local"), "extra": "ignore"}


settings = Settings()
