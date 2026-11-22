// Интерфейс симулятора «Согласие»: исходные данные → расчёт в фоновом потоке →
// результат (карточки), анимация «Что происходит», карта маршрута, показатели, детали для специалистов.
import { PRESETS } from "./presets.js";
import { applyTerms } from "./terms.js";

const $ = (id) => document.getElementById(id);
const form = $("form");
const clone = (o) => JSON.parse(JSON.stringify(o));
const fmt = (x, d = 1) => (x === null || x === undefined ? "—" : Number(x).toFixed(d).replace(".", ","));
const int = (x) => Math.round(x).toLocaleString("ru-RU");
const uid = (i) => "U" + String(i + 1).padStart(2, "0");

let worker = null;
let base = null;      // базовый сценарий (scenarios/base.toml)
let sc = null;        // текущие исходные данные
let preset = null;
let result = null;    // результат прогона + производные для анимации
let frameIdx = 0;
let playing = false;
let playT = 0;        // текущее модельное время воспроизведения
let lastTs = 0;
let runStart = 0;
let build = {};

const SPLITS = {
  "4+4": [[0, 1, 2, 3], [4, 5, 6, 7]],
  "3+5": [[0, 1, 2], [3, 4, 5, 6, 7]],
  "2+2+4": [[0, 1], [2, 3], [4, 5, 6, 7]],
};
const GROUP = ["#2563eb", "#ea580c", "#7c3aed", "#0d9488", "#db2777", "#65a30d", "#0891b2", "#a16207"];
const VARIANT = ["#2563eb", "#ea580c", "#7c3aed", "#0d9488", "#db2777", "#65a30d"];
const GREEN = "#16a34a", RED = "#dc2626", GRAY = "#94a3b8", INK = "#0f172a", MUTED = "#475569";

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
    if (!base) { base = JSON.parse(m.base); choosePreset(PRESETS[0]); }
    build.pyver = m.pyver;
    $("ver").textContent = verLine() + ` · среда загружена за ${fmt(m.load_s)} с`;
    setBusy(false);
  } else if (m.type === "progress") {
    $("bar").style.width = m.pct + "%";
    $("ptext").textContent = `расчёт: модельное время ${Math.round(m.t)} с · ${m.pct} % · прошло ${fmt((performance.now() - runStart) / 1000, 0)} с`;
  } else if (m.type === "result") {
    result = JSON.parse(m.data);
    result.browser_s = m.browser_s;
    prepare();
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

// ------------------------------------------------------------------ подсказки
function initTips() {
  const tip = $("tip");
  const show = (el) => {
    tip.textContent = el.dataset.tip;
    tip.style.display = "block";
    const r = el.getBoundingClientRect(), w = Math.min(340, window.innerWidth - 20);
    tip.style.maxWidth = w + "px";
    let x = Math.min(Math.max(10, r.left), window.innerWidth - w - 10);
    let y = r.bottom + 6;
    tip.style.left = x + "px"; tip.style.top = (y + window.scrollY) + "px";
    const th = tip.getBoundingClientRect().height;
    if (y + th > window.innerHeight - 10) tip.style.top = (r.top - th - 6 + window.scrollY) + "px";
  };
  const hide = () => { tip.style.display = "none"; };
  document.addEventListener("mouseover", (e) => { const el = e.target.closest("[data-tip]"); if (el) show(el); else hide(); });
  document.addEventListener("focusin", (e) => { const el = e.target.closest("[data-tip]"); if (el) show(el); });
  document.addEventListener("focusout", hide);
  window.addEventListener("scroll", hide, { passive: true });
}

// ------------------------------------------------------------------ сценарии
function renderPresets() {
  const box = $("presets");
  box.innerHTML = "";
  for (const p of PRESETS) {
    const div = document.createElement("div");
    div.className = "preset" + (preset && preset.id === p.id ? " on" : "") + (p.disabled ? " off" : "");
    div.innerHTML = `<button data-id="${p.id}" data-off="${p.disabled ? 1 : 0}" ${p.disabled ? "disabled" : ""}>${p.title}</button>` +
      `<div class="pbody"><p><b>Что задано.</b> ${p.what}</p><p><b>Что попробовать.</b> ${p.try}</p><p><b>Что должно получиться.</b> ${p.expect}</p></div>`;
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

const ev = (kind) => sc.event.find((e) => e.kind === kind);

function fillForm(skip) {
  const f = form.elements;
  const set = (name, v) => { if (f[name] && f[name] !== skip) { if (f[name].type === "checkbox") f[name].checked = v; else f[name].value = v; } };
  const n = sc.swarm.n_uav;
  for (const [name, count] of [["fail_uav", n], ["view", n + 1]]) {
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
  f.gap_start.disabled = f.gap_end.disabled = !p0 || f.split.disabled;
  const gaps = sc.net.partition.map((p) => p.end_s - p.start_s);
  $("gap_hint").textContent = gaps.length
    ? "Длительность разрыва: " + gaps.map((g) => `${fmt(g / 60, 1)} мин`).join(", ") + (split === "custom" ? " (поля относятся к первому)" : "")
    : "Все борты всё время на связи.";
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
    if (!(p.start_s >= 0 && p.end_s > p.start_s)) throw new Error("разрыв связи: конец должен быть позже начала");
    if (p.end_s > d) throw new Error("разрыв связи заканчивается после конца полёта — увеличьте длительность");
  }
  for (const e of sc.event) if (e.t_s > d) throw new Error(`${e.kind === "fail" ? "отказ" : "присоединение"} на ${e.t_s} с — позже конца полёта`);
}

// ------------------------------------------------------------------ запуск
function run() {
  try { validate(); } catch (err) { $("ptext").textContent = err.message; return; }
  result = null;
  stopPlay();
  renderCards(null);
  $("bar").style.width = "0%";
  $("ptext").textContent = "расчёт…";
  runStart = performance.now();
  setBusy(true);
  const frame_s = sc.run.duration_s > 600 ? 1 : 0.5;
  worker.postMessage({ type: "run", scenario: sc, frame_s, view_uav: Number(form.elements.view.value) });
}

function stop() {
  worker.terminate();
  $("ptext").textContent = "остановлено; перезапуск среды…";
  $("bar").style.width = "0%";
  startWorker();
}

// ------------------------------------------------------------------ подготовка анимации
function prepare() {
  const fr = result.frames, sce = result.scenario, s = result.summary;
  result.frame_s = fr.length > 2 ? fr[fr.length - 1].t - fr[fr.length - 2].t : 1;
  result.tmax = fr[fr.length - 1].t;
  // картины «расходятся», если различия держатся не менее 1 с: кратковременные различия
  // в момент появления нового объекта (обновление картины — каждые 0,25 с) не показываются
  const div = fr.map((f) => f.variants > 1);
  result.diverged = div.map((d, i) => {
    if (!d) return false;
    let a = i, b = i;
    while (a > 0 && div[a - 1]) a--;
    while (b < div.length - 1 && div[b + 1]) b++;
    return fr[b].t - fr[a].t + result.frame_s >= 1 - 1e-9;
  });
  result.maxRec = Math.max(1, ...fr.flatMap((f) => f.uavs.map((u) => u[6])));
  // строй — в собственной системе группы: курс вверх; отказавший борт — на месте в строю,
  // где он был в момент отказа
  const lastRel = {};
  let ext = 1;
  fr.forEach((f, i) => {
    const a = fr[Math.max(0, i - 4)].c, b = fr[Math.min(fr.length - 1, i + 4)].c;
    const h = (b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2 > 1 ? Math.atan2(b[1] - a[1], b[0] - a[0]) : (i ? fr[i - 1].heading : Math.PI / 2);
    f.heading = h;
    const rot = Math.PI / 2 - h, cs = Math.cos(rot), sn = Math.sin(rot);
    f.rel = {};
    for (const u of f.uavs) {
      let r;
      if (u[4]) {
        const dx = u[1] - f.c[0], dy = u[2] - f.c[1];
        r = [dx * cs - dy * sn, dx * sn + dy * cs];
        lastRel[u[0]] = r;
        ext = Math.max(ext, Math.abs(r[0]), Math.abs(r[1]));
      } else r = lastRel[u[0]] || [0, 0];
      f.rel[u[0]] = r;
    }
  });
  result.formExt = ext;
  // события для полосы времени
  const evs = [];
  for (const e of sce.event)
    if (e.kind === "fail") evs.push({ t: e.t_s, kind: "fail", text: `отказ борта ${uid(e.uav)}` });
  for (const r of s.restores) {
    const okr = r.store_conv_s !== null && r.store_conv_s <= 1.5;
    if (r.kind === "partition_end")
      evs.push({ t: r.t_s, kind: "restore", ok: okr, conv: r.store_conv_s,
        text: `связь восстановлена → согласованность восстановлена ${r.store_conv_s === null ? "— не достигнута" : "за " + fmt(r.store_conv_s) + " с"}` });
    else if (r.kind.startsWith("join"))
      evs.push({ t: r.t_s, kind: "join", ok: okr, conv: r.store_conv_s,
        text: `присоединение борта ${r.kind.split(" ")[1] || ""} → получил картину ${r.store_conv_s === null ? "— не получил" : "за " + fmt(r.store_conv_s) + " с"}` });
  }
  evs.sort((a, b) => a.t - b.t);
  result.events = evs;
  result.joinT = Object.fromEntries(sce.event.filter((e) => e.kind === "join").map((e) => [e.uav, e.t_s]));
  result.failT = Object.fromEntries(sce.event.filter((e) => e.kind === "fail").map((e) => [e.uav, e.t_s]));
  result.view = mapView();
}

// ------------------------------------------------------------------ результат
const okMark = (b) => (b === null ? "" : b ? '<span class="pass">✓</span>' : '<span class="fail">✗</span>');

function cardsData(s, sce) {
  const parts = s.restores.filter((r) => r.kind === "partition_end");
  const joins = s.restores.filter((r) => r.kind.startsWith("join"));
  const worst = (rs) => rs.reduce((m, r) => (r.store_conv_s === null || m === null ? null : Math.max(m, r.store_conv_s)), 0);
  const miss = Object.values(s.records_missing_per_uav).reduce((a, b) => a + b, 0);
  const cards = [];
  if (parts.length) {
    const w = worst(parts);
    cards.push({ term: "восстановление согласованности", title: "Восстановление согласованности после разрыва связи",
      value: w === null ? "не достигнуто" : `${fmt(w)} с`, req: "≤ 1,5 с", ok: w !== null && w <= 1.5,
      note: w === null ? "Картины бортов не сошлись." : `Через столько после возобновления связи картины всех бортов снова совпали${parts.length > 1 ? ` (наибольшее из ${parts.length} разрывов)` : ""}.` });
  } else cards.push({ term: "восстановление согласованности", title: "Восстановление согласованности после разрыва связи", value: "—", req: "≤ 1,5 с", ok: null, note: "В этом сценарии разрыва связи нет." });
  if (joins.length) {
    const w = worst(joins);
    cards.push({ term: "присоединение борта", title: "Получение картины присоединившимся бортом",
      value: w === null ? "не получил" : `${fmt(w)} с`, req: "≤ 1,5 с", ok: w !== null && w <= 1.5,
      note: "За столько новый борт получил всю картину, накопленную группой." });
  } else cards.push({ term: "присоединение борта", title: "Получение картины присоединившимся бортом", value: "—", req: "≤ 1,5 с", ok: null, note: "В этом сценарии присоединения нет." });
  cards.push({ term: "потери записей", title: "Потери записей при синхронизации", value: int(miss), req: "0", ok: miss === 0,
    note: miss === 0 ? `Все ${int(s.records_union)} записей дошли до всех работающих бортов.` : `Не дошли до части бортов из ${int(s.records_union)}.` });
  const d = s.picture.duplicates;
  cards.push({ term: "логический дубликат", title: "Логические дубликаты", value: int(d), req: "минимизируются", ok: d === 0 ? true : null,
    note: d === 0 ? `Каждый из ${s.picture.objects} объектов картины записан один раз.` : `Из ${s.picture.objects} объектов картины (${fmt(100 * d / Math.max(1, s.picture.objects), 0)} %).` });
  return cards;
}

function renderCards(s) {
  const box = $("cards");
  const data = s ? cardsData(s, result.scenario) : cardsData0();
  box.innerHTML = data.map((c) => `<div class="card ${c.ok === true ? "good" : c.ok === false ? "bad" : "neutral"}">
    <div class="ct"><span data-term="${c.term}">${c.title}</span></div>
    <div class="cv">${c.value} ${okMark(c.ok)}</div>
    <div class="cr">требование ФТ № 1: ${c.req}</div>
    <div class="cn">${c.note}</div></div>`).join("");
  applyTerms(box);
}

function cardsData0() {
  const t = (term, title, req) => ({ term, title, value: "—", req, ok: null, note: "Запустите сценарий." });
  return [t("восстановление согласованности", "Восстановление согласованности после разрыва связи", "≤ 1,5 с"),
    t("присоединение борта", "Получение картины присоединившимся бортом", "≤ 1,5 с"),
    t("потери записей", "Потери записей при синхронизации", "0"),
    t("логический дубликат", "Логические дубликаты", "минимизируются")];
}

function restoreName(r) {
  if (r.kind === "partition_end") return `<span data-term="восстановление согласованности">Восстановление согласованности</span> после разрыва связи (связь возобновилась на ${r.t_s} с)`;
  if (r.kind.startsWith("join")) return `Получение картины <span data-term="присоединение борта">присоединившимся бортом</span> ${r.kind.split(" ")[1] || ""} (${r.t_s} с)`;
  return `Восстановление согласованности: ${r.kind} (${r.t_s} с)`;
}

function showResult() {
  const s = result.summary, sce = result.scenario;
  renderCards(s);
  const rows = [["Показатель", "Требование ФТ № 1", "Получено", "", "Что это значит"]];
  rows.push(['<span data-term="квант обработки">Квант обработки</span> и выдачи обновлённого состояния', "≤ 0,5 с",
    `${fmt(s.quantum_s.max)} с`, okMark(s.quantum_s.max <= 0.5), "Как часто каждый борт обновляет свою картину (наибольший интервал за полёт)."]);
  const gaps = sce.net.partition.map((p) => p.end_s - p.start_s);
  rows.push(['Длительность <span data-term="разрыв связи">разрыва связи</span>', "до 15 мин",
    gaps.length ? `${fmt(Math.max(...gaps) / 60)} мин` : "разрыва нет", "", "Сколько подгруппы работали без связи друг с другом."]);
  for (const r of s.restores) {
    const ok = r.store_conv_s !== null && r.store_conv_s <= 1.5;
    rows.push([restoreName(r), "≤ 1,5 с", r.store_conv_s === null ? "не достигнуто" : `${fmt(r.store_conv_s)} с`, okMark(ok),
      r.kind === "partition_end" ? "От возобновления связи до момента, когда у всех бортов одни и те же сведения." : "От появления нового борта до момента, когда у него вся картина группы."]);
  }
  rows.push(['<span data-term="картина борта">Картины</span> всех бортов в конце совпадают', "одинаковое состояние",
    s.final_pictures_identical ? `да, через ${fmt(s.settle_conv_s)} с` : "нет", okMark(s.final_pictures_identical),
    "После остановки записи и продолжения обмена картины всех работающих бортов одинаковы."]);
  const miss = Object.values(s.records_missing_per_uav).reduce((a, b) => a + b, 0);
  rows.push(['<span data-term="потери записей">Потери записей</span>', "0", `${miss} из ${int(s.records_union)}`, okMark(miss === 0),
    "Сведения, которые есть хотя бы у одного борта, но не дошли до другого."]);
  const p = s.picture;
  rows.push(['<span data-term="логический дубликат">Логические дубликаты</span>', "минимизируются", `${p.duplicates}`, okMark(p.duplicates === 0 ? true : null),
    "Один реальный объект записан в картине дважды."]);
  rows.push(['Объекты картины / обнаруженные <span data-term="объект эталона">объекты эталона</span>', "—",
    `${p.objects} / ${p.truth_detected}; ложных ${p.false_objects}, смешанных ${p.mixed_objects}`, "",
    "Сколько объектов в картине и сколько реальных объектов борта увидели; ложный — реального объекта нет; смешанный — два реальных записаны как один."]);
  $("metrics").innerHTML = rows.map((r, i) => `<tr>${r.map((c) => (i ? `<td>${c}</td>` : `<th>${c}</th>`)).join("")}</tr>`).join("");
  applyTerms($("metrics"));

  const n = s.net || {};
  const tech = [
    ["Передача на борт (средняя)", n.kbps_per_uav !== undefined ? `${fmt(n.kbps_per_uav)} кбит/с из ${sce.net.bandwidth_kbps}` : "—"],
    ["Занятость радиоэфира", n.air_busy_pct !== undefined ? `${fmt(n.air_busy_pct)} %` : "—"],
    ["Служебный трафик", `${fmt(n.mbytes_sent, 2)} МБ; сообщений ${int(n.msgs_sent)}, потеряно ${int(n.msgs_lost)}`],
    ["Квант, 95 %", `${fmt(s.quantum_s.p95)} с`],
    ["Пересчёт картины, 95 % / наибольший", s.picture_compute_ms ? `${fmt(s.picture_compute_ms.p95, 2)} / ${fmt(s.picture_compute_ms.max, 2)} мс (замер в браузере)` : "—"],
    ["Задержка фиксации нового наблюдения", s.fix_latency_s ? `медиана ${fmt(s.fix_latency_s.median)} с, наибольшая ${fmt(s.fix_latency_s.max)} с` : "—"],
    ["От обнаружения до фиксации", s.detect_to_fix_s ? `медиана ${fmt(s.detect_to_fix_s.median)} с, наибольшее ${fmt(s.detect_to_fix_s.max)} с` : "—"],
    ["Хранилище борта, наибольшее", s.store_kbytes_max !== undefined ? `${fmt(s.store_kbytes_max)} КБ` : "—"],
    ["Время расчёта в браузере", `${fmt(result.browser_s)} с на ${sce.run.duration_s} с модельного времени`],
  ];
  $("tech").innerHTML = tech.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join("");

  $("ptext").textContent = `готово за ${fmt(result.browser_s)} с · ${preset.title.split(".")[0]}${document.querySelector(".preset.changed") ? " (исходные данные изменены)" : ""} · seed ${sce.run.seed}`;
  $("bar").style.width = "100%";
  for (const id of ["dl_summary", "dl_series", "dl_scenario", "play"]) $(id).disabled = false;
  const t = $("time");
  t.max = result.frames.length - 1; t.disabled = false;
  drawChart();
  playT = 0;
  showFrame(0);
  startPlay();  // воспроизведение запускается само
}

// ------------------------------------------------------------------ «Что происходит»
function wrapText(g, text, x, y, maxW, lh) {
  const words = text.split(" ");
  let line = "", yy = y;
  for (const w of words) {
    const test = line ? line + " " + w : w;
    if (g.measureText(test).width > maxW && line) { g.fillText(line, x, yy); line = w; yy += lh; }
    else line = test;
  }
  if (line) g.fillText(line, x, yy);
  return yy;
}

function drawStory(i, alpha = 0) {
  const fr = result.frames[i], sce = result.scenario;
  const nx = result.frames[Math.min(i + 1, result.frames.length - 1)];
  const tNow = fr.t + alpha * (nx.t - fr.t);
  const cv = $("story"), g = cv.getContext("2d"), W = cv.width, H = cv.height;
  g.clearRect(0, 0, W, H);
  g.fillStyle = "#fff"; g.fillRect(0, 0, W, H);

  // 1. полоса времени
  const L = 24, R = W - 24, by = 44, bh = 20, tmax = result.tmax;
  const X = (t) => L + (t / tmax) * (R - L);
  g.fillStyle = "#dcfce7"; g.fillRect(L, by, R - L, bh);
  g.fillStyle = "#166534"; g.font = "12px system-ui, sans-serif";
  for (const p of sce.net.partition) {
    g.fillStyle = "#fecaca"; g.fillRect(X(p.start_s), by, X(p.end_s) - X(p.start_s), bh);
  }
  // подписи сегментов
  let segs = [], t0 = 0;
  for (const p of [...sce.net.partition].sort((a, b) => a.start_s - b.start_s)) { segs.push([t0, p.start_s, null]); segs.push([p.start_s, p.end_s, p]); t0 = p.end_s; }
  segs.push([t0, tmax, null]);
  for (const [a, b, p] of segs) {
    const w = X(b) - X(a);
    const txt = p ? `разрыв связи: ${p.groups.length} подгруппы` : "связь есть";
    g.font = (p ? "bold " : "") + "12px system-ui, sans-serif";
    if (w > g.measureText(txt).width + 8) { g.fillStyle = p ? "#991b1b" : "#166534"; g.fillText(txt, X(a) + 6, by + 14); }
  }
  // события
  g.font = "12px system-ui, sans-serif";
  let lastRight = -1e9, row = 0;
  for (const e of result.events) {
    const x = X(e.t);
    const col = e.kind === "fail" ? "#475569" : e.ok ? GREEN : RED;
    g.fillStyle = col; g.strokeStyle = col;
    g.beginPath(); g.moveTo(x, by - 2); g.lineTo(x - 5, by - 10); g.lineTo(x + 5, by - 10); g.fill();
    g.beginPath(); g.moveTo(x, by); g.lineTo(x, by + bh); g.stroke();
    const tw = g.measureText(e.text).width;
    row = x - 4 < lastRight ? (row + 1) % 2 : 0;
    let tx = Math.min(Math.max(L, x - 6), R - tw);
    const ty = row ? by + bh + 30 : by + bh + 15;
    g.fillText(e.text, tx, ty);
    lastRight = Math.max(lastRight, tx + tw + 10);
  }
  g.fillStyle = MUTED; g.font = "11px system-ui, sans-serif";
  g.fillText("0 с", L, by - 14); g.fillText(`${Math.round(tmax)} с`, R - 30, by - 14);
  // бегунок
  const px = X(tNow);
  g.strokeStyle = INK; g.lineWidth = 2; g.beginPath(); g.moveTo(px, by - 16); g.lineTo(px, by + bh + 4); g.stroke(); g.lineWidth = 1;
  g.fillStyle = INK; g.font = "bold 13px system-ui, sans-serif";
  const lbl = `${fmt(tNow, 0)} с`; g.fillText(lbl, Math.min(Math.max(px - 14, L), R - 40), by - 20 + 0);

  const top = 112;
  g.strokeStyle = "#e2e8f0"; g.beginPath(); g.moveTo(0, top - 10); g.lineTo(W, top - 10); g.stroke();

  // 2. строй и радиосвязь
  const fw = 420, cx0 = fw / 2, cy0 = top + 40 + (H - top - 80) / 2;
  const k = Math.min((fw - 90) / 2, (H - top - 120) / 2) / result.formExt;
  g.fillStyle = INK; g.font = "bold 14px system-ui, sans-serif";
  g.fillText("Строй (вид вблизи) и радиосвязь", 16, top + 8);
  g.font = "12px system-ui, sans-serif"; g.fillStyle = MUTED;
  g.fillText("линия — борта слышат друг друга по радио", 16, top + 26);
  const P = (u) => { const r = fr.rel[u[0]]; return [cx0 + r[0] * k, cy0 - r[1] * k]; };
  const byIdx = Object.fromEntries(fr.uavs.map((u) => [u[0], u]));
  g.lineWidth = 1.5;
  for (const [a, b] of fr.edges) {
    const ua = byIdx[a], ub = byIdx[b];
    if (!ua || !ub) continue;
    const [x1, y1] = P(ua), [x2, y2] = P(ub);
    g.strokeStyle = fr.groups > 1 ? GROUP[ua[3] % GROUP.length] + "88" : "#86efac";
    g.beginPath(); g.moveTo(x1, y1); g.lineTo(x2, y2); g.stroke();
  }
  g.lineWidth = 1;
  for (const u of fr.uavs) {
    const [x, y] = P(u);
    if (!u[4]) {
      g.strokeStyle = GRAY; g.lineWidth = 3;
      g.beginPath(); g.moveTo(x - 8, y - 8); g.lineTo(x + 8, y + 8); g.moveTo(x + 8, y - 8); g.lineTo(x - 8, y + 8); g.stroke();
      g.lineWidth = 1; g.fillStyle = GRAY; g.font = "11px system-ui, sans-serif"; g.fillText(uid(u[0]), x + 10, y + 4); g.fillText("отказ", x - 14, y + 20);
      continue;
    }
    const jt = result.joinT[u[0]];
    if (jt !== undefined && fr.t - jt < 8) {
      g.strokeStyle = "#f59e0b"; g.lineWidth = 3; g.beginPath(); g.arc(x, y, 15, 0, 2 * Math.PI); g.stroke(); g.lineWidth = 1;
      g.fillStyle = "#b45309"; g.font = "bold 11px system-ui, sans-serif"; g.fillText("новый", x - 15, y - 19);
    }
    g.fillStyle = fr.groups > 1 ? GROUP[u[3] % GROUP.length] : "#2563eb";
    g.beginPath(); g.moveTo(x, y - 9); g.lineTo(x + 8, y + 7); g.lineTo(x, y + 3); g.lineTo(x - 8, y + 7); g.closePath(); g.fill();
    g.fillStyle = INK; g.font = "11px system-ui, sans-serif"; g.fillText(uid(u[0]), x + 10, y + 4);
  }
  // масштаб
  const m = sce.swarm.spacing_m;
  g.strokeStyle = MUTED; g.beginPath(); g.moveTo(16, H - 18); g.lineTo(16 + m * k, H - 18); g.stroke();
  g.fillStyle = MUTED; g.font = "11px system-ui, sans-serif"; g.fillText(`${m} м`, 16, H - 24);
  if (fr.groups > 1) {
    g.fillStyle = RED; g.font = "bold 22px system-ui, sans-serif"; g.fillText("РАЗРЫВ СВЯЗИ", 16, top + 60);
    g.font = "13px system-ui, sans-serif"; g.fillText(`подгрупп связи: ${fr.groups}, они не слышат друг друга`, 16, top + 80);
  }

  // 3. картины бортов
  const x0 = fw + 30, pw = W - x0 - 20;
  g.strokeStyle = "#e2e8f0"; g.beginPath(); g.moveTo(fw + 10, top - 10); g.lineTo(fw + 10, H); g.stroke();
  g.fillStyle = INK; g.font = "bold 14px system-ui, sans-serif";
  g.fillText("Картины бортов", x0, top + 8);
  g.font = "12px system-ui, sans-serif"; g.fillStyle = MUTED;
  g.fillText("полоса — сколько сведений у борта; одинаковый цвет — одинаковая картина;", x0, top + 26);
  g.fillText("точки — объекты в картине борта: ● есть у всех бортов, ○ только у своей подгруппы", x0, top + 40);
  const div = result.diverged[i];
  const nv = fr.variants;
  const syncing = result.events.find((e) => e.kind !== "fail" && e.conv !== null && tNow >= e.t && tNow < e.t + e.conv);
  g.font = "bold 16px system-ui, sans-serif";
  if (syncing) {
    g.fillStyle = "#b45309";
    g.fillText(syncing.kind === "join" ? "Новый борт получает картину группы…" : "Связь восстановлена — идёт согласование картин…", x0, top + 64);
    g.font = "12px system-ui, sans-serif"; g.fillStyle = "#92400e";
    g.fillText(`борта обмениваются накопленными сведениями: ${fmt(tNow - syncing.t, 2)} с`, x0, top + 80);
  } else if (div) {
    g.fillStyle = RED; g.fillText(`Картины расходятся: ${nv} разные картины`, x0, top + 64);
    g.font = "12px system-ui, sans-serif"; g.fillStyle = "#7f1d1d";
    g.fillText("у каждой подгруппы только свои сведения", x0, top + 80);
  } else {
    g.fillStyle = GREEN; g.fillText("✓ Картины всех бортов совпадают", x0, top + 64);
    const rec = result.events.filter((e) => e.kind !== "fail" && e.conv !== null && fr.t >= e.t && fr.t <= e.t + e.conv + 15).pop();
    if (rec) { g.font = "12px system-ui, sans-serif"; g.fillStyle = "#166534"; g.fillText(rec.kind === "join" ? `новый борт получил картину за ${fmt(rec.conv)} с` : `согласованность восстановлена за ${fmt(rec.conv)} с после возобновления связи`, x0, top + 80); }
  }
  const rows = sce.swarm.n_uav + Object.keys(result.joinT).length;
  const rh = Math.min(30, (H - top - 104) / rows), ry0 = top + 96;
  const bx = x0 + 52, bw = 170, dx0 = bx + bw + 112;
  for (let r = 0; r < rows; r++) {
    const u = byIdx[r], y = ry0 + r * rh;
    g.font = "12px system-ui, sans-serif";
    g.fillStyle = u && u[4] ? (fr.groups > 1 ? GROUP[u[3] % GROUP.length] : "#2563eb") : GRAY;
    g.beginPath(); g.arc(x0 + 6, y + rh / 2 - 2, 5, 0, 2 * Math.PI); g.fill();
    g.fillStyle = INK; g.fillText(uid(r), x0 + 16, y + rh / 2 + 2);
    g.fillStyle = "#f1f5f9"; g.fillRect(bx, y + 3, bw, rh - 10);
    if (!u) {
      const coming = syncing && syncing.kind === "join" && nx.uavs.some((q) => q[0] === r);
      g.fillStyle = coming ? "#b45309" : GRAY;
      g.fillText(coming ? "присоединяется — получает картину группы" : "ещё не в группе", bx + bw + 8, y + rh / 2 + 2);
      continue;
    }
    if (!u[4]) { g.fillStyle = GRAY; g.fillText("отказ — не работает", bx + bw + 8, y + rh / 2 + 2); continue; }
    const rec = u[6];  // значения — ровно из кадра модели, без интерполяции
    g.fillStyle = syncing ? (u[7] === 0 ? "#f59e0b" : "#fbbf24") : div ? VARIANT[u[7] % VARIANT.length] : GREEN;
    g.fillRect(bx, y + 3, Math.max(2, bw * rec / result.maxRec), rh - 10);
    g.fillStyle = INK;
    g.fillText(`сведений ${int(rec)}`, bx + bw + 8, y + rh / 2 + 2);
    // точки объектов: общие для всех бортов — залитые, известные только части бортов — полые
    const nC = u[8] || 0, nO = u[9] || 0, n = nC + nO;
    const room = W - 20 - dx0 - 36, step = n ? Math.min(11, room / n) : 11, rd = Math.max(2, Math.min(4, step / 2 - 0.5));
    const cy = y + rh / 2 - 2;
    const ccol = syncing ? "#d97706" : div ? VARIANT[u[7] % VARIANT.length] : GREEN;
    for (let j = 0; j < n; j++) {
      const cx = dx0 + j * step + rd;
      g.beginPath(); g.arc(cx, cy, rd, 0, 2 * Math.PI);
      if (j < nC) { g.fillStyle = "#334155"; g.fill(); }
      else { g.strokeStyle = ccol; g.lineWidth = 1.6; g.stroke(); g.lineWidth = 1; }
    }
    g.fillStyle = MUTED; g.font = "11px system-ui, sans-serif";
    g.fillText(String(n), dx0 + n * step + 4, y + rh / 2 + 2);
  }
}

// ------------------------------------------------------------------ карта маршрута
function mapView() {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  const add = (x, y) => { x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); };
  for (const fr of result.frames) { add(fr.c[0], fr.c[1]); for (const [x, y] of fr.pic) add(x, y); }
  const pad = 150;
  x0 -= pad; x1 += pad; y0 -= pad; y1 += pad;
  const cv = $("map");
  const k = Math.min((cv.width - 20) / (x1 - x0), (cv.height - 40) / (y1 - y0));
  return { x0, y0, x1, y1, k, ox: (cv.width - (x1 - x0) * k) / 2, oy: (cv.height - 26 - (y1 - y0) * k) / 2 };
}

function drawMap(i) {
  const fr = result.frames[i];
  const cv = $("map"), g = cv.getContext("2d"), W = cv.width, H = cv.height, v = result.view;
  const X = (x) => v.ox + (x - v.x0) * v.k, Y = (y) => v.oy + (v.y1 - y) * v.k;
  g.clearRect(0, 0, W, H);
  g.fillStyle = "#f8fafc"; g.fillRect(0, 0, W, H);
  g.strokeStyle = "#93c5fd"; g.lineWidth = 2; g.beginPath();
  for (let j = 0; j <= i; j++) { const c = result.frames[j].c; if (!j) g.moveTo(X(c[0]), Y(c[1])); else g.lineTo(X(c[0]), Y(c[1])); }
  g.stroke(); g.lineWidth = 1;
  g.strokeStyle = GRAY; g.setLineDash([4, 4]);
  g.beginPath(); g.arc(X(fr.c[0]), Y(fr.c[1]), result.scenario.sensor.fov_radius_m * v.k, 0, 2 * Math.PI); g.stroke();
  g.setLineDash([]);
  for (const [x, y, cov] of fr.truth) {
    if (x < v.x0 || x > v.x1 || y < v.y0 || y > v.y1) continue;
    g.strokeStyle = cov ? "#334155" : "#cbd5e1"; g.lineWidth = cov ? 1.6 : 1.2;
    g.beginPath(); g.arc(X(x), Y(y), 7, 0, 2 * Math.PI); g.stroke();
  }
  g.lineWidth = 1;
  const col = { ok: GREEN, dup: "#ea580c", false: RED, mixed: "#9333ea" };
  for (const [x, y, st] of fr.pic) {
    const b = st.split("+")[0];
    g.fillStyle = col[b] || "#000";
    if (b === "false") { g.font = "bold 15px sans-serif"; g.fillText("×", X(x) - 4, Y(y) + 5); }
    else { g.beginPath(); g.arc(X(x), Y(y), 3.8, 0, 2 * Math.PI); g.fill(); }
  }
  g.fillStyle = "#1d4ed8"; g.beginPath(); g.arc(X(fr.c[0]), Y(fr.c[1]), 5, 0, 2 * Math.PI); g.fill();
  const sb = [100, 200, 500, 1000, 2000, 5000].find((m) => m * v.k > 60) || 5000;
  g.strokeStyle = INK; g.beginPath(); g.moveTo(W - 20 - sb * v.k, H - 12); g.lineTo(W - 20, H - 12); g.stroke();
  g.fillStyle = INK; g.font = "11px system-ui, sans-serif"; g.fillText(sb >= 1000 ? `${sb / 1000} км` : `${sb} м`, W - 20 - sb * v.k, H - 17);
  g.font = "13px system-ui, sans-serif";
  g.fillText(`t = ${fmt(fr.t, 0)} с · картина борта ${uid(fr.view)}: объектов ${fr.pic.length}`, 12, H - 10);
}

// ------------------------------------------------------------------ воспроизведение
function showFrame(i, alpha = 0) {
  frameIdx = Math.max(0, Math.min(i, result.frames.length - 1));
  drawStory(frameIdx, alpha);
  drawMap(frameIdx);
  $("time").value = frameIdx;
  $("tlabel").textContent = `${fmt(result.frames[frameIdx].t, 0)} с из ${Math.round(result.tmax)}`;
}

function speedAt(t) {
  const sel = Number($("speed").value);
  if (sel) return sel;
  const auto = Math.max(5, result.tmax / 20);
  // рядом с восстановлением связи и присоединением — замедление, чтобы было видно схождение картин
  for (const e of result.events)
    if (e.kind !== "fail" && t >= e.t - 1 && t <= e.t + (e.conv || 1) + 1) return 0.25;
  for (const e of result.events)
    if (e.kind !== "fail" && t >= e.t - 4 && t <= e.t + (e.conv || 1) + 4) return 2;
  return auto;
}

// кадр по модельному времени: индекс и доля пути до следующего кадра
function frameAt(t) {
  const fr = result.frames;
  let lo = 0, hi = fr.length - 1;
  if (t <= fr[0].t) return [0, 0];
  if (t >= fr[hi].t) return [hi, 0];
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (fr[m].t <= t) lo = m; else hi = m; }
  return [lo, (t - fr[lo].t) / (fr[hi].t - fr[lo].t)];
}

function tick(ts) {
  if (!playing) return;
  const dt = Math.min(0.1, (ts - lastTs) / 1000);
  lastTs = ts;
  playT += dt * speedAt(playT);
  if (playT >= result.tmax) { playT = result.tmax; showFrame(result.frames.length - 1); return stopPlay(); }
  showFrame(...frameAt(playT));
  requestAnimationFrame(tick);
}

function startPlay() {
  if (frameIdx >= result.frames.length - 1) playT = 0;
  playing = true; $("play").textContent = "❚❚";
  lastTs = performance.now();
  requestAnimationFrame(tick);
}

function stopPlay() { playing = false; $("play").textContent = "▶"; }
const togglePlay = () => (playing ? stopPlay() : startPlay());

// ------------------------------------------------------------------ для специалистов: график
function drawChart() {
  const cv = $("chart"), g = cv.getContext("2d"), W = cv.width, H = cv.height;
  const ser = result.series, sce = result.scenario;
  g.clearRect(0, 0, W, H);
  const L = 48, R = 48, T = 14, B = 26, pw = W - L - R, ph = (H - T - B - 16) / 2;
  const tmax = ser[ser.length - 1].t_s;
  const X = (t) => L + (t / tmax) * pw;
  for (const p of sce.net.partition) { g.fillStyle = "rgba(220,38,38,.07)"; g.fillRect(X(p.start_s), T, X(p.end_s) - X(p.start_s), H - T - B); }
  g.font = "11px sans-serif";
  for (const e of sce.event) {
    g.strokeStyle = "#64748b"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(e.t_s), T); g.lineTo(X(e.t_s), H - B); g.stroke(); g.setLineDash([]);
    g.fillStyle = MUTED; g.fillText(e.kind === "fail" ? `отказ ${uid(e.uav)}` : `новый ${uid(e.uav)}`, X(e.t_s) + 3, H - B - 4);
  }
  const y1 = (v, m) => T + 16 + (ph - 16) - (v / m) * (ph - 24);
  const m1 = Math.max(...ser.map((r) => r.components), 1);
  g.strokeStyle = "#2563eb"; g.lineWidth = 1.8; g.beginPath();
  ser.forEach((r, i) => { const x = X(r.t_s); if (!i) g.moveTo(x, y1(r.components, m1)); else { g.lineTo(x, y1(ser[i - 1].components, m1)); g.lineTo(x, y1(r.components, m1)); } });
  g.stroke(); g.lineWidth = 1;
  g.fillStyle = INK; g.fillText("число подгрупп связи; ◆ — восстановление согласованности", L, T + 8);
  for (let v = 1; v <= m1; v++) g.fillText(String(v), L - 14, y1(v, m1) + 4);
  for (const r of result.summary.restores) {
    if (r.store_conv_s === null) continue;
    const x = X(r.t_s + r.store_conv_s), y = y1(1, m1);
    g.fillStyle = r.store_conv_s <= 1.5 ? GREEN : RED;
    g.beginPath(); g.moveTo(x, y - 6); g.lineTo(x + 5, y); g.lineTo(x, y + 6); g.lineTo(x - 5, y); g.fill();
    g.fillText(`${fmt(r.store_conv_s)} с`, x + 7, y - 6);
  }
  const T2 = T + ph + 16;
  const rate = ser.map((r, i) => {
    if (!i) return 0;
    const dt = r.t_s - ser[i - 1].t_s;
    return dt > 0 ? ((r.kbytes_sent - ser[i - 1].kbytes_sent) * 8.192) / dt / sce.swarm.n_uav : 0;
  });
  const bw = sce.net.bandwidth_kbps, peak = Math.max(...rate);
  const m2 = Math.max(peak * 1.25, 1);
  const y2 = (v) => T2 + 16 + (ph - 16) - (v / m2) * (ph - 24);
  if (bw <= m2) { g.strokeStyle = RED; g.setLineDash([6, 4]); g.beginPath(); g.moveTo(L, y2(bw)); g.lineTo(L + pw, y2(bw)); g.stroke(); g.setLineDash([]); }
  g.strokeStyle = "#0f766e"; g.lineWidth = 1.4; g.beginPath();
  rate.forEach((v, i) => { const x = X(ser[i].t_s); if (!i) g.moveTo(x, y2(v)); else g.lineTo(x, y2(v)); });
  g.stroke(); g.lineWidth = 1;
  g.fillStyle = INK;
  g.fillText(`передача на борт, кбит/с: пик ${fmt(peak, 0)} — ${fmt(100 * peak / bw, 0)} % полосы канала ${bw} кбит/с`, L, T2 + 8);
  g.fillText(String(Math.round(m2 / 1.25)), 4, y2(m2 / 1.25) + 4); g.fillText("0", L - 14, y2(0) + 4);
  g.strokeStyle = GRAY; g.beginPath(); g.moveTo(L, H - B); g.lineTo(L + pw, H - B); g.stroke();
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
  $("ver").textContent = verLine() + " · загрузка среды расчёта (≈ 16 МБ при первом открытии)…";
  $("foot").textContent = "Модель исполняется локально в браузере; данные никуда не передаются. " + verLine();
  applyTerms();
  initTips();
  renderCards(null);
  form.addEventListener("change", onInput);
  form.addEventListener("submit", (e) => e.preventDefault());
  $("run").onclick = run;
  $("stop").onclick = stop;
  $("reset").onclick = () => choosePreset(preset);
  $("play").onclick = togglePlay;
  $("time").oninput = (e) => { stopPlay(); showFrame(Number(e.target.value)); playT = result.frames[frameIdx].t; };
  $("dl_summary").onclick = () => download("summary.json", JSON.stringify(result.summary, null, 2), "application/json");
  $("dl_series").onclick = () => download("series.csv", seriesCsv(), "text/csv");
  $("dl_scenario").onclick = () => download("scenario.json", JSON.stringify(result.scenario, null, 2), "application/json");
  setBusy(true);
  $("stop").disabled = true;
  startWorker();
}

// перемотка к моменту модельного времени (для проверок и записи видео)
window.soglasie = {
  seek(t) { if (!result) return; stopPlay(); playT = Math.min(t, result.tmax); showFrame(...frameAt(playT)); },
  get ready() { return !!result; },
};

main();
