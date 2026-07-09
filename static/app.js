// Entry point: shared state + small DOM utilities. Screen rendering lives in
// search.js / puzzle.js, network in api.js — plain ES modules, no build step.
import { renderSearch } from "./search.js";

export const appEl = document.getElementById("app");

export const state = {
  username: "",
  preset: "auto",
  threshold: null,
  mode: "single", // "single" | "line" (multi-move, §13.1)
  puzzles: [],
  index: 0,
  solvedFirstTry: 0, // in line mode: whole lines found without a miss
  lineIndex: 0, // line index (0, 2, 4) of the move currently being sought
  loading: false,
  error: null,
  cg: null,
};

export function el(html) {
  const t = document.createElement("template");
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

export function renderFooter() {
  return el(`
    <footer class="app-footer">
      <a href="https://github.com/PapaPandroni/puzzle-rewind" target="_blank" rel="noopener" class="link-btn">About</a>
    </footer>
  `);
}

renderSearch();
