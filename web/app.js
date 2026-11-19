// Интерфейс симулятора «Согласие»: исходные данные → расчёт в фоновом потоке → показатели, карта, графики.
import { PRESETS } from "./presets.js";

const $ = (id) => document.getElementById(id);
const form = $("form");
const clone = (o) => JSON.parse(JSON.stringify(o));
const fmt = (x, d = 1) => (x === null || x === undefined ? "—" : Number(x).toFixed(d).replace(".", ","));
const uid = (i) => "U" + String(i + 1).padStart(2, "0");

let worker = null;
let base = null;      // базовый сценарий (scenarios/base.toml)
let sc = null;        // текущие исходные данные
let preset = null;
let result = null;
let frameIdx = 0;
let playTimer = null;
let runStart = 0;
let build = {};

const SPLITS = {
  "4+4": [[0, 1, 2, 3], [4, 5, 6, 7]],
  "3+5": [[0, 1, 2], [3, 4, 5, 6, 7]],
  "2+2+4": [[0, 1], [2, 3], [4, 5, 6, 7]],
};

// ------------------------------------------------------------------ среда расчёта
function startWorker() {
  worker = new Worker(new URL("./worker.js", import.meta.url), { type: "module" });
  worker.onmessage = onWorker;
  worker.postMessage({ type: "init" });
}

function onWorker(e) {
  let m = e.data;
  if (typeof m === "string") m = JSON.parse(m);
  if (m.type === "ready") {
    if (!base) {
      base = JSON.parse(m.base);
      choosePreset(PRESETS[0]);
    }
    build.pyver = m.pyver;
    $("ver").textContent = verLine() + ` · среда загружена за ${fmt(m.load_s)} с`;
    setBusy(false);
  } else if (m.type === "progress") {
    $("bar").style.width = m.pct + "%";
    $("ptext").textContent = `модельное время ${Math.round(m.t)} с · ${m.pct} % · прошло ${fmt((performance.now() - runStart) / 1000, 0)} с`;
  } else if (m.type === "result") {
    result = JSON.parse(m.data);
    result.browser_s = m.browser_s;
    showResult();
    setBusy(false);
  } else if (m.type === "warn") {
    console.warn(m.text);
  } else if (m.type === "error") {
    $("ptext").textContent = "Ошибка: " + m.text;
    setBusy(false);
  }
}

function setBusy(busy) {
  $("run").disabled = busy || !base;
  $("reset").disabled = busy || !base;
  $("stop").disabled = !busy;
  for (const el of form.elements) el.disabled = busy;
  for (const b of document.querySelectorAll("#presets button")) b.disabled = busy || b.dataset.off === "1";
  if (!busy && sc) fillForm();
}

function verLine() {
  const b = build;
  return `Версия модели ${b.version || "?"} · коммит ${b.commit || "?"}${b.dirty ? " (с изменениями)" : ""}` +
    ` · сборка ${b.built || "?"} · Pyodide ${b.pyodide || "?"}` + (b.pyver ? ` (Python ${b.pyver})` : "");
}

// ------------------------------------------------------------------ сценарии
function renderPresets() {
  const box = $("presets");
  box.innerHTML = "";
  for (const p of PRESETS) {
    const div = document.createElement("div");
    div.className = "preset" + (preset && preset.id === p.id ? " on" : "");
    div.innerHTML = `<button data-id="${p.id}" data-off="${p.disabled ? 1 : 0}" ${p.disabled ? "disabled" : ""}>${p.title}</button>` +
      `<p><b>Что задано:</b> ${p.what}</p><p><b>Ожидается:</b> ${p.expect}</p>`;
    div.querySelector("button").onclick = () => choosePreset(p);
    box.appendChild(div);
  }
}

function choosePreset(p) {
  preset = p;
  sc = p.apply(clone(base));
  renderPresets();
  buildAllParams();
  fillForm();
}

