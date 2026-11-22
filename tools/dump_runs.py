"""Прогоны сценариев с сохранением итогового хранилища и эталона — сырьё датасета модели сходства.

    python tools/dump_runs.py scenarios/ai/dense.toml --seeds 1-8 --out data/raw

На каждый прогон — файл <сценарий>_s<seed>.pkl: записи хранилища борта U01 после
сходимости (все борта совпадают), принадлежность треков объектам эталона (truth_of),
итоги прогона. Картину по этим записям можно пересчитать любой стратегией ассоциации
без повторного прогона: обмен и хранилище от ассоциации не зависят.
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agreemodel.cli import _apply_override
from agreemodel.config import load
from agreemodel.series import parse_seeds
from agreemodel.sim import Simulation


def one(args: tuple[str, list[str], int, str]) -> str:
    path, sets, seed, out = args
    sc = load(path)
    for e in sets:
        _apply_override(sc, e)
    _apply_override(sc, f"run.seed={seed}")
    t = time.time()
    sim = Simulation(sc)
    res = sim.run()
    truth_of = {(n.uid, k): v for n in sim.nodes.values() for k, v in n.truth_of.items()}
    n0 = sim.nodes[sim.alive()[0]]
    f = Path(out) / f"{Path(path).stem}_s{seed}.pkl"
    with open(f, "wb") as fh:
        pickle.dump({"scenario": path, "set": sets, "seed": seed,
                     "records": list(n0.store.records()), "truth_of": truth_of,
                     "summary": res.summary}, fh, protocol=5)
    return f"{f.name} {time.time() - t:.0f} с"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("scenarios", nargs="+")
    ap.add_argument("--seeds", default="1-8")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--out", default="data/raw")
    a = ap.parse_args()
    Path(a.out).mkdir(parents=True, exist_ok=True)
    tasks = [(p, a.set, s, a.out) for p in a.scenarios for s in parse_seeds(a.seeds)
             if not (Path(a.out) / f"{Path(p).stem}_s{s}.pkl").exists()]
    with ProcessPoolExecutor(a.jobs) as ex:
        for msg in ex.map(one, tasks):
            print(msg, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
