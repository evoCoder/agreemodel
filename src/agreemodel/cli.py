"""Командная строка: прогон сценария с сохранением итогов и временного ряда.

    python -m agreemodel run scenarios/base.toml --out runs/base
    python -m agreemodel run scenarios/base.toml --set net.loss=0.2 --set run.seed=11
    python -m agreemodel series scenarios/base.toml --seeds 1-10 --vary node.protocol=c0,c1 --out runs/s
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

from . import __version__
from .config import load, to_dict
from .sim import run


def _apply_override(sc, expr: str) -> None:
    path, _, raw = expr.partition("=")
    *parents, leaf = path.split(".")
    obj = sc
    for p in parents:
        obj = getattr(obj, p)
    if not hasattr(obj, leaf):
        raise SystemExit(f"неизвестный параметр: {path}")
    cur = getattr(obj, leaf)
    value = type(cur)(json.loads(raw)) if not isinstance(cur, str) else raw
    setattr(obj, leaf, value)


def picture_export(res) -> dict:
    """Итоговая общая картина каждого работающего борта — выход для смежных технологий
    (распределение задач) и для сравнения с эталоном внешнего симулятора."""
    out = {}
    for i in res.sim.alive():
        n = res.sim.nodes[i]
        pic = n.picture
        if pic is None:
            continue
        out[n.uid] = {
            "t_ref_us": pic.t_ref_us, "digest": pic.digest,
            "objects": [{"id": o.oid, "x": round(o.x, 2), "y": round(o.y, 2),
                         "cov": [round(v, 2) for v in o.P],
                         "cls": [round(v, 3) for v in o.cls],
                         "sources": [f"{a}/{b}" for a, b in o.members],
                         "unresolved": o.conflict} for o in pic.objects],
            "uavs": [{"uid": u.uid, "x": round(u.x, 2), "y": round(u.y, 2),
                      "t_us": u.t_us, "alive": u.alive} for u in pic.uavs],
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agreemodel", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="прогон сценария")
    r.add_argument("scenario")
    r.add_argument("--out", help="каталог результатов")
    r.add_argument("--set", action="append", default=[], help="переопределение: раздел.параметр=значение")
    r.add_argument("--quiet", action="store_true")
    sr = sub.add_parser("series", help="серия прогонов по seed и вариантам параметров")
    sr.add_argument("scenario")
    sr.add_argument("--seeds", default="1-10", help="например 1-10 или 3,7,11")
    sr.add_argument("--vary", action="append", default=[],
                    help="ось вариантов: раздел.параметр=знач1,знач2")
    sr.add_argument("--set", action="append", default=[], help="постоянное переопределение")
    sr.add_argument("--jobs", type=int, default=2)
    sr.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    if args.cmd == "series":
        from .series import parse_seeds, parse_vary, run_series, save
        seeds, variants = parse_seeds(args.seeds), parse_vary(args.vary)
        w0 = time.perf_counter()
        rows, summary = run_series(args.scenario, seeds, variants, args.set, args.jobs)
        save(args.out, args.scenario, seeds, variants, args.set, rows, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        print(f"прогонов: {len(rows)}, {time.perf_counter() - w0:.0f} с")
        return 0

    sc = load(args.scenario)
    for expr in args.set:
        _apply_override(sc, expr)
    w0 = time.perf_counter()
    res = run(sc, progress=not args.quiet)
    res.summary["wall_s"] = round(time.perf_counter() - w0, 1)
    res.summary["version"] = __version__
    print(json.dumps(res.summary, ensure_ascii=False, indent=2))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(json.dumps(res.summary, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        (out / "scenario.json").write_text(json.dumps(to_dict(sc), ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        (out / "picture.json").write_text(
            json.dumps(picture_export(res), ensure_ascii=False, indent=1), encoding="utf-8")
        with open(out / "series.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(res.series[0]))
            w.writeheader()
            w.writerows(res.series)
    return 0