// ------------------------------------------------------------------ форма
function splitOf(s) {
  const parts = s.net.partition;
  if (!parts.length) return "none";
  if (parts.length > 1) return "custom";
  const g = JSON.stringify(parts[0].groups);
  for (const [k, v] of Object.entries(SPLITS)) if (JSON.stringify(v) === g) return k;
  return "custom";
}

function ev(kind) { return sc.event.find((e) => e.kind === kind); }

function fillForm(skip) {
  const f = form.elements;
  const set = (name, v) => { if (f[name] && f[name] !== skip) { if (f[name].type === "checkbox") f[name].checked = v; else f[name].value = v; } };
  const n = sc.swarm.n_uav;
  for (const [name, count] of [["fail_uav", n], ["view", n + 2]]) {
    const sel = f[name];
    if (sel.options.length !== count) {
      const cur = sel.value;
      sel.innerHTML = Array.from({ length: count }, (_, i) => `<option value="${i}">${uid(i)}</option>`).join("");
      if (cur) sel.value = cur;
    }
  }
  const split = splitOf(sc);
  set("split", split);
  const p0 = sc.net.partition[0];
  set("gap_start", p0 ? p0.start_s : "");
  set("gap_end", p0 ? p0.end_s : "");
  f.gap_start.disabled = f.gap_end.disabled = !p0;
  const gaps = sc.net.partition.map((p) => p.end_s - p.start_s);
  $("gap_hint").textContent = gaps.length
    ? "Разрыв" + (gaps.length > 1 ? "ы" : "") + ": " + gaps.map((g) => `${fmt(g / 60, 1)} мин`).join(", ") + (split === "custom" ? " (поля относятся к первому)" : "")
    : "";
  set("loss", Math.round(sc.net.loss * 100));
  set("latency", sc.net.latency_ms);
  set("bandwidth", sc.net.bandwidth_kbps);
  set("nav_sigma", sc.nav.sigma_m);
  set("objects", sc.objects.count);
  const fe = ev("fail"), je = ev("join");
  set("fail_on", !!fe); set("fail_uav", fe ? fe.uav : 5); set("fail_t", fe ? fe.t_s : Math.round(sc.run.duration_s / 2));
  set("join_on", !!je); set("join_t", je ? je.t_s : Math.round(sc.run.duration_s * 0.9));
  set("duration", sc.run.duration_s);
  set("seed", sc.run.seed);
  for (const el of $("all").querySelectorAll("input")) {
    if (el === skip) continue;
    const [sec, key] = el.name.split(".").slice(1);
    el.value = sc[sec][key];
  }
}

function num(el) {
  const v = Number(el.value);
  if (el.value === "" || !Number.isFinite(v)) throw new Error("неверное значение");
  return v;
}

function onInput(e) {
  const el = e.target, f = form.elements, name = el.name;
  try {
    if (name.startsWith("all.")) {
      const [sec, key] = name.split(".").slice(1);
      const cur = sc[sec][key];
      sc[sec][key] = typeof cur === "number" ? num(el) : (typeof cur === "boolean" ? el.value === "true" : el.value);
    } else if (name === "split") {
      if (el.value === "none") sc.net.partition = [];
      else {
        const p0 = sc.net.partition[0] || { start_s: 20, end_s: Math.max(30, sc.run.duration_s - 40) };
        sc.net.partition = [{ start_s: p0.start_s, end_s: p0.end_s, groups: SPLITS[el.value] }];
      }
    } else if (name === "gap_start" || name === "gap_end") {
      if (sc.net.partition[0]) sc.net.partition[0][name === "gap_start" ? "start_s" : "end_s"] = num(el);
    } else if (name === "loss") sc.net.loss = num(el) / 100;
    else if (name === "latency") sc.net.latency_ms = num(el);
    else if (name === "bandwidth") sc.net.bandwidth_kbps = num(el);
    else if (name === "nav_sigma") sc.nav.sigma_m = num(el);
    else if (name === "objects") sc.objects.count = num(el);
    else if (name === "duration") sc.run.duration_s = num(el);
    else if (name === "seed") sc.run.seed = num(el);
    else if (name.startsWith("fail") || name.startsWith("join")) {
      sc.event = sc.event.filter((x) => x.kind !== "fail" && x.kind !== "join");
      if (f.fail_on.checked) sc.event.push({ t_s: num(f.fail_t), kind: "fail", uav: Number(f.fail_uav.value) });
      if (f.join_on.checked) sc.event.push({ t_s: num(f.join_t), kind: "join", uav: sc.swarm.n_uav });
      sc.event.sort((a, b) => a.t_s - b.t_s);
    }
    el.classList.remove("bad");
    markChanged();
    fillForm(el);
  } catch (err) {
    el.classList.add("bad");
  }
}

