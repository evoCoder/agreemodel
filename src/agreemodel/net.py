"""Модель сети роя: доступность, радиоканал, разделение на подгруппы (Р1.8).

Событийная модель канала:
* доступность пары — оба борта работают, расстояние не больше range_m и они в одной
  подгруппе действующего разделения (борт, не указанный в группах разделения, изолирован);
* у каждого борта своя очередь передачи; служебные сообщения (priority_kinds: векторы
  версий, запросы) встают впереди данных; сообщение режется на пакеты не больше
  mtu_bytes (к каждому пакету добавляется frag_overhead_bytes служебных байт);
* канал: medium = "shared" — общая радиосреда подгруппы (mesh-модемы на одной частоте):
  в каждый момент передаёт один борт, эфир делится между бортами с непустой очередью
  по пакетам по очереди (круговой порядок); medium = "per_node" — у каждого борта
  свой канал полосой bandwidth_kbps (идеализация, модель цикла 0);
* время передачи пакета = размер / bandwidth_kbps; пакет, начатый, не прерывается;
* широковещание — одна передача слышна всем доступным соседям; адресная передача —
  одному получателю; подтверждений и повторов на канальном уровне нет: пакет теряется
  у каждого получателя независимо с вероятностью loss, сообщение доставляется, если
  получены все его пакеты;
* доставка — через latency_ms ± jitter_ms после окончания последнего пакета; сообщения
  одной пары бортов доставляются в порядке отправки; получатели определяются в момент
  окончания передачи; сообщение, в момент доставки которого пара недоступна, теряется.
"""

from __future__ import annotations

import heapq
import math
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import NetCfg


@dataclass(order=True)
class Message:
    t_deliver: float
    seq: int
    src: int = field(compare=False)
    dst: int = field(compare=False)
    kind: str = field(compare=False)
    payload: Any = field(compare=False)
    size: int = field(compare=False)


@dataclass
class _Out:
    t_enq: float
    src: int
    dst: int | None  # None — широковещание
    kind: str
    payload: Any
    size: int
    on_done: Callable[[float], None] | None
    frags: list[int]
    sent: int = 0
    active: bool = False  # пакет этого сообщения сейчас в эфире
    # проверка перед началом передачи: False — сообщение устарело и снимается с очереди
    valid: Callable[[float], bool] | None = None


@dataclass
class _Chan:
    busy_until: float = 0.0
    current: _Out | None = None
    last_src: int = -1
    air: deque = field(default_factory=deque)  # (начало, конец) недавних передач


