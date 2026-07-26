from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any
import base64
from urllib import error, request


class OllamaError(RuntimeError):
    pass


@dataclass(slots=True)
class OllamaResponse:
    content: str
    raw: dict[str, Any]


class OllamaClient:
    def __init__(self, base_url: str, timeout_seconds: int = 180, request_retries: int = 2) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.request_retries = max(0, request_retries)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers=headers,
        )
        decoded = ""
        for attempt in range(self.request_retries + 1):
            try:
                with request.urlopen(req, timeout=timeout_seconds or self.timeout_seconds) as response:
                    decoded = response.read().decode("utf-8")
                break
            except error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                retryable = exc.code in {408, 429} or exc.code >= 500
                if not retryable or attempt >= self.request_retries:
                    raise OllamaError(f"Ollama HTTP {exc.code}: {detail}") from exc
            except (error.URLError, TimeoutError, OSError) as exc:
                if attempt >= self.request_retries:
                    raise OllamaError(f"Cannot reach Ollama at {self.base_url}: {exc}") from exc
            time.sleep(min(1.0, 0.2 * (2 ** attempt)))

        try:
            return json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise OllamaError("Ollama returned invalid JSON") from exc

    def version(self) -> str:
        data = self._request("GET", "/api/version")
        return str(data.get("version", "unknown"))

    def list_models(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/tags")
        models = data.get("models", [])
        return models if isinstance(models, list) else []

    def show_model(self, model: str) -> dict[str, Any]:
        return self._request("POST", "/api/show", {"model": model})

    def running_models(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/api/ps")
        models = data.get("models", [])
        return models if isinstance(models, list) else []

    def unload_model(self, model: str) -> None:
        self._request("POST", "/api/generate", {"model": model, "keep_alive": 0})

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        format_schema: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
        think: bool | None = None,
        timeout_seconds: int | None = None,
    ) -> OllamaResponse:
        encoded_messages: list[dict[str, Any]] = []
        for message in messages:
            encoded = dict(message)
            if "images" in encoded:
                encoded["images"] = [
                    base64.b64encode(value).decode("ascii") if isinstance(value, bytes) else value
                    for value in encoded["images"]
                ]
            encoded_messages.append(encoded)
        payload: dict[str, Any] = {
            "model": model,
            "messages": encoded_messages,
            "stream": False,
        }
        if format_schema is not None:
            payload["format"] = format_schema
        if options:
            payload["options"] = options
        if think is not None:
            payload["think"] = think

        raw = self._request("POST", "/api/chat", payload, timeout_seconds)
        message = raw.get("message", {})
        content = message.get("content", "") if isinstance(message, dict) else ""
        return OllamaResponse(content=str(content), raw=raw)

    def embed(
        self,
        model: str,
        inputs: list[str],
        *,
        timeout_seconds: int | None = None,
    ) -> OllamaResponse:
        raw = self._request(
            "POST",
            "/api/embed",
            {"model": model, "input": inputs},
            timeout_seconds,
        )
        return OllamaResponse(content="", raw=raw)
