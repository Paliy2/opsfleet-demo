from functools import partial
from pathlib import Path
from typing import override

import anyio
from google.cloud.storage import Client

from opsfleet_agent.core.errors import NotFoundError
from opsfleet_agent.ports.blobs import BlobStore

_GCS_SCHEME = "gs://"


class LocalBlobStore(BlobStore):
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @override
    async def put(self, key: str, data: bytes, content_type: str) -> str:
        path = self._directory / key
        await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
        await anyio.Path(path).write_bytes(data)
        return str(path)

    @override
    async def get(self, location: str) -> bytes:
        path = anyio.Path(location)
        if not await path.exists():
            raise NotFoundError(f"blob {location} not found")
        return await path.read_bytes()


class GcsBlobStore(BlobStore):
    def __init__(self, client: Client, bucket: str) -> None:
        self._bucket = client.bucket(bucket)
        self._name = bucket

    @override
    async def put(self, key: str, data: bytes, content_type: str) -> str:
        blob = self._bucket.blob(key)
        await anyio.to_thread.run_sync(
            partial(blob.upload_from_string, data, content_type=content_type)
        )
        return f"{_GCS_SCHEME}{self._name}/{key}"

    @override
    async def get(self, location: str) -> bytes:
        prefix = f"{_GCS_SCHEME}{self._name}/"
        if not location.startswith(prefix):
            raise NotFoundError(f"blob {location} is outside bucket {self._name}")
        blob = self._bucket.blob(location.removeprefix(prefix))
        data: bytes = await anyio.to_thread.run_sync(blob.download_as_bytes)
        return data
