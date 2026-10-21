import os
from pathlib import Path as _P


def _load_dotenv():
    f = _P(__file__).resolve().parent.parent / ".env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if v.strip():
                    os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(k, d=None):
    return os.environ.get(k, d)


class Settings:
    db_path = Path(_env("RASAD_DB", str(ROOT / "data" / "rasad.db")))
    sources_file = Path(_env("RASAD_SOURCES", str(ROOT / "sources.yaml")))
    web_dir = ROOT / "web"
    # Autopilot cadence (seconds between full pipeline runs)
    interval = int(_env("RASAD_INTERVAL", "600"))
    default_mode = _env("RASAD_MODE", "supervised")  # manual | supervised | autopilot
    fetch_concurrency = int(_env("RASAD_FETCH_CONCURRENCY", "8"))
    fetch_fulltext = _env("RASAD_FULLTEXT", "1") == "1"
    fulltext_per_run = int(_env("RASAD_FULLTEXT_PER_RUN", "60"))
    user_agent = _env("RASAD_UA", "RasadBot/0.5 (+self-hosted news monitor; respects robots.txt)")
    # Optional OpenAI-compatible LLM endpoint (LiteLLM / Ollama / vLLM / cloud)
    llm_base_url = _env("LLM_BASE_URL")  # e.g. http://localhost:11434/v1
    llm_api_key = _env("LLM_API_KEY", "none")
    llm_model = _env("LLM_MODEL", "qwen3:8b")
    llm_fallback_model = _env("LLM_FALLBACK_MODEL", "")
    llm_rpm = int(_env("LLM_RPM", "20"))  # client-side pacing (requests/minute) to respect provider rate limits
    llm_daily_token_budget = int(_env("LLM_DAILY_TOKEN_BUDGET", "3000000"))
    # Optional embeddings endpoint (OpenAI-compatible /embeddings, e.g. TEI with bge-m3)
    embed_base_url = _env("EMBED_BASE_URL")
    embed_model = _env("EMBED_MODEL", "bge-m3")
    # Approval gates used in supervised mode
    approval_gates = [g for g in _env("RASAD_GATES", "writer,alerts").split(",") if g]
    admin_token = _env("RASAD_ADMIN_TOKEN")  # legacy script credential (acts as admin)
    # Authentication & RBAC (users/sessions). Set RASAD_AUTH=0 only for a trusted single-user localhost setup.
    auth_enabled = _env("RASAD_AUTH", "1") == "1"
    cookie_secure = _env("RASAD_COOKIE_SECURE", "0") == "1"  # set to 1 behind HTTPS
    # Operations
    backup_dir = Path(_env("RASAD_BACKUP_DIR", str(ROOT / "data" / "backups")))
    backup_keep = int(_env("RASAD_BACKUP_KEEP", "7"))
    retention_days = int(_env("RASAD_RETENTION_DAYS", "60"))
    public_url = _env("RASAD_PUBLIC_URL", "http://localhost:8080")
    tz = _env("RASAD_TZ", "Asia/Tehran")


settings = Settings()
