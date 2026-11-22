"""Обучение модели сходства (готовые библиотеки: scikit-learn) и экспорт для бортового вывода.

    python tools/train.py data/pairs_v1.npz --val-runs data/raw/*_s[78].pkl \
        --out src/agreemodel/models/sim_v1.json --report analysis/c4_ai/train.json

Обучение — на парах seed 1–6, проверка — seed 7–8 (разбиение по прогонам, без утечки).
Сравниваются логистическая регрессия, перцептрон (MLP) и градиентный бустинг (только как
ориентир качества: на борт идёт модель, вывод которой укладывается в несколько десятков
строк без зависимостей). Выбранная модель квантуется до int8 (масштаб на слой); порог
p_min подбирается по качеству картины на проверочных прогонах (eval_assoc).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss, roc_auc_score
from sklearn.neural_network import MLPClassifier
from sklearn.preprocessing import StandardScaler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

from eval_assoc import evaluate, table

from agreemodel.similarity import FEATURES, Model


def quantize(W: np.ndarray) -> tuple[list[list[int]], float]:
    s = float(np.abs(W).max()) / 127.0 or 1.0
    return np.round(W / s).astype(int).tolist(), s


def export(name: str, scaler: StandardScaler, coefs: list[np.ndarray], intercepts: list[np.ndarray],
           p_min: float, meta: dict[str, Any]) -> dict[str, Any]:
    layers = []
    for W, b in zip(coefs, intercepts, strict=True):
        q, s = quantize(W.T)  # строки — выходы слоя
        layers.append({"w_int8": q, "scale": s, "b": [float(v) for v in b]})
    return {"name": name, "features": list(FEATURES), "p_min": p_min,
            "mean": scaler.mean_.tolist(), "scale": scaler.scale_.tolist(),
            "layers": layers, "meta": meta}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dataset")
    ap.add_argument("--train-seeds", default="1-6")
    ap.add_argument("--val-runs", nargs="+", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--jobs", type=int, default=2)
    a = ap.parse_args()
    d = np.load(a.dataset)
    X, y, seed = d["X"], d["y"].astype(int), d["seed"]
    lo, hi = (int(v) for v in a.train_seeds.split("-"))
    tr = (seed >= lo) & (seed <= hi)
    va = ~tr
    sc = StandardScaler().fit(X[tr])
    Xt, Xv = sc.transform(X[tr]), sc.transform(X[va])
    rep: dict[str, Any] = {"dataset": a.dataset, "pairs_train": int(tr.sum()),
                           "pairs_val": int(va.sum()), "pos_train": int(y[tr].sum()),
                           "pos_val": int(y[va].sum()), "models": {}}
    cands = {
        "logreg": LogisticRegression(C=1.0, max_iter=2000),
        "mlp16x8": MLPClassifier(hidden_layer_sizes=(16, 8), alpha=1e-3, max_iter=600,
                                 early_stopping=True, random_state=0),
        "hgb_reference": HistGradientBoostingClassifier(max_iter=300, random_state=0),
    }
    for n, m in cands.items():
        m.fit(Xt, y[tr])
        p = m.predict_proba(Xv)[:, 1]
        rep["models"][n] = {"auc_val": round(float(roc_auc_score(y[va], p)), 4),
                            "logloss_val": round(float(log_loss(y[va], p, labels=[0, 1])), 4)}
        print(n, rep["models"][n], flush=True)

    # бортовые кандидаты: логистическая регрессия и перцептрон, квантованные
    specs = {
        "logreg": export("logreg", sc, [cands["logreg"].coef_.T], [cands["logreg"].intercept_], 0.5, {}),
        "mlp16x8": export("mlp16x8", sc, cands["mlp16x8"].coefs_, cands["mlp16x8"].intercepts_, 0.5, {}),
    }
    tmp = Path(a.out).with_suffix(".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    best = None
    for n, spec in specs.items():
        mdl = Model(spec)
        pq = np.array([mdl.prob(list(f)) for f in X[va]])
        pr = cands[n].predict_proba(Xv)[:, 1]
        rep["models"][n]["auc_val_int8"] = round(float(roc_auc_score(y[va], pq)), 4)
        rep["models"][n]["max_abs_diff_int8"] = round(float(np.abs(pq - pr).max()), 4)
        f = tmp.with_name(f"{tmp.stem}_{n}.json")
        f.write_text(json.dumps(spec), "utf-8")
        grid = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]
        variants = [{"association": "ai", "ai_model": str(f), "ai_p_min": p} for p in grid]
        agg = table(evaluate(a.val_runs, variants + [{"association": "rules"}], a.jobs))
        score: dict[str, int] = {}
        for vs in agg.values():
            for vk, m in vs.items():
                score[vk] = score.get(vk, 0) + m["absorbed"] + m["duplicates"] + m["mixed_objects"] + m["false_objects"]
        rep["models"][n]["val_picture_errors"] = {
            json.loads(k).get("ai_p_min", "rules"): v for k, v in score.items()}
        rep["models"][n]["val_by_scenario"] = {
            sc_: {json.loads(k).get("ai_p_min", "rules"): {kk: m[kk] for kk in ("absorbed", "duplicates", "mixed_objects", "false_objects")}
                  for k, m in vs.items()} for sc_, vs in agg.items()}
        p_best = min(grid, key=lambda p: score[json.dumps({"ai_model": str(f), "ai_p_min": p, "association": "ai"}, sort_keys=True)])
        err = rep["models"][n]["val_picture_errors"][p_best]
        rep["models"][n]["p_min"] = p_best
        print(n, "ошибки картины (проверка):", rep["models"][n]["val_picture_errors"], flush=True)
        spec["p_min"] = p_best
        f.unlink()
        if best is None or err < best[0]:
            best = (err, n, spec)
    assert best is not None
    _, n, spec = best
    spec["meta"] = {"model": n, "train_seeds": a.train_seeds,
                    "val_runs": [Path(p).name for p in a.val_runs],
                    "dataset": Path(a.dataset).name, **rep["models"][n]}
    spec["meta"].pop("val_by_scenario", None)
    Path(a.out).write_text(json.dumps(spec, ensure_ascii=False, indent=1), "utf-8")
    rep["chosen"] = n
    Path(a.report).parent.mkdir(parents=True, exist_ok=True)
    Path(a.report).write_text(json.dumps(rep, ensure_ascii=False, indent=1), "utf-8")
    print("выбрана", n, "p_min", spec["p_min"], "→", a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
