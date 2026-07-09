import { api } from "./api.js";
import { appEl, el, renderFooter, state } from "./app.js";
import { renderPuzzle } from "./puzzle.js";

// Must stay in sync with app/config.py Settings.thresholds.
const PRESET_THRESHOLDS = { beginner: 25, intermediate: 22, advanced: 20, expert: 18 };

const PERIODS = [
  ["last20", "Last 20"],
  ["day", "Day"],
  ["week", "Week"],
  ["month", "Month"],
  ["year", "Year"],
  ["all", "All time"],
];

const LOADING_COPY = {
  last20: "Fetching games from Lichess&hellip; this can take a few seconds.",
  day: "Scanning the last day of games&hellip; this can take a few seconds.",
  week: "Scanning the last week of games&hellip; this can take a little while.",
  month: "Scanning the last month of games&hellip; this can take a little while.",
  year: "Scanning up to a year of games&hellip; this can take up to half a minute.",
  all: "Scanning their whole game history&hellip; this can take up to half a minute.",
};

export function goToSearch() {
  state.puzzles = [];
  state.error = null;
  state.notice = null;
  renderSearch();
}

export function renderSearch() {
  appEl.innerHTML = "";
  const presets = ["auto", "beginner", "intermediate", "advanced", "expert"];
  // The whole form freezes while a search is in flight — the only live control
  // is the explicit "Cancel search" button in the loading block below.
  const dis = state.loading ? "disabled" : "";
  const wrap = el(`
    <div class="search-screen">
      <h1>Puzzle Rewind</h1>
      <p class="tagline">Turn your own Lichess games into an endless puzzle stream.</p>
      <form id="search-form" class="search-form">
        <input id="username-input" type="text" placeholder="Lichess username" autocomplete="off" ${dis} />
        <div class="preset-row mode-row">
          <button type="button" ${dis} class="preset-btn${state.mode === "single" ? " active" : ""}" data-mode="single">Single move</button>
          <button type="button" ${dis} class="preset-btn${state.mode === "line" ? " active" : ""}" data-mode="line">Full line</button>
        </div>
        <p class="hint">Single move: find the one best move. Full line: follow the engine's refutation for up to 3 of your moves.</p>
        <div class="preset-row period-row">
          ${PERIODS.map(
            ([value, label]) =>
              `<button type="button" ${dis} class="preset-btn${state.period === value ? " active" : ""}" data-period="${value}">${label}</button>`
          ).join("")}
        </div>
        <p class="hint">How far back to mine games for puzzles. Results are cached, so repeat searches are instant.</p>
        <div class="preset-row">
          ${presets
            .map((p) => {
              const label = p[0].toUpperCase() + p.slice(1);
              const suffix = p === "auto" ? "" : ` &middot; ${PRESET_THRESHOLDS[p]}%`;
              return `<button type="button" ${dis} class="preset-btn${state.preset === p ? " active" : ""}" data-preset="${p}">${label}${suffix}</button>`;
            })
            .join("")}
        </div>
        <div class="threshold-section">
          <p class="hint">Auto matches difficulty to your rating in each game; the other presets use one fixed threshold.</p>
          <label class="slider-label">
            Win% drop threshold: <span id="threshold-value">${state.preset === "auto" ? "Auto" : state.threshold ?? 25}</span>
            <input id="threshold-slider" type="range" min="10" max="40" step="1" value="${state.threshold ?? 25}" ${
              state.preset === "auto" || state.loading ? "disabled" : ""
            } />
          </label>
          <p class="hint">How big a mistake counts as a puzzle: lower = more, subtler puzzles.</p>
        </div>
        <button type="submit" ${dis} class="search-btn">Find puzzles</button>
      </form>
      ${
        state.loading
          ? `<div class="loading-block">
              <p class="status">${LOADING_COPY[state.period]}</p>
              <div class="progress-bar"><div class="progress-fill"></div></div>
              <button id="cancel-search-btn" type="button" class="link-btn">Cancel search</button>
            </div>`
          : ""
      }
      ${state.notice ? `<p class="status">${state.notice}</p>` : ""}
      ${state.error ? `<p class="error">${state.error}</p>` : ""}
    </div>
  `);
  appEl.appendChild(wrap);
  appEl.appendChild(renderFooter());

  // Set the value via the DOM property (not template interpolation) so quotes
  // in the field can't break the attribute, and live-sync it into state so
  // option-button re-renders never lose what's been typed.
  const usernameInput = wrap.querySelector("#username-input");
  usernameInput.value = state.username;
  usernameInput.addEventListener("input", () => {
    state.username = usernameInput.value;
  });

  if (state.loading) {
    wrap.querySelector("#cancel-search-btn").addEventListener("click", () => {
      state.abort?.abort();
    });
  }

  wrap.querySelectorAll(".mode-row .preset-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.mode = btn.dataset.mode;
      renderSearch();
    });
  });

  wrap.querySelectorAll(".period-row .preset-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.period = btn.dataset.period;
      renderSearch();
    });
  });

  wrap.querySelectorAll(".preset-btn[data-preset]").forEach((btn) => {
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
    wrap.querySelectorAll(".preset-btn[data-preset]").forEach((b) => b.classList.remove("active"));
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
  if (state.loading) return; // one search at a time
  state.loading = true;
  state.error = null;
  state.notice = null;
  state.abort = new AbortController();
  renderSearch();
  try {
    const params = new URLSearchParams({ preset: state.preset, period: state.period, limit: "50" });
    if (state.threshold != null) params.set("threshold", String(state.threshold));
    const data = await api(`/api/players/${encodeURIComponent(username)}/puzzles?${params}`, {
      signal: state.abort.signal,
    });
    state.loading = false;
    if (data.puzzles.length === 0) {
      if (data.reason === "no_analyzed_games") {
        state.error =
          "No analyzed games found for this player yet. Puzzles come from games Lichess has computer-analyzed — analyze some games on Lichess first, then come back.";
      } else if (data.reason === "no_games_in_period") {
        state.error = "No analyzed games found in this period — try a longer one.";
      } else {
        state.error = "No puzzles found for this player at the current difficulty. Try a lower threshold.";
      }
      renderSearch();
      return;
    }
    state.puzzles = data.puzzles;
    state.gamesScanned = data.games_scanned;
    state.index = 0;
    state.solvedFirstTry = 0;
    renderPuzzle();
  } catch (err) {
    state.loading = false;
    if (err.name === "AbortError") {
      state.notice = "Search cancelled.";
    } else if (err.status === 404) {
      state.error = `Lichess user "${username}" not found.`;
    } else if (err.status === 503) {
      state.error = "Lichess is rate-limiting us — wait a minute and try again.";
    } else if (err.status === 422) {
      state.error = "That doesn't look like a valid Lichess username.";
    } else {
      state.error = "Something went wrong fetching puzzles. Please try again.";
    }
    renderSearch();
  } finally {
    state.abort = null;
  }
}
