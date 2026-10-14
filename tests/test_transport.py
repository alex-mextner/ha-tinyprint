import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1] / "custom_components/tinyprint"
pkg = types.ModuleType("tinyprint_test")
pkg.__path__ = [str(ROOT)]
sys.modules["tinyprint_test"] = pkg
spec = importlib.util.spec_from_file_location("tinyprint_test.transport", ROOT / "transport.py")
t = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = t
spec.loader.exec_module(t)


class FakeClient:
    def __init__(self, fail_at=None, hold=False, notify_failure=False):
        self.writes = []
        self.disconnected = False
        self.stopped = False
        self.fail_at = fail_at
        self.hold = hold
        self.notify_failure = notify_failure
        self.is_connected = True
        self.services = self

    def get_characteristic(self, uuid):
        return types.SimpleNamespace(properties=["write-without-response", "notify"])

    async def start_notify(self, uuid, callback):
        if self.notify_failure:
            raise RuntimeError("notify failed")
        self.callback = callback
        if self.hold:
            callback(None, bytes.fromhex("5178ae0101001070ff"))

    async def stop_notify(self, uuid):
        self.stopped = True

    async def disconnect(self):
        self.disconnected = True
        self.is_connected = False

    async def write_gatt_char(self, uuid, data, response):
        assert response is False and len(data) <= 20
        if self.fail_at is not None and len(self.writes) == self.fail_at:
            raise RuntimeError("write failed")
        self.writes.append(bytes(data))


KW = dict(chunk_delay=0, row_delay=0, settle_delay=0, pause_timeout=0.02, total_timeout=0.2)


def test_send_records_bytes_and_disconnects():
    async def check():
        client = FakeClient()
        stats = await t.send_bitmap(client, bytes(48), 1, **KW)
        assert stats["rows"] == 1
        assert stats["bytes_written"] == sum(map(len, client.writes))
        assert client.disconnected and client.stopped
        assert stats["confirmation"] == "transport_only"

    asyncio.run(check())


def test_failure_does_not_replay_partial_job():
    async def check():
        client = FakeClient(fail_at=2)
        with pytest.raises(RuntimeError, match="write failed"):
            await t.send_bitmap(client, bytes(48), 1, **KW)
        assert len(client.writes) == 2 and client.disconnected

    asyncio.run(check())


def test_full_buffer_times_out_and_cleans_up():
    async def check():
        client = FakeClient(hold=True)
        with pytest.raises(TimeoutError):
            await t.send_bitmap(client, bytes(48), 1, **KW)
        assert client.disconnected and not client.writes

    asyncio.run(check())


def test_ready_notification_resumes_paused_job():
    async def check():
        client = FakeClient(hold=True)

        async def resume():
            await asyncio.sleep(0.002)
            client.callback(None, bytes.fromhex("5178ae0101000000ff"))

        task = asyncio.create_task(resume())
        stats = await t.send_bitmap(client, bytes(48), 1, **KW)
        await task
        assert stats["flow_pauses"] == 1 and client.writes

    asyncio.run(check())


def test_cancellation_cleans_up_and_is_not_success():
    async def check():
        client = FakeClient(hold=True)
        task = asyncio.create_task(t.send_bitmap(client, bytes(48), 1, **KW))
        await asyncio.sleep(0.002)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.disconnected

    asyncio.run(check())


def test_notify_failure_still_disconnects():
    async def check():
        client = FakeClient(notify_failure=True)
        with pytest.raises(RuntimeError, match="notify failed"):
            await t.send_bitmap(client, bytes(48), 1, **KW)
        assert client.disconnected

    asyncio.run(check())


def test_total_timeout_bounds_writes():
    async def check():
        client = FakeClient()
        kw = KW | dict(chunk_delay=0.02, total_timeout=0.03)
        with pytest.raises(TimeoutError):
            await t.send_bitmap(client, bytes(48), 1, **kw)
        assert client.disconnected and len(client.writes) < 10

    asyncio.run(check())


def test_missing_characteristics_disconnects_without_writes():
    async def check():
        client = FakeClient()
        client.get_characteristic = lambda uuid: None
        with pytest.raises(ValueError, match="GATT"):
            await t.send_bitmap(client, bytes(48), 1, **KW)
        assert client.disconnected and not client.writes

    asyncio.run(check())


def test_default_budget_scales_to_proxy_latency_at_maximum_height():
    frames = 2049
    estimated_cost = frames * (3 * (0.006 + 0.05) + 0.012) + 30
    assert t.transfer_timeout(2048) >= estimated_cost
    assert 45 <= t.transfer_timeout(1) < t.transfer_timeout(2048) <= 600
