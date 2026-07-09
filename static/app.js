import { Chessground } from "https://cdn.jsdelivr.net/npm/chessground@9.2.1/+esm";
import { Chess, SQUARES } from "https://cdn.jsdelivr.net/npm/chess.js@1.4.0/+esm";

const app = document.getElementById("app");

const state = {
  username: "",
  preset: "auto",
  threshold: null,
  puzzles: [],
  index: 0,
  solvedFirstTry: 0,
  loading: false,
  error: null,
  cg: null,
};

function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

function goToSearch() {
  state.puzzles = [];
  state.error = null;
  renderSearch();
}

function renderFooter() {
  return el(`
    <footer class="app-footer">
      <a href="https://github.com/PapaPandroni/puzzle-rewind" target="_blank" rel="noopener" class="link-btn">About</a>
    </footer>
  `);
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail || detail;
    } catch {
      // ignore non-JSON error bodies
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

// --- State 1: Search --------------------------------------------------------

// Must stay in sync with app/config.py Settings.thresholds.
const PRESET_THRESHOLDS = { beginner: 25, intermediate: 22, advanced: 20, expert: 18 };

function renderSearch() {
  app.innerHTML = "";
  const presets = ["auto", "beginner", "intermediate", "advanced", "expert"];
  const wrap = el(`
    <div class="search-screen">
      <h1>Puzzle Rewind</h1>
      <p class="tagline">Turn your own Lichess games into an endless puzzle stream.</p>
      <form id="search-form" class="search-form">
        <input id="username-input" type="text" placeholder="Lichess username" autocomplete="off" value="${state.username}" />
        <div class="preset-row">
          ${presets
            .map((p) => {
              const label = p[0].toUpperCase() + p.slice(1);
              const suffix = p === "auto" ? "" : ` &middot; ${PRESET_THRESHOLDS[p]}%`;
              return `<button type="button" class="preset-btn${state.preset === p ? " active" : ""}" data-preset="${p}">${label}${suffix}</button>`;
            })
            .join("")}
        </div>
        <div class="threshold-section">
          <p class="hint">Auto matches difficulty to your rating in each game; the other presets use one fixed threshold.</p>
          <label class="slider-label">
            Win% drop threshold: <span id="threshold-value">${state.preset === "auto" ? "Auto" : state.threshold ?? 25}</span>
            <input id="threshold-slider" type="range" min="10" max="40" step="1" value="${state.threshold ?? 25}" ${
              state.preset === "auto" ? "disabled" : ""
            } />
          </label>
          <p class="hint">How big a mistake counts as a puzzle: lower = more, subtler puzzles.</p>
        </div>
        <button type="submit" class="search-btn">Find puzzles</button>
      </form>
      ${state.loading ? `<p class="status">Fetching games from Lichess&hellip; this can take a few seconds.</p>` : ""}
      ${state.error ? `<p class="error">${state.error}</p>` : ""}
    </div>
  `);
  app.appendChild(wrap);
  app.appendChild(renderFooter());

  wrap.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.preset = btn.dataset.preset;
      state.threshold = state.preset === "auto" ? null : PRESET_THRESHOLDS[state.preset];
      renderSearch();
    });
  });

  const slider = wrap.querySelector("#threshold-slider");
  slider.addEventListener("input", () => {
    state.preset = "custom";
    state.threshold = Number(slider.value);
    wrap.querySelector("#threshold-value").textContent = slider.value;
    wrap.querySelectorAll(".preset-btn").forEach((b) => b.classList.remove("active"));
  });

  wrap.querySelector("#search-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const username = wrap.querySelector("#username-input").value.trim();
    if (!username) return;
    state.username = username;
    await search(username);
  });
}

