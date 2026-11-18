"""Серия прогонов: сценарий × варианты параметров × seed; сводка по показателям ФТ № 1.

    python -m agreemodel series scenarios/base.toml --seeds 1-10 \
        --vary node.protocol=c0,c1 --vary net.medium=per_node,shared --out runs/s1

Каждый прогон — строка runs.csv (параметры варианта, seed, показатели). Сводка
summary.json — по каждому варианту: число прогонов, наибольшее и 95-й перцентиль
времени восстановления и присоединения, кванта, сходимости, потери записей, трафик.
Результат детерминирован: одинаковые параметры и seed дают одинаковые показатели.
"""

from __future__ import annotations

import csv
import itertools
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from .config import load, to_dict


def parse_seeds(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b) + 1)) if b else [int(a)]
    return out


def parse_vary(specs: list[str]) -> list[dict[str, str]]:
    axes = []
    for s in specs:
        path, _, vals = s.partition("=")
        axes.append([(path, v) for v in vals.split(",")])
    return [dict(c) for c in itertools.product(*axes)] if axes else [{}]


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


def _one(args: tuple[str, dict[str, str], list[str], int]) -> dict[str, Any]:
    from .cli import _apply_override
    from .sim import run

    path, variant, fixed, seed = args
    sc = load(path)
    for expr in fixed:
        _apply_override(sc, expr)
    for k, v in variant.items():
        _apply_override(sc, f"{k}={v}")
    _apply_override(sc, f"run.seed={seed}")
    s = run(sc).summary
    row: dict[str, Any] = dict(variant)
    row["seed"] = seed
    part = [r["store_conv_s"] for r in s["restores"] if r["kind"] == "partition_end"]
    join = [r["store_conv_s"] for r in s["restores"] if r["kind"].startswith("join")]
    # не восстановилось до конца прогона — бесконечность (видно в сводке как null/inf)
    row["restore_s"] = max((x if x is not None else float("inf") for x in part), default=None)
    row["join_s"] = max((x if x is not None else float("inf") for x in join), default=None)
    row["quantum_max_s"] = s["quantum_s"]["max"]
    row["settle_s"] = s["settle_conv_s"] if s["settle_conv_s"] is not None else float("inf")
    row["identical"] = s["final_pictures_identical"]
    row["missing"] = sum(s["records_missing_per_uav"].values())
    row["mbytes"] = s["net"]["mbytes_sent"]
    row["kbps_per_uav"] = s["net"]["kbps_per_uav"]
    row["msgs"] = s["net"]["msgs_sent"]
    row["duplicates"] = s["picture"]["duplicates"]
    row["truth_detected"] = s["picture"]["truth_detected"]
    row["false_objects"] = s["picture"]["false_objects"]
    return row


def run_series(path: str, seeds: list[int], variants: list[dict[str, str]],
               fixed: list[str], jobs: int = 2) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks = [(path, v, fixed, s) for v in variants for s in seeds]
    if jobs > 1:
        with ProcessPoolExecutor(jobs) as ex:
            rows = list(ex.map(_one, tasks))
    else:
        rows = [_one(t) for t in tasks]
    summary = []
    for v in variants:
        rs = [r for r in rows if all(r.get(k) == val for k, val in v.items())]
        entry: dict[str, Any] = dict(v)
        entry["runs"] = len(rs)
        for key in ("restore_s", "join_s", "quantum_max_s", "settle_s"):
            vals = [r[key] for r in rs if r[key] is not None]
            entry[key] = {"max": max(vals, default=None), "p95": _pct(vals, 0.95),
                          "median": _pct(vals, 0.5),
                          "not_reached": sum(1 for x in vals if x == float("inf"))}
        entry["all_identical"] = all(r["identical"] for r in rs)
        entry["missing_total"] = sum(r["missing"] for r in rs)
        entry["kbps_per_uav_median"] = _pct([r["kbps_per_uav"] for r in rs], 0.5)
        entry["duplicates_median"] = _pct([r["duplicates"] for r in rs], 0.5)
        summary.append(entry)
    return rows, summary


def _clean(v: Any) -> Any:
    """inf (показатель не достигнут до конца прогона) → null в JSON."""
    if isinstance(v, float) and v == float("inf"):
        return None
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


def save(out: str | Path, path: str, seeds: list[int], variants: list[dict[str, str]],
         fixed: list[str], rows: list[dict[str, Any]], summary: list[dict[str, Any]]) -> None:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "runs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    sc = load(path)
    meta = {"scenario_file": str(path), "seeds": seeds, "fixed": fixed,
            "variants": variants, "scenario": to_dict(sc)}
    (out / "series.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(_clean(summary), ensure_ascii=False, indent=2),
                                      encoding="utf-8")
