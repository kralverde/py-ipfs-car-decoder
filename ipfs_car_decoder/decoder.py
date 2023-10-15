import attr
import dag_cbor

from typing import Dict, Sequence, Mapping

from multiformats import CID

from ipfs_car_decoder.async_stream import AsyncByteStream

_V2_HEADER_LENGTH = 40
_CIDV0_SHA2_256 = 0x12
_CIDV0_LENGTH = 0x20

class CarDecodeException(Exception): pass

@attr.define(slots=True, frozen=True)
class BlockIndex:
    cid: CID
    length: int
    block_length: int
    offset: int
    block_offset: int

class BlockIndexer:
    # CID -> Index
    mapping: Dict[bytes, BlockIndex]
    stream: AsyncByteStream

    def __init__(self, stream: AsyncByteStream):
        self.mapping = dict()
        self.stream = stream

    @classmethod
    async def from_byte_stream(cls, stream: AsyncByteStream) -> 'BlockIndexer':
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
            btole64 = lambda b: int.from_bytes(b, 'little')
            v2_header = {
                'version': 2,
                'characteristics': [
                    btole64(v2_header_raw[:8]),
                    btole64(v2_header_raw[8:16])
                ],
                'data_offset': btole64(v2_header_raw[16:24]),
                'data_size': btole64(v2_header_raw[24:32]),
                'index_offset': btole64(v2_header_raw[32:40])
            }
            stream.move(v2_header['data_offset'])
            v1_header_length = await stream.read_var_int()
            if v1_header_length <= 0:
                raise CarDecodeException('the v1 header length must be positive')
            v1_header_raw = await stream.read_bytes(v1_header_length)
            header = dag_cbor.decode(v1_header_raw)
            if header['version'] != 1:
                raise CarDecodeException('v1 header must be with the v2 header')
            if not isinstance(header['roots'], Sequence):
                raise CarDecodeException('roots must be a sequence')
            header.update(v2_header)
            stream._limit = header['data_size'] + header['data_offset']

        while stream.can_read_more():
            offset = stream._pos
            length = await stream.read_var_int()
            if length <= 0:
                raise CarDecodeException('block length must be positive')
            length += (stream._pos - offset)
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
            block_length = length - (stream._pos - offset)
            index = BlockIndex(cid, length, block_length, offset, stream._pos)
            stream.move(stream._pos + block_length)
            indexer.mapping[bytes(cid)] = index
        return indexer

if __name__ == '__main__':
    import aiohttp
    import asyncio

    from ipfs_car_decoder.async_stream import ChunkedMemoryAsyncByteStream

    ipfs_hash = 'bafybeih47mtobjeh3kclj2xfk3z5fyc5pnpgz3bht6h3ixqhayjksyojqu'
    stream = ChunkedMemoryAsyncByteStream()

    async def test():
        task = asyncio.create_task(BlockIndexer.from_byte_stream(stream))
        async with aiohttp.ClientSession() as session:
            async with session.get(f'https://ipfs.io/ipfs/{ipfs_hash}', headers={'Accept': 'application/vnd.ipld.car'}) as resp:
                async for chunk, _ in resp.content.iter_chunks():
                    await stream.append_bytes(chunk)
        indexer = await task
        for cid_raw, index in indexer.mapping.items():
            cid = CID.decode(cid_raw)
            stream.move(index.block_offset)
            raw = await stream.read_bytes(index.block_length)
            assert cid == index.cid
            hash_fun, _ = cid.hashfun.implementation
            hashed = hash_fun(raw)
            assert hashed == cid.raw_digest

    asyncio.run(test())
