import { Chessground } from "https://cdn.jsdelivr.net/npm/chessground@9.2.1/+esm";
import { Chess, SQUARES } from "https://cdn.jsdelivr.net/npm/chess.js@1.4.0/+esm";
import { api } from "./api.js";
import { appEl, el, renderFooter, state } from "./app.js";
import { goToSearch } from "./search.js";

// The true current position (puzzle FEN + confirmed line moves). Chessground is
// display-only; every position change flows through syncBoardFromPosition().
let position = null;
// Bumped on every renderPuzzle so animation timeouts from a previous puzzle
// (opponent replies, line reveals) can detect they're stale and abort.
let playToken = 0;

function toDests(chess) {
  const dests = new Map();
  for (const s of SQUARES) {
    const moves = chess.moves({ square: s, verbose: true });
    if (moves.length) dests.set(s, moves.map((m) => m.to));
  }
  return dests;
}

function movesToFind(puzzle) {
  // The solution move must always be found, even if no variation was stored.
  return Math.max(1, puzzle.mover_moves_in_line);
}

const PERIOD_CONTEXT = {
  day: "from the last day",
  week: "from the last week",
  month: "from the last month",
  year: "from the last year",
  all: "from all time",
};

export function renderPuzzle() {
  appEl.innerHTML = "";
  playToken++;
  const puzzle = state.puzzles[state.index];
  position = new Chess(puzzle.fen);
  state.lineIndex = 0;
  const date = new Date(puzzle.played_at).toLocaleDateString();
  const lineMode = state.mode === "line";
  const n = movesToFind(puzzle);
  const taskCopy = lineMode ? `Find the best line (${n} move${n === 1 ? "" : "s"})` : "Find the best move";

  const wrap = el(`
    <div class="puzzle-screen">
      <div class="puzzle-topbar">
        <button id="new-search-btn" class="link-btn">&lsaquo; New search</button>
      </div>
      <div class="puzzle-header">
        <div class="matchup"><strong>${state.username}</strong> vs ${puzzle.opponent_name} (${puzzle.opponent_rating}) &middot; ${puzzle.speed} &middot; ${date}</div>
        <div class="side-badge">${puzzle.side_to_move === "white" ? "White" : "Black"} to move</div>
      </div>
      <div class="task-line">${taskCopy}</div>
      <div class="board-wrap"><div id="board" class="cg-board"></div></div>
      <div id="feedback" class="feedback"></div>
      <div class="puzzle-actions">
        <button id="give-up-btn" class="link-btn">${lineMode ? "Give up / show the line" : "Give up / show solution"}</button>
      </div>
      <div class="puzzle-counter">Puzzle ${state.index + 1} of ${state.puzzles.length}${
        PERIOD_CONTEXT[state.period]
          ? ` &middot; ${PERIOD_CONTEXT[state.period]} (${state.gamesScanned} games)`
          : ""
      }</div>
      <p class="thanks-note">Thank you <a href="https://lichess.org" target="_blank" rel="noopener">Lichess</a> for being open source and awesome.</p>
    </div>
  `);
  appEl.appendChild(wrap);
  appEl.appendChild(renderFooter());

  wrap.querySelector("#new-search-btn").addEventListener("click", goToSearch);

  const boardEl = wrap.querySelector("#board");
  state.cg = Chessground(boardEl, {
    fen: puzzle.fen,
    orientation: puzzle.side_to_move,
    turnColor: puzzle.side_to_move,
    movable: {
      free: false,
      color: puzzle.side_to_move,
      dests: toDests(position),
      events: {
        after: (orig, dest) => onUserMove(orig, dest),
      },
    },
  });

  wrap.querySelector("#give-up-btn").addEventListener("click", () => submitAttempt(null));
}

function applyUci(uci) {
  position.move({ from: uci.slice(0, 2), to: uci.slice(2, 4), promotion: uci[4] });
}

