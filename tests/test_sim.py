"""Сквозной прогон: разрыв, отказ, присоединение; сходимость и отсутствие потерь."""

from agreemodel.config import EventCfg, PartitionCfg, Scenario
from agreemodel.sim import run


def _short() -> Scenario:
    sc = Scenario()
    sc.run.duration_s = 60
    sc.swarm.route_length_km = 3
    sc.objects.count = 10
    sc.net.partition = [PartitionCfg(10, 40, [[0, 1, 2, 3], [4, 5, 6, 7]])]
    sc.event = [EventCfg(25, "fail", 5), EventCfg(50, "join", 8)]
    return sc


def test_short_scenario_converges_without_loss():
    s = run(_short()).summary
    assert s["final_pictures_identical"]
    assert s["settle_conv_s"] is not None and s["settle_conv_s"] < 3.0
    assert all(v == 0 for v in s["records_missing_per_uav"].values())
    assert all(r["store_conv_s"] is not None for r in s["restores"])
    assert s["quantum_s"]["max"] <= 0.5


def test_deterministic_for_same_seed():
    a = run(_short()).summary
    b = run(_short()).summary
    for k in ("restores", "picture", "records_union", "net"):
        assert a[k] == b[k]
