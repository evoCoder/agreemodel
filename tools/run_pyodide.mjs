import fs from "fs";
// Прогон сценария в Pyodide (той же среде, что на странице) под Node — для сверки
// «браузер / нативно». Сначала собрать страницу: python tools/build_web.py (→ dist/web).
//   node tools/run_pyodide.mjs scenarios/base.toml [раздел.параметр=значение ...]
// Печатает summary.json без замеров процессорного времени.
const WEB = process.env.WEB_DIR || new URL("../dist/web/", import.meta.url).pathname;
const { loadPyodide } = await import(WEB + "pyodide/pyodide.mjs");
const py = await loadPyodide({ indexURL: WEB + "pyodide/" });
await py.loadPackage(WEB + "pyodide/" + fs.readdirSync(WEB + "pyodide").find(f => f.startsWith("numpy")));
const zip = fs.readFileSync(WEB + "py/app.zip");
py.unpackArchive(new Uint8Array(zip), "zip", { extractDir: "/app" });
py.FS.writeFile("/scen.toml", new Uint8Array(fs.readFileSync(process.argv[2])));
const out = py.runPython(`
import sys, json; sys.path.insert(0, "/app")
from agreemodel.config import load
from agreemodel.cli import _apply_override
from agreemodel.sim import run
sc = load("/scen.toml")
for a in ${JSON.stringify(process.argv.slice(3))}: _apply_override(sc, a)
s = run(sc).summary
json.dumps({k: v for k, v in s.items() if k not in ("picture_compute_ms", "wall_s")}, sort_keys=True)
`);
console.log(out);
