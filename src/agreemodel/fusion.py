"""Общая информационная картина как детерминированная функция реплицированного состояния.

    картина = f(записи хранилища)

f зависит только от содержимого хранилища (не от локальных часов, порядка прихода
сообщений и случайных чисел), поэтому копии с одинаковым содержимым дают одинаковую
картину — сходимость картины следует из сходимости хранилища (Р1.4).

Шаги:
1. Опорное время t_ref — наибольшая метка записей; треки приводятся к t_ref: активные —
   экстраполяцией по скорости не дальше coast_s, потерянные — последним положением
   с увеличенной ковариацией.
2. Ассоциация треков разных бортов в каноническом порядке (t0, origin, key): трек
   присоединяется к объекту с наименьшим расстоянием Махаланобиса в пороге при
   совместимых классах; запрет — два трека одного борта с пересекающимися интервалами
   наблюдения (борт видел их одновременно — это разные объекты).
3. Слияние положения — ковариационное пересечение (CI): ошибки треков разных бортов
   коррелированы (общая ошибка навигации, повторно переданные сведения), CI не даёт
   ложного сужения ковариации. Классы — нормированное геометрическое среднее.
4. Идентификатор объекта — хеш ключа первого трека в каноническом порядке: одинаков
   на всех бортах без согласования.
5. Неоднозначность: если второй кандидат почти так же хорош, как первый, объект
   помечается как неразрешённый (conflict) — противоречие не скрывается (Р1.3).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass, field

from .config import FusionCfg
from .store import Record

Cov = tuple[float, float, float]  # (xx, xy, yy)


def _inv(P: Cov) -> Cov:
    a, b, c = P
    det = a * c - b * b
    if det <= 1e-12:
        det = 1e-12
    return (c / det, -b / det, a / det)


def _mahal2(dx: float, dy: float, P: Cov) -> float:
    ia, ib, ic = _inv(P)
    return dx * dx * ia + 2 * dx * dy * ib + dy * dy * ic


def ci_fuse(xa: float, ya: float, Pa: Cov, xb: float, yb: float, Pb: Cov,
            iters: int = 12) -> tuple[float, float, Cov]:
    """Ковариационное пересечение; ω — минимум следа методом золотого сечения."""
    ia, ib = _inv(Pa), _inv(Pb)

    def fused(w: float) -> tuple[Cov, Cov]:
        info = (w * ia[0] + (1 - w) * ib[0], w * ia[1] + (1 - w) * ib[1],
                w * ia[2] + (1 - w) * ib[2])
        return info, _inv(info)

    def cost(w: float) -> float:
        P = fused(w)[1]
        return P[0] + P[2]

    g = (math.sqrt(5) - 1) / 2
    lo, hi = 0.0, 1.0
    c, d = hi - g * (hi - lo), lo + g * (hi - lo)
    fc, fd = cost(c), cost(d)
    for _ in range(iters):
        if fc < fd:
            hi, d, fd = d, c, fc
            c = hi - g * (hi - lo)
            fc = cost(c)
        else:
            lo, c, fc = c, d, fd
            d = lo + g * (hi - lo)
            fd = cost(d)
    # оптимум на границе (одна оценка заведомо точнее) — золотое сечение до него не доходит
    w = min(((lo + hi) / 2, 0.0, 1.0), key=cost)
    _, P = fused(w)
    # x = P (w Pa⁻¹ xa + (1−w) Pb⁻¹ xb)
    ux = w * (ia[0] * xa + ia[1] * ya) + (1 - w) * (ib[0] * xb + ib[1] * yb)
    uy = w * (ia[1] * xa + ia[2] * ya) + (1 - w) * (ib[1] * xb + ib[2] * yb)
    return P[0] * ux + P[1] * uy, P[1] * ux + P[2] * uy, P


def _cos_dist(p: tuple[float, ...], q: tuple[float, ...]) -> float:
    num = sum(a * b for a, b in zip(p, q, strict=True))
    den = math.sqrt(sum(a * a for a in p) * sum(b * b for b in q)) + 1e-12
    return 1.0 - num / den


@dataclass
class PicObject:
    oid: str
    x: float
    y: float
    P: Cov
    cls: tuple[float, ...]
    members: list[tuple[str, str]]
    t_last_us: int
    conflict: bool = False
    _intervals: dict[str, list[tuple[int, int]]] = field(default_factory=dict, repr=False)
    _log_cls: list[float] = field(default_factory=list, repr=False)


@dataclass
class UavView:
    uid: str
    x: float
    y: float
    t_us: int
    alive: bool


@dataclass
class Picture:
    t_ref_us: int
    objects: list[PicObject]
    uavs: list[UavView]
    digest: str

    @property
    def n_conflicts(self) -> int:
        return sum(1 for o in self.objects if o.conflict)


def _track_state(r: Record, t_ref: int, cfg: FusionCfg) -> tuple[float, float, Cov]:
    d = r.data
    x, y = d["x"], d["y"]
    P = (d["pxx"], d["pxy"], d["pyy"])
    dt = (t_ref - d["t1"]) / 1e6
    if d["st"] == "A" and dt <= cfg.coast_s:
        x += d["vx"] * dt
        y += d["vy"] * dt
        g = (cfg.vel_sigma * dt) ** 2
        P = (P[0] + g, P[1], P[2] + g)
    else:
        g = cfg.stale_sigma_m**2
        P = (P[0] + g, P[1], P[2] + g)
    return x, y, P


def compute_picture(records: Iterable[Record], cfg: FusionCfg) -> Picture:
    tracks: list[Record] = []
    uav_recs: list[Record] = []
    t_ref = 0
    for r in records:
        if r.key == "uav":
            uav_recs.append(r)
            t_ref = max(t_ref, r.t_us)
        elif r.key.startswith("trk/"):
            tracks.append(r)
            t_ref = max(t_ref, r.data["t1"])

    tracks.sort(key=lambda r: (r.data["t0"], r.origin, r.key))
    objs: list[PicObject] = []
    grid: dict[tuple[int, int], list[int]] = {}
    cell = cfg.cell_m
    gate2 = cfg.gate**2

    def cell_of(x: float, y: float) -> tuple[int, int]:
        return (math.floor(x / cell), math.floor(y / cell))

    for r in tracks:
        x, y, P = _track_state(r, t_ref, cfg)
        cx, cy = cell_of(x, y)
        cands: list[tuple[float, int]] = []
        t0, t1 = r.data["t0"], r.data["t1"]
        cls = r.data["cls"]
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for k in grid.get((gx, gy), ()):
                    o = objs[k]
                    iv = o._intervals.get(r.origin)
                    if iv and any(a <= t1 and t0 <= b for a, b in iv):
                        continue
                    if _cos_dist(o.cls, cls) > cfg.class_gate:
                        continue
                    S = (o.P[0] + P[0], o.P[1] + P[1], o.P[2] + P[2])
                    d2 = _mahal2(x - o.x, y - o.y, S)
                    if d2 <= gate2:
                        cands.append((d2, k))
        if cands:
            cands.sort()
            d2best, k = cands[0]
            o = objs[k]
            old_cell = cell_of(o.x, o.y)
            o.x, o.y, o.P = ci_fuse(o.x, o.y, o.P, x, y, P)
            o.members.append(r.gkey)
            o._intervals.setdefault(r.origin, []).append((t0, t1))
            o._log_cls = [a + math.log(max(b, 1e-6)) for a, b in zip(o._log_cls, cls, strict=True)]
            n = len(o.members)
            m = max(o._log_cls)
            ex = [math.exp((v - m) / n) for v in o._log_cls]
            s = sum(ex)
            o.cls = tuple(v / s for v in ex)
            o.t_last_us = max(o.t_last_us, t1)
            if len(cands) > 1 and cands[1][0] - d2best < cfg.ambiguity_margin:
                o.conflict = True
                objs[cands[1][1]].conflict = True
            new_cell = cell_of(o.x, o.y)
            if new_cell != old_cell:
                grid[old_cell].remove(k)
                grid.setdefault(new_cell, []).append(k)
        else:
            oid = "O" + hashlib.sha1(f"{r.origin}/{r.key}".encode()).hexdigest()[:10]
            o = PicObject(oid, x, y, P, tuple(cls), [r.gkey], t1,
                          _intervals={r.origin: [(t0, t1)]},
                          _log_cls=[math.log(max(b, 1e-6)) for b in cls])
            objs.append(o)
            grid.setdefault((cx, cy), []).append(len(objs) - 1)

    uavs = [
        UavView(r.origin, r.data["x"], r.data["y"], r.t_us,
                (t_ref - r.t_us) <= cfg.uav_timeout_s * 1e6 and r.data.get("st") == "A")
        for r in sorted(uav_recs, key=lambda r: r.origin)
    ]
    objs.sort(key=lambda o: o.oid)
    h = hashlib.sha1()
    for o in objs:
        h.update(f"{o.oid}|{o.x:.3f}|{o.y:.3f}|{len(o.members)}|{int(o.conflict)};".encode())
    for u in uavs:
        h.update(f"{u.uid}|{u.t_us}|{int(u.alive)};".encode())
    return Picture(t_ref, objs, uavs, h.hexdigest())
