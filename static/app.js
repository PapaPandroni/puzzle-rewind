// Entry point: shared state + small DOM utilities. Screen rendering lives in
// search.js / puzzle.js, network in api.js — plain ES modules, no build step.
import { renderSearch } from "./search.js";

export const appEl = document.getElementById("app");

export const state = {
  username: "",
  preset: "auto",
  threshold: null,
  mode: "single", // "single" | "line" (multi-move, §13.1)
  period: "last20", // last20 | day | week | month | year | all (§13.2)
  puzzles: [],
  gamesScanned: 0,
  gamesAnalyzed: 0, // subset of gamesScanned already analyzed (Lichess or engine)
  index: 0,
  solvedFirstTry: 0, // in line mode: whole lines found without a miss
  lineIndex: 0, // line index (0, 2, 4) of the move currently being sought
  loading: false,
  abort: null, // AbortController for the in-flight search, if any
  error: null,
  notice: null, // neutral (non-error) status line, e.g. "Search cancelled."
  job: null, // pending engine-analysis job from the last search response (§14.1)
  cg: null,
};

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

renderSearch();
