"""Картина — детерминированная функция содержимого хранилища (Р1.4)."""

import random

from agreemodel.config import FusionCfg
from agreemodel.fusion import ci_fuse, compute_picture
from agreemodel.store import ReplicatedStore


def _track(store: ReplicatedStore, tid: int, x: float, y: float, t0: int, t1: int, cls=0):
    p = [0.1] * 5
    p[cls] = 0.6
    store.write(f"trk/{tid}", t1, {"x": x, "y": y, "vx": 0.0, "vy": 0.0, "pxx": 20.0,
                                   "pxy": 0.0, "pyy": 20.0, "t0": t0, "t1": t1, "n": 5,
                                   "cls": tuple(p), "st": "A"})


def _origins(rng: random.Random) -> list[ReplicatedStore]:
    out = []
    truth = [(rng.uniform(0, 2000), rng.uniform(0, 2000)) for _ in range(25)]
    for k in range(6):
        s = ReplicatedStore(f"U{k:02d}")
        for j, (x, y) in enumerate(truth):
            if rng.random() < 0.7:
                _track(s, j, x + rng.gauss(0, 3), y + rng.gauss(0, 3), 1000 + j, 5000 + k)
        out.append(s)
    return out


def test_same_content_any_arrival_order_same_picture():
    rng = random.Random(3)
    origins = _origins(rng)
    cfg = FusionCfg()
    digests = set()
    for _ in range(5):
        order = origins[:]
        rng.shuffle(order)
        r = ReplicatedStore("R")
        for o in order:
            r.apply(o.delta_for(r.version_vector()))
        digests.add(compute_picture(r.records(), cfg).digest)
    assert len(digests) == 1


def test_tracks_of_same_object_from_different_uavs_merge():
    rng = random.Random(4)
    origins = _origins(rng)
    r = ReplicatedStore("R")
    for o in origins:
        r.apply(o.delta_for(r.version_vector()))
    pic = compute_picture(r.records(), FusionCfg())
    assert len(pic.objects) <= 27  # 25 объектов, допускаются единичные расщепления


def test_same_uav_simultaneous_tracks_never_merge():
    s = ReplicatedStore("A")
    _track(s, 1, 0.0, 0.0, 1000, 5000)
    _track(s, 2, 1.0, 0.0, 1000, 5000)  # тот же борт, то же время — разные объекты
    pic = compute_picture(s.records(), FusionCfg())
    assert len(pic.objects) == 2


def test_ci_does_not_shrink_below_better_input():
    x, y, P = ci_fuse(0, 0, (4.0, 0, 4.0), 1, 1, (9.0, 0, 9.0))
    assert P[0] <= 4.0 + 1e-9 and P[0] > 2.0  # CI не «складывает» информацию как KF
