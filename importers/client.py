"""A small REST client for the memory server's add endpoint, with retries.

Kept dependency-light (httpx only, which the project already pins) so the import
scripts can run from a checkout without installing the server package.
"""

import time

import httpx


class MemoryClient:
    """POSTs memories to ``<base_url>/api/v1/memories`` with bearer auth.

    Retries transient failures (network errors and HTTP 5xx) with exponential
    backoff; 4xx responses are surfaced immediately since retrying won't help.
    With ``dry_run=True`` nothing is sent — ``add`` returns the payload it would
    have posted, which the CLIs print so you can preview an import.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        dry_run: bool = False,
        timeout: float = 30.0,
        max_retries: int = 4,
        backoff_base: float = 2.0,
        sleep=time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.dry_run = dry_run
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep

    def add(
        self,
        *,
        content: str | None = None,
        messages: list[dict] | None = None,
        agent_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        payload: dict = {}
        if content is not None:
            payload["content"] = content
        if messages is not None:
            payload["messages"] = messages
        if agent_id:
            payload["agent_id"] = agent_id
        if metadata:
            payload["metadata"] = metadata
        if not payload.get("content") and not payload.get("messages"):
            raise ValueError("add() requires non-empty content or messages")
        if self.dry_run:
            return {"dry_run": True, "payload": payload}
        return self._post("/api/v1/memories", payload)

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        delay = self.backoff_base
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                # Client errors (4xx) are not retryable — re-raise right away.
                if exc.response.status_code < 500:
                    raise
                last_exc = exc
            except httpx.TransportError as exc:
                last_exc = exc
            if attempt < self.max_retries - 1:
                self._sleep(delay)
                delay *= 2
        assert last_exc is not None
        raise last_exc
