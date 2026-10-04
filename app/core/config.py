from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "groq"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    llm_timeout_s: float = 30.0
    database_url: str = "sqlite:///./data/app.db"
    dry_run: bool = True
    max_task_attempts: int = 2
    retry_backoff_s: float = 2.0         # wait before a retry (gives rate limits time to clear)

    # Phase 5: global guards + verification
    max_llm_calls_per_run: int = 12          # hard cap on LLM calls (every attempt counts) per run
    task_timeout_s: float = 90.0             # per-task wall-clock limit (asyncio.wait_for)
    verify_min_audit_issues: int = 3         # verification rule: the website audit must list at least this many issues
    verify_with_llm: bool = True             # optional LLM check that email claims match the evidence

    # Phase 4: outreach + approval gate
    business_profile_path: str = "business_profile.yaml"
    smtp_host: str = ""                      # only used when DRY_RUN=false
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # Phase 3: web tools
    user_agent: str = "Mozilla/5.0 (compatible; AIBusinessOS/0.1)"
    fetch_timeout_s: float = 10.0
    fetch_max_bytes: int = 2_000_000
    web_search_enabled: bool = True          # DuckDuckGo is unreliable; set WEB_SEARCH_ENABLED=false to skip it
    homepage_text_chars: int = 6000          # ~1.5k tokens of page text per homepage
    subpage_text_chars: int = 2500           # ~600 tokens per contact/about/key page

    @property
    def llm_configured(self) -> bool:
        key = self.groq_api_key.strip()
        return bool(key) and key not in ("...", "your_key_here")


settings = Settings()
