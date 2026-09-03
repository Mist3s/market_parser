"""Протокол между ``api`` и ``forge``: line-JSON поверх AF_UNIX.

Граница здесь физическая, и это её главное свойство. ``api`` умеет только
попросить минтинг и подождать; браузер живёт в другом процессе, и никакая
ошибка в обработчике запроса не может его запустить. Дедлайн принуждается на
стороне ожидающего: ``api`` перестаёт ждать сокет, ``forge`` продолжает
работу до своего собственного потолка, и её результат достаётся следующему
запросу.

**Коалесцирование двухъярусное, и оба яруса нужны.** Ярус сессии: два запроса
к одному маркетплейсу через один адрес ждут ОДИН минтинг, а не два — иначе
всплеск из трёх запросов запустил бы три браузера. Ярус ожидания: повторный
запрос, пришедший, пока минтинг уже идёт, присоединяется к нему, а не встаёт
в очередь за вторым.

Кадры прогресса с ETA существуют ровно чтобы ``api`` мог сказать клиенту
осмысленный ``Retry-After``, прочитав его из памяти процесса, а не спросив
по сокету уже за дедлайном.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Op = Literal["mint", "render", "ping"]

#: Потолок минтинга. К ручке отношения не имеет: минтинг идёт вне запроса.
MINT_HARD_CAP_MS = 25_000


@dataclass(frozen=True, slots=True)
class Request:
    op: Op
    marketplace: str
    proxy_id: int
    url: str | None = None
    request_id: str = ""

    def encode(self) -> bytes:
        return (json.dumps(asdict(self), ensure_ascii=False) + "\n").encode()

    @classmethod
    def decode(cls, line: bytes) -> Request:
        d = json.loads(line)
        return cls(
            op=d["op"],
            marketplace=d["marketplace"],
            proxy_id=int(d["proxy_id"]),
            url=d.get("url"),
            request_id=d.get("request_id", ""),
        )

    @property
    def key(self) -> tuple[str, str, int]:
        """Ключ коалесцирования: операция, маркетплейс, адрес."""
        return (self.op, self.marketplace, self.proxy_id)


@dataclass(frozen=True, slots=True)
class Frame:
    """Кадр ответа. ``progress`` может прийти несколько раз, ``done`` — один."""

    kind: Literal["progress", "done", "error"]
    eta_ms: int | None = None
    ok: bool | None = None
    detail: str = ""

    def encode(self) -> bytes:
        return (json.dumps(asdict(self), ensure_ascii=False) + "\n").encode()

    @classmethod
    def decode(cls, line: bytes) -> Frame:
        d = json.loads(line)
        return cls(
            kind=d["kind"], eta_ms=d.get("eta_ms"), ok=d.get("ok"), detail=d.get("detail", "")
        )


@dataclass
class Coalescer:
    """Двухъярусное коалесцирование запросов к forge."""

    _inflight: dict[tuple[str, str, int], asyncio.Task] = field(default_factory=dict)
    #: Последняя известная ETA по ключу. Читается ИЗ ПАМЯТИ, без сокета:
    #: вызов без дедлайна после его истечения превращал бы обещанный 202 в
    #: краевой 504.
    last_eta_ms: dict[tuple[str, str, int], int] = field(default_factory=dict)

    def inflight(self, key: tuple[str, str, int]) -> bool:
        task = self._inflight.get(key)
        return task is not None and not task.done()

    def eta(self, key: tuple[str, str, int], default: int = MINT_HARD_CAP_MS) -> int:
        return self.last_eta_ms.get(key, default)

    def note_eta(self, key: tuple[str, str, int], eta_ms: int) -> None:
        self.last_eta_ms[key] = eta_ms

    async def run(self, key: tuple[str, str, int], factory) -> Any:
        """Присоединиться к идущей работе или начать новую.

        Ключевое: ждущие получают результат ОДНОЙ работы. Три одновременных
        запроса к одному маркетплейсу через один адрес запускают один браузер,
        а не три — при памяти в 350 МБ на инстанс это разница между работой и
        OOM.
        """
        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.ensure_future(factory())
            self._inflight[key] = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done():
                self._inflight.pop(key, None)

    def forget(self, key: tuple[str, str, int]) -> None:
        self._inflight.pop(key, None)
        self.last_eta_ms.pop(key, None)
