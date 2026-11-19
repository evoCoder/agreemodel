"""Локальное сопровождение: промах строба и ложное обнаружение рядом не раздваивают трек."""

from agreemodel.config import FusionCfg, NodeCfg
from agreemodel.node import Node
from agreemodel.sensors import Detection

CLS = (0.9, 0.025, 0.025, 0.025, 0.025)


def _det(t_s: float, x: float, y: float, truth: str | None = "T1") -> Detection:
    return Detection(int(t_s * 1e6), x, y, 4.0, CLS, truth)


def _confirmed(cfg: NodeCfg) -> tuple[Node, float]:
    n = Node(0, "U01", cfg, FusionCfg())
    t = 0.0
    for _ in range(20):
        t += 0.2
        n.on_frame(t, int(t * 1e6), [_det(t, 0.0, 0.0)])
    assert len(n.tracks) == 1
    return n, t


def test_gate_miss_goes_to_confirmed_track():
    n, t = _confirmed(NodeCfg())
    t += 0.2
    n.on_frame(t, int(t * 1e6), [_det(t, 12.0, 0.0)])  # d² ≈ 30: вне gate_d2, в gate2_d2
    assert len(n.tracks) == 1
    assert n.tracks[0].hits == 21


def test_gate_miss_splits_track_in_cycle0_mode():
    n, t = _confirmed(NodeCfg(gate2_d2=0.0))
    t += 0.2
    n.on_frame(t, int(t * 1e6), [_det(t, 12.0, 0.0)])
    assert len(n.tracks) == 2


def test_false_detection_does_not_steal_object():
    n, t = _confirmed(NodeCfg())
    t += 0.2
    # объект и рядом ложное обнаружение; затем только объект — отметки идут старому треку
    n.on_frame(t, int(t * 1e6), [_det(t, 0.5, 0.0), _det(t, 9.0, 3.0, None)])
    for _ in range(5):
        t += 0.2
        n.on_frame(t, int(t * 1e6), [_det(t, 5.0, 2.0)])
    main = max(n.tracks, key=lambda tr: tr.hits)
    assert main.hits == 26
    assert all(tr.hits < NodeCfg().confirm_hits for tr in n.tracks if tr is not main)


def test_two_objects_seen_in_one_frame_give_two_tracks():
    n, t = _confirmed(NodeCfg())
    for _ in range(5):
        t += 0.2
        n.on_frame(t, int(t * 1e6), [_det(t, 0.0, 0.0), _det(t, 15.0, 0.0, "T2")])
    assert sum(tr.hits >= 3 for tr in n.tracks) == 2
