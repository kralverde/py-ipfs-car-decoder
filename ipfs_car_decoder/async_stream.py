from abc import ABC, abstractmethod

import asyncio

_MSB = 0x80
_REST = 0x7f

class AsyncByteStreamException(Exception):
    pass

class AsyncByteStream(ABC):
    def __init__(self):
        self._pos = 0
        self._limit = None

    @abstractmethod
    def can_read_more(self) -> bool:
        pass

    @abstractmethod
    async def read_slice(self, start: int, end_exclusive: int) -> bytes:
        pass

    def move(self, position: int) -> None:
        self._pos = position

    async def read_bytes(self, count: int, walk_forward=True) -> bytes:
        result = await self.read_slice(self._pos, count + self._pos)
        assert len(result) == count, f'expected {count} bytes, got {len(result)}'
        if walk_forward:
            self._pos += count
        return result
    
    async def read_u8(self) -> int:
        return (await self.read_bytes(1))[0]

    # https://github.com/chrisdickinson/varint/blob/master/decode.js
    async def read_var_int(self) -> int:
        result = 0
        shift = 0
        while True:
            if shift > 49:
                raise AsyncByteStreamException(f'cannot decode varint (at pos {self._pos})')
            num = await self.read_u8()
            result += ((num & _REST) << shift) if (shift < 28) else ((num & _REST) * pow(2, shift))
            shift += 7
            if num < _MSB: break
        return result 

class ChunkedMemoryAsyncByteStream(AsyncByteStream):
    def __init__(self):
        super().__init__()
        self._complete = False
        self._bytes = bytearray()
        self._added_bytes_cond = asyncio.Condition()

    async def mark_complete(self) -> None:
        self._complete = True
        async with self._added_bytes_cond:
            self._added_bytes_cond.notify_all()

    async def append_bytes(self, b: bytes) -> None:
        if self._complete:
            raise AsyncByteStreamException('tried to append bytes but complete flag was set!')
        assert isinstance(b, bytes)
        self._bytes.extend(b)
        async with self._added_bytes_cond:
            self._added_bytes_cond.notify_all()

    async def read_slice(self, start: int, end_exclusive: int) -> bytes:
        if start > end_exclusive:
            raise AsyncByteStreamException('only positive slices are allowed')
        if self._limit and end_exclusive > self._limit:
            raise AsyncByteStreamException('limit will be breached')
        while end_exclusive > len(self._bytes):
            if self._complete:
                raise AsyncByteStreamException('waiting for bytes, but complete flag was set!')
            else:
                async with self._added_bytes_cond:
                    await self._added_bytes_cond.wait()
        return bytes(self._bytes[start:end_exclusive])
    
    def can_read_more(self) -> bool:
        return self._pos < (self._limit or (len(self._bytes) - 1))

