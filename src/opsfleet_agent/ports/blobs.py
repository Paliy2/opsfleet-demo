from typing import Protocol


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes under key and return a location string for get()."""

    async def get(self, location: str) -> bytes: ...
