"""Self-hosted Stockfish analysis emitting Lichess-shaped entries (DESIGN.md §14).

The output contract is `analysis[i]` exactly as app/analysis.py consumes it:
each entry carries the eval *after* half-move `i` ({"eval": cp} from White's
perspective, or {"mate": n} with positive = White mates) plus "best" (UCI) and
"variation" (space-separated SAN, no move numbers) computed from the position
*before* half-move `i`. Unlike Lichess we emit best/variation on every entry,
not just judged moves — the win-drop threshold does the real filtering.
"""

import asyncio
import time
from typing import Any

import chess
import chess.engine

from app.config import settings


class EngineHandle:
    """Lazily spawned Stockfish process (§14.1 cost amendment: lazy lifecycle).

    The sync SimpleEngine API runs via asyncio.to_thread one *position* at a
    time, so each blocking call is bounded by a single movetime and lifespan
    shutdown never waits on a whole game. The lock serializes spawn/analyse/
    quit — the worker is single-flight anyway, but the handle is safe by
    construction.
    """

    def __init__(self) -> None:
        self._engine: chess.engine.SimpleEngine | None = None
        self._last_used = 0.0
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._engine is not None

    async def analyse(self, board: chess.Board, movetime: float) -> chess.engine.InfoDict:
        async with self._lock:
            if self._engine is None:
                self._engine = await asyncio.to_thread(self._spawn)
            self._last_used = time.monotonic()
            limit = chess.engine.Limit(time=movetime, depth=settings.engine_depth_cap)
            return await asyncio.to_thread(self._engine.analyse, board, limit)

    @staticmethod
    def _spawn() -> chess.engine.SimpleEngine:
        engine = chess.engine.SimpleEngine.popen_uci(settings.stockfish_path)
        engine.configure(
            {"Threads": settings.engine_threads, "Hash": settings.engine_hash_mb}
        )
        return engine

    async def quit(self) -> None:
        async with self._lock:
            if self._engine is not None:
                engine, self._engine = self._engine, None
                await asyncio.to_thread(engine.quit)

    async def quit_if_idle(self, idle_seconds: float) -> None:
        if self._engine is not None and time.monotonic() - self._last_used >= idle_seconds:
            await self.quit()


engine_handle = EngineHandle()


def _score_entry(info: chess.engine.InfoDict) -> dict[str, Any]:
    if "score" not in info:
        # Shouldn't happen for a completed analyse(); route to the worker's
        # engine-failure path rather than silently storing a bogus eval.
        raise chess.engine.EngineError("engine returned no score")
    pov = info["score"].white()  # White POV — matches the Lichess sign convention
    if pov.is_mate():
        return {"mate": pov.mate()}
    return {"eval": pov.score()}


def _terminal_score(outcome: chess.Outcome) -> dict[str, Any]:
    # Terminal positions are never sent to the engine: it would answer "mate 0",
    # and win_percent_white treats mate <= 0 as 0% for White — scoring the
    # mating player's own winning move as a 100% win-drop "blunder".
    if outcome.winner is None:
        return {"eval": 0}
    return {"mate": 1} if outcome.winner == chess.WHITE else {"mate": -1}


def _replace_score(entry: dict[str, Any], score: dict[str, Any]) -> dict[str, Any]:
    merged = {k: v for k, v in entry.items() if k not in ("eval", "mate")}
    merged.update(score)
    return merged


def _pv_san(board: chess.Board, pv: list[chess.Move]) -> str:
    b = board.copy(stack=False)
    sans = []
    for move in pv[: settings.engine_variation_max_plies]:
        sans.append(b.san(move))
        b.push(move)
    return " ".join(sans)


async def analyse_game(
    moves_san: list[str],
    *,
    movetime: float | None = None,
    handle: EngineHandle = engine_handle,
) -> list[dict[str, Any]]:
    """Full-game sweep: one Lichess-shaped entry per half-move.

    Visits the N+1 positions of an N-move game; each engine result feeds two
    entries (§5.2 alignment): the eval goes to entry[i-1] (position after move
    i-1) and the PV goes to entry[i] (best/variation *before* move i). SAN
    parse failures propagate as ValueError — the worker skips such games.
    """
    if not moves_san:
        return []
    mt = movetime if movetime is not None else settings.engine_movetime
    n = len(moves_san)
    entries: list[dict[str, Any]] = [{} for _ in range(n)]
    board = chess.Board()

    for i in range(n + 1):
        outcome = board.outcome() if i == n else None  # only the final position can be terminal
        if outcome is not None:
            entries[n - 1].update(_terminal_score(outcome))
        else:
            info = await handle.analyse(board, mt)
            if i >= 1:
                entries[i - 1].update(_score_entry(info))
            if i < n:
                pv = info.get("pv") or []
                if pv:
                    entries[i]["best"] = pv[0].uci()
                    entries[i]["variation"] = _pv_san(board, pv)
        if i < n:
            board.push_san(moves_san[i])

    return entries


async def refine_plies(
    moves_san: list[str],
    entries: list[dict[str, Any]],
    plies: list[int],
    *,
    movetime: float | None = None,
    handle: EngineHandle = engine_handle,
) -> list[dict[str, Any]]:
    """Merged copy of `entries` with each flagged ply re-analyzed at a higher
    budget (the two-pass design, §14.1): the position before ply p refines
    entry[p].best/variation and entry[p-1]'s eval; the position after p refines
    entry[p]'s eval. Both evals feed the win-drop, so both matter. Never
    mutates the input.
    """
    mt = movetime if movetime is not None else settings.engine_refine_movetime
    wanted = set(plies)
    merged = [dict(e) for e in entries]
    board = chess.Board()

    for i, san in enumerate(moves_san):
        if i in wanted:
            info = await handle.analyse(board, mt)
            if i >= 1:
                merged[i - 1] = _replace_score(merged[i - 1], _score_entry(info))
            pv = info.get("pv") or []
            if pv:
                merged[i]["best"] = pv[0].uci()
                merged[i]["variation"] = _pv_san(board, pv)
        board.push_san(san)
        if i in wanted:
            # A flagged move can itself end the game (e.g. a stalemating
            # "blunder" from a winning position) — same terminal rule applies.
            outcome = board.outcome()
            score = (
                _terminal_score(outcome)
                if outcome is not None
                else _score_entry(await handle.analyse(board, mt))
            )
            merged[i] = _replace_score(merged[i], score)

    return merged
