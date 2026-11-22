// Проверка собранной страницы в браузере без интерфейса (Chrome / Chromium / Edge).
//
//   python tools/build_web.py
//   python -m http.server 8765 -d dist/web &
//   npm i --no-save playwright-core
//   node tests/web/e2e.mjs [адрес] [путь к браузеру] [список сценариев, напр. S1,S3]
//
// Проверяется: среда загружается; выбранные сценарии считаются; показатели выводятся;
// изменение исходных данных меняет результат; страница не обращается к внешним адресам.
// Итог — JSON в stdout и снимки экрана в каталоге e2e-out/.
import { chromium } from "playwright-core";
import fs from "fs";

const url = process.argv[2] || "http://127.0.0.1:8765/";
const exe = process.argv[3] || process.env.CHROME || "/usr/bin/google-chrome";
const only = (process.argv[4] || "S1,S3,S4,S5").split(",");
fs.mkdirSync("e2e-out", { recursive: true });

const browser = await chromium.launch({ executablePath: exe, args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1400, height: 1500 } });
const errors = [], hosts = new Set();
page.on("pageerror", (e) => errors.push(e.message));
page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
page.on("request", (r) => hosts.add(new URL(r.url()).host));

const done = () => page.waitForFunction(() => /^(готово|Ошибка)/.test(document.getElementById("ptext").textContent), null, { timeout: 600000 });
const metrics = () => page.$$eval("#metrics tr", (rs) => rs.slice(1).map((r) => [...r.cells].map((c) => c.innerText.trim())));

const report = { url, runs: [] };
const t0 = Date.now();
await page.goto(url);
await page.waitForFunction(() => !document.getElementById("run").disabled, null, { timeout: 180000 });
report.load_s = (Date.now() - t0) / 1000;
report.version = await page.textContent("#ver");

for (const id of only) {
  await page.click(`#presets button[data-id=${id}]`);
  const t1 = Date.now();
  await page.click("#run");
  await done();
  const rows = await metrics();
  report.runs.push({ preset: id, wall_s: (Date.now() - t1) / 1000, status: await page.textContent("#ptext"),
    failed: rows.filter((r) => r[3] === "✗").map((r) => r[0]), rows });
  await page.screenshot({ path: `e2e-out/${id}.png`, fullPage: true });
}

// член Комиссии меняет исходные данные: С1, потери 5 → 30 %
await page.click("#presets button[data-id=S1]");
await page.fill("input[name=loss]", "30");
await page.dispatchEvent("input[name=loss]", "change");
await page.click("#run");
await done();
report.changed = { status: await page.textContent("#ptext"), rows: await metrics() };

report.hosts = [...hosts];
report.errors = errors;
report.ok = errors.length === 0 && report.hosts.every((h) => h === new URL(url).host);
console.log(JSON.stringify(report, null, 1));
await browser.close();
process.exit(report.ok ? 0 : 1);
