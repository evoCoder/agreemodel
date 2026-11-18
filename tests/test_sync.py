"""Обмен цикла 1: широковещание, общая радиосреда, объединение запросов, адаптивный срок,
квант по записанному кадру, сквозная регрессия времени восстановления."""

import random

from agreemodel.config import EventCfg, NetCfg, NodeCfg, FusionCfg, PartitionCfg, Scenario
from agreemodel.net import Network
from agreemodel.node import Node
from agreemodel.sensors import Detection
from agreemodel.sim import run


def _net(**kw) -> Network:
    cfg = NetCfg(jitter_ms=0.0, **kw)
    n = Network(cfg, random.Random(1))
    n.alive = {0, 1, 2, 3}
    n.positions = {i: (10.0 * i, 0.0) for i in n.alive}
    return n


def test_broadcast_is_one_transmission_heard_by_all_neighbours():
    n = _net(loss=0.0)
    n.broadcast(0.0, 0, "DIGEST", {"A": 1}, 100)
    got = sorted(m.dst for m in n.pop_until(1.0))
    assert got == [1, 2, 3]
    assert n.msgs_sent == 1 and n.bytes_sent == 100 and n.deliveries == 3


def test_broadcast_losses_are_independent_per_receiver():
    n = _net(loss=0.5)
    for k in range(400):
        n.broadcast(k * 0.01, 0, "X", None, 10)
    got = {j: 0 for j in (1, 2, 3)}
    for m in n.pop_until(100.0):
        got[m.dst] += 1
    # каждому около половины, и не одинаково (потери не общие)
    assert all(150 < v < 250 for v in got.values())
    assert len(set(got.values())) > 1


def test_shared_medium_serialises_transmissions_of_different_uavs():
    size = 6250  # 0,1 с при 500 кбит/с
    per = _net(loss=0.0, medium="per_node")
    per.send(0.0, 0, 1, "X", None, size)
    per.send(0.0, 2, 3, "X", None, size)
    assert [round(m.t_deliver, 3) for m in per.pop_until(1.0)] == [0.13, 0.13]
    sh = _net(loss=0.0, medium="shared")
    sh.send(0.0, 0, 1, "X", None, size)
    sh.send(0.0, 2, 3, "X", None, size)
    assert [round(m.t_deliver, 3) for m in sh.pop_until(1.0)] == [0.13, 0.23]


def _node(idx: int, uid: str) -> Node:
    return Node(idx, uid, NodeCfg(), FusionCfg())


def test_requests_within_window_are_merged_into_one_broadcast():
    src = _node(0, "U01")
    for k in range(20):
        src.store.write(f"trk/{k}", 1000 + k, {"x": float(k)})
    src.on_request_c1(1.000, {"U01": 12})
    src.on_request_c1(1.005, {"U01": 5})
    assert src.flush(1.005) == []  # окно объединения ещё не истекло
    out = src.flush(1.010)
    assert len(out) == 1
    origin, f, to, msg = out[0]
    assert (origin, f, to) == ("U01", 5, 20) and msg.dst is None


def test_part_already_on_air_is_not_resent_only_increment_is():
    src = _node(0, "U01")
    for k in range(10):
        src.store.write(f"trk/{k}", 1000 + k, {"x": float(k)})
    src.on_request_c1(1.0, {"U01": 0})
    (_, f, to, _), = src.flush(1.01)
    src.note_sent("U01", f, to, t_done=1.2)
    src.on_request_c1(1.1, {"U01": 3})  # повтор, пока передача в эфире
    assert src.flush(1.11) == []
    src.store.write("trk/0", 2000, {"x": 0.5})  # новая запись после передачи
    src.on_request_c1(1.15, {"U01": 3})
    (_, f2, to2, _), = src.flush(1.16)
    assert (f2, to2) == (10, 11)  # досылается только новое
    src.on_request_c1(1.3, {"U01": 3})  # после доставки — значит, получатель её потерял
    (_, f3, _, _), = src.flush(1.31)
    assert f3 == 3


