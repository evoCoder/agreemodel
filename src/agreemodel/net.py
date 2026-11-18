"""Модель сети роя: доступность, задержка, потери, полоса, разделение на подгруппы.

* доступность пары — оба борта работают, расстояние не больше range_m и они в одной
  подгруппе действующего разделения (борт, не указанный в группах разделения, изолирован);
* потери — сообщение теряется целиком с вероятностью loss (дельта применяется атомарно,
  частичной доставки нет);
* полоса — у каждого борта передатчик с маркерным ведром bandwidth_kbps: сообщения
  уходят по очереди, время передачи = размер / скорость;
* задержка — latency_ms ± jitter_ms после окончания передачи;
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
        self._tx_free: dict[int, float] = {}
        self.bytes_sent = 0
        self.msgs_sent = 0
        self.msgs_lost = 0
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

    def send(self, t: float, src: int, dst: int, kind: str, payload: Any, size: int) -> None:
        if not self.reachable(src, dst, t):
            return
        start = max(t, self._tx_free.get(src, 0.0))
        tx = size * 8 / (self.cfg.bandwidth_kbps * 1000.0)
        self._tx_free[src] = start + tx
        self.bytes_sent += size
        self.msgs_sent += 1
        if self.rng.random() < self.cfg.loss:
            self.msgs_lost += 1
            return
        lat = max(0.0, self.cfg.latency_ms + self.rng.uniform(-1, 1) * self.cfg.jitter_ms) / 1e3
        self._seq += 1
        heapq.heappush(self._q, Message(start + tx + lat, self._seq, src, dst, kind, payload, size))

    def pop_until(self, t: float):
        while self._q and self._q[0].t_deliver <= t:
            m = heapq.heappop(self._q)
            if self.reachable(m.src, m.dst, m.t_deliver):
                yield m
            else:
                self.msgs_lost += 1