class Network:
    def __init__(self, cfg: NetCfg, rng: random.Random) -> None:
        self.cfg = cfg
        self.rng = rng
        self._q: list[Message] = []  # доставки
        self._ev: list[tuple[float, int, object]] = []  # окончания пакетов: (t, n, канал)
        self._seq = 0
        self._queues: dict[int, deque[_Out]] = {}
        self._chans: dict[object, _Chan] = {}
        self._last: dict[tuple[int, int], float] = {}
        self.bytes_sent = 0  # байт в эфире, включая служебные байты пакетов
        self.msgs_sent = 0  # передач (широковещание — одна)
        self.frags_sent = 0
        self.msgs_lost = 0  # недоставок получателям
        self.deliveries = 0  # доставок получателям (широковещание — по каждому)
        self.msgs_dropped = 0  # снято с очереди как устаревшее
        self.air_s = 0.0  # суммарное время занятости эфира
        self.positions: dict[int, tuple[float, float]] = {}
        self.alive: set[int] = set()

    # ------------------------------------------------------------------ топология
    def group_of(self, i: int, t: float) -> int | None:
        for p in self.cfg.partition:
            if p.start_s <= t < p.end_s:
                for g, members in enumerate(p.groups):
                    if i in members:
                        return g
                return -1 - i  # не указан — изолирован
        return None

    def reachable(self, i: int, j: int, t: float) -> bool:
        if i == j or i not in self.alive or j not in self.alive:
            return False
        gi, gj = self.group_of(i, t), self.group_of(j, t)
        if gi != gj:
            return False
        (xi, yi), (xj, yj) = self.positions[i], self.positions[j]
        return math.hypot(xi - xj, yi - yj) <= self.cfg.range_m

    def neighbours(self, i: int, t: float) -> list[int]:
        return [j for j in sorted(self.alive) if self.reachable(i, j, t)]

    def _channel(self, src: int, t: float) -> object:
        if self.cfg.medium == "shared":
            return ("g", self.group_of(src, t))
        return src

    # ------------------------------------------------------------------ передача
    def _frags(self, size: int) -> list[int]:
        mtu = max(1, self.cfg.mtu_bytes)
        n = max(1, math.ceil(size / mtu))
        sizes = [mtu] * (n - 1) + [size - mtu * (n - 1)]
        return [s + self.cfg.frag_overhead_bytes for s in sizes]

    def _enqueue_out(self, t: float, out: _Out) -> None:
        if out.src not in self.alive:
            return
        q = self._queues.setdefault(out.src, deque())
        if out.kind in self.cfg.priority_kinds:
            # служебное сообщение — впереди данных, ещё не начатых передачей
            k = 0
            while k < len(q) and (q[k].kind in self.cfg.priority_kinds
                                  or q[k].sent > 0 or q[k].active):
                k += 1
            q.insert(k, out)
        else:
            q.append(out)
        ch_key = self._channel(out.src, t)
        ch = self._chans.setdefault(ch_key, _Chan())
        if ch.current is None:
            self._start_next(ch_key, ch, t)

    def send(self, t: float, src: int, dst: int, kind: str, payload: Any, size: int,
             on_done: Callable[[float], None] | None = None,
             valid: Callable[[float], bool] | None = None) -> bool:
        if not self.reachable(src, dst, t):
            return False
        self._enqueue_out(t, _Out(t, src, dst, kind, payload, size, on_done, self._frags(size),
                                  valid=valid))
        return True

    def broadcast(self, t: float, src: int, kind: str, payload: Any, size: int,
                  on_done: Callable[[float], None] | None = None) -> bool:
        if not self.neighbours(src, t):
            return False
        self._enqueue_out(t, _Out(t, src, None, kind, payload, size, on_done, self._frags(size)))
        return True

    def _members(self, ch_key: object, t: float) -> list[int]:
        if self.cfg.medium != "shared":
            return [ch_key] if isinstance(ch_key, int) else []
        return [i for i in sorted(self._queues) if self._queues[i]
                and self._channel(i, t) == ch_key]

    def _start_next(self, ch_key: object, ch: _Chan, t: float) -> None:
        """Выбрать следующий пакет канала: борта с непустой очередью — по кругу.
        Сообщение, ещё не начатое и устаревшее к моменту передачи (valid → False),
        снимается с очереди без передачи."""
        for i in self._members(ch_key, t):
            q = self._queues.get(i)
            while q and q[0].sent == 0 and not q[0].active and q[0].valid is not None \
                    and not q[0].valid(t):
                q.popleft()
                self.msgs_dropped += 1
        cands = [i for i in self._members(ch_key, t)
                 if i in self.alive and self._queues.get(i) and not self._queues[i][0].active]
        # борт, ушедший из подгруппы или отказавший, свою очередь в этот канал не отдаёт
        if not cands:
            ch.current = None
            return
        after = [i for i in cands if i > ch.last_src]
        src = after[0] if after else cands[0]
        out = self._queues[src][0]
        out.active = True
        size = out.frags[out.sent]
        end = t + size * 8 / (self.cfg.bandwidth_kbps * 1000.0)
        ch.current, ch.busy_until, ch.last_src = out, end, src
        ch.air.append((t, end))
        while ch.air and ch.air[0][1] < t - 2.0:
            ch.air.popleft()
        self.bytes_sent += size
        self.frags_sent += 1
        self.air_s += end - t
        self._seq += 1
        heapq.heappush(self._ev, (end, self._seq, ch_key))

    def _finish(self, t: float, ch_key: object) -> None:
        ch = self._chans[ch_key]
        out = ch.current
        if out is not None and ch.busy_until <= t + 1e-12:
            out.active = False
            out.sent += 1
            q = self._queues.get(out.src)
            if out.src not in self.alive or not q or q[0] is not out:
                pass  # борт отказал во время передачи — сообщение пропало
            elif out.sent == len(out.frags):
                q.popleft()
                self._complete(t, out)
            ch.current = None
        if ch.current is None:
            self._start_next(ch_key, ch, t)

    def _complete(self, t: float, out: _Out) -> None:
        self.msgs_sent += 1
        if out.on_done is not None:
            out.on_done(t)
        dsts = self.neighbours(out.src, t) if out.dst is None else (
            [out.dst] if self.reachable(out.src, out.dst, t) else [])
        p_ok = (1.0 - self.cfg.loss) ** len(out.frags)
        for j in dsts:
            self.deliveries += 1
            if self.rng.random() >= p_ok:
                self.msgs_lost += 1
                continue
            lat = max(0.0, self.cfg.latency_ms
                      + self.rng.uniform(-1, 1) * self.cfg.jitter_ms) / 1e3
            t_deliver = max(t + lat, self._last.get((out.src, j), 0.0))
            self._last[(out.src, j)] = t_deliver
            self._seq += 1
            heapq.heappush(self._q, Message(t_deliver, self._seq, out.src, j, out.kind,
                                            out.payload, out.size))

    def kick(self, t: float) -> None:
        """Запустить простаивающие каналы, у бортов которых есть очередь (после смены
        состава подгрупп очередь борта переходит в канал новой подгруппы)."""
        for i in sorted(self._queues):
            if self._queues[i] and i in self.alive:
                key = self._channel(i, t)
                ch = self._chans.setdefault(key, _Chan())
                if ch.current is None:
                    self._start_next(key, ch, t)

    def drop_queue(self, i: int) -> None:
        """Отказ борта: его очередь передачи пропадает."""
        q = self._queues.get(i)
        if q:
            q.clear()

    # ------------------------------------------------------------------ события
    def next_time(self) -> float | None:
        ts = []
        if self._q:
            ts.append(self._q[0].t_deliver)
        if self._ev:
            ts.append(self._ev[0][0])
        return min(ts) if ts else None

    def step(self, t: float) -> Message | None:
        """Обработать одно событие не позже t; вернуть доставленное сообщение или None."""
        t_ev = self._ev[0][0] if self._ev else math.inf
        t_q = self._q[0].t_deliver if self._q else math.inf
        if t_ev <= t_q and t_ev <= t:
            _, _, ch_key = heapq.heappop(self._ev)
            self._finish(t_ev, ch_key)
            return None
        if t_q <= t:
            m = heapq.heappop(self._q)
            if self.reachable(m.src, m.dst, m.t_deliver):
                return m
            self.msgs_lost += 1
        return None

    def channel_busy(self, src: int, t: float, recent_s: float = 0.0) -> bool:
        """Эфир у борта занят передачей в момент t или был занят в последние recent_s
        (контроль несущей); своя непустая очередь — тоже занят."""
        if self._queues.get(src):
            return True
        ch = self._chans.get(self._channel(src, t))
        if ch is None:
            return False
        for a, b in reversed(ch.air):
            if a <= t and b > t - recent_s:
                return True
        return False
