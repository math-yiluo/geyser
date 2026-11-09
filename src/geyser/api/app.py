"""The loopback operator API and the dashboard."""

from __future__ import annotations

import hmac
import logging
from importlib import resources
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from geyser import operator
from geyser.api.access import COOKIE, host_is_loopback
from geyser.core.models import TaskContract
from geyser.errors import GeyserError, NotFound

if TYPE_CHECKING:
    from geyser.service import GeyserService

log = logging.getLogger(__name__)

# The dashboard is three static files; nothing inline runs (DESIGN.md section 15.2).
_PAGES = {"/": ("dashboard.html", "text/html; charset=utf-8"),
          "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
          "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8")}
_PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                               "img-src 'self' data:; base-uri 'none'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_STATUS = {"not_found": 404, "conflict": 409, "forbidden": 403, "invalid_argument": 400,
           "stale_actor": 409, "unavailable": 503, "integrity": 500}


class SetupBody(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    goal: str = Field(min_length=1, max_length=20_000)
    acceptance: list[str] = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    context: str = ""
    backend: str | None = None


class MessageBody(BaseModel):
    to: str
    text: str = Field(min_length=1)


class TextBody(BaseModel):
    text: str = Field(min_length=1)


class ReasonBody(BaseModel):
    reason: str = Field(min_length=1)


class ClaimBody(BaseModel):
    agent: str


class BackendBody(BaseModel):
    backend: str


class InviteBody(BaseModel):
    agent: str
    reason: str = Field(min_length=1)


class ChoiceBody(BaseModel):
    role: str
    """``workers.<backend>``, ``reviewers``, ``integrator``, ``dispatcher``, ``waker`` or
    ``answer_deadline``."""
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    minutes: float | None = None


def create_app(service: GeyserService, token: str) -> FastAPI:
    hub = service.hub
    app = FastAPI(title="Geyser", docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(request: Request) -> None:
        if not host_is_loopback(request.headers.get("host")):
            raise HTTPException(403, "Requests must name a loopback host.")
        origin = request.headers.get("origin")
        if origin is not None and not host_is_loopback(origin.split("://", 1)[-1]):
            raise HTTPException(403, "Browser requests must come from the dashboard's origin.")
        supplied = request.cookies.get(COOKIE)
        header = request.headers.get("authorization", "")
        if header.startswith("Bearer "):
            supplied = header.removeprefix("Bearer ").strip()
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(401, "The operator token is missing or wrong.")

    guard = [Depends(authorize)]

    @app.exception_handler(GeyserError)
    async def geyser_error(_request: Request, error: GeyserError) -> JSONResponse:
        return JSONResponse(error.to_dict(), status_code=_STATUS.get(error.code, 400))

    @app.exception_handler(Exception)
    async def internal_error(_request: Request, error: Exception) -> JSONResponse:
        log.exception("operator request failed")
        return JSONResponse({"error": "internal", "message": str(error)[:500]},
                            status_code=500)

    # ---------------------------------------------------------------- pages
    @app.get("/login")
    async def login(request: Request, supplied: str = Query("", alias="token")
                    ) -> RedirectResponse:
        if not host_is_loopback(request.headers.get("host")):
            raise HTTPException(403, "Requests must name a loopback host.")
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(401, "Wrong sign-in link.")
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=False)
        return response

    def page(path: str) -> None:
        name, media_type = _PAGES[path]

        async def serve() -> Response:
            text = resources.files("geyser.api").joinpath(name).read_text("utf-8")
            return Response(text, media_type=media_type, headers=_PAGE_HEADERS)

        app.add_api_route(path, serve, methods=["GET"], dependencies=guard,
                          include_in_schema=False)

    for path in _PAGES:
        page(path)

    # ------------------------------------------------------------- reading
    @app.get("/v1/status", dependencies=guard)
    async def status() -> dict[str, Any]:
        return operator.status(hub)

    @app.get("/v1/agents", dependencies=guard)
    async def agent_list() -> list[dict[str, Any]]:
        return operator.agent_list(hub)

    @app.get("/v1/agents/{agent_id}/turns", dependencies=guard)
    async def agent_turns(agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return operator.agent_turns(hub, agent_id, max(1, min(limit, 100)))

    @app.get("/v1/agents/{agent_id}/queued", dependencies=guard)
    async def queued(agent_id: str) -> list[dict[str, Any]]:
        return operator.queued_messages(hub, agent_id)

    @app.get("/v1/tasks", dependencies=guard)
    async def task_list() -> list[dict[str, Any]]:
        return operator.task_list(hub)

    @app.get("/v1/task-graph", dependencies=guard)
    async def task_graph() -> dict[str, Any]:
        return operator.task_graph(hub)

    @app.get("/v1/tasks/{task_id}", dependencies=guard)
    async def task_detail(task_id: str) -> dict[str, Any]:
        return operator.task_detail(hub, task_id)

    @app.get("/v1/requests", dependencies=guard)
    async def request_list() -> list[dict[str, Any]]:
        return operator.requests_open(hub)

    @app.get("/v1/activity", dependencies=guard)
    async def activity(agent: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return operator.activity(hub, agent, min(limit, 500))

    @app.get("/v1/messages/inbound", dependencies=guard)
    async def inbound() -> list[dict[str, Any]]:
        return operator.messages_to_operator(hub)

    # ------------------------------------------------------------- actions
    @app.post("/v1/setup", dependencies=guard)
    async def setup(body: SetupBody) -> dict[str, Any]:
        return operator.setup_project(hub, title=body.title, goal=body.goal,
                                      acceptance=body.acceptance, constraints=body.constraints,
                                      context=body.context, backend=body.backend)

    @app.post("/v1/run/{action}", dependencies=guard)
    async def run(action: str) -> dict[str, Any]:
        return operator.run_action(hub, action.replace("-", "_"), service)

    @app.get("/v1/choices", dependencies=guard)
    async def choice_overview() -> dict[str, Any]:
        return operator.choice_overview(hub)

    @app.post("/v1/choices", dependencies=guard)
    async def choose(body: ChoiceBody) -> dict[str, Any]:
        workers_on = body.role.split(".", 1)[1] if body.role.startswith("workers.") else None
        named = body.backend or workers_on
        if named is not None and named not in service.backends:
            raise NotFound(f"No backend named {named!r} is configured.")
        return operator.choose(hub, body.role, backend=body.backend, model=body.model,
                               effort=body.effort, minutes=body.minutes)

    @app.post("/v1/agents/{agent_id}/move", dependencies=guard)
    async def move(agent_id: str, body: BackendBody) -> dict[str, Any]:
        if body.backend not in service.backends:
            raise NotFound(f"No backend named {body.backend!r} is configured.")
        return operator.move_agent(hub, agent_id, body.backend)

    @app.post("/v1/agents/{agent_id}/{action}", dependencies=guard)
    async def agent_action(agent_id: str, action: str) -> dict[str, Any]:
        return operator.agent_action(hub, agent_id, action.replace("-", "_"), service)

    @app.post("/v1/groups/{group}/{action}", dependencies=guard)
    async def group_action(group: str, action: str) -> dict[str, Any]:
        return operator.group_action(hub, group, action.replace("-", "_"))

    @app.post("/v1/backend", dependencies=guard)
    async def new_backend(body: BackendBody) -> dict[str, Any]:
        if body.backend not in service.backends:
            raise NotFound(f"No backend named {body.backend!r} is configured.")
        return operator.set_new_backend(hub, body.backend)

    @app.post("/v1/messages", dependencies=guard)
    async def message(body: MessageBody) -> dict[str, Any]:
        return operator.send_message(hub, body.to, body.text)

    @app.post("/v1/messages/{message_id}/withdraw", dependencies=guard)
    async def withdraw(message_id: str) -> dict[str, Any]:
        return operator.withdraw_message(hub, message_id)

    @app.post("/v1/requests/{request_id}/answer", dependencies=guard)
    async def answer(request_id: str, body: TextBody) -> dict[str, Any]:
        return operator.answer_request(hub, request_id, body.text)

    @app.post("/v1/tasks", dependencies=guard)
    async def publish(contract: TaskContract) -> dict[str, Any]:
        return operator.publish_task(hub, contract)

    @app.post("/v1/tasks/{task_id}/claim", dependencies=guard)
    async def claim(task_id: str, body: ClaimBody) -> dict[str, Any]:
        return operator.claim(hub, task_id, body.agent)

    @app.post("/v1/tasks/{task_id}/invite-back", dependencies=guard)
    async def invite_back(task_id: str, body: InviteBody) -> dict[str, Any]:
        return operator.invite_back(hub, task_id, body.agent, body.reason)

    @app.post("/v1/declarations/{declaration_id}/cancel", dependencies=guard)
    async def cancel(declaration_id: str, body: ReasonBody) -> dict[str, Any]:
        return operator.cancel_declaration(hub, declaration_id, body.reason)

    @app.post("/v1/candidates/{candidate_id}/retry", dependencies=guard)
    async def retry(candidate_id: str, body: ReasonBody) -> dict[str, Any]:
        return operator.retry_validation(hub, candidate_id, body.reason)

    return app

