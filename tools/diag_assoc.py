"""Разбор дубликатов картины по эталону: почему треки одного объекта не объединились.

    python tools/diag_assoc.py scenarios/base.toml --seeds 1-10 --out runs/diag_c0

Прогон сценария с записью ошибки навигации каждого борта, затем повтор ассоциации
`fusion.compute_picture` по итоговому хранилищу борта U01 с регистрацией каждого случая,
когда трек открыл новый объект картины, хотя объект того же объекта эталона уже был.
Для такого случая берётся ближайший объект картины с тем же объектом эталона и
определяется первая причина отказа в порядке проверки `fusion.py`:

* `own_overlap` — запрет «два трека одного борта, наблюдавшихся одновременно»: у борта-
  источника два пересекающихся по времени трека одного объекта (раздвоение трека
  в локальном сопровождении);
* `class` — несовместимые распределения классов (порог class_gate);
* `gate_nav` — расстояние Махаланобиса за порогом, но после вычитания истинной ошибки
  навигации бортов (известна только по эталону) укладывается в порог — причина в
  смещении навигации;
* `gate_stale` — за порогом и после вычитания; трек или объект устарел (потерян или
  экстраполирован дальше coast_s);
* `gate_other` — прочее: за порогом при свежих данных и без смещения навигации;
* `cell` — в пороге, но объект вне соседних ячеек пространственного индекса.

Эталон и ошибка навигации используются только здесь, для разбора; логика бортов их не видит.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agreemodel import sensors as sensors_mod
from agreemodel.cli import _apply_override
from agreemodel.config import load
from agreemodel.fusion import (
    _cos_dist,
    _mahal2,
    _track_state,
    ci_fuse,
    compute_picture,
)
from agreemodel.sim import Simulation

_ORIG_FRAME = sensors_mod.Sensor.frame
CAUSES = ("own_overlap", "class", "gate_nav", "gate_stale", "gate_other", "cell")


def parse_seeds(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


class NavLog:
    """Ошибка навигации каждого борта по кадрам датчика (по эталону)."""

    def __init__(self) -> None:
        self.log: dict[str, list[tuple[int, float, float]]] = {}

    def install(self) -> None:
        orig = _ORIG_FRAME
        log = self.log

        def frame(sensor, t_us, u, world):  # type: ignore[no-untyped-def]
            log.setdefault(u.uid, []).append((t_us, sensor.nav.x, sensor.nav.y))
            return orig(sensor, t_us, u, world)

        sensors_mod.Sensor.frame = frame  # type: ignore[method-assign]

    def bias(self, uid: str, t0: int, t1: int) -> tuple[float, float]:
        """Средняя ошибка навигации борта на интервале наблюдения трека."""
        pts = [(x, y) for t, x, y in self.log.get(uid, ()) if t0 <= t <= t1]
        if not pts:
            near = min(self.log[uid], key=lambda p: abs(p[0] - t1))
            return near[1], near[2]
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def replay(records: list[Any], cfg: Any, truth_of: dict, nav: NavLog) -> dict[str, Any]:
    """Повтор цикла ассоциации fusion.compute_picture с регистрацией пропущенных слияний."""
    # «M» — трек объединён бортом-источником с другим своим, в картину не входит (fusion.py)
    tracks = [r for r in records if r.key.startswith("trk/") and r.data.get("st") != "M"]
    t_ref = max([r.data["t1"] for r in tracks] + [r.t_us for r in records if r.key == "uav"])
    tracks.sort(key=lambda r: (r.data["t0"], r.origin, r.key))
    cell, gate2 = cfg.cell_m, cfg.gate**2

    def cell_of(x: float, y: float) -> tuple[int, int]:
        return (math.floor(x / cell), math.floor(y / cell))

    # объект: x, y, P, cls (как в fusion), члены, интервалы, истина, смещения членов
    objs: list[dict[str, Any]] = []
    grid: dict[tuple[int, int], list[int]] = {}
    events: list[dict[str, Any]] = []
    for r in tracks:
        x, y, P = _track_state(r, t_ref, cfg)
        t0, t1, cls = r.data["t0"], r.data["t1"], r.data["cls"]
        tid = truth_of.get(r.gkey)
        b = nav.bias(r.origin, t0, t1)
        stale_r = r.data["st"] != "A" or (t_ref - t1) / 1e6 > cfg.coast_s
        cx, cy = cell_of(x, y)
        near = {k for gx in (cx - 1, cx, cx + 1) for gy in (cy - 1, cy, cy + 1)
                for k in grid.get((gx, gy), ())}
        cands = []
        for k in sorted(near):
            o = objs[k]
            iv = o["iv"].get(r.origin)
            if iv and any(a <= t1 and t0 <= bb for a, bb in iv):
                continue
            if _cos_dist(o["cls"], cls) > cfg.class_gate:
                continue
            S = (o["P"][0] + P[0], o["P"][1] + P[1], o["P"][2] + P[2])
            d2 = _mahal2(x - o["x"], y - o["y"], S)
            if d2 <= gate2:
                cands.append((d2, k))
        if cands:
            cands.sort()
            k = cands[0][1]
            o = objs[k]
            old = cell_of(o["x"], o["y"])
            o["x"], o["y"], o["P"] = ci_fuse(o["x"], o["y"], o["P"], x, y, P)
            o["iv"].setdefault(r.origin, []).append((t0, t1))
            o["truth"][tid] += 1
            o["bias"].append(b)
            o["stale"] = o["stale"] and stale_r
            o["n"] += 1
            lc = [a + math.log(max(c, 1e-6)) for a, c in zip(o["log_cls"], cls, strict=True)]
            o["log_cls"] = lc
            m = max(lc)
            ex = [math.exp((v - m) / o["n"]) for v in lc]
            s = sum(ex)
            o["cls"] = tuple(v / s for v in ex)
            new = cell_of(o["x"], o["y"])
            if new != old:
                grid[old].remove(k)
                grid.setdefault(new, []).append(k)
            continue
        # новый объект; был ли уже объект с той же истиной?
        if tid is not None:
            same = [k for k, o in enumerate(objs) if o["truth"].most_common(1)[0][0] == tid]
            if same:
                best = None
                for k in same:
                    o = objs[k]
                    S = (o["P"][0] + P[0], o["P"][1] + P[1], o["P"][2] + P[2])
                    d2 = _mahal2(x - o["x"], y - o["y"], S)
                    bx = sum(v[0] for v in o["bias"]) / len(o["bias"])
                    by = sum(v[1] for v in o["bias"]) / len(o["bias"])
                    d2c = _mahal2((x - b[0]) - (o["x"] - bx), (y - b[1]) - (o["y"] - by), S)
                    iv = o["iv"].get(r.origin)
                    overlap = bool(iv and any(a <= t1 and t0 <= bb for a, bb in iv))
                    cls_bad = _cos_dist(o["cls"], cls) > cfg.class_gate
                    if overlap:
                        cause = "own_overlap"
                    elif cls_bad:
                        cause = "class"
                    elif d2 > gate2:
                        if d2c <= gate2:
                            cause = "gate_nav"
                        elif stale_r or o["stale"]:
                            cause = "gate_stale"
                        else:
                            cause = "gate_other"
                    else:
                        cause = "cell"
                    ev = {"origin": r.origin, "key": r.key, "truth": tid, "cause": cause,
                          "d2": round(d2, 2), "d2_navfree": round(d2c, 2),
                          "dist_m": round(math.hypot(x - o["x"], y - o["y"]), 2),
                          "sigma_m": round(math.sqrt((S[0] + S[2]) / 2), 2),
                          "stale_track": stale_r, "stale_obj": o["stale"],
                          "cos_dist": round(_cos_dist(o["cls"], cls), 3)}
                    if best is None or (CAUSES.index(cause), d2) < (CAUSES.index(best["cause"]), best["d2"]):
                        best = ev
                events.append(best)
        objs.append({"x": x, "y": y, "P": P, "cls": tuple(cls), "iv": {r.origin: [(t0, t1)]},
                     "truth": Counter({tid: 1}), "bias": [b], "stale": stale_r, "n": 1,
                     "log_cls": [math.log(max(c, 1e-6)) for c in cls]})
        grid.setdefault((cx, cy), []).append(len(objs) - 1)
    return {"objects": len(objs), "events": events}


def one(args: tuple[str, list[str], int]) -> dict[str, Any]:
    path, sets, seed = args
    sc = load(path)
    for e in sets:
        _apply_override(sc, e)
    _apply_override(sc, f"run.seed={seed}")
    nav = NavLog()
    nav.install()
    sim = Simulation(sc)
    res = sim.run()
    truth_of = {(n.uid, k): v for n in sim.nodes.values() for k, v in n.truth_of.items()}
    n0 = sim.nodes[sim.alive()[0]]
    recs = list(n0.store.records())
    rp = replay(recs, sc.fusion, truth_of, nav)
    pic = compute_picture(recs, sc.fusion)
    assert rp["objects"] == len(pic.objects), "повтор ассоциации расходится с fusion.py"
    p = res.summary["picture"]
    # ложные объекты картины: все треки — от ложных обнаружений (по эталону)
    false = []
    t_ref = pic.t_ref_us
    for o in pic.objects:
        if any(truth_of.get(m) is not None for m in o.members):
            continue
        recs_o = [n0.store.get(*m) for m in o.members]
        near = min((math.hypot(w.x - o.x, w.y - o.y) for w in sim.world.objects), default=-1)
        false.append({"members": len(o.members),
                      "origins": sorted({m[0] for m in o.members}),
                      "hits_max": max(r.data["n"] for r in recs_o),
                      "dur_s": round(max((r.data["t1"] - r.data["t0"]) / 1e6 for r in recs_o), 1),
                      "age_s": round((t_ref - o.t_last_us) / 1e6, 1),
                      "nearest_truth_m": round(near, 1)})
    return {"seed": seed, "picture": p, "quantum_max": res.summary["quantum_s"]["max"],
            "events": rp["events"], "false": false}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scenario")
    ap.add_argument("--seeds", default="1-10")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    seeds = parse_seeds(a.seeds)
    with ProcessPoolExecutor(a.jobs) as ex:
        runs = list(ex.map(one, [(a.scenario, a.set, s) for s in seeds]))
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    causes = Counter(e["cause"] for r in runs for e in r["events"])
    n_ev = sum(causes.values())
    tot = Counter()
    for r in runs:
        tot.update({k: v for k, v in r["picture"].items()})
    summary = {
        "scenario": a.scenario, "set": a.set, "seeds": seeds,
        "totals": dict(tot),
        "duplicate_rate": round(tot["duplicates"] / max(tot["truth_covered"], 1), 3),
        "per_seed": [{"seed": r["seed"], **r["picture"], "events": len(r["events"])} for r in runs],
        "missed_merges": n_ev,
        "causes": {c: causes.get(c, 0) for c in CAUSES},
        "causes_share": {c: round(causes.get(c, 0) / max(n_ev, 1), 3) for c in CAUSES},
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), "utf-8")
    with open(out / "false.jsonl", "w", encoding="utf-8") as f:
        for r in runs:
            f.writelines(json.dumps({"seed": r["seed"], **e}, ensure_ascii=False) + "\n"
                         for e in r["false"])
    with open(out / "events.jsonl", "w", encoding="utf-8") as f:
        for r in runs:
            f.writelines(json.dumps({"seed": r["seed"], **e}, ensure_ascii=False) + "\n" for e in r["events"])
    print(json.dumps({k: summary[k] for k in ("totals", "duplicate_rate", "missed_merges",
                                              "causes", "causes_share")},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
