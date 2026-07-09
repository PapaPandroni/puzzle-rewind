---
name: verify
description: Launch and drive Puzzle Rewind locally to verify changes end-to-end (API via curl/httpx, UI via headless Playwright Chromium).
---

# Verifying Puzzle Rewind

## Launch

```bash
rm -f dev.db && uv run alembic upgrade head       # fresh DB, run migrations
uv run uvicorn app.main:app --port 8123 &         # background it; /healthz -> {"status":"ok"}
```

Static frontend is served from `/` (check `/app.js`, `/puzzle.js` etc. return 200).

## Drive the API

- `GET /api/players/peremil/puzzles?preset=custom&threshold=10` — real Lichess fetch,
  ~2-4s cold. `peremil` is the maintainer's account and a safe live target; keep total
  upstream fetches low (Lichess etiquette: one at a time, prefer `period=week|month`
  over `year|all` locally).
- `POST /api/puzzles/{id}/attempt` is stateless — the same puzzle can be attempted
  repeatedly, so a give-up (`{"move_uci": null}`) reveals the solution/line for
  scripting correct attempts afterwards. Use python-chess to convert the revealed
  SAN line to UCI per position.
- Gotchas: our own per-IP limiter is 20/min on the puzzles list (bursting it poisons
  the next minute of requests); `games_scanned` can exceed `max=20` — Lichess
  over-delivers a few games (known, see DESIGN §6.3 "~20-24-game fetch").

## Drive the UI

Playwright Python via `uv run --with playwright --with httpx --with chess python <script>`
(Chromium is cached in `~/Library/Caches/ms-playwright`; no node available on this
machine). Chessground accepts click-click moves: click origin square then destination,
computing pixel coords from `#board`'s bounding box (8x8 grid; flip files/ranks when
`side_to_move == "black"`). Working example: scratchpad `drive_browser.py` pattern —
search screen selectors are `#username-input`, `.search-btn`, `button[data-mode=]`,
`button[data-period=]`, `button[data-preset=]`; puzzle screen: `.task-line`,
`#give-up-btn`, `#next-btn`, `.result-correct`, `.result-incorrect`, `.puzzle-counter`.
Intercept the search response with `page.expect_response(lambda r: "/puzzles?" in r.url
and r.status == 200)` to learn puzzle ids/FENs for scripting moves.
