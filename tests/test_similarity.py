"""Модель сходства: формат весов, квантование, детерминированный вывод, стратегия ai."""

import json

from agreemodel.config import FusionCfg
from agreemodel.fusion import ASSOCIATORS, compute_picture
from agreemodel.similarity import DEFAULT_MODEL, FEATURES, Model, get_model
from agreemodel.store import Record

CLS = [0.9, 0.025, 0.025, 0.025, 0.025]


def _trk(origin: str, key: str, x: float, y: float, t0: float, t1: float) -> Record:
    data = {"x": x, "y": y, "vx": 0.0, "vy": 0.0, "pxx": 4.0, "pxy": 0.0, "pyy": 4.0,
            "t0": int(t0 * 1e6), "t1": int(t1 * 1e6), "n": 20, "st": "L", "cls": CLS}
    return Record(origin, key, 1, int(t1 * 1e6), data)


def test_default_model_loads_and_matches_features():
    m = get_model()
    spec = json.loads(DEFAULT_MODEL.read_text("utf-8"))
    assert tuple(spec["features"]) == FEATURES
    assert 0.0 < m.p_min < 1.0
    for L in spec["layers"]:
        assert all(-127 <= q <= 127 for row in L["w_int8"] for q in row)


def test_prob_is_on_grid_and_repeatable():
    m = get_model()
    f = [0.5, 2.0, 9.0, 2.0, 0.8, 0.0, 3.0, 0.0, 0.0, 0.0, 0.0, 3.0, 1.0, 1.0, 1.0, 3.0, 0.0, 4.2]
    p = m.prob(f)
    assert p == m.prob(list(f))
    assert abs(p * 1e4 - round(p * 1e4)) < 1e-6


def test_tiny_model_spec():
    spec = {"name": "t", "features": list(FEATURES), "p_min": 0.5,
            "mean": [0.0] * len(FEATURES), "scale": [1.0] * len(FEATURES),
            "layers": [{"w_int8": [[-127] + [0] * (len(FEATURES) - 1)], "scale": 0.01, "b": [1.27]}]}
    m = Model(spec)
    assert abs(m.prob([0.0] * len(FEATURES)) - round(1 / (1 + 2.718281828459045 ** -1.27), 4)) < 1e-9
    assert abs(m.prob([1.0] + [0.0] * (len(FEATURES) - 1)) - 0.5) < 1e-9


def test_ai_associator_registered_and_deterministic():
    assert "ai" in ASSOCIATORS
    recs = [_trk("U01", "trk/1", 0, 0, 0, 30), _trk("U02", "trk/1", 1.5, -1.0, 1, 30),
            _trk("U03", "trk/1", 2000, 0, 0, 30), _trk("U01", "trk/2", 30, 0, 0, 30)]
    cfg = FusionCfg(association="ai")
    a = compute_picture(recs, cfg)
    b = compute_picture(list(reversed(recs)), cfg)
    assert a.digest == b.digest
    sizes = sorted(len(o.members) for o in a.objects)
    assert sizes == [1, 1, 2]  # U01/U02 рядом — один объект; U01 trk/2 — свой борт одновременно
