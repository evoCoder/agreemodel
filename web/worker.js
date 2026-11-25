// Расчёт модели в фоновом потоке браузера: Pyodide + numpy (локальные файлы) + пакет agreemodel.
import { loadPyodide } from "./pyodide/pyodide.mjs?v=dev";

let py = null;

async function init() {
  const t0 = performance.now();
  py = await loadPyodide({ indexURL: new URL("./pyodide/", import.meta.url).href });
  await py.loadPackage("numpy", { messageCallback: () => {} });
  const zip = await (await fetch(new URL("./py/app.zip?v=dev", import.meta.url))).arrayBuffer();
  py.unpackArchive(zip, "zip", { extractDir: "/app" });
  py.runPython(`
import sys, json
sys.path.insert(0, "/app")
import soglasie_web
_base = json.dumps(soglasie_web.base_scenario("/app/scenarios/base.toml"), ensure_ascii=False)
`);
  const base = py.globals.get("_base");
  const pyver = py.runPython("import sys; sys.version.split()[0]");
  postMessage({ type: "ready", base, pyver, load_s: (performance.now() - t0) / 1000 });
}

async function run({ scenario, frame_s, view_uav }) {
  py.globals.set("_raw", JSON.stringify(scenario));
  py.globals.set("_post", (s) => postMessage(s));
  py.globals.set("_frame_s", frame_s);
  py.globals.set("_view", view_uav);
  const t0 = performance.now();
  const out = py.runPython("soglasie_web.run_json(_raw, _post, _frame_s, _view)");
  postMessage({ type: "result", data: out, browser_s: (performance.now() - t0) / 1000 });
}

onmessage = async (e) => {
  try {
    if (e.data.type === "init") await init();
    else if (e.data.type === "run") await run(e.data);
  } catch (err) {
    postMessage({ type: "error", text: String(err && err.message ? err.message : err) });
  }
};
