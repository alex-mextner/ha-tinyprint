"""Explicit-LE link to the printer: a kernel L2CAP socket on the ATT fixed channel (CID 4).

Why not BlueZ Device1.Connect: the X6h is dual-mode and BlueZ picks BR/EDR for it, which fails
(ProfileUnavailable). A socket bound to CID 4 with BDADDR_LE_PUBLIC can only form an LE link and
needs no BlueZ setting changes. Proven on hardware with the same sockaddr layout as below.

Invariants: the printer address comes only from config; connect is non-blocking and waits with
loop.add_writer under asyncio.timeout (no select, no thread), and the socket is closed on any
timeout/cancel/error because closing is what cancels the kernel's pending LE connect. Channel
IO is bounded by the caller (the ATT client wraps every send; recv has no timeout by design,
the reader task is cancelled instead).
"""

import asyncio
import ctypes
import errno
import os
import socket
import struct
from collections.abc import Callable
from typing import Protocol

AF_BLUETOOTH = 31
ATT_CID = 4
BDADDR_LE_PUBLIC = 1
RECV_SIZE = 517


class Channel(Protocol):
    async def send(self, pdu: bytes) -> None: ...
    async def recv(self) -> bytes: ...
    def close(self) -> None: ...


class LinkFactory(Protocol):
    """Anything that can open one channel to the configured printer."""

    async def connect(self) -> Channel: ...


def pack_le_address(mac: str) -> bytes:
    """struct sockaddr_l2 for an LE public address on the ATT channel (14 bytes)."""
    address = bytes.fromhex(mac.replace(":", ""))[::-1]
    return struct.pack("<HH6sHBx", AF_BLUETOOTH, 0, address, ATT_CID, BDADDR_LE_PUBLIC)


class KernelCalls:
    """bind/connect through libc: Python's socket module cannot set CID and address type."""

    def __init__(self) -> None:
        self._libc = ctypes.CDLL(None, use_errno=True)

    def bind(self, fd: int, address: bytes) -> int:
        if self._libc.bind(fd, ctypes.c_char_p(address), len(address)) != 0:
            return ctypes.get_errno()
        return 0

    def connect(self, fd: int, address: bytes) -> int:
        if self._libc.connect(fd, ctypes.c_char_p(address), len(address)) != 0:
            return ctypes.get_errno()
        return 0


def _open_socket() -> socket.socket:
    return socket.socket(AF_BLUETOOTH, socket.SOCK_SEQPACKET, 0)  # BTPROTO_L2CAP


class L2capChannel:
    def __init__(self, sock: socket.socket, loop: asyncio.AbstractEventLoop) -> None:
        self._sock = sock
        self._loop = loop
        self._closed = False

    async def send(self, pdu: bytes) -> None:
        if self._closed:
            raise ConnectionError("L2CAP channel is closed")
        await self._loop.sock_sendall(self._sock, pdu)

    async def recv(self) -> bytes:
        if self._closed:
            raise ConnectionError("L2CAP channel is closed")
        data = await self._loop.sock_recv(self._sock, RECV_SIZE)
        if self._closed or not data:
            raise ConnectionError("L2CAP peer closed the link")
        return data

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        fd = self._sock.fileno()
        self._loop.remove_reader(fd)
        self._loop.remove_writer(fd)
        self._sock.close()


class L2capLeLink:
    def __init__(
        self,
        adapter_mac: str,
        printer_mac: str,
        connect_timeout: float = 30,
        *,
        socket_factory: Callable[[], socket.socket] = _open_socket,
        kernel: KernelCalls | None = None,
    ) -> None:
        self._source = pack_le_address(adapter_mac)
        self._destination = pack_le_address(printer_mac)
        self._connect_timeout = connect_timeout
        self._socket_factory = socket_factory
        self._kernel = kernel

    async def connect(self) -> L2capChannel:
        loop = asyncio.get_running_loop()
        kernel = self._kernel or KernelCalls()
        sock = self._socket_factory()
        try:
            sock.setblocking(False)
            failure = kernel.bind(sock.fileno(), self._source)
            if failure:
                raise OSError(failure, f"L2CAP bind failed: {os.strerror(failure)}")
            failure = kernel.connect(sock.fileno(), self._destination)
            if failure not in (0, errno.EINPROGRESS, errno.EAGAIN):
                raise OSError(failure, f"LE connect failed: {os.strerror(failure)}")
            async with asyncio.timeout(self._connect_timeout):
                await self._writable(loop, sock.fileno())
            pending = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if pending:
                raise OSError(pending, f"LE connect failed: {os.strerror(pending)}")
        except BaseException:
            # Closing the socket is also what aborts the kernel's pending LE connection.
            sock.close()
            raise
        return L2capChannel(sock, loop)

    @staticmethod
    async def _writable(loop: asyncio.AbstractEventLoop, fd: int) -> None:
        ready: asyncio.Future[None] = loop.create_future()

        def done() -> None:
            if not ready.done():
                ready.set_result(None)

        loop.add_writer(fd, done)
        try:
            await ready
        finally:
            loop.remove_writer(fd)