function markChanged() {
  const pristine = JSON.stringify(preset.apply(clone(base))) === JSON.stringify(sc);
  for (const d of document.querySelectorAll(".preset.on")) d.classList.toggle("changed", !pristine);
}

function buildAllParams() {
  const box = $("all");
  if (box.childElementCount) return;
  for (const sec of ["run", "swarm", "objects", "sensor", "nav", "node", "fusion", "net"]) {
    const fs = document.createElement("fieldset");
    fs.innerHTML = `<legend>[${sec}]</legend>`;
    for (const [k, v] of Object.entries(base[sec])) {
      if (typeof v === "object") continue;
      const lab = document.createElement("label");
      lab.innerHTML = `${k} <input name="all.${sec}.${k}" type="${typeof v === "number" ? "number" : "text"}" step="any">`;
      fs.appendChild(lab);
    }
    box.appendChild(fs);
  }
}

function validate() {
  const d = sc.run.duration_s;
  for (const p of sc.net.partition) {
    if (!(p.start_s >= 0 && p.end_s > p.start_s)) throw new Error("разрыв: конец должен быть позже начала");
    if (p.end_s > d) throw new Error("разрыв заканчивается после конца прогона — увеличьте длительность");
  }
  for (const e of sc.event) if (e.t_s > d) throw new Error(`событие «${e.kind}» на ${e.t_s} с — позже конца прогона`);
}

// ------------------------------------------------------------------ запуск
function run() {
  try { validate(); } catch (err) { $("ptext").textContent = err.message; return; }
  result = null;
  stopPlay();
  $("bar").style.width = "0%";
  $("ptext").textContent = "расчёт…";
  runStart = performance.now();
  setBusy(true);
  const frame_s = sc.run.duration_s > 600 ? 2 : 1;
  worker.postMessage({ type: "run", scenario: sc, frame_s, view_uav: Number(form.elements.view.value) });
}

function stop() {
  worker.terminate();
  $("ptext").textContent = "остановлено; перезапуск среды…";
  $("bar").style.width = "0%";
  startWorker();
}

// ------------------------------------------------------------------ показатели
const ok = (b) => (b ? '<span class="pass">✓</span>' : '<span class="fail">✗</span>');

function restoreName(r) {
  if (r.kind === "partition_end") return `Восстановление согласованности после разрыва связи (${r.t_s} с)`;
  if (r.kind.startsWith("join")) return `Получение картины присоединившимся бортом ${r.kind.split(" ")[1] || ""} (${r.t_s} с)`;
  return `Восстановление согласованности: ${r.kind} (${r.t_s} с)`;
}

