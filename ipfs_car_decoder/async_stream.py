from abc import ABC, abstractmethod
from typing import Optional, TYPE_CHECKING, Awaitable

import os

import aiofiles
import aiofiles.os
import asyncio

if TYPE_CHECKING:
    from _typeshed import FileDescriptorOrPath

_MSB = 0x80
_REST = 0x7f

class CARByteStreamException(Exception):
    pass

class CARByteStream(ABC):
    def __init__(self) -> None:
        self._pos = 0
        self._limit: Optional[int] = None

    @abstractmethod
    async def can_read_more(self) -> bool:
        pass

    @abstractmethod
    async def read_slice(self, start: int, end_exclusive: int) -> bytes:
        pass

    @property
    def pos(self)-> int:
        return self._pos

    def set_limit(self, position: int) -> None:
        self._limit = position

    def move(self, position: int) -> None:
        self._pos = position

    async def read_bytes(self, count: int, walk_forward: bool=True) -> bytes:
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
                raise CARByteStreamException(f'cannot decode varint (at pos {self._pos})')
            num = await self.read_u8()
            result += ((num & _REST) << shift) if (shift < 28) else ((num & _REST) * pow(2, shift))
            shift += 7
            if num < _MSB: break
        return result 

class ChunkedMemoryByteStream(CARByteStream):
    def __init__(self) -> None:
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
            raise CARByteStreamException('tried to append bytes but complete flag was set!')
        assert isinstance(b, bytes)
        self._bytes.extend(b)
        async with self._added_bytes_cond:
            self._added_bytes_cond.notify_all()

    async def read_slice(self, start: int, end_exclusive: int) -> bytes:
        if start >= end_exclusive:
            raise CARByteStreamException('only positive slices are allowed')
        if self._limit and end_exclusive > self._limit:
            raise CARByteStreamException('limit will be breached')
        while end_exclusive > len(self._bytes):
            if self._complete:
                raise CARByteStreamException('waiting for bytes, but complete flag was set!')
            else:
                async with self._added_bytes_cond:
                    await self._added_bytes_cond.wait()
        result = bytes(self._bytes[start:end_exclusive])
        return result
    
    async def can_read_more(self) -> bool:
        return self._pos < (self._limit or (len(self._bytes) - 1))
    
class FileByteStream(CARByteStream):
    _fd: Optional[int]

    def __init__(self, file: 'FileDescriptorOrPath', *, close_fd: bool=True) -> None:
        super().__init__()
        if isinstance(file, int):
            self._fd = file
        else:
            self._fd = os.open(file, os.O_RDONLY)
        self._size: Optional[int] = None
        self._close_fd = close_fd
        
    def close(self) -> None:
        if self._fd is None:
            raise CARByteStreamException('this stream was already closed')
        if self._close_fd:
            os.close(self._fd)
        self._fd = None

    async def __aenter__(self) -> 'FileByteStream':
        return self
    
    async def __aexit__(self, *args):  # type: ignore
        self.close()

    async def can_read_more(self) -> bool:
        if self._size is None:
            if self._fd is None:
                raise CARByteStreamException('this stream was already closed')    
            self._size = await aiofiles.os.path.getsize(self._fd)
        return self._pos < (self._limit or (self._size - 1))
    
    async def read_slice(self, start: int, end_exclusive: int) -> bytes:
        assert start >= 0
        if self._fd is None:
            raise CARByteStreamException('this stream was already closed')
        if start >= end_exclusive:
            raise CARByteStreamException('only positive slices are allowed')
        async with aiofiles.open(self._fd, 'rb', closefd=False) as f:
            file_pos = await f.tell()
            if file_pos != start:
                await f.seek(start)
            result = await f.read(end_exclusive - start)
            return result
        