function syncBoardFromPosition({ unlock }) {
  // cg.set() animates the diff to the new position, which also renders
  // castling rook hops and promotions correctly (a plain cg.move would not).
  const puzzle = state.puzzles[state.index];
  const last = position.history({ verbose: true }).at(-1);
  state.cg.set({
    fen: position.fen(),
    turnColor: position.turn() === "w" ? "white" : "black",
    ...(last ? { lastMove: [last.from, last.to] } : {}),
    movable: unlock
      ? { color: puzzle.side_to_move, dests: toDests(position) }
      : { color: undefined },
  });
}

function animateLine(sanMoves, delayMs = 400) {
  const token = playToken;
  const step = (i) => {
    if (token !== playToken || i >= sanMoves.length) return;
    try {
      position.move(sanMoves[i]);
    } catch {
      return; // defensive: stop rather than desync on a bad SAN
    }
    syncBoardFromPosition({ unlock: false });
    setTimeout(() => step(i + 1), delayMs);
  };
  step(0);
}

async function onUserMove(orig, dest) {
  state.cg.set({ movable: { color: undefined } }); // lock the board while we check the move
  const piece = position.get(orig);
  const isPromotion = piece && piece.type === "p" && (dest[1] === "8" || dest[1] === "1");
  const uci = orig + dest + (isPromotion ? "q" : "");
  await submitAttempt(uci);
}

async function submitAttempt(moveUci) {
  const puzzle = state.puzzles[state.index];
  let result;
  try {
    result = await api(`/api/puzzles/${puzzle.id}/attempt`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ move_uci: moveUci, mode: state.mode, move_index: state.lineIndex }),
    });
  } catch (err) {
    document.getElementById("feedback").innerHTML =
      `<p class="result-incorrect">Couldn't check that move: ${err.message}</p>`;
    return;
  }
  // One counter for both modes: a mid-line correct isn't "solved" yet
  // (line_complete false), and any miss ends the line unsolved.
  if (result.correct && result.line_complete) state.solvedFirstTry++;
  if (state.mode === "line") {
    showLineResult(result, moveUci);
  } else {
    showResult(result, moveUci);
  }
}

// --- single-move mode (Phase 1 behavior, unchanged) ---------------------------

function showResult(result, moveUci) {
  const puzzle = state.puzzles[state.index];
  const feedbackEl = document.getElementById("feedback");
  const boardWrap = document.querySelector(".board-wrap");

  if (result.correct) {
    boardWrap.classList.add("flash-correct");
    feedbackEl.innerHTML = `
      <p class="result-correct">That's the move! You played <strong>${result.played_san}</strong> in the game, dropping your winning chances by ${result.win_drop.toFixed(1)}%.</p>
      ${result.variation_san.length ? `<p class="variation">Line: ${result.variation_san.join(" ")}</p>` : ""}
    `;
  } else {
    boardWrap.classList.add("flash-incorrect");
    const revealSolution = () => {
      const solved = new Chess(puzzle.fen);
      solved.move({
        from: result.solution_uci.slice(0, 2),
        to: result.solution_uci.slice(2, 4),
        promotion: result.solution_uci[4],
      });
      state.cg.set({
        fen: solved.fen(),
        lastMove: [result.solution_uci.slice(0, 2), result.solution_uci.slice(2, 4)],
        movable: { color: undefined },
      });
    };
    if (moveUci) {
      setTimeout(revealSolution, 500); // let the shake/red-flash play first
    } else {
      revealSolution();
    }
    feedbackEl.innerHTML = `
      <p class="result-incorrect">Best was <strong>${result.solution_san}</strong>. In the game you played ${result.played_san}.</p>
      ${result.variation_san.length ? `<p class="variation">Line: ${result.variation_san.join(" ")}</p>` : ""}
      <p class="limitation-note">The answer is the engine's top choice — other equally good moves aren't accepted yet.</p>
    `;
  }

  showEndActions();
}

// --- full-line mode (§13.1) ----------------------------------------------------

