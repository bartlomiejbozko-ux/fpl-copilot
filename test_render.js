/* test_render.js — szybki test frontu bez przeglądarki.
 * Uruchom:  node test_render.js
 * Wyciąga skrypt z index.html, podstawia atrapę DOM + dane z data.json,
 * odpala każdą funkcję renderującą i sprawdza:
 *   (1) czy nie rzuca błędu wykonania (łapie bugi typu użycie niezdefiniowanej zmiennej),
 *   (2) czy w wygenerowanym HTML faktycznie SĄ piłkarze (łapie „renderuje się, ale pusto").
 */
const fs = require("fs");

function readScript() {
  const html = fs.readFileSync("index.html", "utf8");
  const m = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)];
  return m[m.length - 1][1]; // ostatni <script> = główna aplikacja
}
function readData() {
  if (fs.existsSync("data.json")) return JSON.parse(fs.readFileSync("data.json", "utf8"));
  // fallback: wyciągnij __PRELOAD__ z preview, jeśli brak data.json
  const p = fs.readFileSync("fpl-copilot-preview.html", "utf8");
  const i = p.indexOf("window.__PRELOAD__="), j = p.indexOf(";</script>", i);
  return JSON.parse(p.slice(i + "window.__PRELOAD__=".length, j));
}

function fakeEl() {
  return { innerHTML: "", style: {}, value: "", textContent: "", dataset: {},
    onclick: null, oninput: null, querySelectorAll: () => [], querySelector: () => fakeEl(),
    scrollIntoView: () => {}, appendChild: () => {},
    classList: { toggle: () => {}, add: () => {}, remove: () => {} } };
}

function main() {
  const js = readScript();
  const DATA = readData();
  const nodes = {};
  global.document = { getElementById: id => nodes[id] || (nodes[id] = fakeEl()),
    querySelectorAll: () => [], createElement: () => fakeEl() };
  global.el = id => document.getElementById(id);
  const store = {};
  global.localStorage = { getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => (store[k] = v), removeItem: k => delete store[k] };
  global.window = {};

  // usuń deklaracje stanu (ustawiane jako globalne poniżej) i wystaw funkcje na global
  let src = js
    .replace(/let DATA=null[^\n;]*;/, "")
    .replace(/let CMP=\[\][^\n;]*;/, "")
    .replace(/let BUILD=\{[^\n]*BUILD_SLOT=null;/, "")
    .replace(/^function\s+(\w+)/gm, "global.$1 = function $1");
  eval(src);

  global.DATA = DATA; global.CUR = "brief"; global.SEL = null; global.EDIT = false; global.BANK = 0;
  global.CMP = []; global.BUILD = { GK: [], DEF: [], MID: [], FWD: [] };
  global.BUILD_SLOT = null; global.BUILD_NEED = { GK: 2, DEF: 5, MID: 5, FWD: 3 };

  const names = (DATA.squad || []).map(p => p.name);
  const views = {
    renderBrief: true, renderSquad: true, renderFDR: true, renderRisk: true,
    renderRoadmap: true, renderLeague: false, renderAccuracy: false,
    renderPlanner: false, renderSim: false, renderCompare: false, renderBuilder: false,
  };
  let fail = 0;
  for (const [fn, needsPlayers] of Object.entries(views)) {
    nodes["app"] = fakeEl();
    try {
      global[fn]();
      const html = nodes["app"].innerHTML || "";
      const found = names.filter(n => html.includes(n)).length;
      if (needsPlayers && names.length && found === 0) {
        console.log(`✗ ${fn}: renderuje się, ale BRAK piłkarzy w HTML`); fail++;
      } else {
        console.log(`✓ ${fn}${needsPlayers ? ` (${found}/${names.length} zawodników)` : ""}`);
      }
    } catch (e) { console.log(`✗ ${fn}: ${e.message}`); fail++; }
  }
  console.log(fail ? `\n✗ ${fail} problemów` : "\n✓ wszystko OK");
  process.exit(fail ? 1 : 0);
}
main();