async function search(username) {
  state.loading = true;
  state.error = null;
  renderSearch();
  try {
    const params = new URLSearchParams({ preset: state.preset, limit: "50" });
    if (state.threshold != null) params.set("threshold", String(state.threshold));
    const data = await api(`/api/players/${encodeURIComponent(username)}/puzzles?${params}`);
    state.loading = false;
    if (data.puzzles.length === 0) {
      state.error =
        data.reason === "no_analyzed_games"
          ? "No analyzed games found for this player yet. Puzzles come from games Lichess has computer-analyzed — analyze some games on Lichess first, then come back."
          : "No puzzles found for this player at the current difficulty. Try a lower threshold.";
      renderSearch();
      return;
    }
    state.puzzles = data.puzzles;
    state.index = 0;
    state.solvedFirstTry = 0;
    renderPuzzle();
  } catch (err) {
    state.loading = false;
    if (err.status === 404) {
      state.error = `Lichess user "${username}" not found.`;
    } else if (err.status === 503) {
      state.error = "Lichess is rate-limiting us — wait a minute and try again.";
    } else if (err.status === 422) {
      state.error = "That doesn't look like a valid Lichess username.";
    } else {
      state.error = "Something went wrong fetching puzzles. Please try again.";
    }
    renderSearch();
  }
}

// --- State 2: Puzzle ---------------------------------------------------------

function toDests(chess) {
  const dests = new Map();
  for (const s of SQUARES) {
    const moves = chess.moves({ square: s, verbose: true });
    if (moves.length) dests.set(s, moves.map((m) => m.to));
  }
  return dests;
}

function renderPuzzle() {
  app.innerHTML = "";
  const puzzle = state.puzzles[state.index];
  const chessInstance = new Chess(puzzle.fen);
  const date = new Date(puzzle.played_at).toLocaleDateString();

  const wrap = el(`
    <div class="puzzle-screen">
      <div class="puzzle-topbar">
        <button id="new-search-btn" class="link-btn">&lsaquo; New search</button>
      </div>
      <div class="puzzle-header">
        <div class="matchup"><strong>${state.username}</strong> vs ${puzzle.opponent_name} (${puzzle.opponent_rating}) &middot; ${puzzle.speed} &middot; ${date}</div>
        <div class="side-badge">${puzzle.side_to_move === "white" ? "White" : "Black"} to move</div>
      </div>
      <div class="board-wrap"><div id="board" class="cg-board"></div></div>
      <div id="feedback" class="feedback"></div>
      <div class="puzzle-actions">
        <button id="give-up-btn" class="link-btn">Give up / show solution</button>
      </div>
      <div class="puzzle-counter">Puzzle ${state.index + 1} of ${state.puzzles.length}</div>
      <p class="thanks-note">Thank you <a href="https://lichess.org" target="_blank" rel="noopener">Lichess</a> for being open source and awesome.</p>
    </div>
  `);
  app.appendChild(wrap);
  app.appendChild(renderFooter());

  wrap.querySelector("#new-search-btn").addEventListener("click", goToSearch);

  const boardEl = wrap.querySelector("#board");
  state.cg = Chessground(boardEl, {
    fen: puzzle.fen,
    orientation: puzzle.side_to_move,
    turnColor: puzzle.side_to_move,
    movable: {
      free: false,
      color: puzzle.side_to_move,
      dests: toDests(chessInstance),
      events: {
        after: (orig, dest) => onUserMove(chessInstance, orig, dest),
      },
    },
  });

  wrap.querySelector("#give-up-btn").addEventListener("click", () => submitAttempt(null));
}

async function onUserMove(chessInstance, orig, dest) {
  state.cg.set({ movable: { color: undefined } }); // lock the board while we check the move
  const piece = chessInstance.get(orig);
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
      body: JSON.stringify({ move_uci: moveUci }),
    });
  } catch (err) {
    document.getElementById("feedback").innerHTML =
      `<p class="result-incorrect">Couldn't check that move: ${err.message}</p>`;
    return;
  }
  if (result.correct) state.solvedFirstTry++;
  showResult(result, moveUci);
}

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

// --- State 3: Summary --------------------------------------------------------

function renderSummary() {
  app.innerHTML = "";
  const wrap = el(`
    <div class="summary-screen">
      <h2>Session complete</h2>
      <p>You solved ${state.solvedFirstTry}/${state.puzzles.length} on the first try.</p>
      <button id="search-again-btn" class="primary-btn">Search another player</button>
    </div>
  `);
  app.appendChild(wrap);
  app.appendChild(renderFooter());
  wrap.querySelector("#search-again-btn").addEventListener("click", goToSearch);
}

renderSearch();
