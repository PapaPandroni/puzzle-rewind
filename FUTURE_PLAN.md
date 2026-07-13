# Puzzle Rewind — Future Plan (Phases 2 & 3)

> Implementation plan for the phases of `DESIGN.md` that are not yet built: **Phase 2 (Depth)** and **Phase 3 (Self-hosted Stockfish)**. Written against the codebase as of 2026-07-09 (Phase 1 live on Railway, 40 tests passing). Follow the build order in §5; each step ends runnable with tests passing, same discipline as Phase 1.

**Execution notes (read first):**
- Read `DESIGN.md` in full before starting — this plan references its sections (§13, §14, §16) and assumes its context (Lichess API shape, win% math, caching model, deployment setup).
- Run `uv run pytest` after every step; do not move to the next step with failures.
- Code references below name files and symbols (e.g. `_sync_player_games` in `app/routers/puzzles.py`). Any line numbers are approximate as of 2026-07-09 and will drift as edits land — locate by symbol name, not line.
- Where this plan states a **Decision**, follow it as written; it resolves an ambiguity deliberately.

---

## 1. Where the code already is (what Phase 2/3 can lean on)

Phase 1 deliberately left hooks for both phases — the plan below reuses them rather than re-inventing:

- `Puzzle.variation_san` is already stored per puzzle (space-separated SAN engine line, `app/models.py`) — the entire data source for multi-move puzzles. **No new fetching or migration needed for §2.1.**
- `AttemptResponse.opponent_reply_uci` already exists and is hardcoded `None` at the end of `attempt_puzzle` (`app/routers/puzzles.py`) — the response shape anticipated Phase 2.
- `Puzzle.kind` already exists with default `"blunder"` (`app/models.py`) — the brilliant-move column is in place.
- `fetch_games()` already accepts `since` and `max_games` (`app/lichess.py`) — Phase 2 needs to add `until` and Phase 3 needs an `analysed` toggle, both small.
- The "store every ≥10% drop, filter at query time" caching design means threshold changes never refetch — Phase 2's period-based accumulation extends the same pool.
- The Dockerfile already carries the Phase 3 TODO for the Stockfish binary (`DESIGN.md` §11).

---

## 2. Phase 2 — Depth

Three features: multi-move puzzles, time-period selection, and fetching beyond 20 games (the latter two are one feature in practice — periods are *how* you get more games).

### 2.1 Multi-move puzzles (DESIGN §13.1)

**Concept:** in "Full line" mode the user must find up to the first 3 mover-moves of the stored variation (line indices 0, 2, 4). After each correct move the app auto-plays the opponent's reply (line indices 1, 3) with ~300 ms delay. Wrong move / give-up / completion reveals the full line.

**Server is stateless-but-positional:** the client sends `move_index` (position in the variation line); the server reconstructs the board by replaying the variation up to that index and validates against `variation_san[move_index]`. No session state.

#### Backend changes

**`app/schemas.py`**
- `AttemptRequest`: add `move_index: int = 0` (validated `ge=0`, small upper bound e.g. `le=8`).
- `AttemptResponse`: add `line_complete: bool = True` (single-move mode always returns `True`); `opponent_reply_uci` becomes actually populated.
- `PuzzleSummary`: add `mover_moves_in_line: int` — `min(3, number of mover moves in the variation)` — so the UI can show "move 2 of 3" without leaking the moves themselves. Computed at query time from `variation_san`; no migration.

**`app/analysis.py`** — new pure helpers (all unit-testable offline against fixtures):
```python
def variation_board(fen: str, variation_san: list[str], upto: int) -> chess.Board
    # board after pushing variation_san[:upto] onto the puzzle FEN;
    # raises/returns None if any SAN fails to parse (defensive — treat line as ended)

def variation_move_uci(fen: str, variation_san: list[str], index: int) -> str | None
    # UCI of variation_san[index] in its correct position, None if out of range/unparseable

def mover_moves_in_line(variation_san: list[str], cap: int = 3) -> int
    # mover moves are even indices 0, 2, 4 → min(cap, ceil(len(line)/2))
```
SAN→UCI must be converted *at the reconstructed position* (SAN is position-dependent); `board.parse_san()` handles this, including promotions and disambiguation.

