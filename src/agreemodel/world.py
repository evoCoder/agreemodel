"""Модельный мир (эталон истины): маршрут, рой БВС, объекты интереса.

Эталон недоступен логике бортов: узлы получают только обнаружения своих датчиков и
сообщения сети. По эталону вычисляются показатели качества (metrics.py).

Рой: виртуальный ведущий идёт по криволинейному маршруту с постоянной скоростью, каждый
БВС держит место в строю (сетка columns × rows с шагом spacing_m) с малым блужданием.
При 8 БВС и шаге 4 м строй 12 × 4 м — укладывается в 15 м, дистанция не менее 3 м.
"""

from __future__ import annotations

import math
import random
from bisect import bisect_right
from dataclasses import dataclass

from .config import ObjectsCfg, SwarmCfg

CLASSES = ("vehicle", "truck", "person", "uav", "other")
N_CLASSES = len(CLASSES)


class GaussMarkov2D:
    """Двумерный гаусс-марковский процесс первого порядка (медленно меняющаяся ошибка)."""

    def __init__(self, rng: random.Random, sigma: float, tau_s: float) -> None:
        self.rng, self.sigma, self.tau = rng, sigma, tau_s
        self.x = rng.gauss(0, sigma)
        self.y = rng.gauss(0, sigma)

    def step(self, dt: float) -> tuple[float, float]:
        a = math.exp(-dt / self.tau)
        s = self.sigma * math.sqrt(1 - a * a)
        self.x = a * self.x + s * self.rng.gauss(0, 1)
        self.y = a * self.y + s * self.rng.gauss(0, 1)
        return self.x, self.y


class Route:
    """Плавная кривая: курс — сумма синусоид от пройденного пути, точки через 25 м."""

    def __init__(self, rng: random.Random, length_m: float, step_m: float = 25.0) -> None:
        h0 = rng.uniform(0, 2 * math.pi)
        comps = [(rng.uniform(0.3, 0.6), rng.uniform(4000, 9000), rng.uniform(0, 6.3)),
                 (rng.uniform(0.1, 0.3), rng.uniform(1500, 3000), rng.uniform(0, 6.3))]
        self.s = [0.0]
        self.pts = [(0.0, 0.0)]
        x = y = 0.0
        n = int(length_m / step_m)
        for i in range(n):
            s = i * step_m
            h = h0 + sum(a * math.sin(2 * math.pi * s / lam + ph) for a, lam, ph in comps)
            x += step_m * math.cos(h)
            y += step_m * math.sin(h)
            self.pts.append((x, y))
            self.s.append((i + 1) * step_m)
        self.length = self.s[-1]

    def at(self, s: float) -> tuple[float, float, float]:
        """Точка и курс на расстоянии s от начала."""
        s = min(max(s, 0.0), self.length)
        i = min(bisect_right(self.s, s) - 1, len(self.pts) - 2)
        (x0, y0), (x1, y1) = self.pts[i], self.pts[i + 1]
        f = (s - self.s[i]) / (self.s[i + 1] - self.s[i])
        return x0 + f * (x1 - x0), y0 + f * (y1 - y0), math.atan2(y1 - y0, x1 - x0)


@dataclass
class Uav:
    idx: int
    uid: str
    fwd: float
    right: float
    alive: bool = True
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    wobble: GaussMarkov2D | None = None


@dataclass
class GroundObject:
    oid: str
    cls: int
    x: float
    y: float
    speed: float
    heading: float


class World:
    def __init__(self, swarm: SwarmCfg, objects: ObjectsCfg, rng: random.Random) -> None:
        self.cfg = swarm
        self.rng = rng
        self.route = Route(rng, swarm.route_length_km * 1000.0)
        self.speed = swarm.speed_kmh / 3.6
        self.t = 0.0
        self.uavs: dict[int, Uav] = {}
        for i in range(swarm.n_uav):
            self.add_uav(i)
        self.objects = self._make_objects(objects)

    # ------------------------------------------------------------------ рой
    def slot(self, i: int) -> tuple[float, float]:
        cols = self.cfg.columns
        rows = max(1, math.ceil(self.cfg.n_uav / cols))
        # присоединяющийся борт (номер ≥ n_uav) занимает новую ячейку за строем
        r, c = divmod(i, cols)
        sp = self.cfg.spacing_m
        return (-(r - (rows - 1) / 2) * sp, (c - (cols - 1) / 2) * sp)

    def add_uav(self, i: int) -> Uav:
        fwd, right = self.slot(i)
        u = Uav(i, f"U{i + 1:02d}", fwd, right,
                wobble=GaussMarkov2D(self.rng, self.cfg.wobble_m, 5.0))
        self.uavs[i] = u
        self._place(u)
        return u

    def _place(self, u: Uav) -> None:
        lx, ly, h = self.route.at(self.speed * self.t)
        wx, wy = u.wobble.x, u.wobble.y
        u.x = lx + u.fwd * math.cos(h) + u.right * math.sin(h) + wx
        u.y = ly + u.fwd * math.sin(h) - u.right * math.cos(h) + wy
        u.z = self.cfg.altitude_m

    # ------------------------------------------------------------------ объекты
    def _make_objects(self, cfg: ObjectsCfg) -> list[GroundObject]:
        out: list[GroundObject] = []
        while len(out) < cfg.count:
            s = self.rng.uniform(0.02, 0.98) * self.route.length
            x, y, h = self.route.at(s)
            off = self.rng.uniform(-cfg.lateral_m, cfg.lateral_m)
            moving = self.rng.random() < cfg.moving_fraction
            cls = self.rng.choices(range(N_CLASSES), weights=(5, 2, 3, 0.5, 1))[0]
            cx, cy = x - off * math.sin(h), y + off * math.cos(h)
            speed = self.rng.uniform(1.0, cfg.max_speed_mps) if moving else 0.0
            heading = self.rng.uniform(0, 2 * math.pi)
            out.append(GroundObject(f"T{len(out) + 1:03d}", cls, cx, cy, speed, heading))
            # остальные объекты группы: рядом, того же или соседнего класса, общий курс
            for _ in range(min(cfg.cluster_size, cfg.count - len(out) + 1) - 1):
                a = self.rng.uniform(0, 2 * math.pi)
                r = cfg.cluster_spread_m * math.sqrt(self.rng.random())
                c = cls if self.rng.random() < 0.7 else self.rng.randrange(N_CLASSES)
                out.append(GroundObject(f"T{len(out) + 1:03d}", c, cx + r * math.cos(a),
                                        cy + r * math.sin(a), speed, heading))
        return out

    # ------------------------------------------------------------------ шаг
    def step(self, dt: float) -> None:
        self.t += dt
        for u in self.uavs.values():
            if u.alive:
                u.wobble.step(dt)
                self._place(u)
        for o in self.objects:
            if o.speed > 0:
                o.heading += self.rng.gauss(0, 0.05 * math.sqrt(dt))
                o.x += o.speed * math.cos(o.heading) * dt
                o.y += o.speed * math.sin(o.heading) * dt
