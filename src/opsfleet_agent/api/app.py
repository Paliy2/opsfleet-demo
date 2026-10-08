from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from redis.asyncio import Redis

from opsfleet_agent.api.schemas import (
    AuthConfig,
    ConfirmationDecision,
    ErrorEvent,
    Me,
    ReportList,
    ReportView,
    SessionCreated,
    TurnRequest,
)
from opsfleet_agent.api.streaming import TurnRunner, stream_turn
from opsfleet_agent.core.config import Environment, Settings, get_settings
from opsfleet_agent.core.errors import (
    AgentError,
    ErrorCode,
    ForbiddenError,
    NotFoundError,
    UnauthenticatedError,
)
from opsfleet_agent.entities.access import AccessScope
from opsfleet_agent.entities.identity import Principal
from opsfleet_agent.gateways.auth.jwt_verifier import JwtVerifier
from opsfleet_agent.gateways.observability.metrics import serve_metrics
from opsfleet_agent.gateways.ratelimit.redis_limiter import (
    RedisRateLimiter,
    UnlimitedRateLimiter,
)
from opsfleet_agent.ports.identity import RateLimiter, TokenVerifier
from opsfleet_agent.wiring import App, build_app

_STATUS = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN: 403,
    ErrorCode.SCOPE_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.PII_UNAVAILABLE: 503,
}
_BEARER = "Bearer "
_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@dataclass(frozen=True)
class Services:
    app: App
    verifier: TokenVerifier | None
    limiter: RateLimiter
    environment: Environment
    auth: AuthConfig | None


def _auth_config(settings: Settings) -> AuthConfig | None:
    if settings.auth_issuer is None:
        return None
    return AuthConfig(issuer=settings.auth_issuer, cli_client_id=settings.auth_cli_client_id)


def _verifier(settings: Settings) -> TokenVerifier | None:
    if settings.auth_issuer is None or settings.auth_jwks_url is None:
        return None
    return JwtVerifier.from_jwks(
        issuer=settings.auth_issuer,
        jwks_url=settings.auth_jwks_url,
        audience=settings.auth_audience,
    )


@asynccontextmanager
async def _limiter(settings: Settings) -> AsyncIterator[RateLimiter]:
    if settings.redis_url is None:
        yield UnlimitedRateLimiter()
        return
    client = Redis.from_url(settings.redis_url)
    try:
        yield RedisRateLimiter(
            client,
            per_minute=settings.rate_limit_per_minute,
            per_day=settings.rate_limit_per_day,
        )
    finally:
        await client.aclose()


def _services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


async def _principal(
    services: Annotated[Services, Depends(_services)],
    authorization: Annotated[str | None, Header()] = None,
    x_demo_user: Annotated[str | None, Header()] = None,
) -> Principal:
    if authorization and authorization.startswith(_BEARER) and services.verifier is not None:
        return await services.verifier.verify(authorization.removeprefix(_BEARER))
    if x_demo_user and services.environment is Environment.LOCAL:
        return Principal(user_id=x_demo_user, roles=frozenset())
    raise UnauthenticatedError("missing bearer token")


async def _scope(
    services: Annotated[Services, Depends(_services)],
    principal: Annotated[Principal, Depends(_principal)],
) -> AccessScope:
    scope = await services.app.chat.scope(principal.user_id)
    if principal.roles and scope.role not in principal.roles:
        raise ForbiddenError(f"token roles {sorted(principal.roles)} do not include {scope.role}")
    return scope


ServicesDep = Annotated[Services, Depends(_services)]
ScopeDep = Annotated[AccessScope, Depends(_scope)]


def _stream(runner: TurnRunner) -> StreamingResponse:
    return StreamingResponse(
        stream_turn(runner), media_type="text/event-stream", headers=_SSE_HEADERS
    )


def _routes(api: FastAPI) -> None:
    @api.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/readyz")
    async def readyz(services: ServicesDep) -> dict[str, str]:
        await services.app.ping()
        return {"status": "ready"}

    @api.get("/v1/auth")
    async def auth(services: ServicesDep) -> AuthConfig:
        if services.auth is None:
            raise NotFoundError("auth is not configured")
        return services.auth

    @api.get("/v1/me")
    async def me(scope: ScopeDep) -> Me:
        return Me(user_id=scope.user_id, role=scope.role, brands=tuple(sorted(scope.brands)))

    @api.get("/v1/reports")
    async def reports(services: ServicesDep, scope: ScopeDep) -> ReportList:
        return ReportList.of(await services.app.list_reports(scope.user_id, None))

    @api.get("/v1/reports/{report_id}")
    async def report(report_id: UUID, services: ServicesDep, scope: ScopeDep) -> ReportView:
        return ReportView.of(await services.app.get_report(scope.user_id, report_id))

    @api.post("/v1/sessions", status_code=201)
    async def create_session(services: ServicesDep, scope: ScopeDep) -> SessionCreated:
        return SessionCreated(session_id=await services.app.chat.open_session(scope.user_id))

    @api.post("/v1/sessions/{session_id}/turns")
    async def ask(
        session_id: UUID, body: TurnRequest, services: ServicesDep, scope: ScopeDep
    ) -> StreamingResponse:
        await services.limiter.hit(scope.user_id)
        await services.app.chat.require_session(scope.user_id, session_id)
        chat = services.app.chat
        return _stream(lambda sink: chat(scope.user_id, session_id, body.question, sink))

    @api.post("/v1/sessions/{session_id}/confirmations")
    async def confirm(
        session_id: UUID, body: ConfirmationDecision, services: ServicesDep, scope: ScopeDep
    ) -> StreamingResponse:
        await services.app.chat.require_session(scope.user_id, session_id)
        chat = services.app.chat
        return _stream(
            lambda sink: chat.confirm(scope.user_id, session_id, body.turn_id, body.approved, sink)
        )

    @api.get("/v1/sessions/{session_id}/charts/{chart_id}")
    async def chart(
        session_id: UUID, chart_id: UUID, services: ServicesDep, scope: ScopeDep
    ) -> Response:
        await services.app.chat.require_session(scope.user_id, session_id)
        png = await services.app.charts.read(chart_id, session_id)
        return Response(png, media_type="image/png", headers={"Cache-Control": "private"})


async def _agent_error(_request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, AgentError):
        raise error
    body = ErrorEvent(code=error.code, message=error.user_message, trace_id=error.trace_id)
    return JSONResponse(body.model_dump(), status_code=_STATUS.get(error.code, 400))


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()

    @asynccontextmanager
    async def lifespan(api: FastAPI) -> AsyncIterator[None]:
        if resolved.metrics_port is not None:
            serve_metrics(resolved.metrics_port)
        async with build_app(resolved) as application, _limiter(resolved) as limiter:
            api.state.services = Services(
                app=application,
                verifier=_verifier(resolved),
                limiter=limiter,
                environment=resolved.env,
                auth=_auth_config(resolved),
            )
            yield

    api = FastAPI(title="OpsFleet Analyst API", version="1.0.0", lifespan=lifespan)
    api.add_exception_handler(AgentError, _agent_error)
    _routes(api)
    return api