def test_heard_part_suppresses_own_duplicate_answer():
    a, b = _node(0, "U01"), _node(1, "U02")
    for k in range(5):
        a.store.write(f"trk/{k}", 1000 + k, {"x": float(k)})
    b.apply_all = b.store.apply(a.store.delta_for({}))
    b.on_request_c1(1.0, {"U01": 0})  # b тоже получил запрос по U01
    b.on_delta_c1(1.005, 0, a.store.delta_for({"U01": 0}, {"U01"}))  # и услышал ответ a
    assert b.flush(1.01) == []


def test_shared_channel_busy_only_while_transmitting():
    n = _net(loss=0.0, medium="shared")
    n.send(1.0, 0, 1, "X", None, 6250)  # 1,0–1,1 с
    assert not n.channel_busy(2, 0.99)
    assert n.channel_busy(2, 1.05)
    assert not n.channel_busy(2, 1.1)


def test_missing_data_is_requested_from_its_source_first_and_retried_at_two_peers():
    a = _node(0, "U01")
    a.on_digest_c1(1.0, 2, "U03", {"U02": 5, "U03": 7})
    outs = a.on_digest_c1(1.01, 1, "U02", {"U02": 5, "U03": 6})
    reqs = {o.dst: o.payload for o in outs if o.kind == "REQ"}
    assert reqs == {}  # всё уже запрошено у первого соседя
    a2 = _node(0, "U01")
    a2.peer_vv = {1: (1.0, {"U02": 5}), 2: (1.0, {"U02": 5})}
    a2.uid_of = {1: "U02", 2: "U03"}
    outs = a2.request_due(1.0)
    assert [(o.dst, o.payload) for o in outs] == [(1, {"U02": 0})]  # у самого источника
    t_retry = a2.next_timer()
    assert abs(t_retry - (1.0 + NodeCfg().rto_init_s)) < 1e-9
    outs = a2.request_due(t_retry)
    assert sorted(o.dst for o in outs) == [1, 2]  # повтор — у двух соседей


def test_rto_adapts_to_measured_round_trip():
    a = _node(0, "U01")
    for _ in range(30):
        a._rtt_sample(0.08)
    assert NodeCfg().rto_min_s <= a.rto < 0.15


def test_quantum_counts_only_after_own_frame_is_written():
    a = _node(0, "U01")
    det = Detection(1000, 10.0, 0.0, 1.0, (1.0, 0.0, 0.0), None)
    a.on_frame(1.0, 1_000_000, [det])
    a.dirty = True  # пересчёт по чужой дельте: свой кадр ещё не записан
    a.recompute(1.1)
    assert a.quantum_s == []
    a.publish(1_300_000, (0.0, 0.0), 1.0)
    a.recompute(1.3)
    assert [round(q, 3) for q in a.quantum_s] == [0.3]


def _short(seed: int) -> Scenario:
    sc = Scenario()
    sc.run.seed = seed
    sc.run.duration_s = 60
    sc.swarm.route_length_km = 3
    sc.objects.count = 10
    sc.net.partition = [PartitionCfg(10, 40, [[0, 1, 2, 3], [4, 5, 6, 7]])]
    sc.event = [EventCfg(25, "fail", 5), EventCfg(50, "join", 8)]
    return sc


def test_restore_and_join_within_requirement_on_several_seeds():
    for seed in (1, 2, 3):
        s = run(_short(seed)).summary
        assert s["final_pictures_identical"]
        assert all(v == 0 for v in s["records_missing_per_uav"].values())
        for r in s["restores"]:
            assert r["store_conv_s"] is not None and r["store_conv_s"] <= 1.5, (seed, r)


def test_cycle0_protocol_still_available_for_comparison():
    sc = _short(1)
    sc.node.protocol = "c0"
    s = run(sc).summary
    assert s["final_pictures_identical"]
    assert s["net"]["protocol"] == "c0"
