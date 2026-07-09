# Puzzle Rewind

Turn *your own* Lichess games into an endless puzzle stream. Enter any Lichess username and the app mines that player's recently analyzed games for blunders — the puzzle is the position just before the mistake, and the solution is the move they should have played. Game review, but gamified and infinite.

No accounts, no login, no server-side session state — puzzle sessions are entirely stateless.

**Live at:** https://puzzle-rewind-production.up.railway.app/

## Features (current — Phase 1 MVP + post-MVP hardening + Phase 2 Depth)

- Search any Lichess username; pulls their last 20 analyzed games via the public Lichess API (no auth required).
- **Full line mode** (Phase 2): instead of a single move, find up to the first 3 of your moves in the engine's refutation line — the app auto-plays the opponent's replies, and any miss reveals the whole line played out on the board.
- **Time periods** (Phase 2): mine puzzles from the last day, week, month, year, or all time (capped at 300–500 games per fetch to stay polite toward Lichess). The database accumulates each player's puzzle pool across searches, so repeat and shorter-period searches are instant.
- Blunder detection based on win-percentage swing (not raw centipawns), so puzzles reflect genuinely bad decisions rather than cosmetic eval noise in already-lost positions.
- Difficulty presets (Beginner / Intermediate / Advanced / Expert) auto-selected from the player's rating in each game — each button shows its real win%-drop value, and a live threshold slider shows/snaps to it (disabled under Auto, since Auto picks a different threshold per game rather than one fixed number).
- Interactive board (chessground + chess.js) with legal-move-only drag-and-drop, instant correct/incorrect feedback, and a "give up / show solution" path.
- Each puzzle links back to the exact move in the original game on Lichess.
- Session summary ("solved N/M on first try") at the end of a puzzle set, plus a "New search" button mid-session so you don't need to reload the page.
- Results are cached per player in a local database — repeat searches and threshold changes are served instantly without re-hitting Lichess.
- Per-IP rate limiting on the API, and an optional personal Lichess API token (`LICHESS_TOKEN`) to raise the shared rate-limit ceiling with Lichess — see Configuration below.

## Tech stack

Python 3.14 · uv · FastAPI · SQLAlchemy 2.0 (async) · Alembic · Pydantic v2 · SQLite (dev) / PostgreSQL (prod) · httpx · python-chess · vanilla JS/HTML/CSS · chessground · chess.js · Railway (deployed)

See [`DESIGN.md`](DESIGN.md) for the full design spec, including corrections and calibration notes discovered during implementation.

## Getting started

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14 (uv will fetch the interpreter automatically if needed).

```bash
# Install dependencies
uv sync

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

Tests run fully offline against committed Lichess API fixtures in `tests/fixtures/`.

### Building the container

```bash
docker build -t puzzle-rewind .
docker run -p 8000:8000 -e DATABASE_URL="sqlite+aiosqlite:///./dev.db" puzzle-rewind
```

The image runs migrations on boot and needs no network access at startup beyond what the app itself makes to Lichess.

## Upcoming features

**Phase 2 — Depth**
- Multi-move puzzles: play up to 3 moves of the engine's refutation line before the full solution is revealed, with the opponent's replies auto-played.
- Time-period selection (day / week / month / year / all time) instead of just the last 20 games, with pagination and higher game caps.

**Phase 3 — Self-hosted engine**
- Local Stockfish analysis for games Lichess hasn't analyzed, unlocking puzzles from a player's *entire* game history rather than just server-analyzed games.
- Brilliant-move detection — surface positions where the player found a strong, non-obvious move, not just their mistakes.
- Background job queue for engine analysis with live progress in the UI.

Full details, data model, and API design for both phases are in [`DESIGN.md`](DESIGN.md).

## Known MVP limitations

- Puzzles only come from games Lichess has already computer-analyzed; casual players with few analyzed games may see few or no puzzles (fixed properly in Phase 3).
- Solution checking accepts only the engine's top move (mates are always accepted as correct even if not the top line); other equally good alternatives aren't recognized yet.
- Difficulty threshold presets are calibrated against a handful of real accounts and will keep drifting as more usage data comes in — see `# TUNING` markers in `app/config.py`.
