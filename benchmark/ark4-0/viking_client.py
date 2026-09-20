"""Viking experiment API. Mutating requests are deliberately never retried."""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import urlparse

import httpx


class VikingClient:
    def __init__(self, base_url, api_token, *, transport=None):
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/api/v1/",
            headers={"Authorization": f"Bearer {api_token}"},
            timeout=60,
            transport=transport,
            follow_redirects=False,
        )

    async def close(self):
        await self.http.aclose()

    async def request(self, method, path, **kwargs):
        for attempt in range(4):
            try:
                response = await self.http.request(method, path, **kwargs)
                if method == "GET" and response.status_code in {429, 502, 503, 504} and attempt < 3:
                    await asyncio.sleep(2**attempt)
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Viking returned a non-object response")
                if "code" in payload:
                    if payload["code"] != 0:
                        raise ValueError(
                            f"Viking API error {payload['code']}: {payload.get('msg')}"
                        )
                    return payload["data"]
                return payload
            except httpx.TransportError:
                if method != "GET" or attempt == 3:
                    raise
                await asyncio.sleep(2**attempt)

    async def pages(self, path, *, page_size=200, **params):
        page, items = 1, []
        while True:
            data = await self.request(
                "GET", path, params={**params, "page": page, "page_size": page_size}
            )
            rows = data["list"]
            items.extend(rows)
            if len(items) >= data["total"]:
                return items
            if not rows:
                raise ValueError(f"Viking pagination ended early: {path}")
            page += 1

    async def tos_json(self, path):
        data = await self.request("GET", "tos/content", params={"path": path, "max_bytes": 5242880})
        if not data["truncated"]:
            raw = (
                base64.b64decode(data["text"]) if data.get("encoding") == "base64" else data["text"]
            )
        else:
            signed = await self.request("GET", "tos/presign", params={"path": path})
            url = signed["url"]
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.username
                or not any(
                    (parsed.hostname or "").endswith(suffix)
                    for suffix in (".volces.com", ".bytedance.net", ".byteimg.com", ".byted.org")
                )
            ):
                raise ValueError("Untrusted TOS download host; refusing signed URL")
            # Separate client: never forward the Viking bearer token to object storage.
            async with httpx.AsyncClient(timeout=120, follow_redirects=False) as downloader:
                async with downloader.stream("GET", url) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 128 * 1024 * 1024:
                            raise ValueError("Trace exceeds 128 MiB download safety limit")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
        return json.loads(raw)

    async def tos_files(self, prefix):
        marker, files = None, []
        while True:
            params = {"path": prefix, "limit": 1000}
            if marker:
                params["marker"] = marker
            data = await self.request("GET", "tos/list", params=params)
            files.extend(item for item in data["items"] if item["kind"] == "file")
            next_marker = data.get("next_marker")
            if not next_marker:
                return files
            if next_marker == marker:
                raise ValueError("TOS listing repeated its pagination marker")
            marker = next_marker
