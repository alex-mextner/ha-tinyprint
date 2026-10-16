"""L2CAP LE link: sockaddr packing and the non-blocking connect/IO state machine.

No AF_BLUETOOTH socket is ever opened: a fake kernel and socketpairs stand in for it.
"""

import asyncio
import errno
import os
import socket
import struct

import pytest
from tinyprint_bridge import link

PRINTER = "4C:2A:EE:D3:3E:C2"
ADAPTER = "00:1A:7D:DA:71:13"


class FakeSocket:
    """Delegates I/O to a real socketpair end but fakes connect completion state."""

    def __init__(self, io: socket.socket | None = None, *, fd: int | None = None, so_error=0):
        self.io = io
        self._fd = fd
        self.so_error = so_error
        self.closed = False
        self.blocking_calls = []

    def fileno(self):
        return self._fd if self._fd is not None else self.io.fileno()

    def setblocking(self, flag):
        self.blocking_calls.append(flag)
        if self.io:
            self.io.setblocking(flag)

    def getsockopt(self, level, option):
        assert (level, option) == (socket.SOL_SOCKET, socket.SO_ERROR)
        return self.so_error

    def close(self):
        self.closed = True
        if self.io:
            self.io.close()

    def __getattr__(self, name):
        return getattr(self.io, name)


class FakeKernel:
    def __init__(self, bind_result=0, connect_result=errno.EINPROGRESS):
        self.bind_result = bind_result
        self.connect_result = connect_result
        self.calls = []

    def bind(self, fd, address):
        self.calls.append(("bind", address))
        return self.bind_result

    def connect(self, fd, address):
        self.calls.append(("connect", address))
        return self.connect_result


def make_link(sock, kernel=None, timeout=1.0):
    return link.L2capLeLink(
        ADAPTER,
        PRINTER,
        connect_timeout=timeout,
        socket_factory=lambda: sock,
        kernel=kernel or FakeKernel(),
    )


def test_sockaddr_packing_is_the_proven_layout():
    packed = link.pack_le_address(PRINTER)
    assert packed == bytes.fromhex("1f00 0000 c23ed3ee2a4c 0400 01 00".replace(" ", ""))
    assert len(packed) == struct.calcsize("<HH6sHBx") == 14


def test_construction_opens_no_socket():
    def forbidden():
        raise AssertionError("socket opened at construction")

    link.L2capLeLink(ADAPTER, PRINTER, socket_factory=forbidden, kernel=FakeKernel())


async def test_connect_binds_adapter_then_connects_printer_and_returns_channel():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock = FakeSocket(left)
    kernel = FakeKernel()
    channel = await make_link(sock, kernel).connect()
    assert kernel.calls == [
        ("bind", link.pack_le_address(ADAPTER)),
        ("connect", link.pack_le_address(PRINTER)),
    ]
    assert sock.blocking_calls[0] is False
    await channel.send(b"\x52\x03\x00hi")
    assert await asyncio.get_running_loop().sock_recv(right, 100) == b"\x52\x03\x00hi"
    right.send(b"\x1b\x01\x00x")
    assert await channel.recv() == b"\x1b\x01\x00x"
    channel.close()
    channel.close()
    assert sock.closed
    right.close()


async def test_immediate_connect_success_still_checks_so_error():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock = FakeSocket(left, so_error=errno.ECONNREFUSED)
    with pytest.raises(OSError) as caught:
        await make_link(sock, FakeKernel(connect_result=0)).connect()
    assert caught.value.errno == errno.ECONNREFUSED
    assert sock.closed
    right.close()


async def test_recv_eof_is_connection_error():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    channel = await make_link(FakeSocket(left)).connect()
    right.close()
    with pytest.raises(ConnectionError):
        await channel.recv()
    channel.close()


async def test_recv_after_close_raises_instead_of_hanging():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    channel = await make_link(FakeSocket(left)).connect()
    channel.close()
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(channel.recv(), 1)
    with pytest.raises(ConnectionError):
        await channel.send(b"x")
    right.close()


async def test_connect_timeout_closes_socket_to_cancel_kernel_connect():
    read_end, write_end = os.pipe()  # never writable, like a pending LE connect
    sock = FakeSocket(fd=read_end)
    with pytest.raises(TimeoutError):
        await make_link(sock, timeout=0.05).connect()
    assert sock.closed
    os.close(read_end)
    os.close(write_end)


async def test_connect_cancellation_closes_socket():
    read_end, write_end = os.pipe()
    sock = FakeSocket(fd=read_end)
    task = asyncio.create_task(make_link(sock, timeout=5).connect())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sock.closed
    os.close(read_end)
    os.close(write_end)


async def test_so_error_after_wait_is_mapped_to_oserror_and_closes():
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock = FakeSocket(left, so_error=errno.EHOSTUNREACH)
    with pytest.raises(OSError) as caught:
        await make_link(sock).connect()
    assert caught.value.errno == errno.EHOSTUNREACH and sock.closed
    right.close()


@pytest.mark.parametrize("which", ["bind", "connect"])
async def test_syscall_failure_is_oserror_and_closes(which):
    left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    sock = FakeSocket(left)
    kernel = FakeKernel(
        bind_result=errno.EADDRNOTAVAIL if which == "bind" else 0,
        connect_result=errno.ECONNREFUSED if which == "connect" else errno.EINPROGRESS,
    )
    with pytest.raises(OSError) as caught:
        await make_link(sock, kernel).connect()
    expected = errno.EADDRNOTAVAIL if which == "bind" else errno.ECONNREFUSED
    assert caught.value.errno == expected and sock.closed
    right.close()


async def test_socket_creation_failure_propagates():
    def broken():
        raise PermissionError(errno.EPERM, "no bluetooth")

    bridge_link = link.L2capLeLink(ADAPTER, PRINTER, socket_factory=broken, kernel=FakeKernel())
    with pytest.raises(PermissionError):
        await bridge_link.connect()
