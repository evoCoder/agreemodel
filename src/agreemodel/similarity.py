"""Модель сходства наблюдений (ИИ): вероятность того, что трек и объект картины — один объект.

Стратегия ассоциации ``ai`` (``fusion.ASSOCIATORS``). Решение «присоединить трек к
объекту или открыть новый» принимается по вероятности p, а не по фиксированному порогу
расстояния Махаланобиса: трек присоединяется к объекту с наибольшей p, если p ≥ p_min.

Признаки пары (``pair_features``) — положение в опорный момент и на общем интервале
наблюдения, разрыв и перекрытие по времени, скорости, классы, число отметок, устаревание.
Запрет правил «два трека одного борта, наблюдавшихся одновременно» сохраняется как
жёсткое ограничение (датчик даёт одно обнаружение объекта за кадр).

Обучение — вне борта (``tools/train.py``, готовые библиотеки) на эталоне симулятора.
На борту — только вывод: многослойный перцептрон с весами, квантованными до int8 (по
слою — масштаб), на чистом Python без numpy и фреймворков. Вероятность округляется до
шага 1e-4 перед сравнением — одинаковое решение на всех бортах (картина — детерминированная
функция хранилища, Р-03).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import FusionCfg
    from .fusion import PicObject, TrackView
    from .store import Record

FEATURES = (
    "log_d2",        # ln(1 + d²) в опорный момент (как у правил)
    "log_dist",      # ln(1 + расстояние, м) в опорный момент
    "sigma",         # ед. СКО суммарной ковариации, м
    "log_dist_al",   # ln(1 + расстояние, м) на общем моменте с ближайшим по времени членом объекта
    "log_d2_al",     # ln(1 + d²) там же
    "log_gap",       # ln(1 + разрыв по времени, с) между треком и объектом (0 — пересекаются)
    "log_overlap",   # ln(1 + время одновременного наблюдения другими бортами, с)
    "cls_dist",      # 1 − косинус распределений классов
    "speed_trk",     # скорость трека, м/с
    "speed_obj",     # скорость ближайшего по времени члена объекта, м/с
    "dspeed",        # модуль разности векторов скорости, м/с
    "log_hits",      # ln(число отметок трека)
    "log_members",   # ln(число треков в объекте)
    "stale_trk",     # трек потерян или устарел (1/0)
    "stale_obj",     # ближайший член объекта потерян или устарел (1/0)
    "n_origins",     # число бортов в объекте
    "rival_closer",  # у того же борта есть другой трек того же времени, который ближе к объекту (1/0)
    "log_rival_d2",  # ln(1 + d²) этого соперника до объекта (ln 65, если соперника нет)
)
D2_PREFILTER = 64.0  # дальше этого кандидат не рассматривается (у правил порог 16)
ALIGN_MAX_S = 30.0   # дальше этого трек не переносится по скорости к общему моменту
P_STEP = 1e-4

DEFAULT_MODEL = Path(__file__).with_name("models") / "sim_v1.json"


def _mahal2(dx: float, dy: float, S: tuple[float, float, float]) -> float:
    a, b, c = S
    det = a * c - b * b
    if det <= 1e-12:
        return float("inf")
    return (c * dx * dx - 2 * b * dx * dy + a * dy * dy) / det


def _cos_dist(p: tuple[float, ...] | list[float], q: tuple[float, ...] | list[float]) -> float:
    num = sum(a * b for a, b in zip(p, q, strict=True))
    den = math.sqrt(sum(a * a for a in p) * sum(b * b for b in q))
    return 1.0 - num / den if den > 0 else 1.0


def _stale(d: dict[str, Any], t_ref: int, coast_s: float) -> bool:
    return d["st"] != "A" or (t_ref - d["t1"]) / 1e6 > coast_s


def pair_features(obj: PicObject, trk: TrackView, members: list[Record], t_ref: int,
                  cfg: FusionCfg, rivals: list[TrackView] = ()) -> list[float] | None:
    """Признаки пары «объект картины — трек» или None, если присоединение невозможно.

    rivals — треки того же борта (все, в том числе ещё не разобранные): если борт в то же
    время сопровождал другой трек, лучше подходящий к объекту, этот трек — скорее соседний
    объект. Признак не зависит от порядка разбора треков."""
    iv = obj._intervals.get(trk.rec.origin)
    if iv and any(a <= trk.t1 and trk.t0 <= b for a, b in iv):
        return None  # борт видел оба одновременно — разные объекты
    S = (obj.P[0] + trk.P[0], obj.P[1] + trk.P[1], obj.P[2] + trk.P[2])
    dx, dy = trk.x - obj.x, trk.y - obj.y
    d2 = _mahal2(dx, dy, S)
    if d2 > D2_PREFILTER:
        return None
    td = trk.rec.data
    # член объекта, ближайший к треку по времени, и перекрытие с другими бортами
    best, best_gap, overlap = None, float("inf"), 0.0
    for m in members:
        md = m.data
        gap = max(0, md["t0"] - td["t1"], td["t0"] - md["t1"]) / 1e6
        if gap < best_gap or (gap == best_gap and best is not None and md["t1"] > best.data["t1"]):
            best, best_gap = m, gap
        if m.origin != trk.rec.origin:
            overlap += max(0, min(md["t1"], td["t1"]) - max(md["t0"], td["t0"])) / 1e6
    md = best.data
    # общий момент: конец более раннего трека; более поздний переносится назад по скорости
    tau = min(md["t1"], td["t1"])

    def at(d: dict[str, Any]) -> tuple[float, float, float]:
        dt = min((d["t1"] - tau) / 1e6, ALIGN_MAX_S)
        return (d["x"] - d["vx"] * dt, d["y"] - d["vy"] * dt, (cfg.vel_sigma * dt) ** 2)

    rival = D2_PREFILTER + 1
    for o in rivals:
        if o.rec is trk.rec or not (o.t0 <= trk.t1 and trk.t0 <= o.t1):
            continue
        So = (obj.P[0] + o.P[0], obj.P[1] + o.P[1], obj.P[2] + o.P[2])
        rival = min(rival, _mahal2(o.x - obj.x, o.y - obj.y, So))
    ax, ay, ag = at(md)
    bx, by, bg = at(td)
    Sa = (md["pxx"] + td["pxx"] + ag + bg, md["pxy"] + td["pxy"], md["pyy"] + td["pyy"] + ag + bg)
    d2a = _mahal2(bx - ax, by - ay, Sa)
    return [
        math.log1p(min(d2, D2_PREFILTER)),
        math.log1p(math.hypot(dx, dy)),
        math.sqrt((S[0] + S[2]) / 2),
        math.log1p(math.hypot(bx - ax, by - ay)),
        math.log1p(min(d2a, 1e4)),
        math.log1p(best_gap),
        math.log1p(overlap),
        _cos_dist(obj.cls, trk.cls),
        math.hypot(td["vx"], td["vy"]),
        math.hypot(md["vx"], md["vy"]),
        math.hypot(td["vx"] - md["vx"], td["vy"] - md["vy"]),
        math.log(max(td["n"], 1)),
        math.log(len(members)),
        float(_stale(td, t_ref, cfg.coast_s)),
        float(_stale(md, t_ref, cfg.coast_s)),
        float(len({m.origin for m in members})),
        float(rival < d2),
        math.log1p(min(rival, D2_PREFILTER + 1)),
    ]


def track_views_by_origin(tracks: list[Record], t_ref: int, cfg: FusionCfg) -> dict[str, list[TrackView]]:
    """Треки, приведённые к опорному времени, по бортам-источникам (для признака соперника)."""
    from .fusion import TrackView, _track_state

    out: dict[str, list[TrackView]] = {}
    for r in tracks:
        x, y, P = _track_state(r, t_ref, cfg)
        out.setdefault(r.origin, []).append(
            TrackView(r, x, y, P, r.data["t0"], r.data["t1"], tuple(r.data["cls"])))
    return out


class Model:
    """Перцептрон: стандартизация → [Wx + b, ReLU]* → Wx + b → сигмоида. Веса — int8 · масштаб."""

    def __init__(self, spec: dict[str, Any]) -> None:
        if tuple(spec["features"]) != FEATURES:
            raise ValueError("признаки модели не совпадают с similarity.FEATURES")
        self.name = spec["name"]
        self.p_min = float(spec["p_min"])
        self.mean = [float(v) for v in spec["mean"]]
        self.scale = [float(v) for v in spec["scale"]]
        self.layers = []
        for L in spec["layers"]:
            s = float(L["scale"])
            W = [[q * s for q in row] for row in L["w_int8"]]
            self.layers.append((W, [float(v) for v in L["b"]]))

    @classmethod
    def load(cls, path: str | Path = DEFAULT_MODEL) -> Model:
        return cls(json.loads(Path(path).read_text("utf-8")))

    def prob(self, f: list[float]) -> float:
        h = [(v - m) / s for v, m, s in zip(f, self.mean, self.scale, strict=True)]
        last = len(self.layers) - 1
        for i, (W, b) in enumerate(self.layers):
            z = [sum(w * x for w, x in zip(row, h, strict=True)) + bb for row, bb in zip(W, b, strict=True)]
            h = z if i == last else [v if v > 0 else 0.0 for v in z]
        z = max(-30.0, min(30.0, h[0]))
        p = 1.0 / (1.0 + math.exp(-z))
        return round(p / P_STEP) * P_STEP


_CACHE: dict[str, Model] = {}


def get_model(path: str | Path | None = None) -> Model:
    key = str(path or DEFAULT_MODEL)
    if key not in _CACHE:
        _CACHE[key] = Model.load(key)
    return _CACHE[key]


class AiAssociator:
    """Стратегия ``ai``: цена = 1 − p; трек присоединяется к объекту с наибольшей p ≥ p_min."""

    def __init__(self, cfg: FusionCfg) -> None:
        self.cfg = cfg
        self.model = get_model(getattr(cfg, "ai_model", "") or None)
        p_min = getattr(cfg, "ai_p_min", 0.0) or self.model.p_min
        self.threshold = 1.0 - p_min
        self.margin = 0.1  # близкие вероятности двух кандидатов — неоднозначность
        self.recs: dict[tuple[str, str], Record] = {}
        self.t_ref = 0

    def begin(self, tracks: list[Record], t_ref: int) -> None:
        self.recs = {r.gkey: r for r in tracks}
        self.t_ref = t_ref
        self.by_origin = track_views_by_origin(tracks, t_ref, self.cfg)

    def cost(self, obj: PicObject, trk: TrackView) -> float | None:
        members = [self.recs[g] for g in obj.members]
        f = pair_features(obj, trk, members, self.t_ref, self.cfg,
                          self.by_origin.get(trk.rec.origin, ()))
        if f is None:
            return None
        return round(1.0 - self.model.prob(f), 4)
