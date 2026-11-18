"""Свойства реплицируемого хранилища: сходимость при любом порядке, повторах и потерях."""

import random

from agreemodel.hlc import HLC
from agreemodel.store import ReplicatedStore


def _origin_writes(name: str, n: int, rng: random.Random) -> ReplicatedStore:
    s = ReplicatedStore(name)
    for i in range(n):
        s.write(f"trk/{rng.randrange(8)}", 1_000 + i, {"x": rng.random(), "i": i})
    return s


def test_delta_makes_copies_equal():
    rng = random.Random(1)
    a, b = _origin_writes("A", 30, rng), _origin_writes("B", 20, rng)
    a.apply(b.delta_for(a.version_vector()))
    b.apply(a.delta_for(b.version_vector()))
    assert a.digest() == b.digest()
    assert a.version_vector() == b.version_vector() == {"A": 30, "B": 20}


def test_order_duplicates_and_losses_do_not_break_convergence():
    rng = random.Random(2)
    origins = [_origin_writes(n, rng.randrange(5, 40), rng) for n in "ABCD"]
    # дельты нарезаны по ходу записи, доставляются в случайном порядке, с повторами и потерями
    deltas = []
    for o in origins:
        full = ReplicatedStore("tmp")
        full.apply(o.delta_for({}))
        deltas.append(o.delta_for({}))
    replicas = [ReplicatedStore(f"R{k}") for k in range(5)]
    for r in replicas:
        msgs = deltas * 3
        rng.shuffle(msgs)
        for d in msgs:
            if rng.random() < 0.3:
                continue  # потеря
            r.apply(d)
        for d in deltas:  # анти-энтропия закрывает пропуски
            r.apply(d)
    assert len({r.digest() for r in replicas}) == 1


def test_gap_is_rejected_and_copy_stays_complete():
    a = ReplicatedStore("A")
    for i in range(5):
        a.write(f"k{i}", i, {"v": i})
    d_old = a.delta_for({})  # 1..5
    for i in range(5, 8):
        a.write(f"k{i}", i, {"v": i})
    d_tail = a.delta_for({"A": 5})  # 6..8
    b = ReplicatedStore("B")
    assert b.apply(d_tail) == 0  # начало диапазона 5 > 0 — пропуск, часть отброшена
    assert b.version_vector() == {}
    b.apply(d_old)
    b.apply(d_tail)
    assert b.digest() == a.digest()


def test_superseded_versions_are_not_resent():
    a = ReplicatedStore("A")
    for i in range(10):
        a.write("trk/1", i, {"v": i})
    d = a.delta_for({})
    assert d.n_records == 1 and d.parts[0].to_incl == 10


def test_hlc_monotonic_and_causal():
    t = [100]
    a = HLC("A", lambda: t[0])
    b = HLC("B", lambda: 50)  # часы B отстают
    s1 = a.now()
    s2 = a.now()
    assert s2 > s1
    r = b.update(s2)
    assert r > s2
    assert b.now() > r
