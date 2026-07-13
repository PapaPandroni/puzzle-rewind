// Engine-analysis job banner (§14.1): a passive progress strip shown on both
// the search and puzzle screens while a background Stockfish job exists.
// Polls GET /api/jobs/{id} every ~3s and updates the banner IN PLACE — never a
// full re-render, so it can't disturb the board mid-puzzle. New puzzles arrive
// only via the explicit [Refresh puzzles] click, never automatically.
import { api } from "./api.js";
import { el, state } from "./app.js";
// Module cycle with search.js is safe: both references are only invoked at
// event time (clicks/ticks), never during module evaluation.
import { runSearch } from "./search.js";

const POLL_MS = 3000;

let pollTimer = null;

function isTerminal(job) {
  return job.status === "done" || job.status === "failed";
}

function bannerHtml(job) {
  const games = (n) => `${n} game${n === 1 ? "" : "s"}`;
  const refreshBtn = `<button id="refresh-puzzles-btn" type="button" class="link-btn">Refresh puzzles</button>`;
  switch (job.status) {
    case "queued":
      return `<p>Waiting for the engine &mdash; ${games(job.total)} to analyze&hellip;</p>`;
    case "running":
      return `<p>Analyzing your games with Stockfish &mdash; ${job.progress}/${job.total} done. Keep solving meanwhile.</p>`;
    case "done":
      return `<p>Analysis complete &mdash; ${games(job.total)} analyzed.</p>${refreshBtn}`;
    case "failed":
      if (job.error === "daily_budget_reached") {
        return `<p>Daily engine budget reached after ${job.progress}/${job.total} games &mdash; search again tomorrow for the rest.</p>${job.progress > 0 ? refreshBtn : ""}`;
      }
      if (job.error === "player_budget_reached") {
        return `<p>This player's daily analysis budget is used up after ${job.progress}/${job.total} games &mdash; the rest continues tomorrow.</p>${job.progress > 0 ? refreshBtn : ""}`;
      }
      return `<p>Engine analysis hit an error (${job.progress}/${job.total} done). Puzzles from analyzed games are unaffected.</p>${job.progress > 0 ? refreshBtn : ""}`;
  }
  return "";
}

function fillBanner(node) {
  node.innerHTML = bannerHtml(state.job);
  const btn = node.querySelector("#refresh-puzzles-btn");
  if (btn) {
    btn.addEventListener("click", () => {
      runSearch(state.username);
    });
  }
}

/** Banner element for the current state.job, or null when no job exists. */
export function jobBannerEl() {
  if (!state.job) return null;
  const node = el(`<div id="job-banner" class="job-banner"></div>`);
  fillBanner(node);
  return node;
}

export function startJobPolling() {
  if (pollTimer !== null) return; // idempotent across re-renders
  if (!state.job || isTerminal(state.job)) return;
  pollTimer = setInterval(pollOnce, POLL_MS);
}

export function stopJobPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollOnce() {
  if (!state.job) {
    stopJobPolling();
    return;
  }
  let job;
  try {
    job = await api(`/api/jobs/${state.job.id}`);
  } catch {
    return; // network blip or 429: skip this tick, keep polling
  }
  state.job = job;
  const node = document.getElementById("job-banner");
  if (node) fillBanner(node);
  if (isTerminal(job)) stopJobPolling();
}
