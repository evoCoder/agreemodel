"""Реплицируемое хранилище информационной картины (Р1.1–Р1.5).

Модель данных. Запись принадлежит одному борту-источнику (origin) и имеет ключ внутри него
(``trk/<id>`` — локальный трек, ``uav`` — состояние своего БВС). Писать запись может только
её источник, поэтому конфликтов записи нет: для каждого (origin, key) хранится последняя
версия с наибольшим порядковым номером seq источника. Это CRDT (отображение регистров
с единственным писателем): слияние — покомпонентный максимум по seq, оно коммутативно,
ассоциативно и идемпотентно, поэтому копии сходятся при любом порядке и повторах
доставки (Р1.4).

Синхронизация. Вектор версий vv[origin] — наибольший seq источника, до которого копия
**полна**: для каждого ключа источника у неё есть последняя версия с seq ≤ vv[origin].
Дельта для узла с вектором d по источнику o — все записи o с seq > d[o] и диапазон
(d[o], vv[o]]. Получатель применяет часть дельты, только если начало диапазона не больше
его текущего vv[o] — тогда свойство полноты сохраняется и после потерь и перестановок
сообщений. Длинный диапазон передаётся частями (parts_for): часть (a, b] несёт последние
версии ключей с a < seq ≤ b, а ключ, перезаписанный источником позже b, придёт в части,
покрывающей его новую версию. Поэтому в процессе приёма частей копия может временно
держать прежнюю версию такого ключа; когда vv[o] доходит до версии источника, копия
совпадает с источником (последняя версия каждого ключа входит в покрывающую её часть). Это обобщение механизма seq/ack/resend канала «борт — земля» на сеть
«борт — борт»: передаётся только недостающее, после 15 мин разрыва — изменившиеся
записи, а не вся история.

Обязательные сведения о записи (Р1.2): идентификатор (origin, key), источник (origin),
время (t_us — метка источника), пространственная привязка и актуальность — в данных.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

HEADER_BYTES = 24  # origin, key, seq, t_us в компактной двоичной форме


def _estimate_size(key: str, data: Mapping[str, Any]) -> int:
    size = HEADER_BYTES + len(key)
    for v in data.values():
        if isinstance(v, (tuple, list)):
            size += 4 * len(v)
        elif isinstance(v, str):
            size += len(v)
        else:
            size += 4
    return size


@dataclass(frozen=True)
class Record:
    origin: str
    key: str
    seq: int
    t_us: int
    data: Mapping[str, Any]
    size: int = field(default=0, compare=False)

    @property
    def gkey(self) -> tuple[str, str]:
        return (self.origin, self.key)


@dataclass(frozen=True)
class DeltaPart:
    origin: str
    from_excl: int
    to_incl: int
    records: tuple[Record, ...]


@dataclass(frozen=True)
class Delta:
    parts: tuple[DeltaPart, ...]

    @property
    def size(self) -> int:
        return 8 + sum(12 + sum(r.size for r in p.records) for p in self.parts)

    def __bool__(self) -> bool:
        return bool(self.parts)

    @property
    def n_records(self) -> int:
        return sum(len(p.records) for p in self.parts)


class ReplicatedStore:
    def __init__(self, node: str) -> None:
        self.node = node
        self._by_origin: dict[str, dict[str, Record]] = {}
        self._vv: dict[str, int] = {}

    # ------------------------------------------------------------------ запись
    def write(self, key: str, t_us: int, data: Mapping[str, Any]) -> Record:
        """Запись своего источника: новая версия с seq = vv[свой] + 1."""
        seq = self._vv.get(self.node, 0) + 1
        rec = Record(self.node, key, seq, t_us, dict(data), _estimate_size(key, data))
        self._by_origin.setdefault(self.node, {})[key] = rec
        self._vv[self.node] = seq
        return rec

    # ------------------------------------------------------------------ чтение
    def version_vector(self) -> dict[str, int]:
        return dict(self._vv)

    def records(self) -> Iterator[Record]:
        for origin in sorted(self._by_origin):
            recs = self._by_origin[origin]
            for key in sorted(recs):
                yield recs[key]

    def get(self, origin: str, key: str) -> Record | None:
        return self._by_origin.get(origin, {}).get(key)

    def __len__(self) -> int:
        return sum(len(r) for r in self._by_origin.values())

    def digest(self) -> str:
        """Хеш состояния: одинаков у копий с одинаковым содержимым."""
        h = hashlib.sha1()
        for r in self.records():
            h.update(f"{r.origin}|{r.key}|{r.seq};".encode())
        return h.hexdigest()

    # ------------------------------------------------------------------ синхронизация
    def missing_for(self, peer_vv: Mapping[str, int]) -> dict[str, int]:
        """Источники, по которым у нас есть больше, чем у узла с вектором peer_vv."""
        return {o: v for o, v in self._vv.items() if v > peer_vv.get(o, 0)}

    def delta_for(self, peer_vv: Mapping[str, int], origins: set[str] | None = None) -> Delta:
        parts = []
        for origin in sorted(self._vv):
            if origins is not None and origin not in origins:
                continue
            have = self._vv[origin]
            d = peer_vv.get(origin, 0)
            if have <= d:
                continue
            recs = tuple(
                sorted(
                    (r for r in self._by_origin.get(origin, {}).values() if r.seq > d),
                    key=lambda r: r.seq,
                )
            )
            parts.append(DeltaPart(origin, d, have, recs))
        return Delta(tuple(parts))

    def parts_for(self, origin: str, after: int, upto: int | None = None,
                  max_bytes: int | None = None) -> list[DeltaPart]:
        """Части по источнику для диапазона (after, upto] (upto = None — до своей версии),
        каждая не больше max_bytes. Часть (a, b] содержит последние версии ключей с
        a < seq ≤ b; ключ, перезаписанный позже b, придёт в части, покрывающей его новую
        версию. Получатель применяет части по порядку; копии сходятся, когда версия
        получателя доходит до версии источника."""
        have = self._vv.get(origin, 0)
        hi = have if upto is None else min(upto, have)
        if hi <= after:
            return []
        recs = sorted((r for r in self._by_origin.get(origin, {}).values()
                       if after < r.seq <= hi), key=lambda r: r.seq)
        if not max_bytes:
            return [DeltaPart(origin, after, hi, tuple(recs))]
        parts: list[DeltaPart] = []
        lo, buf, size = after, [], 12
        for r in recs:
            if buf and size + r.size > max_bytes:
                parts.append(DeltaPart(origin, lo, buf[-1].seq, tuple(buf)))
                lo, buf, size = buf[-1].seq, [], 12
            buf.append(r)
            size += r.size
        parts.append(DeltaPart(origin, lo, hi, tuple(buf)))
        return parts

    def apply(self, delta: Delta) -> int:
        """Слияние дельты; возвращает число записей, изменивших копию."""
        changed = 0
        for part in delta.parts:
            cur = self._vv.get(part.origin, 0)
            if part.from_excl > cur:
                continue  # пропуск в последовательности: часть отброшена, копия остаётся полной
            if part.to_incl <= cur:
                continue  # уже есть
            recs = self._by_origin.setdefault(part.origin, {})
            for r in part.records:
                old = recs.get(r.key)
                if old is None or r.seq > old.seq:
                    recs[r.key] = r
                    changed += 1
            self._vv[part.origin] = part.to_incl
        return changed
