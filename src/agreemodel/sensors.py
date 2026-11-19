"""Модель бортового датчика обнаружения и навигации без ГНСС.

Обнаружение: вероятность p = p_near · 0,5^((R/R50)²) · …, R — наклонная дальность; объект
в поле зрения, если горизонтальное расстояние не больше fov_radius_m. Ошибка геопривязки
обнаружения: шум σ = σ0 + k·R/100 плюс ошибка навигации своего борта (гаусс-марковский
процесс, ГНСС нет) — у всех обнаружений одного борта она общая, поэтому картины разных
бортов смещены друг относительно друга; это учитывается в ковариации при слиянии.
Ложные срабатывания — пуассоновский поток в поле зрения. Класс — распределение
вероятностей; с вероятностью class_confusion максимум приходится на неверный класс.

``truth_id`` — только для оценки качества по эталону; логика узла его не читает.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .config import NavCfg, SensorCfg
from .world import N_CLASSES, GaussMarkov2D, Uav, World


@dataclass
class Detection:
    t_us: int
    x: float
    y: float
    var: float  # дисперсия по каждой оси, м²
    class_p: tuple[float, ...]
    truth_id: str | None  # только для оценки


def _poisson(rng: random.Random, lam: float) -> int:
    k, p, l_ = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= l_:
            return k
        k += 1


def class_dist(rng: random.Random, true_cls: int, confusion: float) -> tuple[float, ...]:
    top = true_cls
    if rng.random() < confusion:
        top = rng.choice([c for c in range(N_CLASSES) if c != true_cls])
    conf = rng.uniform(0.55, 0.95)
    rest = (1.0 - conf) / (N_CLASSES - 1)
    return tuple(conf if c == top else rest for c in range(N_CLASSES))


class Sensor:
    def __init__(self, cfg: SensorCfg, nav: NavCfg, rng: random.Random) -> None:
        self.cfg = cfg
        self.rng = rng
        self.nav = GaussMarkov2D(rng, nav.sigma_m, nav.tau_s)
        self.nav_sigma = nav.sigma_m
        self.nav_drift = nav.drift
        self._t_us: int | None = None

    def nav_estimate(self, u: Uav) -> tuple[float, float]:
        """Оценка своего положения (навигация без ГНСС): истина плюс ошибка."""
        return u.x + self.nav.x, u.y + self.nav.y

    def frame(self, t_us: int, u: Uav, world: World) -> list[Detection]:
        c, rng = self.cfg, self.rng
        if self.nav_drift and self._t_us is not None:
            self.nav.step((t_us - self._t_us) / 1e6)
        self._t_us = t_us
        out = []
        r2max = c.fov_radius_m**2
        for o in world.objects:
            dx, dy = o.x - u.x, o.y - u.y
            d2 = dx * dx + dy * dy
            if d2 > r2max:
                continue
            slant = math.sqrt(d2 + u.z * u.z)
            if rng.random() > c.p_near * 0.5 ** ((slant / c.r50_m) ** 2):
                continue
            sig = c.sigma0_m + c.sigma_k * slant / 100.0
            out.append(Detection(
                t_us,
                o.x + self.nav.x + rng.gauss(0, sig),
                o.y + self.nav.y + rng.gauss(0, sig),
                sig * sig,
                class_dist(rng, o.cls, c.class_confusion),
                o.oid))
        for _ in range(_poisson(rng, c.false_per_frame)):
            a = rng.uniform(0, 2 * math.pi)
            r = c.fov_radius_m * math.sqrt(rng.random())
            sig = c.sigma0_m + c.sigma_k * math.hypot(r, u.z) / 100.0
            out.append(Detection(
                t_us, u.x + self.nav.x + r * math.cos(a), u.y + self.nav.y + r * math.sin(a),
                sig * sig, class_dist(rng, rng.randrange(N_CLASSES), 0.0), None))
        return out
