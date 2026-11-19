"""Параметры сценария (TOML). Значения по умолчанию — базовый модельный сценарий раздела 2
требований: от 8 аппаратов, дистанция между БВС не менее 3 м, рой не более 15 м, скорость
до 200 км/ч, криволинейный маршрут 30 км, без ГНСС, разрывы связи от 15 мин."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class RunCfg:
    name: str = "base"
    seed: int = 7
    duration_s: float = 1080.0
    dt_s: float = 0.1
    sample_s: float = 0.5
    settle_s: float = 3.0  # проверка Р1.4: запись остановлена, обмен продолжается


@dataclass
class SwarmCfg:
    n_uav: int = 8
    speed_kmh: float = 100.0
    altitude_m: float = 150.0
    spacing_m: float = 4.0
    columns: int = 4
    route_length_km: float = 30.0
    wobble_m: float = 0.3


@dataclass
class ObjectsCfg:
    count: int = 60
    lateral_m: float = 300.0
    moving_fraction: float = 0.3
    max_speed_mps: float = 8.0


@dataclass
class SensorCfg:
    frame_hz: float = 5.0
    fov_radius_m: float = 350.0
    r50_m: float = 320.0
    p_near: float = 0.95
    sigma0_m: float = 1.5
    sigma_k: float = 0.8  # м на каждые 100 м наклонной дальности
    false_per_frame: float = 0.05
    class_confusion: float = 0.1


@dataclass
class NavCfg:
    sigma_m: float = 4.0  # СКО ошибки навигации без ГНСС (вход от технологии навигации)
    tau_s: float = 60.0


@dataclass
class NodeCfg:
    publish_period_s: float = 0.25
    picture_period_s: float = 0.25
    gossip_period_s: float = 0.2
    request_timeout_s: float = 0.6  # c0: фиксированный срок ожидания ответа на запрос
    rto_init_s: float = 0.3  # c1: срок ожидания до первых замеров RTT
    rto_min_s: float = 0.12  # c1: границы адаптивного срока ожидания (RFC 6298)
    rto_max_s: float = 1.0
    rto_backoff: float = 1.5  # c1: множитель срока при повторе (потери в радиоканале
    #                           случайные, а не от перегрузки — удвоение TCP избыточно)
    request_fanout: int = 1  # у скольких соседей запрашивать недостающее (первая попытка)
    retry_fanout: int = 2  # у скольких соседей — повтор
    busy_recent_s: float = 0.05  # эфир «занят», если передача шла в последние столько секунд
    response_window_s: float = 0.01  # окно объединения запросов в одну рассылку дельты
    discover_s: float = 0.1  # сбор векторов новых соседей перед запросом (один ответчик)
    nack_timeout_s: float = 0.25  # c1: срок ожидания ответа на дозапрос пропуска и продления
    #                                 при поступлении данных (ответ — одна-две части)
    max_part_bytes: int = 480  # c1: дельта режется на части не больше (одна часть — один пакет)
    park_parts: int = 256  # c1: сколько частей после пропуска хранить до его заполнения
    push_own: bool = True  # сразу рассылать свои новые записи (pull — только для досылки)
    protocol: str = "c1"  # c1 — текущий обмен; c0 — обмен цикла 0 (для сравнения)
    track_lost_s: float = 4.0
    confirm_hits: int = 3
    gate_d2: float = 13.8  # χ²₂(0,999)
    # второй проход: обнаружение вне строба, но рядом с треком, не получившим отметку в
    # этом кадре, продолжает этот трек (иначе открылся бы второй трек того же объекта)
    relaxed_gate_d2: float = 25.0  # χ²₂(1 − 3,7·10⁻⁶); 0 — отключено (как в цикле 0)
    # слияние своих треков: два активных трека борта, чьи оценки совпадают, — один объект;
    # младший закрывается признаком «M» (объединён) и в картину не входит
    merge_d2: float = 6.0  # порог квадрата расстояния Махаланобиса; 0 — отключено
    q_accel: float = 1.0
    init_vel_sigma: float = 5.0


@dataclass
class FusionCfg:
    gate: float = 4.0  # порог расстояния Махаланобиса между треками разных бортов
    class_gate: float = 0.8  # 1 − косинус распределений классов
    ambiguity_margin: float = 2.0  # разница d² лучшего и второго кандидата — неоднозначность
    coast_s: float = 10.0
    vel_sigma: float = 1.0
    stale_sigma_m: float = 5.0
    uav_timeout_s: float = 3.0
    cell_m: float = 150.0
    association: str = "rules"  # стратегия решения «трек — объект» (см. fusion.ASSOCIATORS)


@dataclass
class PartitionCfg:
    start_s: float
    end_s: float
    groups: list[list[int]]


@dataclass
class NetCfg:
    latency_ms: float = 30.0
    jitter_ms: float = 10.0
    loss: float = 0.05
    bandwidth_kbps: float = 500.0
    range_m: float = 3000.0
    medium: str = "per_node"  # per_node — свой канал у борта; shared — общая радиосреда
    mtu_bytes: int = 512  # наибольший пакет канала; сообщение длиннее режется на пакеты
    frag_overhead_bytes: int = 0  # служебные байты канального уровня на пакет
    priority_kinds: list[str] = field(default_factory=lambda: ["DIGEST", "REQ"])
    partition: list[PartitionCfg] = field(default_factory=list)


@dataclass
class EventCfg:
    t_s: float
    kind: str  # fail | join
    uav: int


@dataclass
class Scenario:
    run: RunCfg = field(default_factory=RunCfg)
    swarm: SwarmCfg = field(default_factory=SwarmCfg)
    objects: ObjectsCfg = field(default_factory=ObjectsCfg)
    sensor: SensorCfg = field(default_factory=SensorCfg)
    nav: NavCfg = field(default_factory=NavCfg)
    node: NodeCfg = field(default_factory=NodeCfg)
    fusion: FusionCfg = field(default_factory=FusionCfg)
    net: NetCfg = field(default_factory=NetCfg)
    event: list[EventCfg] = field(default_factory=list)


def _build(cls: type, raw: dict[str, Any]) -> Any:
    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(f"{cls.__name__}: неизвестные параметры {sorted(unknown)}")
    kw = {}
    for name, value in raw.items():
        ftype = known[name].type
        if name == "partition":
            kw[name] = [PartitionCfg(**p) for p in value]
        elif name == "event":
            kw[name] = [EventCfg(**e) for e in value]
        elif isinstance(value, dict):
            sub = {"run": RunCfg, "swarm": SwarmCfg, "objects": ObjectsCfg, "sensor": SensorCfg,
                   "nav": NavCfg, "node": NodeCfg, "fusion": FusionCfg, "net": NetCfg}[name]
            kw[name] = _build(sub, value)
        else:
            kw[name] = value
        del ftype
    return cls(**kw)


def load(path: str | Path) -> Scenario:
    with open(path, "rb") as f:
        return _build(Scenario, tomllib.load(f))


def to_dict(obj: Any) -> Any:
    if is_dataclass(obj):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [to_dict(v) for v in obj]
    return obj
