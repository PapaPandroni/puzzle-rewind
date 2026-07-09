from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./dev.db"
    lichess_token: str | None = None
    cache_ttl_seconds: int = 3600
    lichess_base: str = "https://lichess.org"
    max_games_mvp: int = 20
    min_win_drop_stored: float = 10.0

    # Period backfill guardrails (§13.2): caps keep single fetches polite toward
    # Lichess; the longer timeout covers streams of hundreds of games.
    max_games_period_short: int = 300  # day / week / month
    max_games_period_long: int = 500  # year / all time
    period_fetch_timeout_seconds: float = 60.0

    # TUNING: preset win%-drop thresholds. Calibrated per DESIGN.md §6.3/§15 step 8
    # against real accounts (halilegebaylam, denisborisovv, peremil, biku008,
    # profile15, lance5500, zhigalko_sergei) so a ~20-24-game fetch yields roughly
    # 10-30 puzzles per band. Individual accounts still vary — see §6.3 notes.
    #
    # intermediate lowered 25->22 to differentiate it from beginner (previously
    # identical). beginner deliberately left untouched: raising it can only ever
    # reduce puzzle counts, and halilegebaylam (a real beginner-band account) is
    # already a low-volatility outlier under the 10-puzzle floor at 25 — pushing
    # beginner higher would only starve accounts like it further. Re-verified
    # against live 20-game fetches for denisborisovv/halilegebaylam (beginner)
    # and peremil (intermediate) before this change.
    thresholds: dict[str, int] = {
        "beginner": 25,
        "intermediate": 22,
        "advanced": 20,
        "expert": 18,
    }

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        # Railway's Postgres plugin has historically emitted both "postgres://" and
        # "postgresql://" — SQLAlchemy's async engine needs the "+asyncpg" driver suffix.
        if v.startswith("postgres://"):
            return "postgresql+asyncpg://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            return "postgresql+asyncpg://" + v[len("postgresql://"):]
        return v


settings = Settings()
