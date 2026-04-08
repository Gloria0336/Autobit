from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from config import get_openrouter_settings


class OpenRouterConfigurationError(RuntimeError):
    pass


class OpenRouterRequestError(RuntimeError):
    pass


@dataclass
class OpenRouterResult:
    model: str
    content: str


class OpenRouterClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        default_http_referer: str | None = None,
        default_title: str | None = None,
        timeout_sec: int = 60,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.default_http_referer = default_http_referer
        self.default_title = default_title
        self.timeout_sec = timeout_sec

    def analyze(
        self,
        *,
        prompt: str,
        model: str,
        api_key: str | None = None,
        referer: str | None = None,
        title: str | None = None,
    ) -> OpenRouterResult:
        settings = get_openrouter_settings()
        api_key = (api_key or self.api_key or settings["api_key"]).strip()
        base_url = self._normalize_base_url(self.base_url or settings["base_url"])
        default_http_referer = (self.default_http_referer or settings["http_referer"]).strip()
        default_title = (self.default_title or settings["x_title"]).strip()

        if not api_key:
            raise OpenRouterConfigurationError("OPENROUTER_API_KEY is not configured")
        if not model:
            raise OpenRouterConfigurationError("OPENROUTER_MODEL is not configured")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        resolved_referer = referer or default_http_referer
        resolved_title = title or default_title
        if resolved_referer:
            headers["HTTP-Referer"] = resolved_referer
        if resolved_title:
            headers["X-Title"] = resolved_title

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a quantitative trading analyst. Follow the user's requested language and return rigorous reasoning.",
                },
                {"role": "user", "content": prompt},
            ],
        }
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code == 401:
                raise OpenRouterRequestError(
                    "OpenRouter authentication failed (401 Unauthorized). "
                    "Please verify OPENROUTER_API_KEY is valid, has no extra spaces/quotes, and restart is no longer required."
                ) from exc
            raise OpenRouterRequestError(f"OpenRouter request failed: {exc}") from exc
        except requests.RequestException as exc:
            raise OpenRouterRequestError(f"OpenRouter request failed: {exc}") from exc

        try:
            body = response.json()
        except ValueError as exc:
            raise OpenRouterRequestError("OpenRouter returned a non-JSON response") from exc

        choices = body.get("choices") or []
        if not choices:
            raise OpenRouterRequestError("OpenRouter response did not include any choices")
        message = choices[0].get("message") or {}
        content = self._coerce_content(message.get("content"))
        if not content.strip():
            raise OpenRouterRequestError("OpenRouter response content was empty")
        model_name = body.get("model") or model
        return OpenRouterResult(model=model_name, content=content)

    def test_connection(
        self,
        *,
        model: str,
        api_key: str | None = None,
        referer: str | None = None,
        title: str | None = None,
    ) -> OpenRouterResult:
        return self.analyze(
            prompt="Reply with a single short sentence: OpenRouter connection OK.",
            model=model,
            api_key=api_key,
            referer=referer,
            title=title,
        )

    def _normalize_base_url(self, base_url: str | None) -> str:
        raw = (base_url or "").strip().rstrip("/")
        if not raw:
            return "https://openrouter.ai/api/v1"

        parsed = urlparse(raw)
        host = parsed.netloc.lower()
        path = parsed.path.rstrip("/")
        if host == "openrouter.ai":
            if path in ("", "/api", "/api/v1"):
                return "https://openrouter.ai/api/v1"
            if path.startswith("/api/") or path.count("/") >= 2:
                return "https://openrouter.ai/api/v1"
        return raw

    def _coerce_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    chunks.append(str(item.get("text", "")))
                elif isinstance(item, str):
                    chunks.append(item)
            return "\n".join(chunk for chunk in chunks if chunk)
        return str(content or "")
