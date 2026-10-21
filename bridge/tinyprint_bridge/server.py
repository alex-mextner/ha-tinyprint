"""aiohttp application: bearer auth first, strict validation, job routes. No radio in handlers.

Middleware order matters: authentication wraps everything (including 404/405) and answers before
any request body is read, so unauthenticated callers cannot make the bridge buffer data or learn
which routes exist. Handlers only validate input and call the JobManager; no route accepts an
address, a path, a URL or a command. Access logging is off and headers are never logged, so the
token cannot reach the journal.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import signal
import time
from typing import Any

from aiohttp import web

from . import __version__
from ._shared import bridge_wire as wire
from .config import BridgeConfig
from .jobs import JobManager
from .link import L2capLeLink

_LOGGER = logging.getLogger(__name__)
BODY_READ_TIMEOUT = 15.0  # an authenticated client may not hold a connection open with a trickle
_STARTED = web.AppKey("started", float)
_MANAGER = web.AppKey("manager", JobManager)
_DIGEST = web.AppKey("digest", bytes)


def _error(code: str, message: str, status: int, headers: dict[str, str] | None = None):
    return web.json_response(
        {"error": {"code": code, "message": message}},
        status=status,
        headers={"Cache-Control": "no-store", **(headers or {})},
    )


def _reply(data: dict[str, Any], status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "replace")).digest()


@web.middleware
async def auth_middleware(request: web.Request, handler):
    scheme, _, presented = request.headers.get("Authorization", "").partition(" ")
    # Always compare, so a missing header costs the same as a wrong token.
    matches = hmac.compare_digest(_digest(presented), request.app[_DIGEST])
    if not (matches and scheme.lower() == "bearer" and presented):
        return _error(
            "unauthorized", "Authentication required", 401, {"WWW-Authenticate": "Bearer"}
        )
    return await handler(request)


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except wire.WireError as error:
        return _error(error.code, error.message, error.status)
    except web.HTTPRequestEntityTooLarge:
        return _error("payload_too_large", "Request body is too large", 413)
    except web.HTTPNotFound:
        return _error("not_found", "No such route", 404)
    except web.HTTPMethodNotAllowed as error:
        allow = error.headers.get("Allow", "")
        return _error(
            "method_not_allowed", "Method not allowed", 405, {"Allow": allow} if allow else None
        )
    except web.HTTPException as error:
        return _error("bad_request", "Request rejected", error.status)
    except Exception as error:
        _LOGGER.error("Unhandled %s in request handler", type(error).__name__)
        return _error("internal_error", "Internal bridge error", 500)


def _reject_constant(_name: str) -> None:
    raise ValueError("NaN and Infinity are not valid JSON")


async def _json_body(request: web.Request) -> object:
    if request.content_type != "application/json":
        raise wire.WireError("unsupported_media_type", "Content-Type must be application/json", 415)
    try:
        async with asyncio.timeout(BODY_READ_TIMEOUT):
            raw = await request.read()  # size bounded by client_max_size
    except TimeoutError:
        raise wire.WireError("bad_request", "Request body was not received in time", 408) from None
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except ValueError, RecursionError:
        raise wire.WireError("bad_request", "Body is not valid JSON") from None


async def status(request: web.Request) -> web.Response:
    return _reply(
        {
            "ok": True,
            "version": __version__,
            "protocol": wire.PROTOCOL_VERSION,
            **request.app[_MANAGER].status(),
            "uptime_seconds": round(time.monotonic() - request.app[_STARTED], 1),
            "printer_configured": True,
        }
    )


async def start_print(request: web.Request) -> web.Response:
    parsed = wire.parse_print_request(await _json_body(request))
    return _reply(request.app[_MANAGER].start_print(parsed), 202)


async def start_diagnose(request: web.Request) -> web.Response:
    job_id = wire.parse_diagnose_request(await _json_body(request))
    return _reply(request.app[_MANAGER].start_diagnose(job_id), 202)


async def get_job(request: web.Request) -> web.Response:
    job_id = wire.validate_job_id(request.match_info["job_id"])
    return _reply(request.app[_MANAGER].get(job_id))


async def cancel_job(request: web.Request) -> web.Response:
    job_id = wire.validate_job_id(request.match_info["job_id"])
    return _reply(await request.app[_MANAGER].cancel(job_id))


def create_app(config: BridgeConfig, manager: JobManager) -> web.Application:
    app = web.Application(
        middlewares=[auth_middleware, error_middleware], client_max_size=wire.MAX_BODY_BYTES
    )
    app[_DIGEST] = _digest(config.token)
    app[_MANAGER] = manager
    app[_STARTED] = time.monotonic()
    prefix = wire.API_PREFIX
    app.router.add_get(f"{prefix}/status", status)
    app.router.add_post(f"{prefix}/jobs", start_print)
    app.router.add_post(f"{prefix}/diagnose", start_diagnose)
    app.router.add_get(f"{prefix}/jobs/{{job_id}}", get_job)
    app.router.add_post(f"{prefix}/jobs/{{job_id}}/cancel", cancel_job)
    return app


async def serve(
    config: BridgeConfig,
    *,
    manager: JobManager | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Run until `stop` is set (SIGTERM/SIGINT when not supplied), then shut down bounded."""
    manager = manager or JobManager(
        L2capLeLink(config.adapter_mac, config.printer_mac), tail_rows=config.tail_rows
    )
    loop = asyncio.get_running_loop()
    handlers = stop is None
    stop = stop or asyncio.Event()
    if handlers:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
    runner = web.AppRunner(
        create_app(config, manager), access_log=None, shutdown_timeout=5, keepalive_timeout=15
    )
    await runner.setup()
    try:
        site = web.TCPSite(runner, config.bind, config.port)
        await site.start()
        if config.is_wildcard:
            _LOGGER.warning(
                "Listening on all interfaces; bind to a private address such as the Tailscale IP"
            )
        _LOGGER.info(
            "TinyPrint bridge %s listening on %s:%s", __version__, config.bind, config.port
        )
        await stop.wait()
    finally:
        _LOGGER.info("Shutting down")
        await manager.shutdown()
        await runner.cleanup()
        if handlers:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)
