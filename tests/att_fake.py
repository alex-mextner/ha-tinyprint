"""In-memory ATT server standing in for the X6h, usable as a channel by `att.AttClient`.

Test double only: it implements just the server half of the PDUs the client uses, with a
configurable GATT table and fault injection. It encodes UUIDs independently from `att.py` so a
shared bug cannot hide. Records every PDU the client sends so tests can assert on the wire.
"""

import asyncio
import struct
from collections.abc import Callable
from dataclasses import dataclass, field

BASE_SUFFIX = "-0000-1000-8000-00805f9b34fb"
SERVICE = "0000ae30" + BASE_SUFFIX
WRITE = "0000ae01" + BASE_SUFFIX
NOTIFY = "0000ae02" + BASE_SUFFIX

PROP_READ = 0x02
PROP_WRITE_NO_RESPONSE = 0x04
PROP_WRITE = 0x08
PROP_NOTIFY = 0x10
PROP_INDICATE = 0x20

PAUSE = bytes.fromhex("5178ae0101001070ff")
RESUME = bytes.fromhex("5178ae0101000000ff")
_EOF = object()


def uuid_bytes(uuid: str, wide: bool = False) -> bytes:
    raw = bytes.fromhex(uuid.replace("-", ""))
    if not wide and uuid.lower().endswith(BASE_SUFFIX) and raw[:2] == b"\x00\x00":
        return raw[2:4][::-1]
    return raw[::-1]


@dataclass
class Char:
    uuid: str
    props: int
    cccd: bool = False
    wide: bool = False
    extra_descriptors: tuple[int, ...] = ()


@dataclass
class Service:
    uuid: str
    chars: list[Char]
    wide: bool = False


@dataclass
class Attr:
    handle: int
    type: bytes
    value: bytes


def x6h_services() -> list[Service]:
    """GAP-like service first so AE30 does not sit at handle 4."""
    return [
        Service(
            "00001800" + BASE_SUFFIX,
            [Char("00002a00" + BASE_SUFFIX, PROP_READ), Char("00002a01" + BASE_SUFFIX, PROP_READ)],
        ),
        Service(
            SERVICE,
            [
                Char("0000ae10" + BASE_SUFFIX, PROP_WRITE_NO_RESPONSE),
                Char(WRITE, PROP_WRITE_NO_RESPONSE | PROP_WRITE),
                Char(NOTIFY, PROP_NOTIFY, cccd=True),
            ],
        ),
    ]


