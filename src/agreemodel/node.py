"""Бортовой узел: локальное сопровождение, запись в хранилище, обмен, картина.

Узел видит только своё: обнаружения своего датчика, оценку своего положения от навигации
и сообщения сети. Обмен — анти-энтропия с вытягиванием (pull):
* раз в gossip_period узел рассылает соседям свой вектор версий (DIGEST);
* получив DIGEST, узел по каждому источнику, где сосед впереди, запрашивает недостающее
  (REQ) — у одного соседа на источник, пока запрос не исполнен или не истёк срок;
* сосед отвечает дельтой (DELTA) — только записи после вектора запросившего.
Так недостающее по каждому источнику вытягивается один раз, а не от всех соседей сразу.

Обмен цикла 1 (protocol = "c1") учитывает, что mesh-радиосеть — общая среда:
* свои новые записи борт сразу рассылает широковещательно (push), вытягивание остаётся
  механизмом досылки пропущенного;
* вектор версий — одно широковещательное сообщение на всех соседей; при появлении нового
  соседа вектор рассылается сразу, не дожидаясь такта;
* недостающее по источнику запрашивается в первую очередь у самого источника (у него
  данные полные), иначе — у соседа с наибольшей версией; повтор — сразу по истечении
  срока ожидания, у двух соседей; пока эфир занят, повтор откладывается;
* перед запросом к новым соседям борт discover_s собирает их векторы — так все запросившие
  обращаются к одному ответчику (к самому источнику, если он доступен);
* ответ — широковещательная дельта по источнику от наименьшей запрошенной версии,
  нарезанная на части по одному пакету канала (потеря пакета теряет одну часть, а не
  весь ответ); её применяют все соседи, кому она нужна; запросы, пришедшие за окно
  response_window_s, объединяются; передаются только куски диапазона, которых нет в
  эфире (своя передача в очереди или услышанная от соседа);
* части, пришедшие после пропуска, откладываются; пропуск дозапрашивается сразу у
  отправителя (NACK) — только он, без ожидания срока; ответ из нескольких частей
  замыкается пустой частью — так виден и пропуск последней части;
* срок ожидания ответа — адаптивный по замерам RTT (RFC 6298: SRTT + 4·RTTVAR, правило
  Карна, умеренное увеличение при повторе), а не фиксированные 0,6 с.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from .config import FusionCfg, NodeCfg
from .fusion import Picture, compute_picture
from .sensors import Detection
from .store import Delta, DeltaPart, ReplicatedStore


@dataclass
class Out:
    """Исходящее сообщение узла: dst = None — широковещание."""
    kind: str
    dst: int | None
    payload: object
    size: int


@dataclass
class Pending:
    want_to: int  # до какой версии источника ждём
    t_sent: float
    deadline: float
    tries: int
    peers: tuple[int, ...]
    nack: bool = False  # дозапрос пропуска: короткий срок, без откладывания по занятости эфира


PEER_FRESH_S = 1.0  # сосед считается доступным, если его вектор слышали не раньше
HEARD_HOLD_S = 0.1  # услышанная часть считается «в эфире» ещё столько после приёма


@dataclass
class LocalTrack:
    tid: int
    x: np.ndarray  # e, n, ve, vn
    P: np.ndarray
    t_us: int
    t0_us: int
    class_log: np.ndarray
    hits: int = 1
    status: str = "A"  # A — активен, L — потерян, M — объединён с другим своим треком
    merged_into: int | None = None
    t_confirm_s: float | None = None  # кадр, в котором трек набрал confirm_hits
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
        self.pending: dict[str, float] = {}  # c0: источник → срок ожидания ответа на REQ
        # обмен цикла 1
        self.req: dict[str, Pending] = {}
        self.peer_vv: dict[int, tuple[float, dict[str, int]]] = {}
        self.uid_of: dict[int, str] = {}
        self.srtt: float | None = None
        self.rttvar = 0.0
        self.rto = ncfg.rto_init_s
        self.rtt_samples: list[float] = []
        # источник → (от версии, до версии (0 — всё), срок отправки)
        self.outbox: dict[str, tuple[list[tuple[int, int]], float]] = {}
        # источник → диапазоны [от, до, «в эфире» до момента] (inf — своя часть в очереди)
        self.air: dict[str, list[list]] = {}
        self.parked: dict[str, dict[int, object]] = {}  # части после пропуска
        self.nacked: dict[str, tuple[tuple[int, int], float]] = {}  # последний дозапрос пропуска
        self.pushed_to = 0
        self.next_trigger = 0.0
        self.hold_until = 0.0  # до этого момента — сбор векторов новых соседей, без запросов
        self.unreflected_frame_t: float | None = None  # первый кадр, ещё не записанный
        self.published_frame_t: float | None = None  # первый записанный, не отражённый в картине
        self.quantum_s: list[float] = []
        self.compute_ms: list[float] = []
        self.fix_s: list[float] = []
        self._frame_t = 0.0
        self._now_s = 0.0
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

    def on_frame(self, t_s: float, t_us: int, dets: list[Detection]) -> None:
        self._frame_t = t_s
        H = np.zeros((2, 4))
        H[0, 0] = H[1, 1] = 1.0
        active = [tr for tr in self.tracks if tr.status == "A"]
        preds = [self._predict(tr, t_us) for tr in active]
        # подтверждённые треки получают обнаружения первыми: иначе новый трек с большой
        # ковариацией (часто открытый ложным обнаружением рядом с объектом) перехватывает
        # отметки объекта и раздваивает его трек
        conf = self.cfg.confirm_hits
        tier = [0 if self.cfg.confirmed_first and tr.hits >= conf else 1 for tr in active]
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
        pairs.sort()
        used_t, used_d = set(), set()
        for _, _, i, j in pairs:
            if i in used_t or j in used_d:
                continue
            used_t.add(i)
            used_d.add(j)
            self._update(active[i], preds[i], dets[j], t_us, H)
        if self.cfg.relaxed_gate_d2 > 0:
            pairs2 = []
            for j, d in enumerate(dets):
                if j in used_d:
                    continue
                z = np.array([d.x, d.y])
                R = np.eye(2) * d.var
                for i, (x, P) in enumerate(preds):
                    if i in used_t:
                        continue
                    S = H @ P @ H.T + R
                    nu = z - x[:2]
                    d2 = float(nu @ np.linalg.solve(S, nu))
                    if d2 <= self.cfg.relaxed_gate_d2:
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
        if self.cfg.merge_d2 > 0:
            self._merge_own(t_us)
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

    def _update(self, tr: LocalTrack, pred, d: Detection, t_us: int, H: np.ndarray) -> None:
        x, P = pred
        R = np.eye(2) * d.var
        S = H @ P @ H.T + R
        K = P @ H.T @ np.linalg.inv(S)
        tr.x = x + K @ (np.array([d.x, d.y]) - H @ x)
        IKH = np.eye(4) - K @ H
        tr.P = IKH @ P @ IKH.T + K @ R @ K.T
        tr.t_us = t_us
        tr.hits += 1
        if tr.hits == self.cfg.confirm_hits and tr.t_confirm_s is None:
            tr.t_confirm_s = self._frame_t
        tr.class_log += np.log(np.maximum(np.array(d.class_p), 1e-6))
        tr.changed = True
        tr.truth[d.truth_id] += 1

    def _merge_own(self, t_us: int) -> None:
        """Два активных трека борта с совпадающими оценками — один объект: младший
        (открыт позже) поглощается старшим; опубликованный младший закрывается «M»."""
        # только подтверждённые треки: неподтверждённый рядом с треком — обычно ложная
        # отметка или выпавшая из строба, он и так не публикуется
        act = sorted((tr for tr in self.tracks
                      if tr.status == "A" and tr.hits >= self.cfg.confirm_hits),
                     key=lambda tr: tr.tid)
        gone: set[int] = set()
        for a_i, a in enumerate(act):
            if a.tid in gone:
                continue
            for b in act[a_i + 1:]:
                if b.tid in gone:
                    continue
                xa, Pa = self._predict(a, t_us)
                xb, Pb = self._predict(b, t_us)
                S = Pa[:2, :2] + Pb[:2, :2]
                nu = xa[:2] - xb[:2]
                if float(nu @ np.linalg.solve(S, nu)) > self.cfg.merge_d2:
                    continue
                # старший получает отметки младшего (число и классы), оценка — у старшего
                a.hits += b.hits
                a.class_log = a.class_log + b.class_log
                a.truth.update(b.truth)
                a.changed = True
                gone.add(b.tid)
                if b.published:
                    b.status, b.changed, b.merged_into = "M", True, a.tid
        if gone:
            self.tracks = [tr for tr in self.tracks
                           if tr.tid not in gone or tr.status == "M"]

    # ------------------------------------------------------------------ запись
    def publish(self, t_us: int, own_xy: tuple[float, float], nav_sigma: float,
                t_s: float | None = None) -> int:
        self._now_s = t_s if t_s is not None else self._frame_t
        n = 0
        nav_var = nav_sigma**2
        keep = []
        for tr in self.tracks:
            if tr.changed and (tr.hits >= self.cfg.confirm_hits or tr.status == "M"):
                key = f"trk/{tr.tid}"
                self.store.write(key, tr.t_us, {
                    "x": round(float(tr.x[0]), 3), "y": round(float(tr.x[1]), 3),
                    "vx": round(float(tr.x[2]), 3), "vy": round(float(tr.x[3]), 3),
                    # ковариация трека плюс ошибка навигации своего борта (интерфейс Р1.6)
                    "pxx": round(float(tr.P[0, 0]) + nav_var, 3),
                    "pxy": round(float(tr.P[0, 1]), 3),
                    "pyy": round(float(tr.P[1, 1]) + nav_var, 3),
                    "t0": tr.t0_us, "t1": tr.t_us, "n": tr.hits,
                    "cls": tr.class_p, "st": tr.status,
                    **({"into": f"trk/{tr.merged_into}"} if tr.status == "M" else {})})
                self.truth_of[key] = tr.truth.most_common(1)[0][0]
                if not tr.published and tr.t_confirm_s is not None:
                    # задержка фиксации нового наблюдения: от кадра, подтвердившего трек,
                    # до записи в хранилище (дальше запись рассылается и входит в картину)
                    self.fix_s.append(self._now_s - tr.t_confirm_s)
                tr.changed, tr.published = False, True
                n += 1
            if tr.status in ("L", "M") and not tr.changed:
                continue
            keep.append(tr)
        self.tracks = keep
        self.store.write("uav", t_us, {"x": round(own_xy[0], 2), "y": round(own_xy[1], 2),
                                       "st": "A"})
        self.dirty = True
        if self.unreflected_frame_t is not None and self.published_frame_t is None:
            self.published_frame_t = self.unreflected_frame_t
        self.unreflected_frame_t = None
        return n

    # ------------------------------------------------------------------ картина
    def recompute(self, t_s: float) -> bool:
        if not self.dirty:
            return False
        w0 = time.perf_counter()
        self.picture = compute_picture(self.store.records(), self.fcfg)
        self.compute_ms.append((time.perf_counter() - w0) * 1e3)
        self.dirty = False
        # квант — от кадра датчика до картины, в которой этот кадр отражён: кадр попадает
        # в картину только через запись в хранилище (publish), поэтому пересчёт по чужой
        # дельте до записи своего кадра квант не закрывает
        if self.published_frame_t is not None:
            self.quantum_s.append(t_s - self.published_frame_t)
            self.published_frame_t = None
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

    # ------------------------------------------------------------------ обмен цикла 1
    def digest_out(self) -> Out:
        vv = self.store.version_vector()
        return Out("DIGEST", None, vv, 8 + 10 * len(vv))

    def _part_out(self, part, kind: str = "DELTA") -> Out:
        d = Delta((part,))
        return Out(kind, None, d, d.size)

    def push_out(self) -> list[Out]:
        """Свои записи после последней рассылки — широковещательными частями."""
        if not self.cfg.push_own:
            return []
        parts = self.store.parts_for(self.uid, self.pushed_to, None, self.cfg.max_part_bytes)
        if parts:
            self.pushed_to = parts[-1].to_incl
        return [self._part_out(p) for p in parts]

    def _rto_for(self, tries: int) -> float:
        return min(self.cfg.rto_max_s, self.rto * self.cfg.rto_backoff ** min(tries - 1, 4))

    def _sources_for(self, origin: str, need: int, t_s: float) -> list[int]:
        """Соседи, у которых есть версия источника выше нашей, в порядке предпочтения:
        сам источник, затем наибольшая версия, затем номер борта (одинаково у всех)."""
        cands = []
        for j, (t_heard, vv) in self.peer_vv.items():
            v = vv.get(origin, 0)
            if t_s - t_heard <= PEER_FRESH_S and v > need:
                own = self.uid_of.get(j) == origin
                cands.append((0 if own else 1, -v, j))
        cands.sort()
        return [j for _, _, j in cands]

    def on_digest_c1(self, t_s: float, src: int, src_uid: str,
                     peer_vv: dict[str, int]) -> list[Out]:
        outs: list[Out] = []
        prev = self.peer_vv.get(src)
        self.peer_vv[src] = (t_s, dict(peer_vv))
        self.uid_of[src] = src_uid
        if prev is None or t_s - prev[0] > PEER_FRESH_S:
            # новый сосед (появился или вернулся после разрыва): сразу свой вектор и короткий
            # сбор векторов остальных — чтобы все запросили часть у одного ответчика
            self.hold_until = max(self.hold_until, t_s + self.cfg.discover_s)
            if t_s >= self.next_trigger:
                self.next_trigger = t_s + 0.05
                outs.append(self.digest_out())
        p_src = {o for o, v in peer_vv.items() if v > 0}
        for origin in p_src:
            p = self.req.get(origin)
            if p is not None and src in p.peers and peer_vv[origin] > p.want_to:
                p.want_to = peer_vv[origin]  # тот же ответчик ушёл вперёд — ждём и это
        if t_s < self.hold_until:
            return outs
        outs += self.request_due(t_s, only=p_src)
        return outs

    def _gap_end(self, origin: str) -> int:
        """Верхняя граница пропуска: начало первой отложенной части (0 — нет)."""
        pk = self.parked.get(origin)
        return min(pk) if pk else 0

    def request_due(self, t_s: float, only: set[str] | None = None,
                    busy: bool = False) -> list[Out]:
        """Запросы недостающего: новые и повторные (срок ожидания истёк).

        Запрос — диапазон (своя версия, конец пропуска]; если отложенных частей нет —
        всё после своей версии. busy — эфир занят: повтор откладывается (при занятом эфире
        он только встал бы в очередь), но не дольше rto_max_s после последнего запроса."""
        self.hold_until = min(self.hold_until, t_s)
        mine = self.store.version_vector()
        best: dict[str, int] = {}
        for j, (t_h, vv) in self.peer_vv.items():
            if t_s - t_h > PEER_FRESH_S:
                continue
            for o, v in vv.items():
                if v > best.get(o, 0):
                    best[o] = v
        per_peer: dict[int, dict[str, tuple[int, int]]] = {}
        for origin in sorted(best):
            if only is not None and origin not in only:
                continue
            have = mine.get(origin, 0)
            if best[origin] <= have:
                self.req.pop(origin, None)
                continue
            p = self.req.get(origin)
            if p is not None and have < p.want_to and t_s < p.deadline:
                continue
            if (p is not None and have < p.want_to and busy and not p.nack
                    and t_s - p.t_sent < self.cfg.rto_max_s):
                p.deadline = t_s + 0.05
                continue
            tries = 1 if p is None or have >= p.want_to else p.tries + 1
            srcs = self._sources_for(origin, have, t_s)
            if not srcs:
                self.req.pop(origin, None)
                continue
            # первая попытка — у лучшего источника, повтор — у retry_fanout соседей
            k = max(1, self.cfg.request_fanout if tries == 1 else self.cfg.retry_fanout)
            shift = 0 if tries <= 2 else (tries - 2) % len(srcs)
            peers = tuple((srcs[shift:] + srcs[:shift])[:k])
            want = max(self.peer_vv[j][1].get(origin, 0) for j in peers)
            self.req[origin] = Pending(want, t_s, t_s + self._rto_for(tries), tries, peers)
            for j in peers:
                per_peer.setdefault(j, {})[origin] = (have, self._gap_end(origin))
        if only is None:
            # срок истёк, а запросить не у кого (соседи пропали) — снять ожидание; запрос
            # возобновится, когда вектор версий придёт от соседа, у которого это есть
            for origin in [o for o, p in self.req.items() if p.deadline <= t_s + 1e-9]:
                del self.req[origin]
        return [Out("REQ", j, per_peer[j], 8 + 14 * len(per_peer[j])) for j in sorted(per_peer)]

    def next_timer(self) -> float | None:
        """Ближайший срок: отправка ответа, повтор запроса, конец сбора векторов."""
        ts = [dl for _, dl in self.outbox.values()]
        ts += [p.deadline for p in self.req.values()]
        if self.hold_until > 0.0:
            ts.append(self.hold_until)
        return min(ts, default=None)

    def hold_due(self, t_s: float) -> bool:
        if 0.0 < self.hold_until <= t_s + 1e-9:
            self.hold_until = 0.0
            return True
        return False

    def on_request_c1(self, t_s: float, req: dict[str, tuple[int, int]]) -> None:
        mine = self.store.version_vector()
        for origin, (d, upto) in req.items():
            if mine.get(origin, 0) <= d:
                continue
            ob = self.outbox.get(origin)
            if ob is None:
                self.outbox[origin] = ([(d, upto)], t_s + self.cfg.response_window_s)
            else:
                ob[0].append((d, upto))

    def flush(self, t_s: float) -> list[tuple[str, int, int, Out]]:
        """Части, срок отправки которых наступил: (источник, от, до, сообщение).

        Запрошенные диапазоны по источнику объединяются; передаются только куски, которых
        нет в эфире. Ограниченный диапазон (дозапрос пропуска) уходит как FIX — впереди
        данных в очереди передачи, иначе пропуск ждал бы за всей очередью ответа."""
        res = []
        mine = self.store.version_vector()
        for origin in sorted(self.outbox):
            ranges, dl = self.outbox[origin]
            if dl > t_s + 1e-9:
                continue
            del self.outbox[origin]
            have = mine.get(origin, 0)
            ivs = sorted((d, have if upto == 0 else min(upto, have), upto != 0)
                         for d, upto in ranges)
            merged: list[list] = []
            for d, hi, fix in ivs:
                if hi <= d:
                    continue
                if merged and d <= merged[-1][1]:
                    m = merged[-1]
                    m[1], m[2] = max(m[1], hi), m[2] and fix
                else:
                    merged.append([d, hi, fix])
            for d, hi, fix in merged:
                for a, b in self.uncovered(origin, d, hi, t_s):
                    parts = self.store.parts_for(origin, a, b, self.cfg.max_part_bytes)
                    kind = "FIX" if fix else "DELTA"
                    for part in parts:
                        res.append((origin, part.from_excl, part.to_incl,
                                    self._part_out(part, kind)))
                    if len(parts) > 1:
                        # замыкающая пустая часть (b, b]: если последняя часть потеряна,
                        # получатель увидит пропуск по ней и дозапросит сразу
                        end = DeltaPart(origin, b, b, ())
                        res.append((origin, b, b, self._part_out(end, kind)))
        return res

    def _on_air(self, origin: str, f: int, to: int, t_done: float) -> list:
        lst = self.air.setdefault(origin, [])
        e = [f, to, t_done]
        lst.append(e)
        return e

    def uncovered(self, origin: str, d: int, hi: int, t_s: float,
                  queued_covers: bool = True) -> list[tuple[int, int]]:
        """Куски диапазона (d, hi], не покрытые тем, что сейчас в эфире или в очереди
        (queued_covers=False — своя часть, ещё стоящая в очереди, не покрывает)."""
        lst = self.air.get(origin)
        if lst:
            lst[:] = [e for e in lst if e[2] >= t_s]
        ivs = sorted((f, to) for f, to, te in (lst or ()) if to > d and f < hi
                     and (queued_covers or te != math.inf))
        out, x = [], d
        for f, to in ivs:
            if f > x:
                out.append((x, min(f, hi)))
            x = max(x, to)
            if x >= hi:
                break
        if x < hi:
            out.append((x, hi))
        return out

    def note_queued(self, origin: str, from_excl: int, to_incl: int) -> list:
        """Своя часть поставлена в очередь: до окончания передачи она «в эфире».
        Возвращает запись, которую note_sent отметит окончанием передачи."""
        return self._on_air(origin, from_excl, to_incl, math.inf)

    @staticmethod
    def note_sent(entry: list, t_done: float) -> None:
        entry[2] = t_done

    def on_delta_c1(self, t_s: float, src: int, delta: Delta) -> list[Out]:
        """Принять части; вернуть немедленные дозапросы пропусков (NACK)."""
        n = 0
        nacks: dict[str, tuple[int, int]] = {}
        for part in delta.parts:
            # услышанная чужая передача: свой ответ по этому источнику не дублировать
            self._on_air(part.origin, part.from_excl, part.to_incl, t_s + HEARD_HOLD_S)
            cur = self.store.version_vector().get(part.origin, 0)
            if part.from_excl > cur:
                pk = self.parked.setdefault(part.origin, {})
                prev_p = pk.get(part.from_excl)
                # после пропуска — отложить до его заполнения; пустая замыкающая часть
                # (b, b] или более короткая часть с тем же началом не вытесняет уже
                # отложенную (иначе отложенные данные терялись и дозапрашивались заново)
                if part.to_incl > part.from_excl and (
                        prev_p is None or part.to_incl > prev_p.to_incl) and (
                        prev_p is not None or len(pk) < self.cfg.park_parts):
                    pk[part.from_excl] = part
                # пропуск обнаружен сразу (доставка от одного борта — по порядку):
                # дозапросить у отправителя только его, не дожидаясь срока ожидания
                gap_end = min(pk) if pk else part.from_excl
                prev = self.nacked.get(part.origin)
                if prev is None or prev[0] != (cur, gap_end) or t_s - prev[1] > self.rto:
                    self.nacked[part.origin] = ((cur, gap_end), t_s)
                    nacks[part.origin] = (cur, gap_end)
                    rq = self.req.get(part.origin)
                    # дозапрос — тоже ожидаемый ответ: таймер не должен его повторить, но
                    # ответ на него (одна-две части) ждётся недолго
                    want = max(part.to_incl, self.peer_vv.get(src, (0, {}))[1]
                               .get(part.origin, 0))
                    if rq is not None:
                        want = max(want, rq.want_to)
                    self.req[part.origin] = Pending(want, t_s, t_s + self.cfg.nack_timeout_s,
                                                    1 if rq is None else rq.tries, (src,), True)
                continue
            k = self.store.apply(Delta((part,))) + self._drain(part.origin)
            n += k
            rq = self.req.get(part.origin)
            if k and rq is not None:
                # данные по источнику идут — ответ в пути, срок ожидания продлевается
                rq.deadline = max(rq.deadline, t_s + self.cfg.nack_timeout_s)
        if n:
            self.dirty = True
        mine = self.store.version_vector()
        if src in self.peer_vv:
            t_h, vv = self.peer_vv[src]
            for part in delta.parts:
                if part.to_incl > vv.get(part.origin, 0):
                    vv[part.origin] = part.to_incl
        for part in delta.parts:
            p = self.req.get(part.origin)
            if p is None or mine.get(part.origin, 0) < p.want_to:
                continue
            if p.tries == 1 and not p.nack and src in p.peers:
                self._rtt_sample(t_s - p.t_sent)
            del self.req[part.origin]
        if not nacks:
            return []
        return [Out("REQ", src, nacks, 8 + 14 * len(nacks))]

    def _drain(self, origin: str) -> int:
        pk = self.parked.get(origin)
        n = 0
        while pk:
            cur = self.store.version_vector().get(origin, 0)
            ready = [f for f in pk if f <= cur]
            if not ready:
                break
            for f in sorted(ready):
                n += self.store.apply(Delta((pk.pop(f),)))
        if pk is not None and not pk:
            del self.parked[origin]
        return n

    def _rtt_sample(self, r: float) -> None:
        self.rtt_samples.append(r)
        if self.srtt is None:
            self.srtt, self.rttvar = r, r / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
            self.srtt = 0.875 * self.srtt + 0.125 * r
        self.rto = min(self.cfg.rto_max_s, max(self.cfg.rto_min_s, self.srtt + 4 * self.rttvar))