function showResult() {
  const s = result.summary, sce = result.scenario;
  const rows = [];
  rows.push(["Показатель", "Требование ФТ № 1", "Получено", ""]);
  rows.push(["Квант обработки и выдачи обновлённого состояния, наибольший", "≤ 0,5 с", `${fmt(s.quantum_s.max)} с (95 % — ${fmt(s.quantum_s.p95)} с)`, ok(s.quantum_s.max <= 0.5)]);
  const gaps = sce.net.partition.map((p) => p.end_s - p.start_s);
  rows.push(["Разрыв связи в прогоне, наибольший", "до 15 мин", gaps.length ? `${fmt(Math.max(...gaps) / 60)} мин` : "без разрыва", ""]);
  for (const r of s.restores)
    rows.push([restoreName(r), "≤ 1,5 с", r.store_conv_s === null ? "не достигнуто" : `${fmt(r.store_conv_s)} с`, ok(r.store_conv_s !== null && r.store_conv_s <= 1.5)]);
  rows.push(["Картины всех бортов в конце совпадают", "одинаковое состояние", s.final_pictures_identical ? `да, через ${fmt(s.settle_conv_s)} с` : "нет", ok(s.final_pictures_identical)]);
  const miss = Object.values(s.records_missing_per_uav).reduce((a, b) => a + b, 0);
  rows.push(["Потери записей при синхронизации", "0", `${miss} из ${s.records_union}`, ok(miss === 0)]);
  const p = s.picture;
  rows.push(["Логические дубликаты объектов", "минимизируются", `${p.duplicates} (${fmt(100 * p.duplicates / Math.max(1, p.objects), 0)} % объектов картины)`, ""]);
  rows.push(["Объекты картины / обнаруженные объекты эталона", "—", `${p.objects} / ${p.truth_detected}; ложных ${p.false_objects}, смешанных ${p.mixed_objects}`, ""]);
  rows.push(["Служебный трафик", "—", `${fmt(s.net.mbytes_sent, 2)} МБ, ${s.net.msgs_sent} сообщ., потеряно ${s.net.msgs_lost}`, ""]);
  rows.push(["Время расчёта в браузере", "—", `${fmt(result.browser_s)} с на ${sce.run.duration_s} с модельного времени`, ""]);
  $("metrics").innerHTML = rows.map((r, i) => `<tr>${r.map((c) => (i ? `<td>${c}</td>` : `<th>${c}</th>`)).join("")}</tr>`).join("");
  $("ptext").textContent = `готово за ${fmt(result.browser_s)} с · сценарий ${preset.title.split(".")[0]}${document.querySelector(".preset.changed") ? " (изменён)" : ""} · seed ${sce.run.seed}`;
  $("bar").style.width = "100%";
  for (const id of ["dl_summary", "dl_series", "dl_scenario", "play"]) $(id).disabled = false;
  const t = $("time");
  t.max = result.frames.length - 1; t.disabled = false; t.value = t.max;
  view = mapView();
  showFrame(result.frames.length - 1);
  drawChart();
}

// ------------------------------------------------------------------ карта
const GROUP_COLORS = ["#2563eb", "#d97706", "#7c3aed", "#059669", "#db2777", "#0891b2"];

let view = null;   // границы карты на весь прогон — вид не «прыгает» при просмотре

function mapView() {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  const add = (x, y) => { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); };
  for (const fr of result.frames) { add(fr.c[0], fr.c[1]); for (const [x, y] of fr.pic) add(x, y); }
  const pad = 120;
  x0 -= pad; x1 += pad; y0 -= pad; y1 += pad;
  const cv = $("map");
  const k = Math.min((cv.width - 20) / (x1 - x0), (cv.height - 50) / (y1 - y0));
  const v = { x0, y0, x1, y1, k, ox: (cv.width - (x1 - x0) * k) / 2, oy: (cv.height - 30 - (y1 - y0) * k) / 2 };
  // врезка строя — в угол, где меньше всего точек карты
  const iw = 230, ih = 150, W = cv.width, H = cv.height - 30;
  const corners = [[10, 10], [W - iw - 10, 10], [10, H - ih - 10], [W - iw - 10, H - ih - 10]];
  const pts = [];
  for (const fr of result.frames) { pts.push(fr.c); for (const q of fr.pic) pts.push(q); }
  let best = 0, bestN = Infinity;
  corners.forEach(([cx, cy], n) => {
    const cnt = pts.filter(([x, y]) => { const sx = v.ox + (x - x0) * k, sy = v.oy + (y1 - y) * k; return sx > cx - 10 && sx < cx + iw + 10 && sy > cy - 10 && sy < cy + ih + 10; }).length;
    if (cnt < bestN) { bestN = cnt; best = n; }
  });
  v.inset = corners[best];
  return v;
}

