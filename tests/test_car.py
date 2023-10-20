import asyncio
import os
import shutil

from ipfs_car_decoder import FileByteStream, stream_bytes, write_car_filesystem_to_path

DIR = os.path.dirname(__file__)

def test_raw_files():
    cid = 'bafkreibm6jg3ux5qumhcn2b3flc3tyu6dmlb4xa7u5bf44yegnrjhc4yeq'
    with open(os.path.join(DIR, 'fs', cid), 'rb') as f:
        raw_data = f.read()

    async def test():
        acc = b''
        async with FileByteStream(os.path.join(DIR, 'cars', f'{cid}.car')) as stream:
            async for chunk in stream_bytes(cid, stream):
                acc += chunk
        assert acc == raw_data 

    asyncio.run(test())

def test_directory():
    cid = 'bafybeia6po64b6tfqq73lckadrhpihg2oubaxgqaoushquhcek46y3zumm'
    temp_dir = '_test'
    async def test():
        async with FileByteStream(os.path.join(DIR, 'cars', f'{cid}.car')) as stream:
            await write_car_filesystem_to_path(cid, stream, DIR, temp_dir)

        for file in os.listdir(os.path.join(DIR, 'fs', cid)):
            with open(os.path.join(DIR, 'fs', cid, file), 'rb') as f:
                base_line = f.read()
            with open(os.path.join(DIR, temp_dir, file), 'rb') as f:
                testable = f.read()
            assert base_line == testable

        base_line_files = set(file for file in os.listdir(os.path.join(DIR, 'fs', cid)))
        testable_files = set(file for file in os.listdir(os.path.join(DIR, temp_dir)))
        assert base_line_files == testable_files

    try:
        asyncio.run(test())
    finally:
        shutil.rmtree(os.path.join(DIR, temp_dir), ignore_errors=True)
