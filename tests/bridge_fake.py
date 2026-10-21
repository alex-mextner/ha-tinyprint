"""Scripted fake TinyPrint bridge (aiohttp only) shared by the pure and HA-image client tests."""

import asyncio

from aiohttp import web

TERMINAL = frozenset({"sent", "failed", "cancelled"})
MAX_RESPONSE_BYTES = 128 * 1024

TOKEN = "T" * 40
LEAK = "SECRET-BODY-MARKER"


def make_view(job_id, state="accepted", kind="print", **over):
    # Defaults follow the real bridge: confirmation and error are fixed by state and kind.
    confirmation = "none"
    if state == "sent":
        confirmation = "transport_only" if kind == "print" else "connection_only"
    error = None
    if state in ("failed", "cancelled"):
        error = {"code": "cancelled" if state == "cancelled" else "link_lost", "message": "x"}
    view = {
        "job_id": job_id,
        "kind": kind,
        "state": state,
        "terminal": state in TERMINAL,
        "bytes_written": 0,
        "frames_written": 0,
        "writes": 0,
        "notifications": 0,
        "flow_pauses": 0,
        "delivery_uncertain": False,
        "confirmation": confirmation,
        "physical_print_confirmed": False,
        "elapsed_seconds": 0.1,
        "error": error,
        "diagnosis": None,
    }
    view.update(over)
    return view


class FakeBridge:
    """Scripted bridge. `script` yields the next GET view for the single job."""

    def __init__(self):
        self.requests = []
        self.bodies = []
        self.job_id = None
        self.start_mode = "ok"  # ok | hang | drop | busy | error500 | redirect | garbage | huge
        self.start_state = "accepted"
        self.polls = []  # queue of dicts (view overrides) or "fail" / "drop"
        self.default_poll = None
        self.cancel_states = []
        self.cancel_not_found_first = 0
        self.cancel_delay = 0.0
        self.cancel_finished = False
        self.token = TOKEN
        self.status_body = {
            "ok": True,
            "version": "0.2.1",
            "protocol": 1,
            "busy": False,
            "active_job_id": None,
            "retained_jobs": 0,
            "uptime_seconds": 1,
            "printer_configured": True,
        }
        self.extra_key = False

    def app(self):
        @web.middleware
        async def auth(request, handler):
            self.requests.append((request.method, request.path))
            if request.headers.get("Authorization") != f"Bearer {self.token}":
                return web.json_response(
                    {"error": {"code": "unauthorized", "message": "no"}}, status=401
                )
            return await handler(request)

        app = web.Application(middlewares=[auth])
        app.router.add_get("/v1/status", self.status)
        app.router.add_post("/v1/jobs", self.start)
        app.router.add_post("/v1/diagnose", self.start)
        app.router.add_get("/v1/jobs/{job_id}", self.job)
        app.router.add_post("/v1/jobs/{job_id}/cancel", self.cancel)
        app.router.add_get("/elsewhere", self.status)
        return app

    def view(self, state, **over):
        view = make_view(self.job_id, state, **over)
        if self.extra_key:
            view["secret"] = LEAK
        return view

    async def status(self, request):
        return web.json_response(self.status_body)

    async def start(self, request):
        body = await request.json()
        self.bodies.append(body)
        self.job_id = body["job_id"]
        mode = self.start_mode
        if mode == "hang":
            await asyncio.sleep(30)
        if mode == "drop":
            request.transport.close()
            raise asyncio.CancelledError
        if mode == "busy":
            return web.json_response({"error": {"code": "busy", "message": LEAK}}, status=409)
        if mode == "error500":
            return web.Response(status=500, text=LEAK)
        if mode == "redirect":
            return web.Response(status=302, headers={"Location": "/elsewhere"})
        if mode == "garbage":
            return web.Response(status=202, text="not json " + LEAK)
        if mode == "huge":
            return web.Response(status=202, body=b"x" * (MAX_RESPONSE_BYTES + 10))
        kind = "diagnose" if request.path.endswith("diagnose") else "print"
        view = self.view(self.start_state, kind=kind)
        return web.json_response(view, status=202)

    async def job(self, request):
        if self.polls:
            item = self.polls.pop(0)
        else:
            item = self.default_poll if self.default_poll is not None else {"state": "transmitting"}
        if item == "fail":
            return web.Response(status=500, text="boom")
        if item == "drop":
            request.transport.close()
            raise asyncio.CancelledError
        item = dict(item)
        return web.json_response(self.view(item.pop("state"), **item))

    async def cancel(self, request):
        if self.cancel_delay:
            await asyncio.sleep(self.cancel_delay)
        self.cancel_finished = True
        if self.cancel_not_found_first:
            self.cancel_not_found_first -= 1
            return web.json_response(
                {"error": {"code": "not_found", "message": "unknown"}}, status=404
            )
        item = self.cancel_states.pop(0) if self.cancel_states else {"state": "cancelled"}
        item = dict(item)
        return web.json_response(self.view(item.pop("state"), **item))

    def count(self, method, suffix):
        return sum(1 for m, p in self.requests if m == method and p.endswith(suffix))
