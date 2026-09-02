import asyncio

import pytest

from hlsd.memory import MemoryPressureError, VolatileSegmentStore, process_rss_bytes


def test_put_get_delete():
    store = VolatileSegmentStore(max_bytes=1000)
    asyncio.run(store.put(("s", "v", 0), b"x" * 10))
    asyncio.run(store.put(("s", "a", 0), b"y" * 5))
    assert store.get(("s", "v", 0)) == b"x" * 10
    assert store.bytes_used == 15
    store.delete(("s", "v", 0))
    assert store.bytes_used == 5


def test_put_nowait_enforces_cap():
    store = VolatileSegmentStore(max_bytes=10)
    store.put_nowait(("s", "v", 0), b"x" * 8)
    with pytest.raises(MemoryPressureError):
        store.put_nowait(("s", "v", 1), b"y" * 8)


def test_backpressure_waits_until_release():
    store = VolatileSegmentStore(max_bytes=10)

    async def scenario():
        await store.put(("s", "v", 0), b"x" * 8)
        task = asyncio.create_task(store.put(("s", "v", 1), b"y" * 8))
        await asyncio.sleep(0.05)
        assert not task.done()
        store.delete(("s", "v", 0))
        await asyncio.wait_for(task, timeout=1)
        assert store.get(("s", "v", 1)) == b"y" * 8

    asyncio.run(scenario())


def test_delete_prefix_releases_whole_source():
    store = VolatileSegmentStore(max_bytes=1 << 20)
    for seq in range(3):
        asyncio.run(store.put(("s", "v", seq), b"z" * 4))
    released = store.delete_prefix(("s",))
    assert released == 3
    assert store.bytes_used == 0


def test_process_rss_returns_positive():
    assert process_rss_bytes() > 0