function showFrame(i) {
  frameIdx = i;
  const fr = result.frames[i];
  if (!fr) return;
  const cv = $("map"), g = cv.getContext("2d"), W = cv.width, H = cv.height, v = view;
  const X = (x) => v.ox + (x - v.x0) * v.k, Y = (y) => v.oy + (v.y1 - y) * v.k;
  g.clearRect(0, 0, W, H);
  g.fillStyle = "#f8fafc"; g.fillRect(0, 0, W, H);
  // путь роя и зона обзора датчиков
  g.strokeStyle = "#93c5fd"; g.lineWidth = 2; g.beginPath();
  for (let j = 0; j <= i; j++) { const c = result.frames[j].c; if (!j) g.moveTo(X(c[0]), Y(c[1])); else g.lineTo(X(c[0]), Y(c[1])); }
  g.stroke(); g.lineWidth = 1;
  g.strokeStyle = "#94a3b8"; g.setLineDash([4, 4]);
  g.beginPath(); g.arc(X(fr.c[0]), Y(fr.c[1]), result.scenario.sensor.fov_radius_m * v.k, 0, 2 * Math.PI); g.stroke();
  g.setLineDash([]);
  g.strokeStyle = "#64748b"; g.lineWidth = 1.2;
  for (const [x, y] of fr.truth) {
    if (x < v.x0 || x > v.x1 || y < v.y0 || y > v.y1) continue;
    g.beginPath(); g.arc(X(x), Y(y), 6, 0, 2 * Math.PI); g.stroke();
  }
  const col = { ok: "#16a34a", dup: "#ea580c", false: "#dc2626", mixed: "#9333ea" };
  for (const [x, y, st] of fr.pic) {
    const b = st.split("+")[0];
    g.fillStyle = col[b] || "#000";
    if (b === "false") { g.font = "bold 14px sans-serif"; g.fillText("×", X(x) - 4, Y(y) + 5); }
    else { g.beginPath(); g.arc(X(x), Y(y), 3.5, 0, 2 * Math.PI); g.fill(); }
    if (st.includes("conflict")) { g.strokeStyle = "#000"; g.beginPath(); g.arc(X(x), Y(y), 9, 0, 2 * Math.PI); g.stroke(); }
  }
  g.fillStyle = "#1d4ed8"; g.beginPath(); g.arc(X(fr.c[0]), Y(fr.c[1]), 5, 0, 2 * Math.PI); g.fill();
  // масштабная линейка
  const sb = [100, 200, 500, 1000, 2000, 5000].find((m) => m * v.k > 60) || 5000;
  g.strokeStyle = "#0f172a"; g.beginPath(); g.moveTo(W - 20 - sb * v.k, H - 12); g.lineTo(W - 20, H - 12); g.stroke();
  g.fillStyle = "#0f172a"; g.font = "11px sans-serif"; g.fillText(sb >= 1000 ? `${sb / 1000} км` : `${sb} м`, W - 20 - sb * v.k, H - 17);
  // строй и подгруппы связи — увеличенная врезка
  const [ix, iy] = v.inset, iw = 230, ih = 150, ik = 9;
  g.fillStyle = "rgba(255,255,255,.95)"; g.strokeStyle = "#cbd5e1"; g.fillRect(ix, iy, iw, ih); g.strokeRect(ix, iy, iw, ih);
  g.fillStyle = "#334155"; g.font = "12px sans-serif";
  g.fillText(`Строй (×${ik}); подгрупп связи: ${fr.groups}`, ix + 8, iy + 16);
  for (const [idx, x, y, grp] of fr.uavs) {
    const px = ix + iw / 2 + (x - fr.c[0]) * ik, py = iy + ih / 2 + 10 - (y - fr.c[1]) * ik;
    g.fillStyle = GROUP_COLORS[grp % GROUP_COLORS.length] || "#000";
    g.beginPath(); g.arc(px, py, idx === fr.view ? 7 : 5, 0, 2 * Math.PI); g.fill();
    g.fillStyle = "#0f172a"; g.font = "10px sans-serif"; g.fillText(uid(idx), px + 7, py + 3);
  }
  g.fillStyle = "#0f172a"; g.font = "13px sans-serif";
  g.fillText(`t = ${fmt(fr.t)} с · картина борта ${uid(fr.view)}: объектов ${fr.pic.length}`, 12, H - 12);
  $("tlabel").textContent = `${fmt(fr.t, 0)} с`;
  $("time").value = i;
}

