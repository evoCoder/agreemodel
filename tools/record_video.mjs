// Автоматическая запись видео показа симулятора (прил. 10, п. 3).
// Запись без монтажа: все расчёты идут в реальном времени в браузере; титры поясняют шаги.
//
//   python tools/build_web.py && python win/serve.py --root dist/web --no-browser &
//   npm i --no-save playwright-core
//   node tools/record_video.mjs [адрес] [путь к браузеру] [каталог]
//   ffmpeg -i <каталог>/*.webm -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p video.mp4
import { chromium } from "playwright-core";
import fs from "fs";

const url = process.argv[2] || "http://127.0.0.1:8765/";
const exe = process.argv[3] || process.env.CHROME || "/usr/bin/google-chrome";
const dir = process.argv[4] || "video-out";
fs.mkdirSync(dir, { recursive: true });
const W = 1600, H = 900;

const browser = await chromium.launch({ executablePath: exe, args: ["--no-sandbox"] });
const ctx = await browser.newContext({ viewport: { width: W, height: H }, recordVideo: { dir, size: { width: W, height: H } } });
const page = await ctx.newPage();
const pause = (s) => page.waitForTimeout(s * 1000);

async function caption(text, sec = 4) {
  await page.evaluate((t) => {
    let el = document.getElementById("__cap");
    if (!el) {
      el = document.createElement("div");
      el.id = "__cap";
      el.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);max-width:1200px;" +
        "background:rgba(15,23,42,.88);color:#fff;font:600 22px/1.35 system-ui,sans-serif;padding:12px 22px;" +
        "border-radius:10px;z-index:9999;text-align:center;box-shadow:0 4px 20px rgba(0,0,0,.25)";
      document.body.appendChild(el);
    }
    el.style.display = t ? "block" : "none";
    el.textContent = t;
  }, text);
  if (sec) await pause(sec);
}
async function clock() {   // метка реального времени записи в углу — подтверждает отсутствие монтажа
  await page.evaluate(() => {
    const el = document.createElement("div");
    el.style.cssText = "position:fixed;right:10px;top:8px;font:12px ui-monospace,monospace;color:#64748b;z-index:9999;background:#fff8;padding:2px 6px";
    document.body.appendChild(el);
    const t0 = Date.now();
    setInterval(() => { const s = (Date.now() - t0) / 1000; el.textContent = `запись ${new Date().toLocaleString("ru-RU")} · ${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`; }, 250);
  });
}
const scrollTo = async (sel, block = "start") => { await page.$eval(sel, (e, b) => e.scrollIntoView({ behavior: "smooth", block: b }), block); await pause(1.2); };
const runAndWait = async () => {
  await page.click("#run");
  await page.waitForFunction(() => /^(готово|Ошибка)/.test(document.getElementById("ptext").textContent), null, { timeout: 600000 });
};

await page.goto(url);
await clock();
await caption("Симулятор «Согласие»: согласование информационной картины между БВС (лот 1, ФТ № 1). Открываем страницу — сервер и регистрация не нужны.", 0);
await page.waitForFunction(() => !document.getElementById("run").disabled, null, { timeout: 180000 });
await caption("Среда расчёта загружена: модель на Python выполняется прямо в браузере. Вверху — версия модели и коммит репозитория.", 5);

await caption("Сценарий С1: 8 БВС без ГНСС, разрыв связи 3 мин (две подгруппы 4 + 4), отказ U06 во время разрыва, присоединение нового U09.", 6);
await page.hover("#run");
await caption("Запускаем расчёт. Строка хода выполнения показывает модельное время.", 0);
await runAndWait();
await scrollTo("#metrics", "start");
await caption("Показатели ФТ № 1 против требований: квант обработки ≤ 0,5 с, восстановление согласованности после разрыва ≤ 1,5 с, совпадение картин всех бортов, потери записей.", 8);

await scrollTo("#map", "center");
await page.$eval("#time", (e) => { e.value = 0; e.dispatchEvent(new Event("input")); });
await caption("Карта: путь роя, объекты эталона (кружки) и картина борта U01 — верные объекты, дубликаты, ложные. Врезка — строй и подгруппы связи.", 0);
await page.click("#play");
await pause(9);
await caption("Во время разрыва подгруппы работают автономно (два цвета во врезке); после восстановления связи картины сходятся.", 8);
await page.waitForFunction(() => document.getElementById("play").textContent === "▶", null, { timeout: 60000 }).catch(() => {});
await scrollTo("#chart", "center");
await caption("Ход прогона: число подгрупп связи, моменты восстановления согласованности (◆ с временем), загрузка канала против его полосы.", 7);

await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
await pause(1);
await scrollTo("input[name=loss]", "center");
await caption("Изменяем исходные данные: потери сообщений в канале 5 % → 30 %.", 0);
await page.click("input[name=loss]", { clickCount: 3 });
await page.keyboard.type("30", { delay: 150 });
await page.keyboard.press("Tab");
await pause(2);
await caption("Запускаем снова. Сценарий помечен «исходные данные изменены».", 0);
await runAndWait();
await scrollTo("#metrics", "start");
await caption("Результат изменился: при потерях 30 % время согласования растёт — модель правдоподобно реагирует на условия канала; картины в конце совпадают, потерь записей нет.", 9);

await scrollTo("#dl_summary", "center");
await caption("Результаты выгружаются в тех же файлах, что журнал циклов разработки: summary.json, series.csv, scenario.json.", 6);
await caption("", 1);
const video = page.video();
await ctx.close();
await browser.close();
console.log(await video.path());
