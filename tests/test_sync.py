"""Обмен цикла 1: широковещание, общая радиосреда, объединение запросов, адаптивный срок,
квант по записанному кадру, сквозная регрессия времени восстановления."""

import random

from agreemodel.config import EventCfg, NetCfg, NodeCfg, FusionCfg, PartitionCfg, Scenario
from agreemodel.net import Network
from agreemodel.node import Node
from agreemodel.sensors import Detection
from agreemodel.sim import Simulation, run


def _net(**kw) -> Network:
    cfg = NetCfg(jitter_ms=0.0, **kw)
    n = Network(cfg, random.Random(1))
    n.alive = {0, 1, 2, 3}
    n.positions = {i: (10.0 * i, 0.0) for i in n.alive}
    return n


def _drain(n: Network, t_end: float) -> list:
    got = []
    while (t := n.next_time()) is not None and t <= t_end:
        m = n.step(t)
        if m is not None:
            got.append(m)
    return got


def test_broadcast_is_one_transmission_heard_by_all_neighbours():
    n = _net(loss=0.0)
    n.broadcast(0.0, 0, "DIGEST", {"A": 1}, 100)
    got = sorted(m.dst for m in _drain(n, 1.0))
    assert got == [1, 2, 3]
    assert n.msgs_sent == 1 and n.bytes_sent == 100 and n.deliveries == 3


def test_broadcast_losses_are_independent_per_receiver():
    n = _net(loss=0.5)
    for k in range(400):
        n.broadcast(k * 0.01, 0, "X", None, 10)
    got = {j: 0 for j in (1, 2, 3)}
    for m in _drain(n, 100.0):
        got[m.dst] += 1
    assert all(150 < v < 250 for v in got.values())
    assert len(set(got.values())) > 1


def test_message_is_lost_if_any_packet_is_lost():
    n = _net(loss=0.1, mtu_bytes=100)
    for k in range(1000):
        n.send(k * 0.1, 0, 1, "X", None, 500)  # 5 пакетов: доставка (0,9)^5 ≈ 0,59
    ok = len(_drain(n, 1000.0)) / 1000
    assert 0.54 < ok < 0.64


def test_shared_medium_serialises_transmissions_of_different_uavs():
    size = 6250  # 0,1 с при 500 кбит/с
    per = _net(loss=0.0, medium="per_node", mtu_bytes=10_000)
    per.send(0.0, 0, 1, "X", None, size)
    per.send(0.0, 2, 3, "X", None, size)
    assert [round(m.t_deliver, 3) for m in _drain(per, 1.0)] == [0.13, 0.13]
    sh = _net(loss=0.0, medium="shared", mtu_bytes=10_000)
    sh.send(0.0, 0, 1, "X", None, size)
    sh.send(0.0, 2, 3, "X", None, size)
    assert [round(m.t_deliver, 3) for m in _drain(sh, 1.0)] == [0.13, 0.23]


def test_shared_medium_is_shared_by_packets_short_message_is_not_blocked():
    sh = _net(loss=0.0, medium="shared", mtu_bytes=625)  # пакет — 0,01 с
    sh.send(0.0, 0, 1, "BIG", None, 62_500)  # 100 пакетов — 1 с эфира
    sh.send(0.001, 2, 3, "SMALL", None, 100)
    got = {m.kind: round(m.t_deliver, 3) for m in _drain(sh, 5.0)}
    assert got["SMALL"] < 0.1  # проходит между пакетами длинного сообщения
    assert 1.0 < got["BIG"] < 1.1


def test_control_messages_overtake_queued_data_of_same_uav():
    n = _net(loss=0.0, medium="shared", mtu_bytes=625)
    for _ in range(10):
        n.send(0.0, 0, 1, "DELTA", None, 625)  # 10 частей — 0,1 с
    n.send(0.0, 0, 1, "REQ", None, 20)
    got = [m.kind for m in _drain(n, 1.0)]
    assert got.index("REQ") <= 1


def test_shared_channel_busy_while_transmitting_and_recently():
    n = _net(loss=0.0, medium="shared", mtu_bytes=10_000)
    n.send(1.0, 0, 1, "X", None, 6250)  # 1,0–1,1 с
    _drain(n, 1.05)
    assert n.channel_busy(2, 1.05)
    _drain(n, 2.0)
    assert not n.channel_busy(2, 1.2)
    assert n.channel_busy(2, 1.12, recent_s=0.05)


