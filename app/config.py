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

    # TUNING: preset win%-drop thresholds. Calibrated per DESIGN.md §6.3/§15 step 8
    # against real accounts (halilegebaylam, denisborisovv, peremil, biku008,
    # profile15, lance5500, zhigalko_sergei) so a ~20-24-game fetch yields roughly
    # 10-30 puzzles per band. Individual accounts still vary — see §6.3 notes.
    thresholds: dict[str, int] = {
        "beginner": 25,
        "intermediate": 25,
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
