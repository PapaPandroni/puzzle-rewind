# Puzzle Rewind

Turn *your own* Lichess games into an endless puzzle stream. Enter any Lichess username and the app mines that player's recent games for blunders — using Lichess's server analysis where it exists and its own background Stockfish where it doesn't. The puzzle is the position just before the mistake, and the solution is the move they should have played. Game review, but gamified and infinite.

No accounts, no login, no server-side session state — puzzle sessions are entirely stateless.

**Live at:** https://www.puzzle-rewind.eu

## Features (current — Phase 1 MVP + post-MVP hardening + Phase 2 Depth + Phase 3 engine pipeline)

- Search any Lichess username; pulls their last 20 games via the public Lichess API (no auth required).
- **Self-hosted engine analysis** (Phase 3): games Lichess never analyzed are analyzed by our own Stockfish in a background job — a progress banner shows "Analyzing 7/11 games…" while you keep solving, and a [Refresh puzzles] click pulls in the newly mined puzzles. Detection runs as a cheap 0.1s/position sweep with flagged blunders re-checked at 0.4s, so solutions stay sharp. Budgeted at ≤40 games per search, 150/day globally and 60/day per player to keep hosting costs hobby-sized.
- **Full line mode** (Phase 2): instead of a single move, find up to the first 3 of your moves in the engine's refutation line — the app auto-plays the opponent's replies, and on a miss, a give-up, or after you complete the line you get **Back / Forward step-through controls** to walk the solution move by move at your own pace (rather than the line flashing past automatically).
- **Time periods** (Phase 2): mine puzzles from the last day, week, month, year, or all time (capped at 300–500 games per fetch to stay polite toward Lichess). The database accumulates each player's puzzle pool across searches, so repeat and shorter-period searches are instant.
- Blunder detection based on win-percentage swing (not raw centipawns), so puzzles reflect genuinely bad decisions rather than cosmetic eval noise in already-lost positions.
- Difficulty presets (Beginner / Intermediate / Advanced / Expert) auto-selected from the player's rating in each game — each button shows its real win%-drop value, and a live threshold slider shows/snaps to it (disabled under Auto, since Auto picks a different threshold per game rather than one fixed number).
- Interactive board (chessground + chess.js) with legal-move-only drag-and-drop, instant correct/incorrect feedback, and a "give up / show solution" path.
- Each puzzle links back to the exact move in the original game on Lichess.
- Session summary ("solved N/M on first try") at the end of a puzzle set, plus a "New search" button mid-session so you don't need to reload the page.
- Results are cached per player in a local database — repeat searches and threshold changes are served instantly without re-hitting Lichess.
- Per-IP rate limiting on the API, and an optional personal Lichess API token (`LICHESS_TOKEN`) to raise the shared rate-limit ceiling with Lichess — see Configuration below.

## Tech stack

Python 3.14 · uv · FastAPI · SQLAlchemy 2.0 (async) · Alembic · Pydantic v2 · SQLite (dev) / PostgreSQL (prod) · httpx · python-chess · Stockfish · vanilla JS/HTML/CSS · chessground · chess.js · Railway (deployed)

See [`DESIGN.md`](DESIGN.md) for the full design spec, including corrections and calibration notes discovered during implementation.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14 (uv will fetch the interpreter automatically if needed).

```bash
# Install dependencies
uv sync

# Optional: Stockfish for background engine analysis (the app runs fine
# without it — engine jobs fail gracefully and engine tests auto-skip)
brew install stockfish

# Apply database migrations (creates ./dev.db by default)
uv run alembic upgrade head

# Run the dev server with auto-reload
uv run uvicorn app.main:app --reload
```

Open http://localhost:8000 and search a Lichess username.

### Configuration

Copy `.env.example` to `.env` to override defaults:

```bash
cp .env.example .env
```

Key settings:
- `DATABASE_URL` (defaults to `sqlite+aiosqlite:///./dev.db`; use a `postgresql://` or `postgresql+asyncpg://` URL in production — it's normalized automatically).
- `LICHESS_TOKEN` (optional) — a personal Lichess API token (no scopes needed, create one at [lichess.org/account/oauth/token/create](https://lichess.org/account/oauth/token/create)), sent as a Bearer header on outbound Lichess requests. Off by default; only affects rate-limit treatment, not functionality.

### Running tests

```bash
uv run pytest
```

Tests run fully offline against committed Lichess API fixtures in `tests/fixtures/`. The handful of engine tests additionally need a local Stockfish binary (`brew install stockfish`) and are auto-skipped when it's absent.

### Building the container

```bash
docker build -t puzzle-rewind .
docker run -p 8000:8000 -e DATABASE_URL="sqlite+aiosqlite:///./dev.db" puzzle-rewind
```

The image runs migrations on boot and needs no network access at startup beyond what the app itself makes to Lichess.

## Upcoming features

**Phase 3 — remaining**
- Brilliant-move detection — surface positions where the player found a strong, non-obvious move, not just their mistakes.
- Accepting near-best alternative solutions on engine-analyzed puzzles (today only the top move — or any checkmate — counts).

Full details, data model, and API design are in [`DESIGN.md`](DESIGN.md).

## Known limitations

- Games without Lichess analysis are analyzed by our own Stockfish in the background: the first search on a rarely-analyzed account shows a progress banner instead of instant puzzles, and engine work is budgeted (≤40 games per search, 150/day globally, 60/day per player) — big histories fill in across repeat searches and days. Analysis is scoped to the time period you searched, so games outside every window you've looked at stay unanalyzed until a search covers them.
- Solution checking accepts only the engine's top move (mates are always accepted as correct even if not the top line); other equally good alternatives aren't recognized yet.
- Difficulty threshold presets are calibrated against a handful of real accounts and will keep drifting as more usage data comes in — see `# TUNING` markers in `app/config.py`.