def _node(idx: int, uid: str) -> Node:
    return Node(idx, uid, NodeCfg(), FusionCfg())


def _writes(n: Node, k: int) -> None:
    for i in range(k):
        n.store.write(f"trk/{i}", 1000 + i, {"x": float(i)})


def test_requests_within_window_are_merged_into_one_answer_cut_into_packets():
    src = _node(0, "U01")
    _writes(src, 40)
    src.on_request_c1(1.000, {"U01": (12, 0)})
    src.on_request_c1(1.005, {"U01": (5, 0)})
    assert src.flush(1.005) == []  # окно объединения ещё не истекло
    out = src.flush(1.010)
    assert len(out) > 1  # ответ нарезан на части
    assert out[0][1] == 5 and out[-1][2] == 40
    assert all(o.size <= NodeCfg().max_part_bytes + 20 for *_, o in out)
    assert all(a[2] == b[1] for a, b in zip(out, out[1:]))  # части стыкуются


def test_range_already_on_air_is_not_resent_only_increment_is():
    src = _node(0, "U01")
    _writes(src, 10)
    src.on_request_c1(1.0, {"U01": (0, 0)})
    entries = [src.note_queued(o, f, to) for o, f, to, _ in src.flush(1.01)]
    src.on_request_c1(1.05, {"U01": (3, 0)})  # повтор, пока часть в очереди
    assert src.flush(1.06) == []
    for e in entries:
        src.note_sent(e, t_done=1.2)
    src.store.write("trk/0", 2000, {"x": 0.5})  # новая запись после передачи
    push = src.note_queued("U01", 10, 11)  # её рассылка не отменяет покрытия (0, 10]
    src.on_request_c1(1.15, {"U01": (3, 0)})
    assert src.flush(1.16) == []
    src.note_sent(push, t_done=1.2)
    src.on_request_c1(1.3, {"U01": (3, 0)})  # после доставки — получатель её потерял
    assert src.flush(1.31)[0][1] == 3


def test_only_uncovered_head_of_range_is_sent():
    src = _node(0, "U01")
    _writes(src, 30)
    src.note_queued("U01", 10, 30)  # (10, 30] уже в очереди
    src.on_request_c1(1.0, {"U01": (4, 0)})
    out = src.flush(1.01)
    assert [(f, to) for _, f, to, _ in out] == [(4, 10)]


def test_heard_part_suppresses_own_duplicate_answer():
    a, b = _node(0, "U01"), _node(1, "U02")
    _writes(a, 5)
    b.store.apply(a.store.delta_for({}))
    b.on_request_c1(1.0, {"U01": (0, 0)})  # b тоже получил запрос по U01
    b.on_delta_c1(1.005, 0, a.store.delta_for({"U01": 0}, {"U01"}))  # и услышал ответ a
    assert b.flush(1.01) == []


def test_parts_after_gap_are_parked_and_only_gap_is_requested():
    from agreemodel.store import Delta
    a, r = _node(0, "U01"), _node(1, "U02")
    _writes(a, 40)
    parts = a.store.parts_for("U01", 0, None, 480)
    assert len(parts) >= 3
    r.peer_vv = {0: (1.0, {"U01": 40})}
    r.uid_of = {0: "U01"}
    nacks = []
    for p in parts[1:]:  # первая часть потеряна
        nacks += r.on_delta_c1(1.0, 0, Delta((p,)))
    assert r.store.version_vector().get("U01", 0) == 0
    # пропуск дозапрошен сразу (один раз) и только он
    assert [(o.dst, o.payload) for o in nacks] == [(0, {"U01": (0, parts[0].to_incl)})]
    assert r.request_due(1.0) == []  # таймер его не дублирует
    r.on_delta_c1(1.1, 0, Delta((parts[0],)))
    assert r.store.version_vector()["U01"] == 40
    assert r.store.digest() == _copy_digest(a)


def _copy_digest(n: Node) -> str:
    from agreemodel.store import ReplicatedStore
    c = ReplicatedStore("c")
    c.apply(n.store.delta_for({}))
    return c.digest()


