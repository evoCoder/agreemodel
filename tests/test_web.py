"""Связка веб-страницы: кадры для карты не влияют на результат модели."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "web" / "py"))

import soglasie_web as sw  # noqa: E402
from agreemodel.sim import run  # noqa: E402


def _short() -> dict:
    raw = sw.base_scenario(str(ROOT / "scenarios" / "base.toml"))
    raw["run"]["duration_s"] = 60
    raw["objects"]["count"] = 15
    raw["net"]["partition"] = [{"start_s": 10, "end_s": 40, "groups": [[0, 1, 2, 3], [4, 5, 6, 7]]}]
    raw["event"] = [{"t_s": 25, "kind": "fail", "uav": 5}, {"t_s": 50, "kind": "join", "uav": 8}]
    return raw


def test_frames_do_not_change_result():
    raw = _short()
    out = json.loads(sw.run_json(json.dumps(raw), frame_s=1.0))
    ref = run(sw.scenario_from(raw)).summary
    # picture_compute_ms — замер процессорного времени, от прогона к прогону различается
    skip = ("wall_s", "version", "picture_compute_ms")
    got = {k: v for k, v in out["summary"].items() if k not in skip}
    assert got == {k: v for k, v in ref.items() if k not in skip}
    assert out["series"] == run(sw.scenario_from(raw)).series


def test_frames_and_progress():
    msgs: list[dict] = []
    out = json.loads(sw.run_json(json.dumps(_short()), post=lambda s: msgs.append(json.loads(s)),
                                 frame_s=2.0, view_uav=2))
    fr = out["frames"]
    assert 30 <= len(fr) <= 40
    assert {"t", "c", "uavs", "truth", "pic", "view", "groups", "agreed"} <= set(fr[0])
    assert max(f["groups"] for f in fr) == 2
    assert fr[-1]["view"] == 2 and len(fr[-1]["truth"]) == 15
    assert any(u[0] == 8 for u in fr[-1]["uavs"])  # присоединившийся U09
    pct = [m["pct"] for m in msgs if m["type"] == "progress"]
    assert pct == sorted(pct) and pct[-1] >= 99


def test_scenario_roundtrip():
    raw = sw.base_scenario(str(ROOT / "scenarios" / "base.toml"))
    out = json.loads(sw.run_json(json.dumps(_short()), frame_s=5.0))
    assert set(out["scenario"]) == set(raw)
