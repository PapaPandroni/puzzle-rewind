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

    # Forward-fill pagination (§13.2 contiguity): when the forward top-up returns
    # a full page, continuation pages (sized max_games_period_short) chase the gap
    # down to the newest stored game; this caps how many we request per sync.
    forward_fill_max_pages: int = 3

    # TUNING: self-hosted Stockfish (§14.1, cost amendments 2026-07-10). Limits
    # are wall-clock, so on usage-billed Railway `engine_movetime` is THE cost
    # lever and extra threads multiply billed CPU-seconds for the same wall time.
    # The Docker image sets STOCKFISH_PATH=/usr/games/stockfish (Debian puts the
    # binary outside PATH on slim images).
    stockfish_path: str = "stockfish"
    engine_movetime: float = 0.1  # s/position, cheap detection sweep
    engine_refine_movetime: float = 0.4  # s/position, re-check of flagged blunder plies
    engine_depth_cap: int = 18  # Limit(time=..., depth=...): whichever stops first
    engine_threads: int = 1
    engine_hash_mb: int = 128
    engine_idle_quit_seconds: float = 300.0  # lazy lifecycle: quit after idle
    engine_variation_max_plies: int = 12  # cap stored PV length, like Lichess lines

    # TUNING: engine job guardrails (§14.1 cost amendments). The per-search cap
    # bounds one visitor's ask; the daily fuse bounds worst-case total engine
    # spend under unbounded traffic (150/day ≈ $2.5/month worst case on Railway
    # usage pricing — env-tunable without a deploy if the app gets popular).
    max_engine_games_per_search: int = 40
    max_engine_games_per_day: int = 150
    worker_poll_seconds: float = 2.0  # job-claim + engine-idle-quit poll interval

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