function stopPlay() { if (playTimer) { clearInterval(playTimer); playTimer = null; $("play").textContent = "▶"; } }

function togglePlay() {
  if (playTimer) return stopPlay();
  if (frameIdx >= result.frames.length - 1) frameIdx = 0;
  $("play").textContent = "❚❚";
  playTimer = setInterval(() => {
    if (frameIdx >= result.frames.length - 1) return stopPlay();
    showFrame(frameIdx + 1);
  }, 1000 / 15);
}

// ------------------------------------------------------------------ график
function drawChart() {
  const cv = $("chart"), g = cv.getContext("2d"), W = cv.width, H = cv.height;
  const ser = result.series, sce = result.scenario;
  g.clearRect(0, 0, W, H);
  const L = 48, R = 48, T = 14, B = 26, pw = W - L - R, ph = (H - T - B - 16) / 2;
  const tmax = ser[ser.length - 1].t_s;
  const X = (t) => L + (t / tmax) * pw;
  // интервалы разрыва и события
  for (const p of sce.net.partition) { g.fillStyle = "rgba(234,88,12,.08)"; g.fillRect(X(p.start_s), T, X(p.end_s) - X(p.start_s), H - T - B); }
  g.font = "11px sans-serif";
  for (const e of sce.event) {
    g.strokeStyle = "#64748b"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(e.t_s), T); g.lineTo(X(e.t_s), H - B); g.stroke(); g.setLineDash([]);
    g.fillStyle = "#475569"; g.fillText(e.kind === "fail" ? `отказ ${uid(e.uav)}` : `новый ${uid(e.uav)}`, X(e.t_s) + 3, H - B - 4);
  }
  // панель 1: число подгрупп связи и моменты восстановления согласованности
  const y1 = (v, m) => T + 16 + (ph - 16) - (v / m) * (ph - 24);
  const m1 = Math.max(...ser.map((r) => r.components), 1);
  g.strokeStyle = "#2563eb"; g.lineWidth = 1.8; g.beginPath();
  ser.forEach((r, i) => { const x = X(r.t_s); if (!i) g.moveTo(x, y1(r.components, m1)); else { g.lineTo(x, y1(ser[i - 1].components, m1)); g.lineTo(x, y1(r.components, m1)); } });
  g.stroke(); g.lineWidth = 1;
  g.fillStyle = "#1e293b"; g.fillText("подгрупп связи; ◆ — хранилища согласованы после восстановления связи / присоединения", L, T + 8);
  for (let v = 1; v <= m1; v++) g.fillText(String(v), L - 14, y1(v, m1) + 4);
  for (const r of result.summary.restores) {
    if (r.store_conv_s === null) continue;
    const x = X(r.t_s + r.store_conv_s), y = y1(1, m1);
    g.fillStyle = r.store_conv_s <= 1.5 ? "#16a34a" : "#dc2626";
    g.beginPath(); g.moveTo(x, y - 6); g.lineTo(x + 5, y); g.lineTo(x, y + 6); g.lineTo(x - 5, y); g.fill();
    g.fillText(`${fmt(r.store_conv_s)} с`, x + 7, y - 6);
  }
  // панель 2: передача на борт, кбит/с
  const T2 = T + ph + 16;
  const rate = ser.map((r, i) => {
    if (!i) return 0;
    const dt = r.t_s - ser[i - 1].t_s;
    return dt > 0 ? ((r.kbytes_sent - ser[i - 1].kbytes_sent) * 8.192) / dt / sce.swarm.n_uav : 0;
  });
  const bw = sce.net.bandwidth_kbps, peak = Math.max(...rate);
  const m2 = Math.max(peak * 1.25, 1);
  const y2 = (v) => T2 + 16 + (ph - 16) - (v / m2) * (ph - 24);
  if (bw <= m2) { g.strokeStyle = "#dc2626"; g.setLineDash([6, 4]); g.beginPath(); g.moveTo(L, y2(bw)); g.lineTo(L + pw, y2(bw)); g.stroke(); g.setLineDash([]); }
  g.strokeStyle = "#0f766e"; g.lineWidth = 1.4; g.beginPath();
  rate.forEach((v, i) => { const x = X(ser[i].t_s); if (!i) g.moveTo(x, y2(v)); else g.lineTo(x, y2(v)); });
  g.stroke(); g.lineWidth = 1;
  g.fillStyle = "#1e293b";
  g.fillText(`передача на борт, кбит/с: пик ${fmt(peak, 0)} — ${fmt(100 * peak / bw, 0)} % полосы канала ${bw} кбит/с` + (bw <= m2 ? " (красная линия)" : ""), L, T2 + 8);
  g.fillText(String(Math.round(m2 / 1.25)), 4, y2(m2 / 1.25) + 4); g.fillText("0", L - 14, y2(0) + 4);
  // ось времени
  g.strokeStyle = "#94a3b8"; g.beginPath(); g.moveTo(L, H - B); g.lineTo(L + pw, H - B); g.stroke();
  const step = tmax > 600 ? 120 : tmax > 200 ? 30 : 10;
  for (let t = 0; t <= tmax; t += step) g.fillText(String(t), X(t) - 6, H - B + 14);
  g.fillText("t, с", L + pw + 8, H - B + 14);
}

