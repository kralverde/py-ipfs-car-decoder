import os

import aiofiles
import attr
import dag_cbor

from typing import Set, List, Dict, Sequence, Mapping, MutableMapping, Optional, Union, AsyncIterator, Callable

from multiformats import CID

from ipfs_car_decoder.async_stream import CARByteStream

from unix_fs_exporter import export, recursive_export, BlockStore, UnixFSFile, UnixFSDirectory, RawNode, IdentityNode

_V2_HEADER_LENGTH = 40
_CIDV0_SHA2_256 = 0x12
_CIDV0_LENGTH = 0x20

_CID = Union[CID, str, bytes]


class CarDecodeException(Exception): pass


@attr.define(slots=True, frozen=True)
class CARStreamBlockIndex:
    cid: CID
    length: int
    block_length: int
    offset: int
    block_offset: int


class CARStreamBlockIndexer:
    # CID -> Index
    mapping: Dict[bytes, CARStreamBlockIndex]
    header: Optional[Mapping[str, dag_cbor.IPLDKind]]
    stream: CARByteStream

    def __init__(self, stream: CARByteStream):
        self.mapping = dict()
        self.stream = stream
        self.header = None

    @classmethod
    async def from_stream(cls, stream: CARByteStream) -> 'CARStreamBlockIndexer':
        indexer = cls(stream)

        header_length = await stream.read_var_int()
        if header_length <= 0:
            raise CarDecodeException('header length must be positive')
        raw_header = await stream.read_bytes(header_length)
        header = dag_cbor.decode(raw_header)
        if not isinstance(header, Mapping):
            raise CarDecodeException('the header must be a map')
        if header['version'] not in (1, 2):
            raise CarDecodeException(f'unknown car version {header["version"]}')
        if header['version'] == 1:
            if not isinstance(header['roots'], Sequence):
                raise CarDecodeException('roots must be a sequence')
        else:
            if 'roots' in header:
                raise CarDecodeException('roots cannot be in a v2 header')
            v2_header_raw = await stream.read_bytes(_V2_HEADER_LENGTH)
            btole64: Callable[[bytes], int] = lambda b: int.from_bytes(b, 'little')
            v2_header: Mapping[str, dag_cbor.IPLDKind] = {
                'version': 2,
                'characteristics': [
                    btole64(v2_header_raw[:8]),
                    btole64(v2_header_raw[8:16])
                ],
                'data_offset': btole64(v2_header_raw[16:24]),
                'data_size': btole64(v2_header_raw[24:32]),
                'index_offset': btole64(v2_header_raw[32:40])
            }
            data_offset = v2_header['data_offset']
            assert isinstance(data_offset, int)
            stream.move(data_offset)
            v1_header_length = await stream.read_var_int()
            if v1_header_length <= 0:
                raise CarDecodeException('the v1 header length must be positive')
            v1_header_raw = await stream.read_bytes(v1_header_length)
            header = dag_cbor.decode(v1_header_raw)
            if not isinstance(header, MutableMapping):
                raise CarDecodeException('the header must be a map')
            if header['version'] != 1:
                raise CarDecodeException('v1 header must be with the v2 header')
            if not isinstance(header['roots'], Sequence):
                raise CarDecodeException('roots must be a sequence')
            header.update(v2_header)
            data_size = header['data_size']
            assert isinstance(data_size, int)
            stream._limit = data_size + data_offset

        indexer.header = header

        while await stream.can_read_more():
            offset = stream.pos
            length = await stream.read_var_int()
            if length <= 0:
                raise CarDecodeException('block length must be positive')
            length += (stream.pos - offset)
            first = await stream.read_bytes(2, walk_forward=False)
            if first[0] == _CIDV0_SHA2_256 and first[1] == _CIDV0_LENGTH:
                raw_multihash = await stream.read_bytes(34)
                assert len(raw_multihash) == 34
                cid = CID.decode(raw_multihash)
            else:
                version = await stream.read_var_int()
                codec = await stream.read_var_int()
                multihash_code = await stream.read_var_int()
                multihash_length = await stream.read_var_int()
                raw_hash = await stream.read_bytes(multihash_length)
                assert len(raw_hash) == multihash_length
                cid = CID('base32', version, codec, (multihash_code, raw_hash))
            block_length = length - (stream.pos - offset)
            index = CARStreamBlockIndex(cid, length, block_length, offset, stream.pos)
            stream.move(stream.pos + block_length)
            indexer.mapping[cid.digest] = index
        return indexer


class IndexedBlockstore(BlockStore):
    def __init__(self, stream: CARByteStream, indexer: CARStreamBlockIndexer, validate: bool=True) -> None:
        self.stream = stream
        self.indexer = indexer
        self.validate = validate
        self.checked: Set[bytes] = set()

    @classmethod
    async def from_stream(cls, stream: CARByteStream) -> 'IndexedBlockstore':
        indexer = await CARStreamBlockIndexer.from_stream(stream)
        return cls(stream, indexer)

    async def get_block(self, cid: CID) -> bytes:
        digest = cid.digest
        try:
            index = self.indexer.mapping[digest]
        except KeyError:
            raise CarDecodeException(f'no index for {repr("base32")}')
        self.stream.move(index.block_offset)
        raw = await self.stream.read_bytes(index.block_length)
        if self.validate and digest not in self.checked:
            if cid != index.cid:
                raise CarDecodeException('cid does not match index')
            hash_fun, _ = cid.hashfun.implementation
            hashed = hash_fun(raw)
            if hashed != cid.raw_digest:
                raise CarDecodeException('cid does not match digest')
            self.checked.add(digest)
        return raw


async def stream_bytes(cid: _CID, stream: CARByteStream) -> AsyncIterator[bytes]:
    if isinstance(cid, (bytes, str)):
        cid = CID.decode(cid)
    if not isinstance(cid, CID):
        raise CarDecodeException('cid must be CID decodable')

    block_store = await IndexedBlockstore.from_stream(stream)

    result = await export(cid, block_store)
    if not isinstance(result, (UnixFSFile, RawNode, IdentityNode)):
        raise CarDecodeException(f'car stream for {cid} not convertable to bytes')

    async for chunk in result.content:
        yield chunk


async def write_car_filesystem_to_path(cid: _CID, stream: CARByteStream, parent_directory: str, name: str='') -> None:
    if isinstance(cid, (bytes, str)):
        cid = CID.decode(cid)
    if not isinstance(cid, CID):
        raise CarDecodeException('cid must be CID decodable')

    block_store = await IndexedBlockstore.from_stream(stream)

    async for entry in recursive_export(cid, block_store):
        local_path = entry.path
        if name:
            path_parts: List[str] = []
            head = local_path
            while True:
                head, tail = os.path.split(head)
                path_parts.append(tail)
                if not head:
                    break
            if CID.decode(path_parts[-1]).digest == cid.digest:
                path_parts[-1] = name
            local_path = os.path.join(*reversed(path_parts))
        path = os.path.join(parent_directory, local_path)
        if isinstance(entry, (UnixFSFile, RawNode, IdentityNode)):
            async with aiofiles.open(path, 'wb') as f:
                async for chunk in entry.content:
                    await f.write(chunk)
        elif isinstance(entry, UnixFSDirectory):
            os.makedirs(path, exist_ok=True)
        else:
            raise CarDecodeException(f'car stream for {cid} is not convertable to a file system')
