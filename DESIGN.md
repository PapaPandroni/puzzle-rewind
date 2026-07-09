# Puzzle Rewind — Design Document

> **Working title:** Puzzle Rewind (rename freely). A web app that turns *your own games* into an endless puzzle stream. Search any Lichess username, and the app mines that player's games for critical moments — blunders they made, brilliancies they found — and serves them back as puzzles. Game review, but gamified and infinite.

**This document is the single source of truth for building this app. It is written for Claude Code to execute autonomously. Follow the phases in order. Do not skip ahead: Phase 1 (MVP) must be fully working and deployed before Phase 2 begins.**

---

## 1. Product concept

- The user enters a Lichess username (their own, a friend's, a GM's, a recent opponent's).
- The app fetches that player's recent **analyzed** games from the Lichess API.
- It extracts positions where the player blundered: the puzzle is the position *just before* the blunder, and the solution is the move they *should* have played.
- The user solves puzzles on an interactive board, one at a time, with instant feedback and a link back to the original game.
- No accounts, no login, no server-side user state. **Stateless puzzle sessions.**

The emotional hook: "you actually had this position, on this date, against this opponent — can you do better this time?"

---

## 2. Phase overview

| Phase | Scope | Engine needed? |
|---|---|---|
| **1 — MVP** | Last 20 analyzed games → single-move blunder puzzles → interactive board. Rating-based difficulty presets + advanced slider. | No (Lichess evals) |
| **2 — Depth** | Multi-move puzzles (play up to 3 moves of the refutation, then reveal full line). Time-period selection (day/week/month/year/all). Pagination beyond 20 games. | No (Lichess variations) |
| **3 — Engine** | Self-hosted Stockfish: analyze games that have no Lichess analysis (i.e., pull from *all* of a user's games). Brilliant-move detection. | Yes |

---

## 3. Tech stack (locked)

| Layer | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | |
| Package manager | **uv** | `uv init`, `uv add`, `uv run`. No pip, no poetry. |
| Backend | **FastAPI** | Async endpoints. |
| ORM | **SQLAlchemy 2.0** | `Mapped[]` / `mapped_column()` style only. No legacy Query API. |
| Validation | **Pydantic v2** | `model_config = ConfigDict(from_attributes=True)` for ORM schemas. |
| DB (dev) | SQLite | Zero setup. |
| DB (prod) | PostgreSQL | Railway Postgres plugin. |
| DB switch | `DATABASE_URL` env var | Code must never care which DB it talks to. See §10. |
| Migrations | Alembic | From day one, even on SQLite. |
| HTTP client | `httpx` (async) | For Lichess API, with streaming support. |
| Chess logic | `python-chess` | PGN/move parsing, FEN generation, move validation server-side. |
| Frontend | **Vanilla JS + HTML + CSS** | No framework, no build step. Single page. |
| Board UI | **chessground** (Lichess's own board lib, via CDN/ESM) | Free, battle-tested, handles drag/drop, orientation, arrows. |
| Client chess rules | **chess.js** (via CDN/ESM) | Legal move generation client-side. |
| Deployment | **Railway** via Dockerfile | See §11. |
| Testing | pytest + pytest-asyncio + httpx test client | |

---

## 4. Repository layout

```
puzzle-rewind/
├── pyproject.toml            # uv-managed
├── uv.lock
├── Dockerfile
├── .env.example              # DATABASE_URL=sqlite+aiosqlite:///./dev.db
├── alembic/
│   └── versions/
├── alembic.ini
├── app/
│   ├── __init__.py
│   ├── main.py               # FastAPI app, lifespan, static file mounting
│   ├── config.py             # pydantic-settings: DATABASE_URL, thresholds
│   ├── database.py           # async engine, session factory, get_db dependency
│   ├── models.py             # SQLAlchemy ORM models
│   ├── schemas.py            # Pydantic schemas
│   ├── lichess.py            # Lichess API client (httpx, NDJSON streaming)
│   ├── analysis.py           # win% math, blunder extraction, puzzle building
│   └── routers/
│       ├── __init__.py
│       └── puzzles.py        # API endpoints
├── static/
│   ├── index.html
│   ├── app.js
│   └── style.css
└── tests/
    ├── conftest.py
    ├── fixtures/             # saved real NDJSON responses (see §12)
    ├── test_analysis.py
    ├── test_lichess.py
    └── test_api.py
```

---

## 5. Lichess API integration

### 5.1 The one endpoint that matters (MVP)

```
GET https://lichess.org/api/games/user/{username}
Accept: application/x-ndjson
```

Query parameters for the MVP:

| Param | Value | Why |
|---|---|---|
| `max` | `20` | MVP scope: last 20 analyzed games. |
| `analysed` | `true` | Only games that have server analysis. Critical — without this, evals are missing. |
| `evals` | `true` | Include the per-move analysis array. |
| `moves` | `true` | Include the move list (default true, be explicit). |
| `rated` | omit | Include casual games too. |
| `perfType` | `ultraBullet,bullet,blitz,rapid,classical,correspondence` | Standard chess only. **Exclude variants** — puzzle logic assumes standard rules. Also check `"variant": "standard"` per game as a belt-and-braces filter. |

No authentication is required for public game export. Do not implement OAuth in any phase. Anonymous throttle is ~20 games/second, which is far more than needed.

### 5.2 Response shape (verified)

One JSON object per line (NDJSON). Relevant fields per game:

```json
{
  "id": "9Llyao5C",
  "rated": true,
  "variant": "standard",
  "speed": "rapid",
  "createdAt": 1535766417332,
  "lastMoveAt": 1535766890981,
  "status": "resign",
  "players": {
    "white": {"user": {"id": "zidane1986", "name": "Zidane1986"}, "rating": 1674},
    "black": {"user": {"id": "e4guardian", "name": "e4Guardian"}, "rating": 1813}
  },
  "winner": "white",
  "moves": "c4 Nc6 Nc3 e5 Nf3 ...",
  "analysis": [
    {"eval": 0},
    {"eval": 77, "best": "c7c5", "variation": "c5 Nf3 Nf6 g3 d5 ...",
     "judgment": {"name": "Inaccuracy", "comment": "Inaccuracy. c5 was best."}},
    {"eval": 543},
    {"mate": 3}
  ],
  "clock": {"initial": 600, "increment": 0, "totalTime": 600}
}
```

Key facts to build on:

- `analysis[i]` is the engine eval **after** move `i+1` (i.e., after the 1st half-move, 2nd half-move, ...). `analysis[0]` is the eval after White's first move. Even indices = positions after White moved; odd = after Black moved. **Verify this indexing against a real game during implementation before relying on it** — write a test with a known game.
- Each entry has either `eval` (centipawns, from White's perspective) or `mate` (moves to mate, positive = White mates, negative = Black mates).
- Entries where the played move was judged bad additionally carry `best` (UCI, e.g. `c7c5`), `variation` (SAN line, space-separated), and `judgment.name` ∈ {`Inaccuracy`, `Mistake`, `Blunder`}.
- **`best` is the puzzle solution and `variation` is the phase-2 multi-move line. No engine required.**
- Player ratings are embedded per game — this powers rating-based thresholds with zero extra API calls.

### 5.3 Client implementation notes (`app/lichess.py`)

- Use `httpx.AsyncClient` with `client.stream("GET", url, ...)` and iterate `aiter_lines()`, parsing each line with `json.loads`. Skip empty lines.
- Set a generous timeout (30s read) — Lichess streams can be slow to start.
- Set a descriptive `User-Agent` header, e.g. `puzzle-rewind/0.1 (hobby project)`. Lichess asks API consumers to identify themselves.
- **429 handling:** if a 429 is received, stop, and surface a clean error to the frontend ("Lichess is rate-limiting us, wait a minute and retry"). Do not auto-retry in a loop. Lichess policy: wait a full minute after a 429.
- **Edge cases the client must handle gracefully:**
  - Username does not exist → Lichess returns 404 → surface "user not found".
  - User exists but has zero analyzed games → empty stream → surface "no analyzed games found; analyze some games on Lichess first" with a short explanation that puzzles come from computer-analyzed games (MVP limitation, fixed in Phase 3).
  - User has disabled game export in privacy settings → empty stream → same message path.
  - Games with `analysis` missing despite `analysed=true` (defensive) → skip game.

### 5.4 Phase 2/3 additions

- `since` / `until` (epoch **milliseconds**) for the time-period picker.
- Drop `analysed=true` in Phase 3 (self-hosted engine analyzes everything).
- Raise `max` with server-side batching (see §13, §14).

---

## 6. Blunder detection (`app/analysis.py`)

### 6.1 Core metric: win-percentage swing, not raw centipawns

Raw centipawn deltas are misleading in decided positions (going from −500 to −900 is a 400cp "blunder" but a worthless puzzle — the player was lost either way). Lichess's own accuracy system converts evals to winning chances. Use the same formula:

```
win% (for White) = 50 + 50 * (2 / (1 + exp(-0.00368208 * cp)) - 1)
```

- `cp` is the eval in centipawns from White's perspective.
- Mate scores: treat `mate: n` as win% = 100 for the side that mates (0 for the other). i.e., `mate > 0` → White win% 100; `mate < 0` → White win% 0.
- Win% for Black = 100 − win% for White.
- Clamp cp input to ±1000 before the formula to avoid float silliness.

A **blunder by the target player** on half-move `i` (0-indexed into the moves list) is detected as:

```
drop = winP_mover(before move i) − winP_mover(after move i)
is_blunder = drop >= threshold
```

Where `before move i` uses `analysis[i-1]` (or the start position eval ≈ 0cp for `i == 0`) and `after move i` uses `analysis[i]`, both converted to the mover's perspective.

### 6.2 Puzzle-quality filters (all must pass)

1. **The mover is the searched player.** Determine the player's color per game by matching `players.white.user.id` / `players.black.user.id` against the searched username (lowercase; Lichess ids are lowercase).
2. **Not already lost:** mover's win% *before* the move ≥ 20. A "blunder" from a dead-lost position is not an interesting puzzle.
3. **Solution exists:** the analysis entry for the move has a `best` field. (In practice, every judged move does.)
4. **Not a trivial recapture-only position** — skip for MVP; revisit in Phase 2 if puzzle quality is poor.
5. **Deduplicate:** one puzzle per (game, ply). Also skip the first 5 full moves (ply < 10) — opening blunders at low depth make poor puzzles and evals there are noisy.

### 6.3 Rating-based presets + advanced slider

The frontend offers a difficulty preset auto-selected from the player's rating, with an advanced slider override. Presets map to **win%-drop thresholds** (constants in `config.py`, explicitly marked as tunable):

| Preset | Auto-selected when player rating | Threshold (win% drop) | Intent |
|---|---|---|---|
| Beginner | < 1200 | ≥ 25 | Only huge, obvious blunders (hung pieces, missed mates). |
| Intermediate | 1200–1799 | ≥ 25 | Clear blunders. |
| Advanced | 1800–2199 | ≥ 20 | Blunders + serious mistakes. |
| Expert | ≥ 2200 | ≥ 18 | Includes subtler mistakes. |

- The rating used is the target player's rating **in each game** (it's in the export). If threshold preset is "auto", each game uses its own rating band — this handles players whose rating varies across time controls naturally.
- The user can override with an explicit preset, or open "advanced" and set the slider directly: range 10–40 (win% drop), step 1. Show a live hint translating it roughly ("≈ how big a mistake counts as a puzzle: lower = more, subtler puzzles").
- **Calibrated against real accounts** (§15 step 8): beginner (halilegebaylam ~880, denisborisovv ~1100), intermediate (peremil ~1590, biku008 ~1350 bullet), advanced (profile15 ~1800), expert (lance5500 ~2500, zhigalko_sergei ~2500) at a ~20-24-game fetch. Beginner moved 30→25 and expert moved 15→18 from the original guesses — both were off by enough to push typical accounts outside the 10–30-puzzle target (e.g. beginner at 30 gave one real account only 3 puzzles; expert at 15 gave another 42). Individual accounts still vary a lot even within a band — some low-volatility accounts (e.g. halilegebaylam, ~5 puzzles even at 25) will land under 10 regardless of threshold; that's accepted MVP variance, not a bug (see §16).
- Note: Lichess's own `judgment.name == "Blunder"` uses fixed thresholds. Use it in tests as a sanity check (our detector at threshold ≈ 30 should broadly agree), but the custom-threshold feature requires computing from raw evals, which is the primary path.

### 6.4 Building the puzzle object

For each detected blunder at half-move index `i`:

1. Replay `moves` with `python-chess` up to (not including) move `i` → this position's FEN is the **puzzle position**.
2. `side_to_move` = the player (puzzle is always from the searched player's perspective; board oriented to their color).
3. `solution_uci` = `analysis[i].best`.
4. `solution_san` = convert `best` to SAN using the python-chess board at the puzzle position.
5. `played_uci` / `played_san` = the move actually played (for the "you played X" reveal).
6. `variation_san` = `analysis[i].variation` split on spaces (stored for Phase 2; also shown after solving in MVP as a static text line).
7. `eval_before` / `eval_after` / `win_drop` = for display ("this move dropped your winning chances by 34%").
8. Metadata: `game_id`, `game_url` = `https://lichess.org/{game_id}` + `/black` suffix if player was Black, plus `#{ply}` anchor so the link opens at the exact move; `opponent_name`, `player_rating`, `opponent_rating`, `speed`, `played_at` (from `createdAt`).

### 6.5 Solution checking

Server-side check (endpoint in §8): the submitted move (UCI) is correct iff it equals `solution_uci`, **or** it is a different move that python-chess confirms delivers checkmate (a mate is never wrong). Do not accept "equally good alternative" moves in MVP — we only have one engine line. Document this limitation in the UI copy ("the answer is the engine's top choice").

**Implementation note:** `chess.Board.is_checkmate()` only reflects the *current* position — there's no single-call "would this move deliver mate" method. Push the candidate move, check, then pop:
```python
move = chess.Move.from_uci(candidate_uci)
is_mate = False
if move in board.legal_moves:
    board.push(move)
    is_mate = board.is_checkmate()
    board.pop()
```

---

## 7. Data model (`app/models.py`)

Caching is the reason a DB exists at all here (sessions are stateless). Fetch + parse once per (username, settings) and serve repeats instantly.

```python
class Player(Base):
    __tablename__ = "players"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(30), unique=True, index=True)  # lowercase lichess id
    last_fetched_at: Mapped[datetime | None]

class Game(Base):
    __tablename__ = "games"
    id: Mapped[int] = mapped_column(primary_key=True)
    lichess_id: Mapped[str] = mapped_column(String(12), unique=True, index=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"), index=True)
    player_color: Mapped[str] = mapped_column(String(5))          # "white" | "black"
    player_rating: Mapped[int]
    opponent_name: Mapped[str] = mapped_column(String(30))
    opponent_rating: Mapped[int]
    speed: Mapped[str] = mapped_column(String(20))
    played_at: Mapped[datetime]
    raw_analysis_processed: Mapped[bool] = mapped_column(default=False)

    player: Mapped["Player"] = relationship(back_populates="games")
    puzzles: Mapped[list["Puzzle"]] = relationship(back_populates="game")

class Puzzle(Base):
    __tablename__ = "puzzles"
    id: Mapped[int] = mapped_column(primary_key=True)
    game_id: Mapped[int] = mapped_column(ForeignKey("games.id"), index=True)
    ply: Mapped[int]                                   # half-move index of the blunder
    fen: Mapped[str] = mapped_column(String(100))
    side_to_move: Mapped[str] = mapped_column(String(5))
    solution_uci: Mapped[str] = mapped_column(String(6))
    solution_san: Mapped[str] = mapped_column(String(10))
    played_uci: Mapped[str] = mapped_column(String(6))
    played_san: Mapped[str] = mapped_column(String(10))
    variation_san: Mapped[str] = mapped_column(Text)    # space-separated SAN line
    win_drop: Mapped[float]                             # win% drop of the actual played move
    eval_before_cp: Mapped[int | None]                  # null if mate score
    eval_after_cp: Mapped[int | None]
    kind: Mapped[str] = mapped_column(String(10), default="blunder")  # "blunder" | "brilliant" (phase 3)

    game: Mapped["Game"] = relationship(back_populates="puzzles")

    __table_args__ = (UniqueConstraint("game_id", "ply"),)
```

Design decisions:

- **Store every judged mistake ≥ 10 win% drop** (the loosest possible threshold), and filter by the requested threshold **at query time** (`WHERE win_drop >= :threshold`). This way changing the slider never refetches or reparses — it's just a different query. This is the key caching insight.
- Cache freshness: if `Player.last_fetched_at` is within `CACHE_TTL` (default 1 hour, config), serve from DB only. Otherwise fetch from Lichess (games already in DB are skipped by `lichess_id` uniqueness — use `since=last game's timestamp` to only fetch newer games), then serve.
- No user/session tables. Stateless.

---

## 8. API endpoints (`app/routers/puzzles.py`)

```
GET  /api/players/{username}/puzzles
     ?threshold=25          # win% drop, 10–40, default from rating presets ("auto")
     &preset=auto           # auto | beginner | intermediate | advanced | expert | custom
     &limit=50              # max puzzles returned
     → 200 PuzzleSetResponse
     → 404 {"detail": "lichess_user_not_found"}
     → 200 with empty list + reason field when no analyzed games
     → 503 {"detail": "lichess_rate_limited"} on upstream 429
```

`PuzzleSetResponse`:

```json
{
  "username": "peremil",
  "player_ratings_seen": [1642, 1655],
  "games_scanned": 20,
  "puzzles": [
    {
      "id": 17,
      "fen": "r1bqkb1r/...",
      "side_to_move": "black",
      "game_url": "https://lichess.org/9Llyao5C/black#34",
      "opponent_name": "e4Guardian",
      "opponent_rating": 1813,
      "speed": "rapid",
      "played_at": "2018-09-01T02:26:57Z",
      "win_drop": 34.2
    }
  ],
  "reason": null
}
```

**The response deliberately excludes the solution.** Checking happens server-side:

```
POST /api/puzzles/{puzzle_id}/attempt
     body: {"move_uci": "e2e4"}
     → 200 {
         "correct": true,
         "solution_uci": "c7c5",
         "solution_san": "c5",
         "played_san": "Nf6",          # what the player actually played in the real game
         "win_drop": 34.2,
         "variation_san": ["c5", "Nf3", "Nf6", "g3"],
         "opponent_reply_uci": null     # phase 2: engine line's reply for multi-move
       }
```

Return the full solution info on both correct and incorrect attempts *after the first attempt* — MVP flow is one guess, then reveal, with a "show me" button that calls the same endpoint with `{"move_uci": null}` (give-up path).

```
GET /healthz → 200 {"status": "ok"}    # Railway healthcheck
```

Static files: `app.mount("/", StaticFiles(directory="static", html=True))` — mounted **after** the API routers so `/api/*` wins.

Shuffle puzzle order server-side (stable shuffle seeded per request) so consecutive puzzles jump between games.

### Input validation

- `username`: 2–30 chars, `[a-zA-Z0-9_-]` only, lowercase before lookup. Reject anything else with 422 — never interpolate raw input into the Lichess URL.
- `threshold`: int 10–40. `limit`: int 1–200.

---

## 9. Frontend (`static/`)

Single page, three states. No framework, no build step. Use ESM imports from **jsDelivr's `+esm` endpoint** (not esm.sh — jsDelivr serves both the JS module and chessground's required CSS from one domain) for chessground and chess.js — pin exact versions:

```
https://cdn.jsdelivr.net/npm/chessground@9/+esm       (resolve & pin exact patch, e.g. @9.2.1)
https://cdn.jsdelivr.net/npm/chess.js@1/+esm           (resolve & pin exact patch, e.g. @1.4.0)
https://cdn.jsdelivr.net/npm/chessground@9/assets/chessground.base.css
https://cdn.jsdelivr.net/npm/chessground@9/assets/chessground.brown.css     (board theme)
https://cdn.jsdelivr.net/npm/chessground@9/assets/chessground.cburnett.css  (piece theme)
```

**chessground ships zero chess rules** — it needs the CSS above to render at all (unstyled/invisible without it, a gap easy to miss). Set `movable.free: false` and compute `movable.dests` from a chess.js `Chess()` instance (a map of square → legal target squares) to restrict drag/drop to legal moves; after each move, re-derive `dests` from chess.js's updated state and call `cg.set({ movable: { dests } })`.

### State 1 — Search

- Big centered input: "Enter a Lichess username", search button, Enter submits.
- Difficulty control: segmented preset buttons `[Auto] [Beginner] [Intermediate] [Advanced] [Expert]` + a collapsed "Advanced: custom threshold" slider (10–40). Auto is default and shows "based on their rating".
- Loading state while fetching (this can take a few seconds: streaming + parsing 20 games). Show a small progress hint ("Fetching games from Lichess…").
- Error states rendered inline: user not found / no analyzed games (with the one-line explanation and a link to how Lichess analysis works) / rate limited.

### State 2 — Puzzle

- Chessground board, oriented to the searched player's color, position from FEN.
- Header line: "**{username}** vs {opponent} ({opponent_rating}) · {speed} · {date}" — and a "{side} to move" badge.
- The user drags a move. Legal-move validation client-side with chess.js (only legal moves can be dropped). On drop → POST to `/attempt`.
  - **Correct:** green flash, "That's the move! You played {played_san} in the game, dropping your winning chances by {win_drop}%." Show variation line as text. Buttons: [Next puzzle] [View game on Lichess].
  - **Incorrect:** shake/red flash, reveal "Best was **{solution_san}**. In the game you played {played_san}." Animate the solution move on the board. Same buttons.
- "Give up / show solution" link under the board.
- Puzzle counter: "Puzzle 4 of 23".
- After the last puzzle: summary ("You solved 15/23 on the first try") — computed client-side in memory, no persistence — and a "Search another player" button.

### State 3 — Empty/error

Covered above; always give the user a next action.

Keep CSS minimal and clean; dark-friendly. Board is the hero, centered, max ~560px, responsive down to mobile widths (chessground handles touch).

---

## 10. Configuration & database switching

`app/config.py` with pydantic-settings:

```python
class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    cache_ttl_seconds: int = 3600
    lichess_base: str = "https://lichess.org"
    max_games_mvp: int = 20
    min_win_drop_stored: float = 10.0
    # TUNING: preset thresholds (calibrated per §6.3/§15 step 8)
    thresholds: dict = {"beginner": 25, "intermediate": 25, "advanced": 20, "expert": 18}
```

- Async engine: `create_async_engine(settings.database_url)`.
- Drivers: `aiosqlite` for dev, `asyncpg` for prod. Add both with uv.
- **Railway quirk:** Railway's Postgres plugin provides `DATABASE_URL` in the form `postgresql://...` (or historically `postgres://...` — handle **both** prefixes). SQLAlchemy async needs `postgresql+asyncpg://...`. Normalize in `config.py` via a `field_validator("database_url", mode="before")`: rewrite either `postgres://` or `postgresql://` to `postgresql+asyncpg://`. Also strip any `?sslmode=` query param if asyncpg complains and pass ssl via connect args if needed — handle this defensively with a comment.
- Alembic: use the official **async template** (`alembic init -t async alembic`). Its `env.py` uses `async_engine_from_config(...)` + `connection.run_sync(do_run_migrations)` + `poolclass=pool.NullPool`, and works directly against both `sqlite+aiosqlite://` and `postgresql+asyncpg://` — no separate sync driver (`psycopg2`/`psycopg`) is needed at all. Set `config.set_main_option("sqlalchemy.url", settings.database_url)` in `env.py` so `DATABASE_URL` stays the single source of truth instead of hardcoding it in `alembic.ini`.

---

## 11. Deployment (Railway)

- **Dockerfile** (Railway auto-detects it):

```dockerfile
FROM python:3.14-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY . .
EXPOSE 8000
# UV_NO_SYNC: without it, "uv run" re-syncs the environment (pulling the dev
# dependency group from PyPI) on every container start, silently defeating the
# --no-dev build above and requiring network access just to boot.
ENV UV_NO_SYNC=1
CMD ["sh", "-c", "uv run alembic upgrade head && uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
```

Base image version must track `requires-python` in `pyproject.toml` (3.14 here, not the 3.12 originally sketched — verify locally with `docker build . && docker run --network none <image>` that the container boots with zero network access, which proves `UV_NO_SYNC` is doing its job).

- Railway injects `PORT` — the CMD must bind to it (as above).
- Add the Railway Postgres plugin; reference its `DATABASE_URL` in the service variables.
- Migrations run on boot (fine at this scale; simple and predictable).
- `/healthz` as the Railway healthcheck path.
- No volumes needed (SQLite is dev-only; prod state lives in Postgres).
- Phase 3 note: the Docker image will additionally need the Stockfish binary (`apt-get install stockfish` in a slim-compatible way, or download a static binary) — leave a TODO comment in the Dockerfile now.

---

## 12. Testing strategy

- **Fixtures over mocks:** during development, download 2–3 real NDJSON responses (`curl 'https://lichess.org/api/games/user/<some_user>?max=5&analysed=true&evals=true' -H 'Accept: application/x-ndjson' > tests/fixtures/games_sample.ndjson`) and commit them. All analysis tests run against these — deterministic, offline, real-world-shaped.
- `test_analysis.py`:
  - win% formula spot-checks (cp=0 → 50; cp=+300 → ~75; mate → 100/0).
  - Analysis index alignment (confirmed correct against a real-world Lichess NDJSON consumer, but still test defensively — see §16): for a fixture game, find the ply where Lichess's own `judgment.name == "Blunder"` fires, replay `moves` with python-chess to that ply, and assert the move actually played there (converted to SAN) matches the move Lichess judged — not just "a blunder exists somewhere." Then assert our detector flags the same ply at threshold ≈ 30.
  - Filters: already-lost positions excluded; ply < 10 excluded; player-color matching correct for both colors.
  - FEN reconstruction: replaying fixture moves to a puzzle ply yields a legal position and `solution_uci` is legal in it.
- `test_api.py`: httpx `AsyncClient` against the app with the Lichess client monkeypatched to return fixture data. Test the full flow: fetch puzzles → attempt wrong move → attempt right move; threshold query filtering; 404 username; empty-games reason.
- `test_lichess.py`: NDJSON line parsing, 429 → clean error, 404 → clean error.
- Run with `uv run pytest`.

---

## 13. Phase 2 — Depth (build only after MVP is deployed and working)

### 13.1 Multi-move puzzles

- Source: the stored `variation_san` (engine line, alternating mover/opponent moves, starting with the mover's best move).
- Flow: user must find up to the **first 3 mover-moves** of the variation (moves 1, 3, 5 of the line). After each correct user move, the app auto-plays the opponent's reply from the variation with a short animation delay (~300ms).
- If the variation is shorter than 3 mover-moves, require however many exist.
- After the 3rd correct move (or a wrong move, or give-up): reveal and animate the **complete** variation line, then offer [Next puzzle].
- Server changes: `/attempt` becomes stateless-but-positional — request includes `move_index`; server validates against `variation_san[move_index]` (convert SAN↔UCI with python-chess from the reconstructed position). Response includes `opponent_reply_uci` and `line_complete: bool`.
- UI toggle on the search screen: `[Single move] [Full line]`.

### 13.2 Time-period selection

- UI: `[Last 20 games] [Day] [Week] [Month] [Year] [All time]` → maps to `since`/`until` epoch-ms params.
- Guardrails: cap at `max=300` games per fetch for Day/Week/Month, `max=500` for Year/All (config constants). Show game count in the loading state ("scanned 134 games…" — achievable because NDJSON streams; update via a simple polling endpoint or just show an indeterminate spinner + final count, simplest wins).
- Cache interplay: `since = Player.last_fetched_at` avoids refetching known games; the DB accumulates a player's puzzle pool over time, which is exactly the "puzzles just keep coming" feel.

## 14. Phase 3 — Self-hosted Stockfish (build only after Phase 2)

Purpose: (a) generate puzzles from games that were **never analyzed** on Lichess (the majority, for most users), and (b) detect **brilliant moves**.

### 14.1 Engine infrastructure

- Stockfish binary in the Docker image; driven via `python-chess`'s `chess.engine.SimpleEngine` (run in a thread executor — it's a sync API) or `popen_uci` async variant.
- Budget per position: ~0.1–0.3s at depth 16–20, `multipv=2`. A 60-move game ≈ 120 positions ≈ 15–40s. Therefore **analysis must be a background job**, not inline in a request.
- Keep it simple: a `jobs` table (id, username, params, status: queued/running/done/failed, progress int, created_at) + a single asyncio worker task started in the FastAPI lifespan that polls for queued jobs. No Celery, no Redis — one Railway service. Frontend polls `GET /api/jobs/{id}` and shows progress ("analyzing game 7/40…"), serving already-extracted puzzles immediately while the rest cook.
- Railway resource note: Stockfish is CPU-hungry; limit to 1 engine process, `Threads=1..2`, `Hash=128`. This is a hobby deployment, not a farm.

### 14.2 Brilliant-move detection (heuristic, tunable)

Lichess marks nothing as "brilliant" — this is our own heuristic. A mover's move at ply `i` is *brilliant-candidate* if all hold (all constants in config, marked `# TUNING`):

1. **It was the engine's top move** (matches multipv[1] at our depth), or within 10cp of it.
2. **Uniqueness:** the gap between best and second-best move (multipv 1 vs 2) is ≥ 150cp — i.e., every other move loses significant ground. "Only move" quality.
3. **Non-obviousness proxy (pick at least one):**
   - The move is a **sacrifice**: static material balance after the move (and after the forced recapture sequence, approximated by the engine line's next 2 plies) is worse for the mover than before, yet eval holds or improves. Compute material with python-chess piece values.
   - OR the move is not a capture and not a check, yet satisfies (1)+(2) in a sharp position (|eval| swing among alternatives high).
4. **Position mattered:** mover's win% before the move between 20 and 90 (drama filter).

Brilliancies become puzzles with `kind="brilliant"`, framed differently in the UI: "You found a brilliant move here on {date}. Can you find it again?" Expect these to be rare (that's the point) — surface a mixed feed with blunder-redos by default and a filter toggle `[Mistakes] [Brilliancies] [Both]`.

### 14.3 Eval source hierarchy

For any game: if Lichess analysis exists → use it (free, instant). Else → queue for local engine. Store `eval_source` (`lichess` | `stockfish`) on Game for debugging.

---

## 15. Build order for Claude Code (Phase 1)

Work in this exact order; each step ends with something runnable and its tests passing before moving on.

1. **Scaffold:** `uv init`, add deps (`fastapi uvicorn[standard] sqlalchemy aiosqlite asyncpg alembic httpx python-chess pydantic-settings`, dev: `pytest pytest-asyncio`), repo layout, config, database.py, empty FastAPI app with `/healthz`, Alembic init + first migration. Runs locally with `uv run uvicorn app.main:app --reload`.
2. **Lichess client:** streaming fetch, NDJSON parse, error mapping. Download and commit test fixtures. Tests pass offline.
3. **Analysis module:** win% math, blunder extraction, puzzle building with python-chess. Test against fixtures, including the index-alignment test (§12).
4. **Persistence:** models, migration, cache-aware fetch flow (store all ≥10% drops, query-time threshold filtering).
5. **API endpoints:** puzzles list + attempt + give-up path. API tests.
6. **Frontend:** search → puzzle loop → summary. Manual test against a real account.
7. **Deploy:** Dockerfile, Railway service + Postgres plugin, verify `DATABASE_URL` normalization, healthcheck green, end-to-end test on the live URL.
8. **Tune:** run against 2–3 real accounts of different ratings, adjust preset thresholds so 20 games yield ~10–30 puzzles per band.

Definition of done for MVP: a stranger can open the URL, type any Lichess username, and grind blunder-redo puzzles with sensible difficulty defaults, on desktop and mobile.

---

## 16. Risks & open questions

- **Analysis index alignment** (§5.2) is the most likely silent-bug source. Cross-checked against a real-world Lichess NDJSON consumer prior to implementation and confirmed correct (`analysis[i]` = eval after half-move `i`, 0-indexed), but the dedicated test in step 3 is still mandatory as a regression guard.
- **Analyzed-games scarcity:** casual players often have very few analyzed games, so the MVP may return 0–3 puzzles for many usernames. Acceptable for MVP; the empty-state copy must explain it, and Phase 3 is the real fix.
- **Single-solution strictness:** engine-top-move-only checking will occasionally reject a move that's equally good. Accepted MVP tradeoff (mates excepted); Phase 3's multipv data can relax this (accept any move within ~25cp of best).
- **Preset threshold values** are guesses until step 8 tuning.
- **Lichess ToS/etiquette:** public data, anonymous access, low volume, identified User-Agent — comfortably within acceptable use. Do not parallelize fetches.