// ------------------------------------------------------------------ выгрузка
function download(name, text, type) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function seriesCsv() {
  const ser = result.series, keys = Object.keys(ser[0]);
  return [keys.join(","), ...ser.map((r) => keys.map((k) => r[k]).join(","))].join("\n") + "\n";
}

// ------------------------------------------------------------------ начало
async function main() {
  try { build = await (await fetch("version.json", { cache: "no-store" })).json(); } catch { build = {}; }
  $("ver").textContent = verLine() + " · загрузка среды расчёта (≈ 15–20 МБ при первом открытии)…";
  $("foot").textContent = "Модель исполняется локально в браузере; данные никуда не передаются. " + verLine();
  form.addEventListener("change", onInput);
  form.addEventListener("submit", (e) => e.preventDefault());
  $("run").onclick = run;
  $("stop").onclick = stop;
  $("reset").onclick = () => choosePreset(preset);
  $("play").onclick = togglePlay;
  $("time").oninput = (e) => { stopPlay(); showFrame(Number(e.target.value)); };
  $("dl_summary").onclick = () => download("summary.json", JSON.stringify(result.summary, null, 2), "application/json");
  $("dl_series").onclick = () => download("series.csv", seriesCsv(), "text/csv");
  $("dl_scenario").onclick = () => download("scenario.json", JSON.stringify(result.scenario, null, 2), "application/json");
  setBusy(true);
  $("stop").disabled = true;
  startWorker();
}

main();
