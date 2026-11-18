"""Гибридные логические часы (HLC, Kulkarni et al., 2014).

Внешнего опорного времени у роя нет (раздел 2 требований), часы бортов расходятся.
HLC даёт метки, которые:
* близки к физическому времени узла (не убегают дальше максимального расхождения часов);
* согласованы с причинностью: событие, узнавшее о другом, получает метку больше.

Метка — (wall_us, counter, node); сравнение лексикографическое, node разрывает равенство,
поэтому порядок меток полный и одинаковый на всех бортах.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Timestamp:
    wall_us: int
    counter: int
    node: str


class HLC:
    def __init__(self, node: str, clock_us: Callable[[], int]) -> None:
        self.node = node
        self._clock = clock_us
        self._l = 0
        self._c = 0

    def now(self) -> Timestamp:
        """Метка локального события (запись в хранилище, отправка)."""
        pt = self._clock()
        if pt > self._l:
            self._l, self._c = pt, 0
        else:
            self._c += 1
        return Timestamp(self._l, self._c, self.node)

    def update(self, remote: Timestamp) -> Timestamp:
        """Учёт метки принятого сообщения; возвращает метку события приёма."""
        pt = self._clock()
        l_new = max(self._l, remote.wall_us, pt)
        if l_new == self._l == remote.wall_us:
            c_new = max(self._c, remote.counter) + 1
        elif l_new == self._l:
            c_new = self._c + 1
        elif l_new == remote.wall_us:
            c_new = remote.counter + 1
        else:
            c_new = 0
        self._l, self._c = l_new, c_new
        return Timestamp(self._l, self._c, self.node)
