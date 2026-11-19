"""Прогон сценария: мир → датчики → узлы → сеть; показатели по эталону.

Показатели (Р1.7, Р1.8):
* квант обработки — время от кадра датчика до выдачи обновлённой картины своим бортом
  (наибольшее и 95-й перцентиль); затраты вычислителя на пересчёт картины, мс;
* время восстановления согласованности — от восстановления канала до момента, когда
  каждый работающий борт имеет фиксированный набор изменений, накопленный всеми бортами
  к моменту восстановления (векторы версий не меньше их объединения);
* сходимость (Р1.4) — после окончания сценария запись новых данных останавливается, обмен
  продолжается; время, за которое хранилища и картины всех работающих бортов совпадают;
* потери данных — записи, имеющиеся хотя бы у одного работающего борта, но отсутствующие
  у другого, в конце прогона;
* качество картины по эталону — объекты, дубликаты (несколько объектов картины на один
  объект эталона), ложные и ошибочно объединённые объекты, охват.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .config import Scenario
from .net import Network
from .node import Node
from .sensors import Sensor
from .world import World

EPOCH_US = 1_790_000_000_000_000  # опорная эпоха модельного времени


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


@dataclass
class Restore:
    t_s: float
    kind: str
    target: dict[str, int] | None = None
    store_conv_s: float | None = None


@dataclass
class Result:
    summary: dict[str, Any]
    series: list[dict[str, Any]] = field(default_factory=list)


class Simulation:
    def __init__(self, sc: Scenario) -> None:
        self.sc = sc
        root = random.Random(sc.run.seed)
        self.world = World(sc.swarm, sc.objects, random.Random(root.random()))
        self.net = Network(sc.net, random.Random(root.random()))
        self._sensor_seed = random.Random(root.random())
        self.nodes: dict[int, Node] = {}
        self.sensors: dict[int, Sensor] = {}
        for i in range(sc.swarm.n_uav):
            self._spawn(i)
        self.restores = [Restore(p.end_s, "partition_end") for p in sc.net.partition]
        self.restores += [Restore(e.t_s, f"join U{e.uav + 1:02d}")
                          for e in sc.event if e.kind == "join"]
        self.events = sorted(sc.event, key=lambda e: e.t_s)
        self.series: list[dict[str, Any]] = []
        self.agree_samples = 0
        self.agree_hits = 0
        self.settle_conv_s: float | None = None
        self.c1 = sc.node.protocol == "c1"

    def _spawn(self, i: int) -> None:
        if i not in self.world.uavs:
            self.world.add_uav(i)
        u = self.world.uavs[i]
        self.nodes[i] = Node(i, u.uid, self.sc.node, self.sc.fusion)
        self.sensors[i] = Sensor(self.sc.sensor, self.sc.nav,
                                 random.Random(self._sensor_seed.random() + i))

    def alive(self) -> list[int]:
        return [i for i, u in self.world.uavs.items() if u.alive and i in self.nodes]

    # ------------------------------------------------------------------ обработка
    def _emit(self, t: float, i: int, out, on_done=None) -> bool:
        if out.dst is None:
            return self.net.broadcast(t, i, out.kind, out.payload, out.size, on_done)
        return self.net.send(t, i, out.dst, out.kind, out.payload, out.size, on_done)

    def _emit_part(self, t: float, i: int, origin: str, f: int, to: int, out) -> None:
        n = self.nodes[i]
        lat_max = (self.sc.net.latency_ms + self.sc.net.jitter_ms) / 1e3
        entry = n.note_queued(origin, f, to)
        if not self._emit(t, i, out, on_done=lambda end: n.note_sent(entry, end + lat_max)):
            entry[2] = t  # некому передавать — не держать

    def _timers(self, t: float) -> None:
        """Сроки узлов: отправка объединённых ответов и повторные запросы."""
        for i in self.alive():
            n = self.nodes[i]
            for origin, f, to, out in n.flush(t):
                self._emit_part(t, i, origin, f, to, out)
            if n.hold_due(t) or any(p.deadline <= t + 1e-9 for p in n.req.values()):
                busy = self.net.channel_busy(i, t, self.sc.node.busy_recent_s)
                for out in n.request_due(t, busy=busy):
                    self._emit(t, i, out)

    def _deliver(self, t_end: float) -> None:
        """События сети и сроки узлов по времени до t_end включительно."""
        net = self.net
        while True:
            t_timer = None
            if self.c1:
                due = [dl for i in self.alive() if (dl := self.nodes[i].next_timer()) is not None]
                t_timer = min(due) if due else None
            t_net = net.next_time()
            if t_timer is not None and t_timer <= t_end and (t_net is None or t_timer <= t_net):
                self._timers(t_timer)
                continue
            if t_net is None or t_net > t_end:
                break
            m = net.step(t_net)
            if m is None:
                continue
            node = self.nodes[m.dst]
            t = m.t_deliver
            if self.c1:
                if m.kind == "DIGEST":
                    for out in node.on_digest_c1(t, m.src, self.nodes[m.src].uid, m.payload):
                        self._emit(t, m.dst, out)
                elif m.kind == "REQ":
                    node.on_request_c1(t, m.payload)
                elif m.kind == "DELTA":
                    for out in node.on_delta_c1(t, m.src, m.payload):
                        self._emit(t, m.dst, out)
            else:
                if m.kind == "DIGEST":
                    req = node.on_digest(t, m.payload)
                    if req:
                        net.send(t, m.dst, m.src, "REQ", req, 8 + 10 * len(req))
                elif m.kind == "REQ":
                    delta = node.on_request(m.payload)
                    if delta:
                        net.send(t, m.dst, m.src, "DELTA", delta, delta.size)
                elif m.kind == "DELTA":
                    node.on_delta(m.payload)

    def _event(self, t: float) -> None:
        while self.events and self.events[0].t_s <= t:
            e = self.events.pop(0)
            if e.kind == "fail" and e.uav in self.world.uavs:
                self.world.uavs[e.uav].alive = False
                self.net.drop_queue(e.uav)
            elif e.kind == "join":
                self._spawn(e.uav)

    def _restore_check(self, t: float) -> None:
        alive = self.alive()
        for r in self.restores:
            if r.t_s > t:
                continue
            if r.target is None:
                tgt: dict[str, int] = {}
                for i in alive:
                    for o, v in self.nodes[i].store.version_vector().items():
                        tgt[o] = max(tgt.get(o, 0), v)
                r.target = tgt
            if r.store_conv_s is None:
                ok = all(all(self.nodes[i].store.version_vector().get(o, 0) >= v
                             for o, v in r.target.items()) for i in alive)
                if ok:
                    r.store_conv_s = round(t - r.t_s, 3)

    # ------------------------------------------------------------------ прогон
    def run(self, progress: bool = False) -> Result:
        sc = self.sc
        dt = sc.run.dt_s
        n_steps = int(round(sc.run.duration_s / dt))
        n_settle = int(round(sc.run.settle_s / dt))
        frame_dt = 1.0 / sc.sensor.frame_hz
        next_frame = 0.0
        next_sample = 0.0
        for step in range(n_steps + n_settle):
            t0 = step * dt
            t1 = t0 + dt
            settling = step >= n_steps
            if not settling:
                self._event(t0)
            self.world.step(dt)
            alive = self.alive()
            self.net.alive = set(alive)
            self.net.positions = {i: (self.world.uavs[i].x, self.world.uavs[i].y) for i in alive}
            t_us = EPOCH_US + int(round(t1 * 1e6))
            # сначала — все события сети до конца шага, затем передачи этого шага
            self._deliver(t1)
            self.net.kick(t1)
            if not settling and t1 + 1e-9 >= next_frame:
                next_frame += frame_dt
                for i in alive:
                    dets = self.sensors[i].frame(t_us, self.world.uavs[i], self.world)
                    self.nodes[i].on_frame(t1, t_us, dets)
            for i in alive:
                n = self.nodes[i]
                if not settling and t1 + 1e-9 >= n.next_publish:
                    n.next_publish = t1 + sc.node.publish_period_s
                    s = self.sensors[i]
                    n.publish(t_us, s.nav_estimate(self.world.uavs[i]), s.nav_sigma)
                    if self.c1:
                        for out in n.push_out():
                            part = out.payload.parts[0]
                            self._emit_part(t1, i, part.origin, part.from_excl, part.to_incl, out)
                if t1 + 1e-9 >= n.next_gossip:
                    n.next_gossip += sc.node.gossip_period_s
                    if self.c1:
                        self._emit(t1, i, n.digest_out())
                    else:
                        vv = n.store.version_vector()
                        for j in self.net.neighbours(i, t1):
                            self.net.send(t1, i, j, "DIGEST", vv, 8 + 10 * len(vv))
            for i in alive:
                n = self.nodes[i]
                if t1 + 1e-9 >= n.next_picture:
                    n.next_picture = t1 + sc.node.picture_period_s
                    n.recompute(t1)
            self._restore_check(t1)
            if settling and self.settle_conv_s is None:
                stores = {self.nodes[i].store.digest() for i in alive}
                pics = {self.nodes[i].picture.digest for i in alive if self.nodes[i].picture}
                if len(stores) == 1 and len(pics) == 1:
                    self.settle_conv_s = round(t1 - sc.run.duration_s, 3)
            if t1 + 1e-9 >= next_sample:
                next_sample += sc.run.sample_s
                self._sample(t1, alive)
            if progress and step % int(60 / dt) == 0:
                print(f"  t = {t1:7.1f} с, записей у U01: {len(self.nodes[0].store)}", flush=True)
        return Result(self._summary(), self.series)

    def _components(self, alive: list[int], t: float) -> list[list[int]]:
        seen: set[int] = set()
        comps = []
        for i in alive:
            if i in seen:
                continue
            comp, stack = [], [i]
            seen.add(i)
            while stack:
                k = stack.pop()
                comp.append(k)
                for j in self.net.neighbours(k, t):
                    if j not in seen:
                        seen.add(j)
                        stack.append(j)
            comps.append(sorted(comp))
        return comps

    def _sample(self, t: float, alive: list[int]) -> None:
        comps = self._components(alive, t)
        agree = 0
        for c in comps:
            ds = {self.nodes[i].picture.digest if self.nodes[i].picture else "" for i in c}
            self.agree_samples += 1
            if len(ds) == 1:
                self.agree_hits += 1
                agree += 1
        n0 = self.nodes[alive[0]]
        self.series.append({
            "t_s": round(t, 2),
            "components": len(comps),
            "components_agreed": agree,
            "records_U01": len(n0.store),
            "objects_U01": len(n0.picture.objects) if n0.picture else 0,
            "kbytes_sent": round(self.net.bytes_sent / 1024, 1),
        })

    # ------------------------------------------------------------------ итоги
    def _summary(self) -> dict[str, Any]:
        alive = self.alive()
        union: dict[tuple[str, str], int] = {}
        for i in alive:
            for r in self.nodes[i].store.records():
                union[r.gkey] = max(union.get(r.gkey, 0), r.seq)
        missing = {self.nodes[i].uid: sum(
            1 for k, s in union.items()
            if (rec := self.nodes[i].store.get(*k)) is None or rec.seq < s) for i in alive}
        truth_of: dict[tuple[str, str], str | None] = {}
        for n in self.nodes.values():
            for key, tid in n.truth_of.items():
                truth_of[(n.uid, key)] = tid
        n0 = self.nodes[alive[0]]
        pic = n0.picture
        majority = Counter()
        false_obj = mixed = 0
        for o in pic.objects:
            ts = [truth_of.get(m) for m in o.members]
            real = [x for x in ts if x is not None]
            if not real:
                false_obj += 1
                continue
            top = Counter(real).most_common(1)[0][0]
            majority[top] += 1
            if len(set(real)) > 1:
                mixed += 1
        detected = {v for v in truth_of.values() if v is not None}
        duplicates = sum(c - 1 for c in majority.values())
        digests = {self.nodes[i].picture.digest for i in alive}
        quantum = [q for i in alive for q in self.nodes[i].quantum_s]
        compute = [c for i in alive for c in self.nodes[i].compute_ms]
        return {
            "scenario": self.sc.run.name,
            "seed": self.sc.run.seed,
            "duration_s": self.sc.run.duration_s,
            "uav_alive": len(alive),
            "quantum_s": {"max": round(max(quantum), 3), "p95": round(_pct(quantum, 0.95), 3)},
            "picture_compute_ms": {"p95": round(_pct(compute, 0.95), 2),
                                   "max": round(max(compute), 2)},
            "restores": [{"t_s": r.t_s, "kind": r.kind, "store_conv_s": r.store_conv_s} for r in self.restores],
            "settle_conv_s": self.settle_conv_s,
            "final_pictures_identical": len(digests) == 1,
            "records_union": len(union),
            "records_missing_per_uav": missing,
            "picture": {
                "objects": len(pic.objects),
                "truth_detected": len(detected),
                "truth_covered": len(majority),
                "duplicates": duplicates,
                "false_objects": false_obj,
                "mixed_objects": mixed,
                "conflicts_marked": pic.n_conflicts,
            },
            "net": {"medium": self.sc.net.medium,
                    "protocol": self.sc.node.protocol,
                    "mbytes_sent": round(self.net.bytes_sent / 2**20, 2),
                    # средняя передача одного борта, кбит/с (сравнить с полосой канала)
                    "kbps_per_uav": round(self.net.bytes_sent * 8 / 1e3 / self.sc.run.duration_s
                                          / max(1, self.sc.swarm.n_uav), 1),
                    "msgs_sent": self.net.msgs_sent, "msgs_lost": self.net.msgs_lost,
                    "deliveries": self.net.deliveries,
                    # доля времени, когда эфир занят (для общей среды — загрузка канала)
                    "air_busy_pct": round(100 * self.net.air_s / self.sc.run.duration_s
                                          / (1 if self.sc.net.medium == "shared"
                                             else max(1, self.sc.swarm.n_uav)), 1)},
        }


def run(sc: Scenario, progress: bool = False) -> Result:
    return Simulation(sc).run(progress=progress)
