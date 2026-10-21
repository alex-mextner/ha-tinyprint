"""Bridge configuration from the environment: addresses, token, bind. Fails closed.

The printer and adapter addresses are fixed here at startup; no HTTP request can ever choose
one. The token comes from a file that only its owner may access (or the environment) and must
pass the shared strength check. Errors name the setting, never its value.
"""

import ipaddress
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field

from ._shared import bridge_wire

DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8765
_MAC = re.compile(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}")
_TOKEN_FILE_LIMIT = 4096


class ConfigError(Exception):
    """Actionable configuration problem; the process exits with code 2."""


@dataclass(frozen=True)
class BridgeConfig:
    printer_mac: str
    adapter_mac: str
    token: str = field(repr=False)
    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT

    @property
    def is_wildcard(self) -> bool:
        return ipaddress.ip_address(self.bind).is_unspecified


def parse_mac(value: str | None, setting: str) -> str:
    if not value or _MAC.fullmatch(value.strip()) is None:
        raise ConfigError(f"{setting} must be a MAC address like AA:BB:CC:DD:EE:FF")
    mac = value.strip().upper()
    if mac == "00:00:00:00:00:00":
        raise ConfigError(f"{setting} must not be the null address")
    return mac


def read_token_file(path: str) -> str:
    """Read the token, refusing symlinks, non-regular files and group/other access."""
    try:
        # O_NONBLOCK: opening a FIFO must not hang startup; it is rejected as non-regular below.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as error:
        raise ConfigError(f"Cannot open TINYPRINT_BRIDGE_TOKEN_FILE: {error.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("TINYPRINT_BRIDGE_TOKEN_FILE must be a regular file")
        if info.st_mode & 0o077:
            raise ConfigError("TINYPRINT_BRIDGE_TOKEN_FILE must not be group or world accessible")
        if info.st_uid != os.geteuid():
            raise ConfigError("TINYPRINT_BRIDGE_TOKEN_FILE must be owned by the service user")
        raw = os.read(fd, _TOKEN_FILE_LIMIT + 1)
    finally:
        os.close(fd)
    if len(raw) > _TOKEN_FILE_LIMIT:
        raise ConfigError("TINYPRINT_BRIDGE_TOKEN_FILE is too large")
    try:
        return raw.decode("ascii").removesuffix("\n").removesuffix("\r")
    except UnicodeDecodeError:
        raise ConfigError("TINYPRINT_BRIDGE_TOKEN_FILE must be ASCII") from None


def parse_bind(value: str) -> str:
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        raise ConfigError(
            "Bind address must be an IP address (hostnames are not accepted)"
        ) from None


def parse_port(value: object) -> int:
    try:
        port = int(str(value))
    except ValueError:
        raise ConfigError("Port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise ConfigError("Port must be an integer from 1 to 65535")
    return port


def load_config(
    env: Mapping[str, str], *, bind: str | None = None, port: int | None = None
) -> BridgeConfig:
    """Build the config from `TINYPRINT_BRIDGE_*` variables; CLI flags may override bind/port."""
    printer = parse_mac(env.get("TINYPRINT_BRIDGE_PRINTER_MAC"), "TINYPRINT_BRIDGE_PRINTER_MAC")
    adapter = parse_mac(env.get("TINYPRINT_BRIDGE_ADAPTER_MAC"), "TINYPRINT_BRIDGE_ADAPTER_MAC")
    token_file = env.get("TINYPRINT_BRIDGE_TOKEN_FILE")
    token_env = env.get("TINYPRINT_BRIDGE_TOKEN")
    if token_file and token_env:
        raise ConfigError("Set only one of TINYPRINT_BRIDGE_TOKEN and TINYPRINT_BRIDGE_TOKEN_FILE")
    if not token_file and not token_env:
        raise ConfigError("TINYPRINT_BRIDGE_TOKEN_FILE (or TINYPRINT_BRIDGE_TOKEN) is required")
    token = read_token_file(token_file) if token_file else token_env
    try:
        token = bridge_wire.check_token_strength(token)
    except ValueError as error:
        raise ConfigError(str(error)) from None
    bind_value = parse_bind(
        bind if bind is not None else env.get("TINYPRINT_BRIDGE_BIND", DEFAULT_BIND)
    )
    port_value = parse_port(
        port if port is not None else env.get("TINYPRINT_BRIDGE_PORT", DEFAULT_PORT)
    )
    return BridgeConfig(printer, adapter, token, bind_value, port_value)
