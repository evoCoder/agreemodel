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
* ответ — широковещательная дельта по источнику от наименьшей запрошенной версии: её
  применяют все соседи, кому она нужна; запросы, пришедшие за окно response_window_s,
  объединяются; если часть уже в эфире (своя или услышанная от соседа), досылается только
  записанное после неё;
* срок ожидания ответа — адаптивный по замерам RTT (RFC 6298: SRTT + 4·RTTVAR, правило
  Карна, умеренное увеличение при повторе), а не фиксированные 0,6 с.
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
        self.pending: dict[str, float] = {}  # c0: источник → срок ожидания ответа на REQ
        # обмен цикла 1
        self.req: dict[str, Pending] = {}
        self.peer_vv: dict[int, tuple[float, dict[str, int]]] = {}
        self.uid_of: dict[int, str] = {}
        self.srtt: float | None = None
        self.rttvar = 0.0
        self.rto = ncfg.rto_init_s
        self.rtt_samples: list[float] = []
        self.outbox: dict[str, tuple[int, float]] = {}  # источник → (от версии, срок отправки)
        self.sent: dict[str, tuple[int, int, float]] = {}  # источник → (от, до, доставлено к)
        self.pushed_to = 0
        self.next_trigger = 0.0
        self.hold_until = 0.0  # до этого момента — сбор векторов новых соседей, без запросов
        self.unreflected_frame_t: float | None = None  # первый кадр, ещё не записанный
        self.published_frame_t: float | None = None  # первый записанный, не отражённый в картине
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

    def on_frame(self, t_s: float, t_us: int, dets: list[Detection]) -> None:
        H = np.zeros((2, 4))
        H[0, 0] = H[1, 1] = 1.0
        active = [tr for tr in self.tracks if tr.status == "A"]
        preds = [self._predict(tr, t_us) for tr in active]
        pairs = []
        for j, d in enumerate(dets):
            z = np.array([d.x, d.y])
            R = np.eye(2) * d.var
            for i, (x, P) in enumerate(preds):
                S = H @ P @ H.T + R
                nu = z - x[:2]
                d2 = float(nu @ np.linalg.solve(S, nu))
                if d2 <= self.cfg.gate_d2:
                    pairs.append((d2, i, j))
        pairs.sort()
        used_t, used_d = set(), set()
        for _, i, j in pairs:
            if i in used_t or j in used_d:
                continue
            used_t.add(i)
            used_d.add(j)
            tr, (x, P), d = active[i], preds[i], dets[j]
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

    def push_out(self) -> Out | None:
        """Свои записи после последней рассылки — одной широковещательной частью."""
        if not self.cfg.push_own:
            return None
        have = self.store.version_vector().get(self.uid, 0)
        if have <= self.pushed_to:
            return None
        d = self.store.delta_for({self.uid: self.pushed_to}, {self.uid})
        self.pushed_to = have
        return Out("DELTA", None, d, d.size)

    def _rto_for(self, tries: int) -> float:
        return min(self.cfg.rto_max_s, self.rto * self.cfg.rto_backoff ** min(tries - 1, 4))

    def _sources_for(self, origin: str, need: int, t_s: float) -> list[int]:
        """Соседи, у которых есть версия источника выше нашей, в порядке предпочтения."""
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
                p.want_to = peer_vv[origin]  # тот же источник ушёл вперёд — ждём и это
        if t_s < self.hold_until:
            return outs
        outs += self.request_due(t_s, only=p_src)
        return outs

    def request_due(self, t_s: float, only: set[str] | None = None,
                    busy: bool = False) -> list[Out]:
        """Запросы недостающего: новые и повторные (срок ожидания истёк).

        busy — канал сейчас занят: повтор откладывается (при занятом эфире он только встал
        бы в очередь), но не дольше rto_max_s после последнего запроса."""
        self.hold_until = min(self.hold_until, t_s)
        mine = self.store.version_vector()
        best: dict[str, int] = {}
        for j, (t_h, vv) in self.peer_vv.items():
            if t_s - t_h > PEER_FRESH_S:
                continue
            for o, v in vv.items():
                if v > best.get(o, 0):
                    best[o] = v
        per_peer: dict[int, dict[str, int]] = {}
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
            if (p is not None and have < p.want_to and busy
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
                per_peer.setdefault(j, {})[origin] = have
        if only is None:
            # срок истёк, а запросить не у кого (соседи пропали) — снять ожидание; запрос
            # возобновится, когда вектор версий придёт от соседа, у которого это есть
            for origin in [o for o, p in self.req.items() if p.deadline <= t_s + 1e-9]:
                del self.req[origin]
        return [Out("REQ", j, per_peer[j], 8 + 10 * len(per_peer[j])) for j in sorted(per_peer)]

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

    def on_request_c1(self, t_s: float, req: dict[str, int]) -> None:
        mine = self.store.version_vector()
        for origin, d in req.items():
            if mine.get(origin, 0) <= d:
                continue
            ob = self.outbox.get(origin)
            if ob is None:
                self.outbox[origin] = (d, t_s + self.cfg.response_window_s)
            else:
                self.outbox[origin] = (min(ob[0], d), ob[1])

    def flush(self, t_s: float) -> list[tuple[str, int, int, Out]]:
        """Дельты, срок отправки которых наступил: (источник, от, до, сообщение).

        Если часть по источнику уже в эфире (своя или услышанная от соседа) и покрывает
        запрошенное начало, передаётся только записанное после неё."""
        res = []
        mine = self.store.version_vector()
        for origin in sorted(self.outbox):
            d, dl = self.outbox[origin]
            if dl > t_s + 1e-9:
                continue
            del self.outbox[origin]
            s = self.sent.get(origin)
            if s is not None and s[0] <= d and t_s <= s[2]:
                if s[1] >= mine.get(origin, 0):
                    continue  # уже в эфире целиком
                d = max(d, s[1])
            delta = self.store.delta_for({origin: d}, {origin})
            if delta:
                part = delta.parts[0]
                res.append((origin, part.from_excl, part.to_incl,
                            Out("DELTA", None, delta, delta.size)))
        return res

    def note_sent(self, origin: str, from_excl: int, to_incl: int, t_done: float) -> None:
        s = self.sent.get(origin)
        if s is not None and s[0] <= from_excl and s[2] >= t_done - 1.0:
            # продолжение уже переданной части — покрытие (s.от, до]
            self.sent[origin] = (s[0], max(s[1], to_incl), max(s[2], t_done))
        else:
            self.sent[origin] = (from_excl, to_incl, t_done)

    def on_delta_c1(self, t_s: float, src: int, delta: Delta) -> int:
        for part in delta.parts:
            # услышанная чужая передача: свой ответ по этому источнику не дублировать
            s = self.sent.get(part.origin)
            if s is None or part.from_excl < s[0] or t_s > s[2]:
                self.sent[part.origin] = (part.from_excl, part.to_incl, t_s + HEARD_HOLD_S)
            elif part.to_incl > s[1]:
                self.sent[part.origin] = (s[0], part.to_incl, max(s[2], t_s + HEARD_HOLD_S))
        n = self.store.apply(delta)
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
            if p.tries == 1 and src in p.peers:
                self._rtt_sample(t_s - p.t_sent)
            del self.req[part.origin]
        return n

    def _rtt_sample(self, r: float) -> None:
        self.rtt_samples.append(r)
        if self.srtt is None:
            self.srtt, self.rttvar = r, r / 2
        else:
            self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - r)
            self.srtt = 0.875 * self.srtt + 0.125 * r
        self.rto = min(self.cfg.rto_max_s, max(self.cfg.rto_min_s, self.srtt + 4 * self.rttvar))
