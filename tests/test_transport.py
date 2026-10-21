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

sys.path.insert(0, str(Path(__file__).parent))
from att_fake import GET_STATE, state_reply  # noqa: E402


class FakeClient:
    def __init__(self, fail_at=None, hold=False, notify_failure=False, replies=(0, 0)):
        # One A3 reply flags byte per state request; an exhausted list stays silent.
        self.replies = list(replies)
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
        if bytes(data) == GET_STATE and self.replies:
            self.callback(None, state_reply(self.replies.pop(0)))


KW = dict(chunk_delay=0, pause_timeout=0.02, total_timeout=0.2, precheck_timeout=0.01)


def test_send_records_bytes_and_disconnects():
    async def check():
        client = FakeClient()
        stats = await t.send_bitmap(client, bytes(48), 1, **KW)
        assert stats["rows"] == 1
        assert stats["bytes_written"] == sum(map(len, client.writes))
        assert client.disconnected and client.stopped
        assert stats["printer_done"] is True
        assert stats["printer_state"] == [] and stats["confirmation"] == "transport_only"
        assert client.writes[-1] == GET_STATE

    asyncio.run(check())


def test_silent_printer_is_asked_again_then_fails_not_reported_as_sent():
    async def check():
        client = FakeClient(replies=())
        with pytest.raises(TimeoutError):
            await t.send_bitmap(client, bytes(48), 1, **KW | dict(state_poll=0.01))
        assert client.disconnected and client.writes[-1] == GET_STATE
        assert client.writes.count(GET_STATE) > 2

    asyncio.run(check())


def test_late_reply_after_silence_completes_the_job():
    async def check():
        client = FakeClient(replies=())
        original = client.write_gatt_char

        async def answer_second_request(uuid, data, response):
            await original(uuid, data, response)
            if bytes(data) == GET_STATE and client.writes.count(GET_STATE) == 2:
                client.callback(None, state_reply(0))

        client.write_gatt_char = answer_second_request
        stats = await t.send_bitmap(
            client, bytes(48), 1, **KW | dict(state_poll=0.01, total_timeout=1)
        )
        assert stats["printer_done"] is True

    asyncio.run(check())

    asyncio.run(check())


def test_link_is_held_while_the_printer_reports_printing():
    async def check():
        client = FakeClient(replies=(0, 0x80, 0x80, 0x00))
        stats = await t.send_bitmap(client, bytes(48), 1, **KW | dict(state_poll=0.001))
        assert client.writes.count(GET_STATE) == 4  # precheck + final + two polls
        assert stats["printer_done"] is True

    asyncio.run(check())


@pytest.mark.parametrize(
    "flags,fault", [(0x01, "out_of_paper"), (0x02, "cover_open"), (0x04, "overheating")]
)
def test_printer_fault_is_raised_not_reported_as_sent(flags, fault):
    async def check():
        client = FakeClient(replies=(0, flags))
        with pytest.raises(t.PrinterFaultError, match=fault):
            await t.send_bitmap(client, bytes(48), 1, **KW)
        assert client.disconnected

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
        assert client.disconnected and client.writes == [GET_STATE]  # no raster

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


def test_frame_encoding_runs_outside_event_loop_thread(monkeypatch):
    import threading

    caller_thread = threading.get_ident()
    original = t.print_frames

    def encode(*args, **kwargs):
        assert threading.get_ident() != caller_thread
        return original(*args, **kwargs)

    monkeypatch.setattr(t, "print_frames", encode)

    async def check():
        await t.send_bitmap(FakeClient(), bytes(48), 1, **KW)

    asyncio.run(check())


def test_fault_before_the_job_stops_it_without_sending_raster():
    async def check():
        client = FakeClient(replies=(0x01,))
        with pytest.raises(t.PrinterFaultError, match="out_of_paper"):
            await t.send_bitmap(client, bytes(48), 1, **KW)
        assert client.writes == [GET_STATE] and client.disconnected

    asyncio.run(check())
