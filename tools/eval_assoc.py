"""Качество картины при разных стратегиях ассоциации по сохранённым прогонам (data/raw).

    python tools/eval_assoc.py data/raw/*_s1[1-9].pkl data/raw/*_s20.pkl \
        --assoc rules --assoc ai --out analysis/c4_ai/eval.json

Хранилище и обмен от стратегии ассоциации не зависят, поэтому картина пересчитывается
по итоговому хранилищу прогона (как на борту: ``fusion.compute_picture``). Показатели —
как в итогах прогона (``sim.py``): дубликаты, ложные и смешанные объекты; добавлено
«поглощённые» — объекты эталона, обнаруженные, но не ставшие отдельным объектом картины.
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agreemodel.config import FusionCfg
from agreemodel.fusion import compute_picture

KEYS = ("objects", "truth_detected", "absorbed", "duplicates", "false_objects", "mixed_objects",
        "conflicts_marked")


def quality(pic: Any, truth_of: dict) -> dict[str, int]:
    majority: Counter = Counter()
    false_obj = mixed = 0
    for o in pic.objects:
        real = [t for t in (truth_of.get(m) for m in o.members) if t is not None]
        if not real:
            false_obj += 1
            continue
        majority[Counter(real).most_common(1)[0][0]] += 1
        if len(set(real)) > 1:
            mixed += 1
    detected = {v for v in truth_of.values() if v is not None}
    return {"objects": len(pic.objects), "truth_detected": len(detected),
            "absorbed": len(detected) - len(majority),
            "duplicates": sum(c - 1 for c in majority.values()),
            "false_objects": false_obj, "mixed_objects": mixed,
            "conflicts_marked": pic.n_conflicts}


def eval_run(args: tuple[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    path, variants = args
    run = pickle.loads(Path(path).read_bytes())
    out = []
    for v in variants:
        cfg = FusionCfg(**v)
        t = time.perf_counter()
        pic = compute_picture(run["records"], cfg)
        ms = (time.perf_counter() - t) * 1e3
        out.append({"run": Path(path).stem, "scenario": Path(run["scenario"]).stem,
                    "seed": run["seed"], "variant": v, "compute_ms": round(ms, 1),
                    **quality(pic, run["truth_of"])})
    return out


def evaluate(paths: list[str], variants: list[dict[str, Any]], jobs: int = 2) -> list[dict[str, Any]]:
    with ProcessPoolExecutor(jobs) as ex:
        return [r for rs in ex.map(eval_run, [(p, variants) for p in paths]) for r in rs]


def table(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """Суммы показателей по сценарию и варианту; время расчёта — наибольшее."""
    agg: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for r in rows:
        vk = json.dumps(r["variant"], ensure_ascii=False, sort_keys=True)
        a = agg[r["scenario"]].setdefault(vk, {k: 0 for k in KEYS} | {"runs": 0, "compute_ms_max": 0.0})
        for k in KEYS:
            a[k] += r[k]
        a["runs"] += 1
        a["compute_ms_max"] = max(a["compute_ms_max"], r["compute_ms"])
    return agg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--assoc", action="append", default=[],
                    help="вариант: rules | ai | ai:p_min=0.6 | ai:model=путь")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--out")
    a = ap.parse_args()
    variants = []
    for s in a.assoc or ["rules", "ai"]:
        name, _, opts = s.partition(":")
        v: dict[str, Any] = {"association": name}
        for o in filter(None, opts.split(",")):
            k, _, val = o.partition("=")
            v["ai_p_min" if k == "p_min" else "ai_model"] = float(val) if k == "p_min" else val
        variants.append(v)
    rows = evaluate(a.runs, variants, a.jobs)
    agg = table(rows)
    for sc, vs in sorted(agg.items()):
        for vk, m in vs.items():
            print(sc, vk, {k: m[k] for k in (*KEYS, "runs", "compute_ms_max")})
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps({"runs": [Path(p).name for p in a.runs], "rows": rows,
                                           "totals": agg}, ensure_ascii=False, indent=1), "utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