def test_missing_data_is_requested_from_its_source_first_and_retried_at_two_peers():
    a2 = _node(0, "U01")
    a2.peer_vv = {1: (1.0, {"U02": 5}), 2: (1.0, {"U02": 5})}
    a2.uid_of = {1: "U02", 2: "U03"}
    outs = a2.request_due(1.0)
    assert [(o.dst, o.payload) for o in outs] == [(1, {"U02": (0, 0)})]  # у самого источника
    t_retry = a2.next_timer()
    assert abs(t_retry - (1.0 + NodeCfg().rto_init_s)) < 1e-9
    outs = a2.request_due(t_retry)
    assert sorted(o.dst for o in outs) == [1, 2]  # повтор — у двух соседей
    outs = a2.request_due(a2.next_timer(), busy=True)
    assert outs == []  # эфир занят — повтор отложен


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


# ---------------------------------------------------------------- цикл 2

def test_seeding_does_not_depend_on_float_hash():
    """Засев только целыми: hash(float) различается на 32- и 64-битных платформах."""
    import inspect
    import re
    import agreemodel.sim as S
    for arg in re.findall(r"random\.Random\(([^\n]*)\)", inspect.getsource(S)):
        assert ".random()" not in arg, arg


def test_on_sample_callback_reads_state_without_changing_result():
    seen = []
    sc = _short(1)
    a = Simulation(sc).run().summary
    b = Simulation(_short(1), on_sample=lambda sim, t, alive: seen.append(
        (t, len(sim.components(alive, t)), len(sim.nodes[alive[0]].truth_of)))).run().summary
    assert seen and a["restores"] == b["restores"] and a["picture"] == b["picture"]


def test_joining_uav_gets_its_own_formation_slot():
    from agreemodel.config import ObjectsCfg, SwarmCfg
    from agreemodel.world import World
    w = World(SwarmCfg(), ObjectsCfg(count=1), random.Random(1))
    slots = {w.slot(i) for i in range(w.cfg.n_uav + 2)}
    assert len(slots) == w.cfg.n_uav + 2


def test_navigation_error_changes_during_run():
    sim = Simulation(_short(1))
    nav = sim.sensors[0].nav
    x0, y0 = nav.x, nav.y
    sim.run()
    assert (nav.x, nav.y) != (x0, y0)


def test_split_own_tracks_are_merged_and_closed_as_merged():
    n = _node(0, "U01")
    for k in range(4):  # один объект, отметки двух треков
        n.on_frame(k * 0.2, 1_000_000 + k * 200_000,
                   [Detection(1_000_000 + k * 200_000, 0.0, 0.0, 1.0, (1.0, 0, 0, 0, 0), "T1")])
    n.publish(1_600_000, (0.0, 0.0), 1.0)
    # второй трек того же объекта (как после выпадения отметки из строба)
    tr = n.tracks[0]
    from agreemodel.node import LocalTrack
    import numpy as np
    dup = LocalTrack(99, tr.x.copy(), tr.P.copy(), tr.t_us, tr.t_us, tr.class_log.copy(),
                     hits=3, published=True)
    dup.truth["T1"] = 3
    n.tracks.append(dup)
    n.on_frame(0.8, 1_800_000, [])
    n.publish(1_800_000, (0.0, 0.0), 1.0)
    assert n.store.get("U01", "trk/99").data["st"] == "M"
    from agreemodel.fusion import compute_picture
    pic = compute_picture(n.store.records(), FusionCfg())
    assert len(pic.objects) == 1


def test_scenario_json_round_trip_gives_same_result(tmp_path):
    import json
    from agreemodel.config import load, to_dict
    sc = _short(2)
    p = tmp_path / "scenario.json"
    p.write_text(json.dumps(to_dict(sc)), encoding="utf-8")
    a, b = run(sc).summary, run(load(p)).summary
    assert a["restores"] == b["restores"] and a["picture"] == b["picture"]


def test_join_restore_counts_from_appearance_of_joining_uav():
    s = run(_short(1)).summary
    join = [r for r in s["restores"] if r["kind"].startswith("join")][0]
    assert join["store_conv_s"] > 0.0  # новичку нужно время на получение состояния


def test_fix_latency_within_publish_period():
    s = run(_short(1)).summary
    assert s["fix_latency_s"]["max"] <= NodeCfg().publish_period_s + 1e-9
