"""Связка симулятора с веб-страницей (исполняется в браузере в Pyodide и нативно в тестах).

Модель (`agreemodel`) не изменяется: здесь только сборка сценария из параметров страницы,
снятие кадров для карты и передача хода выполнения. Показатели берутся из того же
`summary`, что и в журнале циклов.
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


class WebSimulation(Simulation):
    """Прогон с кадрами для карты и ходом выполнения.

    Кадры снимаются в моменты выборки модели (`_sample`), не чаще `frame_s`.
    """

    def __init__(self, sc: Scenario, post: Post | None, frame_s: float, view_uav: int) -> None:
        super().__init__(sc)
        self._post = post
        self._frame_s = frame_s
        self._next_frame = 0.0
        self._view = view_uav
        self._total = sc.run.duration_s + getattr(sc.run, "settle_s", 0.0)
        self._last_progress = -1
        self.frames: list[dict[str, Any]] = []

    def _sample(self, t: float, alive: list[int]) -> None:  # noqa: D401 — расширение выборки
        super()._sample(t, alive)
        pct = int(100 * t / self._total) if self._total else 100
        if self._post and pct != self._last_progress:
            self._last_progress = pct
            self._post(json.dumps({"type": "progress", "t": round(t, 1), "pct": pct}))
        if t + 1e-9 >= self._next_frame:
            self._next_frame += self._frame_s
            try:
                self.frames.append(self._frame(t, alive))
            except Exception as e:  # кадр — только для показа, на прогон не влияет
                if self._post:
                    self._post(json.dumps({"type": "warn", "text": f"кадр {t:.1f} с: {e}"}))

    def _frame(self, t: float, alive: list[int]) -> dict[str, Any]:
        w = self.world
        comps = self._components(alive, t)
        group_of = {i: g for g, c in enumerate(comps) for i in c}
        uavs = [[u.idx, round(u.x, 1), round(u.y, 1), group_of.get(u.idx, -1)]
                for u in w.uavs.values() if u.alive]
        cx = sum(u[1] for u in uavs) / len(uavs)
        cy = sum(u[2] for u in uavs) / len(uavs)
        truth = [[round(o.x, 1), round(o.y, 1)] for o in w.objects]
        view = self._view if self._view in alive else alive[0]
        pic = self.nodes[view].picture
        objs: list[list[Any]] = []
        if pic is not None:
            truth_of: dict[tuple[str, str], str | None] = {}
            for n in self.nodes.values():
                for key, tid in n.truth_of.items():
                    truth_of[(n.uid, key)] = tid
            seen: Counter[str] = Counter()
            for o in sorted(pic.objects, key=lambda o: o.oid):
                real = [x for x in (truth_of.get(m) for m in o.members) if x is not None]
                if not real:
                    status = "false"
                else:
                    top = Counter(real).most_common(1)[0][0]
                    seen[top] += 1
                    status = "dup" if seen[top] > 1 else ("mixed" if len(set(real)) > 1 else "ok")
                if o.conflict:
                    status += "+conflict"
                objs.append([round(o.x, 1), round(o.y, 1), status])
        digests = [self.nodes[i].picture.digest if self.nodes[i].picture else "" for i in alive]
        return {"t": round(t, 1), "c": [round(cx, 1), round(cy, 1)], "uavs": uavs,
                "truth": truth, "pic": objs, "view": view, "groups": len(comps),
                "agreed": len(set(digests)) == 1}


def run_json(raw_json: str, post: Post | None = None, frame_s: float = 1.0,
             view_uav: int = 0) -> str:
    """Прогон сценария; вход и выход — JSON (удобно для обмена с JavaScript)."""
    raw = json.loads(raw_json)
    sc = scenario_from(raw)
    w0 = time.perf_counter()
    sim = WebSimulation(sc, post, frame_s, view_uav)
    res = sim.run(progress=False)
    summary = res.summary
    summary["wall_s"] = round(time.perf_counter() - w0, 1)
    summary["version"] = __version__
    return json.dumps({"summary": summary, "series": res.series, "frames": sim.frames,
                       "scenario": to_dict(sc)}, ensure_ascii=False)