**`app/routers/puzzles.py` — `attempt_puzzle` rework:**
1. Reconstruct `board = variation_board(puzzle.fen, line, body.move_index)`. If `move_index > 0` and reconstruction fails or the index is past the mover moves → 422 (`"invalid_move_index"`).
2. `expected_uci = variation_move_uci(...)`. For `move_index == 0` keep the existing fallback: `puzzle.solution_uci` is authoritative (the variation's first move *should* equal `best`, but trust the stored solution).
3. `correct = submitted == expected` **or** `move_delivers_checkmate(board, submitted)` — the mate exception applies at every step. If an *alternate* mate is accepted, set `line_complete = True` immediately (the stored line no longer applies).
4. On correct and more mover moves remain (`move_index + 2` is still within the first `2 * mover_moves_in_line` plies and the line has a reply): return `opponent_reply_uci = variation_move_uci(fen, line, move_index + 1)` and `line_complete = False`. The MVP response always includes the full `variation_san`, which in line mode would spoil moves 2–3. **Decision: mid-line responses (`line_complete = False`) return `variation_san = []` and `solution_san` = the SAN of the current move only; the full line is returned only when `line_complete = True`.** This is the one behavior change to the existing contract; single-move clients are unaffected because their responses always have `line_complete = True`.
5. On wrong move or give-up (`move_uci: null`): `correct = False`, `line_complete = True`, full `variation_san` returned for the reveal.

**Rate limit:** line mode sends up to 3 attempts per puzzle — raise the `@limiter.limit` on `attempt_puzzle` from `60/minute` to `120/minute` (`app/routers/puzzles.py`).

#### Frontend changes (`static/app.js`)

- Search screen: mode toggle `[Single move] [Full line]` (a second `preset-row`-style segmented control; persists in `state.mode`).
- Puzzle state gains `lineIndex` (current mover-move number) and a per-puzzle `firstTryClean` flag — `solvedFirstTry` increments only if the *whole line* was found with no misses.
- `onUserMove` in line mode: POST with `move_index`; on `correct && !line_complete`: play the user's move on the chess.js instance, then after ~300 ms animate `opponent_reply_uci` via `cg.move()` + chess.js `.move()`, re-derive `dests`, unlock board, show progress ("Found it — 1 of 3. Keep going."), `lineIndex += 1` (in line indices: `+2`).
- On `line_complete` (success, failure, or give-up): reveal — animate the remaining variation moves sequentially (~400 ms apart) from the current position, then show the existing feedback block + [Next puzzle] buttons.
- Header shows "Find the best line (3 moves)" vs "Find the best move" per mode; use `mover_moves_in_line` for the real count (lines shorter than 3 mover-moves require fewer — DESIGN §13.1).

#### Tests

- `test_analysis.py`: `variation_board`/`variation_move_uci` against fixture variations, plus **synthetic hand-built game dicts covering a promotion line and a castling line** (don't rely on the committed fixtures containing them — write the synthetic cases unconditionally), unparseable-SAN defensive path, `mover_moves_in_line` for lines of length 1–7.
- `test_api.py`: full line flow — attempt index 0 correct → reply returned, mid-line response contains no future moves; index 1 wrong → `line_complete` + full line; give-up mid-line; `move_index` past end → 422; alternate checkmate mid-line → correct + complete. Single-move requests unchanged (regression).

### 2.2 Time-period selection + fetching beyond 20 games (DESIGN §13.2)

**Concept:** UI picker `[Last 20] [Day] [Week] [Month] [Year] [All time]` → `since`/`until` epoch-ms on the Lichess export. Capped `max` per period. The DB accumulates each player's puzzle pool over time.

#### The hard part: cache coverage, not TTL

Current sync (`_sync_player_games` in `app/routers/puzzles.py`) only ever fetches **forward** (`since = newest stored game`). A period request like "Year" needs games **older** than anything stored. So the cache model becomes a *coverage window* per player:

**Migration — `players` gains one column:**
```python
history_fetched_until: Mapped[datetime | None]   # oldest point in time we have fully fetched back to
```
(Naive UTC like the rest — see the SQLite tzinfo comment on `_utcnow` in `app/routers/puzzles.py`.)

**Sync algorithm** (replaces the body of `_sync_player_games`; two independent directions):
1. **Forward fill (existing logic, kept):** if TTL stale → fetch `since = newest stored game.played_at`, no `until`, `max = max_games_mvp`. Updates `last_fetched_at`.
2. **Backward fill (new):** if requested period start `< history_fetched_until` (or it's `None`, meaning only the initial 20-game window exists) → fetch `since = period_start_ms`, `until = oldest stored game.played_at`, `max = period cap`. On completion set `history_fetched_until = period_start` (or epoch 0 for "All time" **only if** the stream ended before hitting the cap — if the cap was hit, set it to the oldest game actually received, so a later request honestly refetches the gap).
3. `lichess_id` uniqueness makes overlap harmless (existing dedup check in `_sync_player_games`).

**Caps (config constants, `app/config.py`):**
```python
max_games_period_short: int = 300   # day / week / month
max_games_period_long: int = 500    # year / all
```

**Lichess client (`app/lichess.py`):** add `until: int | None = None` param → `params["until"]`. One line each.

**Endpoint (`app/routers/puzzles.py`):**
- New query param `period: Literal["last20", "day", "week", "month", "year", "all"] = "last20"`.
- Map to `period_start` (`_utcnow() - timedelta(...)`; `all` → `None`/epoch; `last20` → current behavior exactly, no backward fill).
- Puzzle query filters `Game.played_at >= period_start` when set.
- `limit` ceiling stays 200; with big pools the shuffle-then-slice already handles selection.

**Timeout/UX reality check:** Lichess streams ~20 games/s with evals; 300–500 games ≈ 15–30 s inside one request. Per DESIGN §13.2, "simplest wins": keep it inline (no job queue in Phase 2 — that arrives in Phase 3 anyway), but
- raise the httpx read timeout for period fetches (60 s → pass a timeout override into `fetch_games`),
- frontend shows an indeterminate spinner with period-aware copy ("Scanning up to a year of games — this can take up to half a minute…"), then the final `games_scanned` count on arrival,
- **Decision: keep the endpoint's rate limit at 20/min for all periods.** A per-period split (10/min for long periods) isn't worth a second route or in-handler limiting — Lichess 429s already map to a clean 503, which is the real backstop. Revisit only if 429s actually show up in production.

**Frontend:** period segmented control on the search screen; pass `period` in the querystring; period-aware loading copy; show "from the last {period}" in the puzzle header context line.

#### Tests

- Sync: backward fill triggers when period predates coverage; does not trigger for `last20`; cap-hit sets `history_fetched_until` to oldest-received (the honesty rule above); overlap dedup.
- Client: `until` param serialization.
- API: period filters `played_at` correctly (insert fixture games with spread-out dates); `last20` behavior byte-identical to Phase 1 (regression).
- Migration: `uv run alembic upgrade head` from the Phase 1 schema on a fresh SQLite file.

### 2.3 Phase 2 quality follow-up (deferred from §6.2 filter 4)

If multi-move play makes trivial recapture-only puzzles noticeable, add the recapture filter in `find_blunder_plies`: skip when the solution move is a capture on the same square as the opponent's immediately preceding capture *and* the line is one move long. Behind a config flag, default off, tune with real accounts. Do not block Phase 2 completion on this.

---

## 3. Phase 3 — Self-hosted Stockfish

Two goals: (a) puzzles from games that were **never analyzed** on Lichess — the majority for most users; (b) **brilliant-move** detection. Build only after Phase 2 is deployed and working.

> **Implementation note (2026-07-13, steps 7–9 built on branch `phase-3-engine`):** §3.1 below predates DESIGN §14.1's cost amendments and the decisions made at build time; where they disagree, the implementation (and DESIGN §14.1's dated deltas) is authoritative. The deltas: `engine_movetime` 0.2 → **0.1 s** with a **0.4 s two-pass refinement** of only the flagged blunder plies; **multipv=1** in both passes (brilliance deferred — §3.2's multipv-2 data comes from re-analyzing candidate plies, pre-filtered from the new `games.analysis_json` column, which stores the merged engine output per engine-sourced game); engine lifecycle **lazy** (spawn on job pickup, quit after ~5 idle minutes) instead of alive for the app's lifetime; `Job` keyed by **`player_id`** with no `params` column; budget fuses **150 games/day global + 60/day per player**, enforced in the worker only, accounted via `games.analyzed_at`; `moves_san` stored for **all** new games, not just unanalyzed ones; and `analysed=False` applied **uniformly** to every fetch ("last 20" now means last 20 games *total* — accepted product change; mixing modes would break the §2.2 coverage invariants). Empty-state reasons: `no_analyzed_games` → `no_games`, plus `analysis_pending` when games exist but nothing is solvable yet. **Scheduling (2026-07-13, after live testing):** the worker is round-robin per *game* across pending jobs (lowest progress first, oldest id tie-break) rather than FIFO per job, so a second concurrent search shows progress within seconds instead of waiting behind the first job; jobs alternate game-by-game on the single shared engine.

### 3.1 Engine infrastructure (DESIGN §14.1)

**Dockerfile:** install Stockfish (`apt-get update && apt-get install -y --no-install-recommends stockfish && rm -rf /var/lib/apt/lists/*` on slim works; verify the Debian package version is ≥ SF15 — if too old, download an official static `stockfish-ubuntu-x86-64` binary instead). Re-verify the `docker run --network none` boot check still passes.

**Config (`app/config.py`), all marked `# TUNING`:**
```python
stockfish_path: str = "stockfish"
engine_movetime: float = 0.2       # seconds per position, §14.1 budget
engine_depth_cap: int = 18
engine_threads: int = 2
engine_hash_mb: int = 128
engine_multipv: int = 2
```

**Migration — two changes:**
```python
class Job(Base):                    # new table, §14.1
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(30), index=True)
    params: Mapped[str] = mapped_column(Text)          # JSON blob: period, max_games
    status: Mapped[str] = mapped_column(String(10), default="queued", index=True)
                                                       # queued|running|done|failed
    progress: Mapped[int] = mapped_column(default=0)   # games analyzed so far
    total: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]

# games table:
eval_source: Mapped[str] = mapped_column(String(10), default="lichess")  # lichess|stockfish, §14.3
```

**New module `app/engine.py`:**
- `chess.engine.SimpleEngine.popen_uci(settings.stockfish_path)` run via `asyncio.to_thread` (sync API in a thread executor, per §14.1), or python-chess's async `popen_uci` — pick the sync-in-executor form: simpler lifecycle, and the worker is single-flight anyway.
- `analyse_game(moves_san) -> list[AnalysisEntry]`: walk the game, `engine.analyse(board, Limit(time=movetime, depth=depth_cap), multipv=2)` per position, and emit entries **shaped exactly like Lichess's `analysis[i]` array** (`eval`/`mate`, plus `best` in UCI and `variation` in SAN built from the PV, and multipv-2 data stashed under a private key for brilliance detection). Shaping the output like the Lichess format is the key move: `find_blunder_plies`/`build_puzzle` in `app/analysis.py` then work on engine output **unchanged**.
- One engine process for the app's lifetime, `Threads`/`Hash` from config; quit it in lifespan shutdown.

**New module `app/worker.py`:**
- A single `asyncio.Task` started in the FastAPI lifespan (`app/main.py`): loop — pick oldest `queued` job → mark `running` → for each un-analyzed game: run `analyse_game`, run the existing extraction, insert puzzles, `progress += 1`, commit per game (so puzzles appear incrementally and a crash loses at most one game) → mark `done`/`failed` with the error string. No Celery, no Redis (§14.1).
- Use its own session factory (`async_sessionmaker` from `app/database.py`), never the request-scoped `get_db`.
- On startup, reset any stale `running` jobs to `queued` (container restarts mid-job).

**Fetch-flow changes (`app/routers/puzzles.py` / `app/lichess.py`):**
- `fetch_games` gains `analysed: bool = True` param; Phase 3 sync fetches with `analysed=False` (i.e., omit the filter — all games), `evals=true` still set so analyzed games keep coming with evals attached.
- `_sync_player_games`: games **with** `analysis` → existing inline path, `eval_source="lichess"` (§14.3 hierarchy: Lichess analysis is free and instant, always preferred). Games **without** → store the Game row with `raw_analysis_processed=False` (the column has waited for this since Phase 1) plus the raw moves — **which requires storing `moves` on Game** (add `moves_san: Mapped[str] = mapped_column(Text)` to the same migration; currently moves are discarded after extraction).
- If any unprocessed games exist after sync → create a Job (if none pending for that player) and include `job_id` in `PuzzleSetResponse`.

**New endpoints:**
```
GET /api/jobs/{id} → {"status": "running", "progress": 7, "total": 40}
```
(Job creation stays implicit in the puzzles fetch — no separate POST needed; keeps the no-accounts, one-search UX.) Rate limit it generously (it's a cheap DB read) but do rate limit it (polling).

**Frontend:** when the response carries a `job_id`, show already-available puzzles immediately and a passive banner "Analyzing 40 more games with Stockfish — 7/40 done. New puzzles will appear next search." Poll `GET /api/jobs/{id}` every ~3 s while on the search/loading screen; a [Refresh puzzles] button re-runs the search when the job completes. Don't block puzzle-solving on the job.

**Railway note (§14.1):** one engine process, `Threads=2`, `Hash=128` — this is a hobby box. A 40-game backlog ≈ 10–25 min; that's fine because serving is never blocked on it.

### 3.2 Brilliant-move detection (DESIGN §14.2)

Runs only on the Stockfish path (needs multipv-2, which Lichess evals don't carry). New function in `app/analysis.py`, constants in config marked `# TUNING`:

`find_brilliant_plies(analysis_with_multipv, moves_san, color)` — a mover's move at ply `i` is brilliant-candidate iff **all** hold:
1. Played move matches multipv-1's move, or evals within `brilliant_match_cp = 10` of it.
2. Uniqueness: multipv-1 vs multipv-2 gap ≥ `brilliant_gap_cp = 150`.
3. Non-obviousness (at least one):
   - **Sacrifice:** material balance (python-chess piece values: 1/3/3/5/9) after the move and after the line's next 2 plies is worse for the mover than before, yet eval holds or improves; or
   - not a capture, not a check, and the position is sharp (multipv spread high).
4. Drama filter: mover's win% before the move in [20, 90].

Store as `Puzzle(kind="brilliant")` — the column already exists; puzzle position is *before* the brilliant move, solution is the brilliant move itself (the played move — note `played_uci == solution_uci` for these, which is correct and slightly cute).

**API:** `kind: Literal["blunder", "brilliant", "both"] = "blunder"` query param on the puzzles endpoint; default keeps current behavior. `PuzzleSummary` gains `kind`.

**Frontend:** filter toggle `[Mistakes] [Brilliancies] [Both]`; brilliant puzzles get the alternate framing: "You found a brilliant move here on {date}. Can you find it again?" Expect them to be rare — that's the point (§14.2).

**Also unlockable now (DESIGN §16):** with multipv stored, relax single-solution strictness — accept any move within ~25 cp of best on Stockfish-sourced puzzles (`brilliant_alt_accept_cp = 25`, config). Lichess-sourced puzzles keep the strict rule + mate exception.

### 3.3 Phase 3 tests

- `app/engine.py`: needs Stockfish locally (`brew install stockfish` for dev) — mark engine tests with a `pytest.mark.engine` marker, skipped when the binary is absent, so CI/offline runs stay green. One smoke test: analyse a 10-move game, assert output shape matches the Lichess `analysis` schema and `find_blunder_plies` consumes it.
- Brilliance heuristic: **pure-function tests with hand-built multipv dicts** (no engine needed) — sacrifice detection, uniqueness gap, drama filter, each condition independently.
- Worker: enqueue a job against a monkeypatched `analyse_game` (instant fake), assert progress increments, puzzles land, `done` status; startup-reset of stale `running` jobs.
- API: `kind` filtering; `job_id` in response when unanalyzed games exist.

---

## 4. Cross-cutting concerns

- **Migrations:** three total (Phase 2: `players.history_fetched_until`; Phase 3: `jobs` table + `games.eval_source` + `games.moves_san` — one migration, which as built also carries `games.analysis_json` and `games.analyzed_at`, see §3 implementation note). Each tested against a fresh SQLite upgrade and deployed to Railway where `alembic upgrade head` runs on boot — zero manual prod steps, same as Phase 1.
- **API compatibility:** every change is additive with defaults preserving Phase 1 behavior (`move_index=0`, `period=last20`, `kind=blunder`). The one deliberate exception is stripping the full variation from *mid-line* attempt responses (§2.1 step 4), which no existing client sends.
- **Lichess etiquette (§16):** period fetches are bigger but still one-request-at-a-time, identified User-Agent, no parallelization, 429 → clean 503. The 300/500 caps exist precisely to stay polite. `LICHESS_TOKEN` already raises the shared ceiling.
- **`static/app.js` growth:** it's 311 lines now; Phase 2+3 roughly doubles it. Stay vanilla (locked stack), but split into ES modules (`search.js`, `puzzle.js`, `api.js`) served as-is — still no build step.
- **Keep the `PRESET_THRESHOLDS` constant in `static/app.js` in sync** with `Settings.thresholds` in `app/config.py` if any threshold is retuned during Phase 2 calibration (known duplication, documented in the code comment).

---

## 5. Build order

Work in this exact order; each step ends with something runnable and its tests passing before moving on. Phase 2 must be fully working and deployed before Phase 3 begins (DESIGN mandate).

### Phase 2
1. [x] **Variation helpers:** `variation_board` / `variation_move_uci` / `mover_moves_in_line` in `app/analysis.py` + tests against fixtures.
2. [x] **Attempt endpoint rework:** `move_index`, positional validation, `opponent_reply_uci`, `line_complete`, mid-line spoiler stripping, mate exception per step, rate-limit bump. API tests incl. single-move regression. *Implementation note (2026-07-09): an explicit `mode: single|line` field was added to `AttemptRequest` — `move_index` alone can't distinguish a Phase 1 single-move attempt from the first move of a line attempt, and this plan requires both an opponent reply on index-0 line attempts and unchanged single-move behavior.*
3. [x] **Frontend line mode:** mode toggle, guided line flow with auto-replies, full-line reveal animation, summary scoring = clean lines. Verified in a real (headless) browser against a real account.
4. [x] **Coverage-window sync:** migration (`history_fetched_until`), backward-fill logic, `until` in the Lichess client, period caps in config. Sync + client tests.
5. [x] **Period endpoint + UI:** `period` param, `played_at` filtering, period picker, loading copy, longer read timeout. API tests + `last20` regression.
6. [ ] **Deploy + calibrate:** Railway deploy, run Year/All against the §6.3 calibration accounts, sanity-check pool sizes and fetch times; tune caps if needed. *(Pending merge of `phase-2-depth`.)*

### Phase 3
7. [x] **Engine module + Dockerfile:** Stockfish in the image, `app/engine.py` producing Lichess-shaped analysis, `--network none` boot check, engine-marked tests. *(Done 2026-07-13; Debian trixie ships Stockfish 17.1 at `/usr/games/stockfish`, exposed via `STOCKFISH_PATH`.)*
8. [x] **Jobs + worker:** migration (jobs, `eval_source`, `moves_san`, plus `analysis_json`/`analyzed_at` per the implementation note above), `app/worker.py` in lifespan, stale-job reset, per-game commits. Worker tests with fake engine. *(Done 2026-07-13.)*
9. [x] **Full-pool fetch flow:** `analysed=False` fetching, unanalyzed games → stored + queued, job inlined in the response, `GET /api/jobs/{id}`, frontend progress banner + refresh. API tests. *(Done 2026-07-13; verified end-to-end locally — real fetch, 11-game engine job, banner lifecycle, fuse drill, offline Docker boot.)*
10. [ ] **Brilliance:** heuristic + config constants, `kind` filtering end-to-end, alternate-solution acceptance (≤25 cp) for Stockfish puzzles, UI toggle + framing. Pure-function tests.
11. [ ] **Deploy + tune:** Railway deploy (watch CPU), calibrate brilliance constants against real accounts until brilliancies are rare-but-real; update `DESIGN.md` Status.

**Definition of done, Phase 2:** a user can pick "Full line" + "Year", grind multi-move puzzles from a year of games, and repeat searches hit the accumulated cache instantly.
**Definition of done, Phase 3:** a username with *zero* Lichess-analyzed games still yields puzzles (after a visible analysis job), and a `[Brilliancies]` toggle surfaces rare `kind="brilliant"` puzzles with the "can you find it again?" framing.
