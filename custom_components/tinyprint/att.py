"""Pure ATT client for the X6h: strict PDU codec, dynamic GATT discovery, bleak-like facade.

No Home Assistant, no sockets, no threads. The bridge supplies a channel object with
`async send(pdu)`, `async recv()` (raises EOFError/ConnectionError on link loss) and a sync,
idempotent `close()`. Reached at runtime only from the standalone bridge (jobs.py) and tests.

Invariants:
* One reader task owns `recv`; one request is outstanding at a time; every send, request and
  cleanup step is bounded. After an ATT transaction timeout the channel state is unknown, so the
  client closes it instead of risking a late response being taken for the next answer.
* Discovery never trusts the peer: every PDU is length-checked, handles must stay inside the
  requested range and strictly increase, and every pagination loop is capped. Handles are never
  hardcoded; the CCCD is searched only between the AE02 value and the next characteristic.
* We never negotiate an MTU above 23, so writes are Write Commands of at most 20 bytes.

Past bug (probe): hardcoded h6/h8/h9, unbounded Find Information loops and a CCCD search that
could grab a neighbouring characteristic's descriptor. Hence the rules above.
"""

import asyncio
import logging
import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .protocol import (
    NOTIFY_UUID,
    SERVICE_UUID,
    WRITE_UUID,
    FrameCounter,
    NotificationParser,
)

_LOGGER = logging.getLogger(__name__)

ATT_DEFAULT_MTU = 23
MAX_PDU_LENGTH = 517
MAX_DISCOVERY_REQUESTS = 64
MAX_WRITE_LENGTH = ATT_DEFAULT_MTU - 3

OP_ERROR = 0x01
OP_MTU_RESPONSE = 0x03
OP_FIND_INFO_RESPONSE = 0x05
OP_READ_BY_TYPE_RESPONSE = 0x09
OP_READ_BY_GROUP_RESPONSE = 0x11
OP_WRITE_RESPONSE = 0x13
OP_NOTIFICATION = 0x1B
OP_INDICATION = 0x1D
OP_CONFIRMATION = 0x1E
OP_WRITE_COMMAND = 0x52

ERR_REQUEST_NOT_SUPPORTED = 0x06
ERR_ATTRIBUTE_NOT_FOUND = 0x0A

_BASE_UUID_TAIL = bytes.fromhex("fb349b5f8000008000100000")
_RESPONSES = frozenset(
    (0x01, 0x03, 0x05, 0x07, 0x09, 0x0B, 0x0D, 0x0F, 0x11, 0x13, 0x15, 0x17, 0x19, 0x1F)
)
_REQUESTS = frozenset(
    (0x02, 0x04, 0x06, 0x08, 0x0A, 0x0C, 0x0E, 0x10, 0x12, 0x14, 0x16, 0x18, 0x20)
)
_PROPERTY_NAMES = (
    (0x01, "broadcast"),
    (0x02, "read"),
    (0x04, "write-without-response"),
    (0x08, "write"),
    (0x10, "notify"),
    (0x20, "indicate"),
    (0x40, "authenticated-signed-writes"),
    (0x80, "extended-properties"),
)


class AttError(Exception):
    """Base for ATT-level failures."""


class AttProtocolError(AttError):
    """The peer sent a PDU that breaks the protocol or our strict limits."""


class AttErrorResponse(AttError):
    """The peer answered a request with an Error Response."""

    def __init__(self, opcode: int, handle: int, error_code: int) -> None:
        super().__init__(f"ATT error 0x{error_code:02x} for opcode 0x{opcode:02x}")
        self.opcode = opcode
        self.handle = handle
        self.error_code = error_code


class AttTimeout(AttError, TimeoutError):
    """An ATT send or transaction timed out; the channel is closed afterwards."""


class ProfileError(AttError):
    """The device does not expose the X6h AE30/AE01/AE02 profile."""


