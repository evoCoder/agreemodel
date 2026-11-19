"""Бортовой узел: локальное сопровождение, запись в хранилище, обмен, картина.

Узел видит только своё: обнаружения своего датчика, оценку своего положения от навигации
и сообщения сети. Обмен — анти-энтропия с вытягиванием (pull):
* раз в gossip_period узел рассылает соседям свой вектор версий (DIGEST);
* получив DIGEST, узел по каждому источнику, где сосед впереди, запрашивает недостающее
  (REQ) — у одного соседа на источник, пока запрос не исполнен или не истёк срок;
* сосед отвечает дельтой (DELTA) — только записи после вектора запросившего.
Так недостающее по каждому источнику вытягивается один раз, а не от всех соседей сразу.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .config import FusionCfg, NodeCfg
from .fusion import Picture, compute_picture
from .sensors import Detection
from .store import Delta, ReplicatedStore


@dataclass
class LocalTrack:
    tid: int
    x: np.ndarray  # e, n, ve, vn
    P: np.ndarray
    t_us: int
    t0_us: int
    class_log: np.ndarray
    hits: int = 1
    status: str = "A"  # A — активен, L — потерян
    changed: bool = True
    published: bool = False
    truth: Counter = field(default_factory=Counter)  # только для оценки по эталону

    @property
    def class_p(self) -> tuple[float, ...]:
        n = self.hits
        v = np.exp((self.class_log - self.class_log.max()) / n)
        v = v / v.sum()
        return tuple(round(float(a), 4) for a in v)


class Node:
    def __init__(self, idx: int, uid: str, ncfg: NodeCfg, fcfg: FusionCfg) -> None:
        self.idx, self.uid = idx, uid
        self.cfg, self.fcfg = ncfg, fcfg
        self.store = ReplicatedStore(uid)
        self.tracks: list[LocalTrack] = []
        self._next_tid = 1
        self.picture: Picture | None = None
        self.dirty = True
        phase = (idx * 0.037) % ncfg.gossip_period_s  # разнесение тактов обмена
        self.next_gossip = phase
        self.next_publish = 0.0
        self.next_picture = 0.0
        self.pending: dict[str, float] = {}  # источник → срок ожидания ответа на REQ
        self.unreflected_frame_t: float | None = None
        self.quantum_s: list[float] = []
        self.compute_ms: list[float] = []
        self.truth_of: dict[str, str | None] = {}  # ключ записи → объект эталона (оценка)

    # ------------------------------------------------------------------ сопровождение
    def _predict(self, tr: LocalTrack, t_us: int) -> tuple[np.ndarray, np.ndarray]:
        dt = (t_us - tr.t_us) / 1e6
        F = np.eye(4)
        F[0, 2] = F[1, 3] = dt
        q = self.cfg.q_accel
        Q = q * np.array([[dt**3 / 3, 0, dt**2 / 2, 0], [0, dt**3 / 3, 0, dt**2 / 2],
                          [dt**2 / 2, 0, dt, 0], [0, dt**2 / 2, 0, dt]])
        return F @ tr.x, F @ tr.P @ F.T + Q

    @staticmethod
    def _update(tr: LocalTrack, pred: tuple[np.ndarray, np.ndarray], d: Detection,
                t_us: int, H: np.ndarray) -> None:
        x, P = pred
        R = np.eye(2) * d.var
        S = H @ P @ H.T + R
        K = P @ H.T @ np.linalg.inv(S)
        tr.x = x + K @ (np.array([d.x, d.y]) - H @ x)
        IKH = np.eye(4) - K @ H
        tr.P = IKH @ P @ IKH.T + K @ R @ K.T
        tr.t_us = t_us
        tr.hits += 1
        tr.class_log += np.log(np.maximum(np.array(d.class_p), 1e-6))
        tr.changed = True
        tr.truth[d.truth_id] += 1

    def on_frame(self, t_s: float, t_us: int, dets: list[Detection]) -> None:
        H = np.zeros((2, 4))
        H[0, 0] = H[1, 1] = 1.0
        active = [tr for tr in self.tracks if tr.status == "A"]
        preds = [self._predict(tr, t_us) for tr in active]
        conf = self.cfg.confirm_hits
        tier = [0 if self.cfg.gate2_d2 > 0 and tr.hits >= conf else 1 for tr in active]
        pairs = []
        for j, d in enumerate(dets):
            z = np.array([d.x, d.y])
            R = np.eye(2) * d.var
            for i, (x, P) in enumerate(preds):
                S = H @ P @ H.T + R
                nu = z - x[:2]
                d2 = float(nu @ np.linalg.solve(S, nu))
                if d2 <= self.cfg.gate_d2:
                    pairs.append((tier[i], d2, i, j))
        # подтверждённые треки — первыми: иначе новый трек с большой ковариацией (часто от
        # ложного обнаружения рядом) перехватывает отметки объекта и раздваивает его трек
        pairs.sort()
        used_t, used_d = set(), set()
        for _, _, i, j in pairs:
            if i in used_t or j in used_d:
                continue
            used_t.add(i)
            used_d.add(j)
            self._update(active[i], preds[i], dets[j], t_us, H)
        if self.cfg.gate2_d2 > 0:
            # второй этап: датчик даёт не больше одного обнаружения объекта за кадр, поэтому
            # обнаружение рядом с подтверждённым треком, не получившим отметки, — скорее
            # промах строба этого трека, чем новый объект; новый трек того же объекта
            # раздвоил бы его (в картине два трека одного борта одновременно не объединяются)
            pairs2 = []
            for j, d in enumerate(dets):
                if j in used_d:
                    continue
                z = np.array([d.x, d.y])
                R = np.eye(2) * d.var
                for i, (x, P) in enumerate(preds):
                    if i in used_t or tier[i]:
                        continue
                    S = H @ P @ H.T + R
                    nu = z - x[:2]
                    d2 = float(nu @ np.linalg.solve(S, nu))
                    if d2 <= self.cfg.gate2_d2:
                        pairs2.append((d2, i, j))
            pairs2.sort()
            for _, i, j in pairs2:
                if i in used_t or j in used_d:
                    continue
                used_t.add(i)
                used_d.add(j)
                self._update(active[i], preds[i], dets[j], t_us, H)
        for j, d in enumerate(dets):
            if j in used_d:
                continue
            v = self.cfg.init_vel_sigma**2
            tr = LocalTrack(
                self._next_tid, np.array([d.x, d.y, 0.0, 0.0]),
                np.diag([d.var, d.var, v, v]), t_us, t_us,
                np.log(np.maximum(np.array(d.class_p), 1e-6)))
            tr.truth[d.truth_id] += 1
            self._next_tid += 1
            self.tracks.append(tr)
        lost_us = self.cfg.track_lost_s * 1e6
        keep = []
        for tr in self.tracks:
            if tr.status == "A" and t_us - tr.t_us > lost_us:
                if tr.published:
                    tr.status, tr.changed = "L", True
                    keep.append(tr)  # опубликовать признак потери, затем забыть
                continue
            keep.append(tr)
        self.tracks = keep
        if self.unreflected_frame_t is None:
            self.unreflected_frame_t = t_s

    # ------------------------------------------------------------------ запись
    def publish(self, t_us: int, own_xy: tuple[float, float], nav_sigma: float) -> int:
        n = 0
        nav_var = nav_sigma**2
        keep = []
        for tr in self.tracks:
            if tr.changed and tr.hits >= self.cfg.confirm_hits:
                key = f"trk/{tr.tid}"
                self.store.write(key, tr.t_us, {
                    "x": round(float(tr.x[0]), 3), "y": round(float(tr.x[1]), 3),
                    "vx": round(float(tr.x[2]), 3), "vy": round(float(tr.x[3]), 3),
                    # ковариация трека плюс ошибка навигации своего борта (интерфейс Р1.6)
                    "pxx": round(float(tr.P[0, 0]) + nav_var, 3),
                    "pxy": round(float(tr.P[0, 1]), 3),
                    "pyy": round(float(tr.P[1, 1]) + nav_var, 3),
                    "t0": tr.t0_us, "t1": tr.t_us, "n": tr.hits,
                    "cls": tr.class_p, "st": tr.status})
                self.truth_of[key] = tr.truth.most_common(1)[0][0]
                tr.changed, tr.published = False, True
                n += 1
            if tr.status == "L" and not tr.changed:
                continue
            keep.append(tr)
        self.tracks = keep
        self.store.write("uav", t_us, {"x": round(own_xy[0], 2), "y": round(own_xy[1], 2),
                                       "st": "A"})
        self.dirty = True
        return n

    # ------------------------------------------------------------------ картина
    def recompute(self, t_s: float) -> bool:
        if not self.dirty:
            return False
        w0 = time.perf_counter()
        self.picture = compute_picture(self.store.records(), self.fcfg)
        self.compute_ms.append((time.perf_counter() - w0) * 1e3)
        self.dirty = False
        if self.unreflected_frame_t is not None:
            self.quantum_s.append(t_s - self.unreflected_frame_t)
            self.unreflected_frame_t = None
        return True

    # ------------------------------------------------------------------ обмен
    def on_digest(self, t_s: float, peer_vv: dict[str, int]) -> dict[str, int] | None:
        """Вернуть запрос (источник → наш vv) недостающего у соседя или None."""
        want = {}
        mine = self.store.version_vector()
        for origin, v in peer_vv.items():
            if v > mine.get(origin, 0) and self.pending.get(origin, -1.0) < t_s:
                want[origin] = mine.get(origin, 0)
                self.pending[origin] = t_s + self.cfg.request_timeout_s
        return want or None

    def on_request(self, req: dict[str, int]) -> Delta:
        return self.store.delta_for(req, set(req))

    def on_delta(self, delta: Delta) -> int:
        for p in delta.parts:
            self.pending.pop(p.origin, None)
        n = self.store.apply(delta)
        if n:
            self.dirty = True
        return n
