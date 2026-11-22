"""Датасет пар «объект картины — трек» для модели сходства из сохранённых прогонов с эталоном.

    python tools/make_dataset.py data/raw/*_s[1-8].pkl --out data/pairs_v1.npz

Повтор цикла ассоциации ``fusion.compute_picture`` по хранилищу прогона, в котором решения
принимает учитель по эталону: трек присоединяется к кандидату того же объекта эталона, иначе
открывает новый объект. Для каждого трека записываются признаки ``similarity.pair_features``
со всеми кандидатами в соседних ячейках (метка 1 — тот же объект эталона, 0 — другой или
трек от ложных обнаружений). Так в датасет попадают и трудные пары: соседи по группе,
треки, потерянные в разное время, треки одного объекта, разнесённые ошибкой навигации.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import pickle
import sys
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agreemodel.config import FusionCfg
from agreemodel.fusion import PicObject, TrackView, _track_state, ci_fuse
from agreemodel.similarity import FEATURES, pair_features, track_views_by_origin


def pairs_of_run(run: dict, cfg: FusionCfg) -> tuple[list[list[float]], list[int], list[int], list[Counter]]:
    truth_of = run["truth_of"]
    tracks = [r for r in run["records"] if r.key.startswith("trk/") and r.data.get("st") != "M"]
    t_ref = max([r.data["t1"] for r in tracks] + [r.t_us for r in run["records"] if r.key == "uav"])
    tracks.sort(key=lambda r: (r.data["t0"], r.origin, r.key))
    recs = {r.gkey: r for r in tracks}
    by_origin = track_views_by_origin(tracks, t_ref, cfg)
    cell = cfg.cell_m
    objs: list[PicObject] = []
    otruth: list[Counter] = []
    grid: dict[tuple[int, int], list[int]] = {}
    X: list[list[float]] = []
    y: list[int] = []
    grp: list[int] = []  # номер трека: пары одного решения

    def cell_of(x: float, yy: float) -> tuple[int, int]:
        return (math.floor(x / cell), math.floor(yy / cell))

    for ti, r in enumerate(tracks):
        x, yy, P = _track_state(r, t_ref, cfg)
        t0, t1, cls = r.data["t0"], r.data["t1"], tuple(r.data["cls"])
        tv = TrackView(r, x, yy, P, t0, t1, cls)
        tid = truth_of.get(r.gkey)
        cx, cy = cell_of(x, yy)
        right = []
        for k in sorted({k for gx in (cx - 1, cx, cx + 1) for gy in (cy - 1, cy, cy + 1)
                         for k in grid.get((gx, gy), ())}):
            o = objs[k]
            f = pair_features(o, tv, [recs[g] for g in o.members], t_ref, cfg, by_origin[r.origin])
            if f is None:
                continue
            lab = int(tid is not None and otruth[k].most_common(1)[0][0] == tid)
            X.append(f)
            y.append(lab)
            grp.append(ti)
            if lab:
                right.append((f[0], k))
        if right:
            k = min(right)[1]
            o = objs[k]
            old = cell_of(o.x, o.y)
            o.x, o.y, o.P = ci_fuse(o.x, o.y, o.P, x, yy, P)
            o.members.append(r.gkey)
            o._intervals.setdefault(r.origin, []).append((t0, t1))
            o._log_cls = [a + math.log(max(b, 1e-6)) for a, b in zip(o._log_cls, cls, strict=True)]
            n = len(o.members)
            m = max(o._log_cls)
            ex = [math.exp((v - m) / n) for v in o._log_cls]
            s = sum(ex)
            o.cls = tuple(v / s for v in ex)
            o.t_last_us = max(o.t_last_us, t1)
            otruth[k][tid] += 1
            new = cell_of(o.x, o.y)
            if new != old:
                grid[old].remove(k)
                grid.setdefault(new, []).append(k)
        else:
            oid = "O" + hashlib.sha1(f"{r.origin}/{r.key}".encode()).hexdigest()[:10]
            objs.append(PicObject(oid, x, yy, P, cls, [r.gkey], t1,
                                  _intervals={r.origin: [(t0, t1)]},
                                  _log_cls=[math.log(max(b, 1e-6)) for b in cls]))
            otruth.append(Counter({tid: 1}))
            grid.setdefault((cx, cy), []).append(len(objs) - 1)
    return X, y, grp, otruth


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    cfg = FusionCfg()
    Xs, ys, gs, runs_id, seeds, scen = [], [], [], [], [], []
    names = []
    g_off = 0
    for i, p in enumerate(sorted(a.runs)):
        run = pickle.loads(Path(p).read_bytes())
        X, y, g, _ = pairs_of_run(run, cfg)
        Xs += X
        ys += y
        gs += [v + g_off for v in g]
        g_off += (max(g) + 1) if g else 0
        runs_id += [i] * len(y)
        seeds += [run["seed"]] * len(y)
        scen += [Path(run["scenario"]).stem] * len(y)
        names.append(Path(p).name)
        print(f"{Path(p).name}: пар {len(y)}, из них «тот же объект» {sum(y)}", flush=True)
    np.savez_compressed(a.out, X=np.array(Xs, dtype=np.float64), y=np.array(ys, dtype=np.int8),
                        group=np.array(gs), run=np.array(runs_id), seed=np.array(seeds),
                        scenario=np.array(scen), features=np.array(FEATURES), runs=np.array(names))
    print(f"всего пар {len(ys)}, положительных {sum(ys)} → {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
