Validate what an IPFS gateway returns by decoding and validating the CAR block.

Usage
-----

CARs can be read from memory or file and can be exported as a file/folder or byte stream

>>> import asyncio
>>> import aiohttp
...
>>> from ipfs_car_decoder import ChunkedMemoryByteStream, stream_bytes
...
>>> ipfs_hash = 'bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq'
...
>>> async def test():
...     stream = ChunkedMemoryByteStream()
...     async with aiohttp.ClientSession() as session:
...         async with session.get(f'https://ipfs.io/ipfs/{ipfs_hash}', headers={'Accept':'application/vnd.ipld.car'}) as resp:
...             resp.raise_for_status()
...             assert resp.content_type == 'application/vnd.ipld.car'
...             async for chunk, _ in resp.content.iter_chunks():
...                 await stream.append_bytes(chunk)
...     await stream.mark_complete()
...     acc = b''
...     async for chunk in stream_bytes(ipfs_hash, stream):
...         acc += chunk
...     print(acc)
...
>>> asyncio.run(test())
b'hello'

