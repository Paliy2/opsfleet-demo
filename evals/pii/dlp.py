from time import perf_counter

import anyio
import google.auth
from google.api_core.exceptions import (
    DeadlineExceeded,
    InternalServerError,
    ResourceExhausted,
    ServiceUnavailable,
)
from google.cloud import dlp_v2

from opsfleet_agent.gateways.dlp.redactor import InspectClient

TRANSIENT = (DeadlineExceeded, InternalServerError, ResourceExhausted, ServiceUnavailable)
BACKOFF_SECONDS = (1.0, 2.0, 4.0)


class AdcProjectMismatchError(RuntimeError):
    pass


def ensure_adc_quota_project(project: str) -> None:
    credentials, _ = google.auth.default()
    quota_project = getattr(credentials, "quota_project_id", None)
    if quota_project != project:
        raise AdcProjectMismatchError(
            f"Application Default Credentials bill {quota_project!r}, not {project!r}. Run: "
            f"gcloud auth application-default login && "
            f"gcloud auth application-default set-quota-project {project}"
        )


class Pacer:
    def __init__(self, per_second: float) -> None:
        self._interval = 1 / per_second
        self._next = 0.0
        self._lock = anyio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = anyio.current_time()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self._interval
        await anyio.sleep(delay)


class MeteredInspectClient:
    def __init__(
        self,
        client: InspectClient,
        backoff_seconds: tuple[float, ...] = BACKOFF_SECONDS,
    ) -> None:
        self._client = client
        self._backoff = backoff_seconds
        self.seconds: list[float] = []
        self.retries = 0

    async def inspect_content(
        self, request: dlp_v2.InspectContentRequest
    ) -> dlp_v2.InspectContentResponse:
        for delay in self._backoff:
            try:
                return await self._timed(request)
            except TRANSIENT:
                self.retries += 1
                await anyio.sleep(delay)
        return await self._timed(request)

    async def _timed(self, request: dlp_v2.InspectContentRequest) -> dlp_v2.InspectContentResponse:
        started = perf_counter()
        response = await self._client.inspect_content(request)
        self.seconds.append(perf_counter() - started)
        return response