class FakeGattServer:
    """Server side of ATT plus the channel interface (`send`, `recv`, `close`)."""

    def __init__(
        self,
        services: list[Service] | None = None,
        *,
        first_handle: int = 1,
        gap: int = 0,
        mtu: int = 23,
    ) -> None:
        self.mtu = mtu
        self.attrs: list[Attr] = []
        self.service_ranges: list[tuple[int, int, Service]] = []
        self.value_handles: dict[str, int] = {}
        self.cccd_handles: dict[str, int] = {}
        self._build(services or x6h_services(), first_handle, gap)
        self.cccd_values: dict[int, bytes] = {}
        self.received: list[bytes] = []
        self.commands: list[tuple[int, bytes]] = []
        self.requests: list[int] = []
        self.events: list[tuple] = []
        self.closed = False
        self.silent: set[int] = set()
        self.mutators: dict[int, Callable[[list[bytes]], list[bytes]]] = {}
        self.on_command: Callable[["FakeGattServer", int, bytes], None] | None = None
        self.fail_send_after_commands: int | None = None
        self.stall_commands = False
        self.reject_cccd_write: int | None = None
        self._queue: asyncio.Queue = asyncio.Queue()

    def _build(self, services: list[Service], first: int, gap: int) -> None:
        handle = first

        def add(type_: bytes, value: bytes) -> int:
            nonlocal handle
            used = handle
            self.attrs.append(Attr(used, type_, value))
            handle += 1 + gap
            return used

        for service in services:
            start = add(b"\x00\x28", uuid_bytes(service.uuid, service.wide))
            for char in service.chars:
                declaration = handle
                value_handle = declaration + 1 + gap
                add(
                    b"\x03\x28",
                    bytes((char.props,))
                    + struct.pack("<H", value_handle)
                    + uuid_bytes(char.uuid, char.wide),
                )
                add(uuid_bytes(char.uuid, char.wide), b"")
                self.value_handles.setdefault(char.uuid, value_handle)
                if char.cccd:
                    self.cccd_handles.setdefault(char.uuid, add(b"\x02\x29", b"\x00\x00"))
                for extra in char.extra_descriptors:
                    add(struct.pack("<H", extra), b"")
            self.service_ranges.append((start, self.attrs[-1].handle, service))

    # -- channel interface -------------------------------------------------------------
    async def send(self, pdu: bytes) -> None:
        if self.closed:
            raise ConnectionError("channel closed")
        pdu = bytes(pdu)
        if self.stall_commands and pdu[:1] == b"\x52":
            await asyncio.Event().wait()
        if pdu[:1] == b"\x52":
            if (
                self.fail_send_after_commands is not None
                and len(self.commands) >= self.fail_send_after_commands
            ):
                raise ConnectionError("send failed")
        self.received.append(pdu)
        for response in self._handle(pdu):
            self._queue.put_nowait(response)

    async def recv(self) -> bytes:
        item = await self._queue.get()
        if item is _EOF:
            self._queue.put_nowait(_EOF)
            raise EOFError("peer closed")
        return item

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._queue.put_nowait(_EOF)

    # -- test controls -----------------------------------------------------------------
    def push(self, pdu: bytes) -> None:
        self._queue.put_nowait(bytes(pdu))

    def push_notification(self, handle: int, value: bytes) -> None:
        self.push(b"\x1b" + struct.pack("<H", handle) + value)

    def push_indication(self, handle: int, value: bytes) -> None:
        self.push(b"\x1d" + struct.pack("<H", handle) + value)

    def drop_link(self) -> None:
        self._queue.put_nowait(_EOF)

    @property
    def notify_handle(self) -> int:
        return self.value_handles[NOTIFY]

    @property
    def write_handle(self) -> int:
        return self.value_handles[WRITE]

    @property
    def cccd_handle(self) -> int | None:
        return self.cccd_handles.get(NOTIFY)

    def request_opcodes(self, opcode: int) -> int:
        return self.requests.count(opcode)

    # -- server -----------------------------------------------------------------------
    def _error(self, opcode: int, handle: int, code: int) -> bytes:
        return bytes((0x01, opcode)) + struct.pack("<HB", handle, code)

    def _handle(self, pdu: bytes) -> list[bytes]:
        opcode = pdu[0]
        if opcode == 0x1E:
            self.events.append(("confirmation",))
            return []
        if opcode == 0x52:
            return self._command(pdu)
        self.requests.append(opcode)
        if opcode in self.silent:
            return []
        handler = {
            0x10: self._group,
            0x08: self._by_type,
            0x04: self._find_info,
            0x12: self._write_request,
        }.get(opcode)
        responses = handler(pdu) if handler else [self._error(opcode, 0, 0x06)]
        if opcode in self.mutators:
            responses = self.mutators[opcode](responses)
        return responses

    def _command(self, pdu: bytes) -> list[bytes]:
        handle = struct.unpack("<H", pdu[1:3])[0]
        value = pdu[3:]
        self.commands.append((handle, value))
        self.events.append(("cmd", len(self.commands)))
        if self.on_command:
            self.on_command(self, handle, value)
        return []

    def _page(self, entries: list[bytes]) -> list[bytes]:
        if not entries:
            return []
        size = len(entries[0])
        limit = max(1, (self.mtu - 2) // size)
        return entries[:limit]

    def _group(self, pdu: bytes) -> list[bytes]:
        start, end = struct.unpack("<HH", pdu[1:5])
        if pdu[5:] != b"\x00\x28":
            return [self._error(0x10, start, 0x10)]
        entries: list[bytes] = []
        for first, last, service in self.service_ranges:
            if start <= first <= end:
                entry = struct.pack("<HH", first, last) + uuid_bytes(service.uuid, service.wide)
                if entries and len(entry) != len(entries[0]):
                    break
                entries.append(entry)
        page = self._page(entries)
        if not page:
            return [self._error(0x10, start, 0x0A)]
        return [bytes((0x11, len(page[0]))) + b"".join(page)]

    def _by_type(self, pdu: bytes) -> list[bytes]:
        start, end = struct.unpack("<HH", pdu[1:5])
        if pdu[5:] != b"\x03\x28":
            return [self._error(0x08, start, 0x0A)]
        entries: list[bytes] = []
        for attr in self.attrs:
            if attr.type == b"\x03\x28" and start <= attr.handle <= end:
                entry = struct.pack("<H", attr.handle) + attr.value
                if entries and len(entry) != len(entries[0]):
                    break
                entries.append(entry)
        page = self._page(entries)
        if not page:
            return [self._error(0x08, start, 0x0A)]
        return [bytes((0x09, len(page[0]))) + b"".join(page)]

    def _find_info(self, pdu: bytes) -> list[bytes]:
        start, end = struct.unpack("<HH", pdu[1:5])
        entries: list[bytes] = []
        for attr in self.attrs:
            if start <= attr.handle <= end:
                entry = struct.pack("<H", attr.handle) + attr.type
                if entries and len(entry) != len(entries[0]):
                    break
                entries.append(entry)
        page = self._page(entries)
        if not page:
            return [self._error(0x04, start, 0x0A)]
        fmt = 1 if len(page[0]) == 4 else 2
        return [bytes((0x05, fmt)) + b"".join(page)]

    def _write_request(self, pdu: bytes) -> list[bytes]:
        handle = struct.unpack("<H", pdu[1:3])[0]
        value = pdu[3:]
        if not any(attr.handle == handle for attr in self.attrs):
            return [self._error(0x12, handle, 0x01)]
        if handle in self.cccd_handles.values():
            if self.reject_cccd_write is not None:
                return [self._error(0x12, handle, self.reject_cccd_write)]
            self.cccd_values[handle] = value
            self.events.append(("cccd", value))
        return [b"\x13"]


@dataclass
class FakeLink:
    """`LinkFactory` stand-in: hands out a prepared server as the channel."""

    server: FakeGattServer
    connects: int = 0
    connect_error: BaseException | None = None
    connect_delay: float = 0
    channels: list = field(default_factory=list)

    async def connect(self) -> FakeGattServer:
        self.connects += 1
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.connect_error is not None:
            raise self.connect_error
        self.channels.append(self.server)
        return self.server