function showLineResult(result, moveUci) {
  const puzzle = state.puzzles[state.index];
  const feedbackEl = document.getElementById("feedback");
  const boardWrap = document.querySelector(".board-wrap");

  if (result.correct && !result.line_complete) {
    // Move confirmed, line continues: play it, then auto-play the reply.
    // Give-up is paused meanwhile — it would post the not-yet-advanced index.
    applyUci(moveUci);
    syncBoardFromPosition({ unlock: false });
    const found = state.lineIndex / 2 + 1;
    feedbackEl.innerHTML = `<p class="result-correct">Found it — ${found} of ${movesToFind(puzzle)}. Keep going.</p>`;
    const giveUpBtn = document.getElementById("give-up-btn");
    if (giveUpBtn) giveUpBtn.disabled = true;
    const token = playToken;
    setTimeout(() => {
      if (token !== playToken) return;
      applyUci(result.opponent_reply_uci);
      state.lineIndex += 2;
      syncBoardFromPosition({ unlock: true });
      if (giveUpBtn) giveUpBtn.disabled = false;
    }, 300);
    return;
  }

  if (result.correct) {
    boardWrap.classList.add("flash-correct");
    applyUci(moveUci);
    syncBoardFromPosition({ unlock: false });
    const followedLine = moveUci === result.solution_uci;
    if (followedLine) {
      // Show how the line continues past the last move the user had to find.
      animateLine(result.variation_san.slice(state.lineIndex + 1));
      feedbackEl.innerHTML = `
        <p class="result-correct">That's the whole line! In the game you played <strong>${result.played_san}</strong>, dropping your winning chances by ${result.win_drop.toFixed(1)}%.</p>
        ${result.variation_san.length ? `<p class="variation">Line: ${result.variation_san.join(" ")}</p>` : ""}
      `;
    } else {
      // Divergent checkmate: the stored line no longer applies, the board is mate.
      feedbackEl.innerHTML = `
        <p class="result-correct">Checkmate! Not the engine's line, but a mate is never wrong.</p>
        ${result.variation_san.length ? `<p class="variation">Engine line was: ${result.variation_san.join(" ")}</p>` : ""}
      `;
    }
  } else {
    boardWrap.classList.add("flash-incorrect");
    const reveal = () => {
      const remaining = result.variation_san.slice(state.lineIndex);
      if (remaining.length) {
        syncBoardFromPosition({ unlock: false }); // clears any wrong move off the board
        animateLine(remaining);
      } else {
        applyUci(result.solution_uci);
        syncBoardFromPosition({ unlock: false });
      }
    };
    if (moveUci) {
      setTimeout(reveal, 500); // let the shake/red-flash play first
    } else {
      reveal();
    }
    feedbackEl.innerHTML = `
      <p class="result-incorrect">Best was <strong>${result.solution_san}</strong>. Watch the line play out.</p>
      ${result.variation_san.length ? `<p class="variation">Line: ${result.variation_san.join(" ")}</p>` : ""}
      <p class="limitation-note">The answer is the engine's top line — other equally good moves aren't accepted yet.</p>
    `;
  }

  showEndActions();
}

function showEndActions() {
  const puzzle = state.puzzles[state.index];
  document.querySelector(".puzzle-actions").innerHTML = `
    <button id="next-btn" class="primary-btn">Next puzzle</button>
    <a href="${puzzle.game_url}" target="_blank" rel="noopener" class="link-btn">View game on Lichess</a>
  `;
  document.getElementById("next-btn").addEventListener("click", nextPuzzle);
}

function nextPuzzle() {
  state.index++;
  if (state.index >= state.puzzles.length) {
    renderSummary();
  } else {
    renderPuzzle();
  }
}

// --- summary -------------------------------------------------------------------

function renderSummary() {
  appEl.innerHTML = "";
  const score = `${state.solvedFirstTry}/${state.puzzles.length}`;
  const wrap = el(`
    <div class="summary-screen">
      <h2>Session complete</h2>
      <p>${state.mode === "line" ? `You found ${score} full lines without a miss.` : `You solved ${score} on the first try.`}</p>
      <button id="search-again-btn" class="primary-btn">Search another player</button>
    </div>
  `);
  appEl.appendChild(wrap);
  appEl.appendChild(renderFooter());
  wrap.querySelector("#search-again-btn").addEventListener("click", goToSearch);
}
