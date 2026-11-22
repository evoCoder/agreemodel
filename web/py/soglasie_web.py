"""Связка симулятора с веб-страницей (исполняется в браузере в Pyodide и нативно в тестах).

Модель (`agreemodel`) не изменяется: здесь только сборка сценария из параметров страницы,
снятие кадров для анимации через открытый вызов ядра ``on_sample`` и передача хода
выполнения. Показатели берутся из того же ``summary``, что и в журнале циклов.

Кадр (каждые ``frame_s`` модельного времени):
  t, c       — время, центр группы;
  uavs       — [номер, x, y, подгруппа, работает, объектов в картине, сведений, вариант картины];
               «сведений» — число изменений, известных борту (сумма вектора версий хранилища);
               «вариант картины» — номер различающегося набора объектов картины
               (у бортов с одинаковым набором — один номер);
  edges      — пары бортов, связанных по радио в этот момент;
  groups     — число подгрупп связи; variants — число различающихся картин;
  truth      — объекты эталона [x, y, вошёл ли в картину выбранного борта];
  pic, view  — картина выбранного борта: [x, y, состояние], номер борта.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from agreemodel import __version__
from agreemodel.config import Scenario, _build, load, to_dict
from agreemodel.sim import Simulation

Post = Callable[[str], None]

_HERE = Path(__file__).resolve().parent


def base_scenario(path: str | None = None) -> dict[str, Any]:
    """Базовый сценарий в виде словаря (для формы параметров)."""
    p = Path(path) if path else _HERE / "scenarios" / "base.toml"
    return to_dict(load(p))


def scenario_from(raw: dict[str, Any]) -> Scenario:
    return _build(Scenario, raw)


def frame(sim: Simulation, t: float, alive: list[int], view: int) -> dict[str, Any]:
    """Снимок состояния модели для анимации. Только читает состояние."""
    comps = sim.components(alive, t)
    group_of = {i: g for g, c in enumerate(comps) for i in c}
    alive_set = set(alive)
    variants: dict[frozenset, int] = {}
    uavs = []
    for u in sorted(sim.world.uavs.values(), key=lambda u: u.idx):
        row = [u.idx, round(u.x, 2), round(u.y, 2), group_of.get(u.idx, -1), int(u.idx in alive_set), 0, 0, -1]
        if u.idx in alive_set:
            node = sim.nodes[u.idx]
            pic = node.picture
            oids = frozenset(o.oid for o in pic.objects) if pic else frozenset()
            row[5] = len(oids)
            row[6] = sum(node.store.version_vector().values())
            row[7] = variants.setdefault(oids, len(variants))
        uavs.append(row)
    edges = [[i, j] for i in alive for j in sim.net.neighbours(i, t) if i < j and j in alive_set]
    live = [u for u in uavs if u[4]]
    cx = sum(u[1] for u in live) / len(live)
    cy = sum(u[2] for u in live) / len(live)

    v = view if view in alive_set else alive[0]
    truth_of: dict[tuple[str, str], str | None] = {}
    for n in sim.nodes.values():
        for key, tid in n.truth_of.items():
            truth_of[(n.uid, key)] = tid
    pic = sim.nodes[v].picture
    objs: list[list[Any]] = []
    covered: set[str] = set()
    if pic is not None:
        seen: Counter[str] = Counter()
        for o in sorted(pic.objects, key=lambda o: o.oid):
            real = [x for x in (truth_of.get(m) for m in o.members) if x is not None]
            if not real:
                status = "false"
            else:
                top = Counter(real).most_common(1)[0][0]
                covered.add(top)
                seen[top] += 1
                status = "dup" if seen[top] > 1 else ("mixed" if len(set(real)) > 1 else "ok")
            if o.conflict:
                status += "+conflict"
            objs.append([round(o.x, 1), round(o.y, 1), status])
    truth = [[round(o.x, 1), round(o.y, 1), int(o.oid in covered)] for o in sim.world.objects]
    return {"t": round(t, 2), "c": [round(cx, 2), round(cy, 2)], "uavs": uavs, "edges": edges,
            "groups": len(comps), "variants": len(variants), "truth": truth, "pic": objs, "view": v}


class FrameRecorder:
    """Обработчик ``on_sample``: ход выполнения и кадры."""

    def __init__(self, sc: Scenario, post: Post | None, frame_s: float, view_uav: int) -> None:
        self.post = post
        self.frame_s = frame_s
        self.view = view_uav
        self.total = sc.run.duration_s + getattr(sc.run, "settle_s", 0.0)
        self.next_frame = 0.0
        self.last_pct = -1
        self.frames: list[dict[str, Any]] = []

    def __call__(self, sim: Simulation, t: float, alive: list[int]) -> None:
        pct = int(100 * t / self.total) if self.total else 100
        if self.post and pct != self.last_pct:
            self.last_pct = pct
            self.post(json.dumps({"type": "progress", "t": round(t, 1), "pct": pct}))
        if t + 1e-9 >= self.next_frame:
            self.next_frame += self.frame_s
            try:
                self.frames.append(frame(sim, t, alive, self.view))
            except Exception as e:  # кадр — только для показа, на прогон не влияет
                if self.post:
                    self.post(json.dumps({"type": "warn", "text": f"кадр {t:.1f} с: {e}"}))


def run_json(raw_json: str, post: Post | None = None, frame_s: float = 0.5,
             view_uav: int = 0) -> str:
    """Прогон сценария; вход и выход — JSON (удобно для обмена с JavaScript)."""
    raw = json.loads(raw_json)
    sc = scenario_from(raw)
    w0 = time.perf_counter()
    rec = FrameRecorder(sc, post, frame_s, view_uav)
    res = Simulation(sc, on_sample=rec).run(progress=False)
    summary = res.summary
    summary["wall_s"] = round(time.perf_counter() - w0, 1)
    summary["version"] = __version__
    return json.dumps({"summary": summary, "series": res.series, "frames": rec.frames,
                       "scenario": to_dict(sc)}, ensure_ascii=False)
