"""Модель сети роя: доступность, задержка, потери, полоса, разделение на подгруппы.

* доступность пары — оба борта работают, расстояние не больше range_m и они в одной
  подгруппе действующего разделения (борт, не указанный в группах разделения, изолирован);
* потери — сообщение теряется целиком с вероятностью loss (дельта применяется атомарно,
  частичной доставки нет);
* полоса — bandwidth_kbps; сообщения уходят по очереди, время передачи = размер / скорость.
  medium = "per_node" — у каждого борта свой канал (модель цикла 0); medium = "shared" —
  общая радиосреда: одна передача в подгруппе в каждый момент, полоса делится между
  всеми бортами подгруппы (mesh-модемы на одной частоте);
* широковещание (broadcast) — одна передача слышна всем доступным соседям, потери у
  каждого получателя независимы; так передаются векторы версий и дельты;
* задержка — latency_ms ± jitter_ms после окончания передачи;
* сообщения одной пары бортов доставляются в порядке отправки (как в радиоканале);
* сообщение, в момент доставки которого пара уже недоступна, теряется.
"""

from __future__ import annotations

import heapq
import math
import random
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


class Network:
    def __init__(self, cfg: NetCfg, rng: random.Random) -> None:
        self.cfg = cfg
        self.rng = rng
        self._q: list[Message] = []
        self._seq = 0
        self._tx_free: dict[object, float] = {}
        self.bytes_sent = 0
        self.msgs_sent = 0
        self.msgs_lost = 0
        self._last: dict[tuple[int, int], float] = {}
        self._air: dict[object, list[tuple[float, float]]] = {}  # интервалы передач канала
        self.deliveries = 0  # доставок получателям (широковещание — по каждому)
        self.positions: dict[int, tuple[float, float]] = {}
        self.alive: set[int] = set()

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

    def _transmit(self, t: float, src: int, size: int) -> float:
        """Занять канал; вернуть момент окончания передачи."""
        ch = self._channel(src, t)
        start = max(t, self._tx_free.get(ch, 0.0))
        end = start + size * 8 / (self.cfg.bandwidth_kbps * 1000.0)
        self._tx_free[ch] = end
        iv = self._air.setdefault(ch, [])
        iv.append((start, end))
        if len(iv) > 64:
            del iv[:32]
        self.bytes_sent += size
        self.msgs_sent += 1
        return end

    def channel_busy(self, src: int, t: float, recent_s: float = 0.0) -> bool:
        """Канал борта занят передачей в момент t или был занят в последние recent_s
        (контроль несущей)."""
        for a, b in reversed(self._air.get(self._channel(src, t), ())):
            if a <= t and b > t - recent_s:
                return True
        return False

    def tx_backlog(self, src: int, t: float) -> float:
        """Сколько секунд канал борта ещё занят ранее поставленными передачами."""
        return max(0.0, self._tx_free.get(self._channel(src, t), 0.0) - t)

    def _enqueue(self, t_end: float, src: int, dst: int, kind: str, payload: Any,
                 size: int) -> None:
        self.deliveries += 1
        if self.rng.random() < self.cfg.loss:
            self.msgs_lost += 1
            return
        lat = max(0.0, self.cfg.latency_ms + self.rng.uniform(-1, 1) * self.cfg.jitter_ms) / 1e3
        t_deliver = max(t_end + lat, self._last.get((src, dst), 0.0))
        self._last[(src, dst)] = t_deliver
        self._seq += 1
        heapq.heappush(self._q, Message(t_deliver, self._seq, src, dst, kind, payload, size))

    def send(self, t: float, src: int, dst: int, kind: str, payload: Any, size: int) -> float | None:
        if not self.reachable(src, dst, t):
            return None
        end = self._transmit(t, src, size)
        self._enqueue(end, src, dst, kind, payload, size)
        return end

    def broadcast(self, t: float, src: int, kind: str, payload: Any, size: int) -> float | None:
        dsts = self.neighbours(src, t)
        if not dsts:
            return None
        end = self._transmit(t, src, size)
        for j in dsts:
            self._enqueue(end, src, j, kind, payload, size)
        return end

    def pop_until(self, t: float):
        while self._q and self._q[0].t_deliver <= t:
            m = heapq.heappop(self._q)
            if self.reachable(m.src, m.dst, m.t_deliver):
                yield m
            else:
                self.msgs_lost += 1
