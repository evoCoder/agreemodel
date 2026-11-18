"""Командная строка: прогон сценария с сохранением итогов и временного ряда.

    python -m agreemodel run scenarios/base.toml --out runs/base
    python -m agreemodel run scenarios/base.toml --set net.loss=0.2 --set run.seed=11
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agreemodel", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="прогон сценария")
    r.add_argument("scenario")
    r.add_argument("--out", help="каталог результатов")
    r.add_argument("--set", action="append", default=[], help="переопределение: раздел.параметр=значение")
    r.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

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
        with open(out / "series.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(res.series[0]))
            w.writeheader()
            w.writerows(res.series)
    return 0