# -- UUIDs ---------------------------------------------------------------------------------


def uuid_from_bytes(raw: bytes) -> str:
    """Little-endian 16/128-bit wire UUID to canonical lowercase string."""
    if len(raw) == 2:
        return f"0000{raw[1]:02x}{raw[0]:02x}-0000-1000-8000-00805f9b34fb"
    if len(raw) == 16:
        text = bytes(reversed(raw)).hex()
        return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"
    raise AttProtocolError("UUID must be 2 or 16 bytes")


def uuid_to_bytes(uuid: str, *, wide: bool = False) -> bytes:
    """Canonical UUID to wire bytes, shortened to 16 bit for Bluetooth base UUIDs."""
    text = uuid.replace("-", "").lower()
    if len(text) != 32:
        raise ValueError("UUID must have 128 bits")
    raw = bytes.fromhex(text)
    if not wide and raw[:2] == b"\x00\x00" and raw[4:] == bytes(reversed(_BASE_UUID_TAIL)):
        return raw[2:4][::-1]
    return raw[::-1]


def property_names(properties: int) -> tuple[str, ...]:
    return tuple(name for bit, name in _PROPERTY_NAMES if properties & bit)


# -- PDU builders --------------------------------------------------------------------------


def _handle(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 0xFFFF:
        raise ValueError("ATT handle out of range")
    return value


def _range(start: int, end: int) -> tuple[int, int]:
    _handle(start)
    _handle(end)
    if start > end:
        raise ValueError("ATT handle range is empty")
    return start, end


def error_response(request_opcode: int, handle: int, code: int) -> bytes:
    return struct.pack("<BBHB", OP_ERROR, request_opcode, handle, code)


def exchange_mtu_response(mtu: int) -> bytes:
    return struct.pack("<BH", OP_MTU_RESPONSE, mtu)


def read_by_group_type_request(start: int, end: int, uuid16: int) -> bytes:
    return struct.pack("<BHHH", 0x10, *_range(start, end), uuid16)


def read_by_type_request(start: int, end: int, uuid16: int) -> bytes:
    return struct.pack("<BHHH", 0x08, *_range(start, end), uuid16)


def find_information_request(start: int, end: int) -> bytes:
    return struct.pack("<BHH", 0x04, *_range(start, end))


def write_request(handle: int, value: bytes) -> bytes:
    return struct.pack("<BH", 0x12, _handle(handle)) + bytes(value)


def write_command(handle: int, value: bytes) -> bytes:
    return struct.pack("<BH", OP_WRITE_COMMAND, _handle(handle)) + bytes(value)


def confirmation() -> bytes:
    return bytes((OP_CONFIRMATION,))


# -- PDU parsers (strict) ------------------------------------------------------------------


def _entries(pdu: bytes, opcode: int, sizes: tuple[int, ...]) -> tuple[int, memoryview]:
    if len(pdu) < 2 or pdu[0] != opcode or pdu[1] not in sizes:
        raise AttProtocolError("Malformed ATT response")
    size = pdu[1]
    payload = memoryview(pdu)[2:]
    if not len(payload) or len(payload) % size:
        raise AttProtocolError("Malformed ATT response payload")
    return size, payload


def parse_error_response(pdu: bytes) -> tuple[int, int, int]:
    if len(pdu) != 5 or pdu[0] != OP_ERROR:
        raise AttProtocolError("Malformed ATT error response")
    return pdu[1], int.from_bytes(pdu[2:4], "little"), pdu[4]


def parse_write_response(pdu: bytes) -> None:
    if pdu != bytes((OP_WRITE_RESPONSE,)):
        raise AttProtocolError("Malformed ATT write response")


@dataclass(frozen=True)
class GattService:
    start: int
    end: int
    uuid: str


@dataclass(frozen=True)
class GattCharacteristic:
    declaration_handle: int
    properties: int
    value_handle: int
    uuid: str

    @property
    def property_names(self) -> tuple[str, ...]:
        return property_names(self.properties)


def parse_read_by_group_response(pdu: bytes, start: int, end: int) -> list[GattService]:
    size, payload = _entries(pdu, OP_READ_BY_GROUP_RESPONSE, (6, 20))
    services: list[GattService] = []
    previous_end = start - 1
    for offset in range(0, len(payload), size):
        entry = bytes(payload[offset : offset + size])
        first, last = struct.unpack("<HH", entry[:4])
        # Groups must start in range, not overlap, and each be non-empty: this also
        # guarantees the next request start advances.
        if not start <= first <= end or last < first or first <= previous_end:
            raise AttProtocolError("ATT group handles violate range or ordering")
        services.append(GattService(first, last, uuid_from_bytes(entry[4:])))
        previous_end = last
    return services


def parse_characteristic_declarations(pdu: bytes, start: int, end: int) -> list[GattCharacteristic]:
    size, payload = _entries(pdu, OP_READ_BY_TYPE_RESPONSE, (7, 21))
    chars: list[GattCharacteristic] = []
    previous_value = start - 1
    for offset in range(0, len(payload), size):
        entry = bytes(payload[offset : offset + size])
        declaration = int.from_bytes(entry[:2], "little")
        value_handle = int.from_bytes(entry[3:5], "little")
        if (
            not start <= declaration <= end
            or declaration <= previous_value
            or not declaration < value_handle <= end
        ):
            raise AttProtocolError("ATT characteristic handles violate range or ordering")
        chars.append(
            GattCharacteristic(declaration, entry[2], value_handle, uuid_from_bytes(entry[5:]))
        )
        previous_value = value_handle
    return chars


def parse_find_information_response(pdu: bytes, start: int, end: int) -> list[tuple[int, str]]:
    if len(pdu) < 2 or pdu[0] != OP_FIND_INFO_RESPONSE or pdu[1] not in (1, 2):
        raise AttProtocolError("Malformed ATT find-information response")
    size = 4 if pdu[1] == 1 else 18
    payload = memoryview(pdu)[2:]
    if not len(payload) or len(payload) % size:
        raise AttProtocolError("Malformed ATT find-information payload")
    result: list[tuple[int, str]] = []
    previous = start - 1
    for offset in range(0, len(payload), size):
        entry = bytes(payload[offset : offset + size])
        handle = int.from_bytes(entry[:2], "little")
        if not start <= handle <= end or handle <= previous:
            raise AttProtocolError("ATT descriptor handles violate range or ordering")
        result.append((handle, uuid_from_bytes(entry[2:])))
        previous = handle
    return result


# -- client --------------------------------------------------------------------------------


class Channel(Protocol):
    async def send(self, pdu: bytes) -> None: ...
    async def recv(self) -> bytes: ...
    def close(self) -> None: ...


@dataclass
class _Pending:
    request_opcode: int
    expected_opcode: int
    future: asyncio.Future[bytes]


class AttClient:
    """Single-reader ATT client over a transport-agnostic channel."""

    def __init__(
        self, channel: Channel, *, request_timeout: float = 10, send_timeout: float = 5
    ) -> None:
        self._channel = channel
        self._request_timeout = request_timeout
        self._send_timeout = send_timeout
        self._request_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: _Pending | None = None
        self._reader: asyncio.Task[None] | None = None
        self._handlers: dict[int, Callable[[bytes], None]] = {}
        self._closed = False
        self.close_reason: str | None = None
        self.closed = asyncio.Event()
        self.stats: dict[str, int] = {
            "stray_pdus": 0,
            "malformed_pdus": 0,
            "notifications": 0,
            "indications": 0,
            "peer_requests": 0,
        }

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        if self._reader is None:
            self._reader = asyncio.create_task(self._read_loop(), name="tinyprint-att-reader")

    def set_handler(self, handle: int, callback: Callable[[bytes], None]) -> None:
        self._handlers[handle] = callback

    def remove_handler(self, handle: int) -> None:
        self._handlers.pop(handle, None)

    def _mark_closed(self, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.close_reason = reason
        self.closed.set()
        pending = self._pending
        if pending is not None and not pending.future.done():
            pending.future.set_exception(ConnectionError(f"ATT link closed: {reason}"))
            pending.future.exception()  # mark retrieved: the requester may already be gone
        try:
            self._channel.close()
        except Exception as error:
            _LOGGER.debug("ATT channel close failed: %s", type(error).__name__)
        reader = self._reader
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            reader.cancel()

    async def aclose(self) -> None:
        """Close the channel, stop the reader, bounded. Safe to call repeatedly."""
        self._mark_closed("closed")
        reader = self._reader
        if reader is not None and reader is not asyncio.current_task():
            await asyncio.wait({reader}, timeout=2)

    async def _send(self, pdu: bytes) -> None:
        if self._closed:
            raise ConnectionError(f"ATT link closed: {self.close_reason}")
        try:
            async with asyncio.timeout(self._send_timeout), self._send_lock:
                await self._channel.send(pdu)
        except TimeoutError:
            self._mark_closed("send_timeout")
            raise AttTimeout("ATT send timed out") from None
        except asyncio.CancelledError:
            # A cancelled send may be half written; the channel state is unknown.
            self._mark_closed("cancelled")
            raise
        except Exception:
            self._mark_closed("send_failed")
            raise

    async def write_command(self, handle: int, value: bytes) -> None:
        await self._send(write_command(handle, value))

    async def request(
        self, pdu: bytes, expected_opcode: int, *, timeout: float | None = None
    ) -> bytes:
        """Send one request and return the matching response or Error Response PDU."""
        wait = self._request_timeout if timeout is None else timeout
        async with self._request_lock:
            if self._closed:
                raise ConnectionError(f"ATT link closed: {self.close_reason}")
            future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
            self._pending = _Pending(pdu[0], expected_opcode, future)
            try:
                await self._send(pdu)
                async with asyncio.timeout(wait):
                    return await future
            except AttTimeout:
                raise
            except TimeoutError:
                self._mark_closed("att_timeout")
                raise AttTimeout("ATT response timed out") from None
            except asyncio.CancelledError:
                self._mark_closed("cancelled")
                raise
            finally:
                self._pending = None

    # -- reader ----------------------------------------------------------------------------
    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                await self._dispatch(bytes(await self._channel.recv()))
        except asyncio.CancelledError:
            raise
        except EOFError, OSError:
            self._mark_closed("link_lost")
        except Exception as error:
            _LOGGER.debug("ATT reader failed: %s", type(error).__name__)
            self._mark_closed("reader_error")

    async def _dispatch(self, pdu: bytes) -> None:
        if not pdu or len(pdu) > MAX_PDU_LENGTH:
            self.stats["malformed_pdus"] += 1
            return
        opcode = pdu[0]
        if opcode in (OP_NOTIFICATION, OP_INDICATION):
            await self._handle_value(pdu)
        elif opcode == OP_CONFIRMATION or opcode & 0x40:
            return  # confirmations and commands need no answer
        elif opcode in _RESPONSES:
            self._deliver(pdu)
        elif opcode in _REQUESTS:
            await self._answer_peer_request(pdu)
        else:
            self.stats["malformed_pdus"] += 1

    def _deliver(self, pdu: bytes) -> None:
        pending = self._pending
        if pending is not None and not pending.future.done():
            if pdu[0] == pending.expected_opcode or (
                pdu[0] == OP_ERROR and len(pdu) >= 2 and pdu[1] == pending.request_opcode
            ):
                pending.future.set_result(pdu)
                return
        self.stats["stray_pdus"] += 1

    async def _answer_peer_request(self, pdu: bytes) -> None:
        self.stats["peer_requests"] += 1
        if pdu[0] == 0x02:
            if len(pdu) != 3:
                self.stats["malformed_pdus"] += 1
                return
            # We never negotiate above the default, so writes stay <= 20 bytes.
            await self._send(exchange_mtu_response(ATT_DEFAULT_MTU))
            return
        await self._send(error_response(pdu[0], 0, ERR_REQUEST_NOT_SUPPORTED))

    async def _handle_value(self, pdu: bytes) -> None:
        if len(pdu) < 3:
            self.stats["malformed_pdus"] += 1
            return
        if pdu[0] == OP_INDICATION:
            self.stats["indications"] += 1
            await self._send(confirmation())
        else:
            self.stats["notifications"] += 1
        handler = self._handlers.get(int.from_bytes(pdu[1:3], "little"))
        if handler is None:
            return
        try:
            handler(pdu[3:])
        except Exception as error:
            _LOGGER.debug("ATT notification handler failed: %s", type(error).__name__)


# -- discovery -----------------------------------------------------------------------------


async def _discovery_request(client: AttClient, pdu: bytes, expected: int) -> bytes | None:
    """Return the response, None for Attribute Not Found (end of scan), else raise."""
    response = await client.request(pdu, expected)
    if response[0] == OP_ERROR:
        opcode, handle, code = parse_error_response(response)
        if code == ERR_ATTRIBUTE_NOT_FOUND:
            return None
        raise AttErrorResponse(opcode, handle, code)
    return response


async def read_primary_services(
    client: AttClient, stop_uuid: str | None = None
) -> list[GattService]:
    services: list[GattService] = []
    start = 1
    for _ in range(MAX_DISCOVERY_REQUESTS):
        response = await _discovery_request(
            client, read_by_group_type_request(start, 0xFFFF, 0x2800), OP_READ_BY_GROUP_RESPONSE
        )
        if response is None:
            return services
        page = parse_read_by_group_response(response, start, 0xFFFF)
        services.extend(page)
        if stop_uuid is not None and any(service.uuid == stop_uuid for service in page):
            return services
        last = page[-1].end
        if last >= 0xFFFF:
            return services
        if last < start:
            raise AttProtocolError("ATT service discovery made no progress")
        start = last + 1
    raise AttProtocolError("ATT service discovery needed too many requests")


async def read_characteristics(client: AttClient, start: int, end: int) -> list[GattCharacteristic]:
    chars: list[GattCharacteristic] = []
    if start > end:
        return chars
    for _ in range(MAX_DISCOVERY_REQUESTS):
        response = await _discovery_request(
            client, read_by_type_request(start, end, 0x2803), OP_READ_BY_TYPE_RESPONSE
        )
        if response is None:
            return chars
        page = parse_characteristic_declarations(response, start, end)
        chars.extend(page)
        following = page[-1].value_handle + 1
        if following <= start:
            raise AttProtocolError("ATT characteristic discovery made no progress")
        start = following
        if start > end:
            return chars
    raise AttProtocolError("ATT characteristic discovery needed too many requests")


async def find_descriptor(
    client: AttClient, start: int, end: int, uuid16: int = 0x2902
) -> int | None:
    """Handle of the first descriptor of `uuid16` in start..end, or None."""
    if start > end:
        return None
    wanted = uuid_from_bytes(struct.pack("<H", uuid16))
    for _ in range(MAX_DISCOVERY_REQUESTS):
        response = await _discovery_request(
            client, find_information_request(start, end), OP_FIND_INFO_RESPONSE
        )
        if response is None:
            return None
        entries = parse_find_information_response(response, start, end)
        for handle, uuid in entries:
            if uuid == wanted:
                return handle
        start = entries[-1][0] + 1
        if start > end:
            return None
    raise AttProtocolError("ATT descriptor discovery needed too many requests")


@dataclass(frozen=True)
class GattProfile:
    service: GattService
    write: GattCharacteristic
    notify: GattCharacteristic
    cccd_handle: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "service": {"start": self.service.start, "end": self.service.end},
            "write_handle": self.write.value_handle,
            "write_properties": list(self.write.property_names),
            "notify_handle": self.notify.value_handle,
            "notify_properties": list(self.notify.property_names),
            "cccd_handle": self.cccd_handle,
        }


def _pick_characteristics(
    service: GattService, chars: list[GattCharacteristic]
) -> tuple[GattCharacteristic, GattCharacteristic, int, int]:
    write = next((c for c in chars if c.uuid == WRITE_UUID), None)
    notify = next((c for c in chars if c.uuid == NOTIFY_UUID), None)
    if write is None:
        raise ProfileError("AE01 write characteristic missing")
    if notify is None:
        raise ProfileError("AE02 notify characteristic missing")
    if not write.properties & 0x04:
        raise ProfileError("AE01 lacks write-without-response")
    if not notify.properties & 0x10:
        raise ProfileError("AE02 lacks notify")
    later = [
        c.declaration_handle for c in chars if c.declaration_handle > notify.declaration_handle
    ]
    # The CCCD must sit between the AE02 value and whatever follows it; never beyond.
    last = min(later) - 1 if later else service.end
    return write, notify, notify.value_handle + 1, last


async def discover_profile(client: AttClient) -> GattProfile:
    services = await read_primary_services(client, stop_uuid=SERVICE_UUID)
    service = next((s for s in services if s.uuid == SERVICE_UUID), None)
    if service is None:
        raise ProfileError("AE30 service missing")
    chars = await read_characteristics(client, service.start, service.end)
    write, notify, first, last = _pick_characteristics(service, chars)
    cccd = await find_descriptor(client, first, last)
    if cccd is None:
        raise ProfileError("AE02 CCCD missing")
    return GattProfile(service, write, notify, cccd)


async def discover_all(
    client: AttClient, max_services: int = 32, max_chars: int = 64
) -> dict[str, Any]:
    """JSON-safe, bounded GATT survey for diagnose jobs. No addresses, no values."""
    services = await read_primary_services(client)
    truncated = len(services) > max_services
    listing: list[dict[str, Any]] = []
    profile: GattProfile | None = None
    problem: str | None = "AE30 service missing"
    for service in services[:max_services]:
        entry: dict[str, Any] = {"uuid": service.uuid, "start": service.start, "end": service.end}
        try:
            chars = await read_characteristics(client, service.start, service.end)
        except AttErrorResponse as error:
            entry["characteristics"] = []
            entry["error"] = f"att_error_0x{error.error_code:02x}"
            listing.append(entry)
            continue
        if len(chars) > max_chars:
            truncated = True
        entry["characteristics"] = [
            {
                "uuid": char.uuid,
                "declaration_handle": char.declaration_handle,
                "value_handle": char.value_handle,
                "properties": list(char.property_names),
            }
            for char in chars[:max_chars]
        ]
        listing.append(entry)
        if service.uuid == SERVICE_UUID and profile is None:
            try:
                write, notify, first, last = _pick_characteristics(service, chars)
                cccd = await find_descriptor(client, first, last)
                if cccd is None:
                    problem = "AE02 CCCD missing"
                else:
                    profile = GattProfile(service, write, notify, cccd)
                    problem = None
            except ProfileError as error:
                problem = str(error)
    return {
        "services": listing,
        "truncated": truncated,
        "profile_supported": profile is not None,
        "profile": profile.as_dict() if profile else None,
        "profile_problem": problem,
        "ae02_cccd_handle": profile.cccd_handle if profile else None,
    }


# -- bleak-like facade for transport.send_bitmap -------------------------------------------


@dataclass(frozen=True)
class _Characteristic:
    uuid: str
    handle: int
    properties: list[str]


class _Services:
    def __init__(self, profile: GattProfile) -> None:
        self._by_uuid = {
            WRITE_UUID: _Characteristic(
                WRITE_UUID, profile.write.value_handle, list(profile.write.property_names)
            ),
            NOTIFY_UUID: _Characteristic(
                NOTIFY_UUID, profile.notify.value_handle, list(profile.notify.property_names)
            ),
        }

    def get_characteristic(self, uuid: str) -> _Characteristic | None:
        return self._by_uuid.get(uuid)


class AttGattClient:
    """The slice of the bleak client API that `transport.send_bitmap` uses.

    Counters live here, not in send_bitmap's return value, so they survive any failure.
    A write that raised or was cancelled may still have reached the printer, so it marks
    `delivery_uncertain` before re-raising.
    """

    def __init__(self, client: AttClient, profile: GattProfile) -> None:
        self._att = client
        self._profile = profile
        self._disconnected = False
        self._callback: Callable[[Any, bytearray], None] | None = None
        self._frames = FrameCounter()
        self._parser = NotificationParser()
        self._flow_ready = True
        self.services = _Services(profile)
        self.counters: dict[str, Any] = {
            "bytes_written": 0,
            "writes": 0,
            "frames_written": 0,
            "notifications": 0,
            "flow_pauses": 0,
            "delivery_uncertain": False,
        }

    @property
    def is_connected(self) -> bool:
        return not self._disconnected and not self._att.is_closed

    @property
    def flow_paused(self) -> bool:
        return not self._flow_ready

    @property
    def stats(self) -> dict[str, Any]:
        return {**self.counters, "stray_pdus": self._att.stats["stray_pdus"]}

    def _on_value(self, value: bytes) -> None:
        self.counters["notifications"] += 1
        for can_send in self._parser.feed(value):
            if not can_send and self._flow_ready:
                self.counters["flow_pauses"] += 1
            self._flow_ready = can_send
        if self._callback is not None:
            self._callback(None, bytearray(value))

    async def _write_cccd(self, value: bytes) -> None:
        response = await self._att.request(
            write_request(self._profile.cccd_handle, value), OP_WRITE_RESPONSE
        )
        if response[0] == OP_ERROR:
            raise AttErrorResponse(*parse_error_response(response))
        parse_write_response(response)

    async def start_notify(self, uuid: str, callback: Callable[[Any, bytearray], None]) -> None:
        if uuid != NOTIFY_UUID:
            raise ValueError("Only AE02 notifications are supported")
        self._callback = callback
        # Register first: the printer may notify right after the CCCD write is accepted.
        self._att.set_handler(self._profile.notify.value_handle, self._on_value)
        try:
            await self._write_cccd(b"\x01\x00")
        except BaseException:
            self._att.remove_handler(self._profile.notify.value_handle)
            raise

    async def stop_notify(self, uuid: str) -> None:
        if uuid != NOTIFY_UUID:
            raise ValueError("Only AE02 notifications are supported")
        try:
            if self.is_connected:
                await self._write_cccd(b"\x00\x00")
        finally:
            self._att.remove_handler(self._profile.notify.value_handle)

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = False) -> None:
        if uuid != WRITE_UUID or response is not False:
            raise ValueError("Only write-without-response to AE01 is supported")
        if not 1 <= len(data) <= MAX_WRITE_LENGTH:
            raise ValueError(f"Writes must be 1..{MAX_WRITE_LENGTH} bytes")
        if not self.is_connected:
            raise ConnectionError("ATT link is closed")
        payload = bytes(data)
        try:
            await self._att.write_command(self._profile.write.value_handle, payload)
        except BaseException:
            self.counters["delivery_uncertain"] = True
            raise
        self.counters["bytes_written"] += len(payload)
        self.counters["writes"] += 1
        self.counters["frames_written"] += self._frames.feed(payload)

    async def disconnect(self) -> None:
        self._disconnected = True
        await self._att.aclose()
