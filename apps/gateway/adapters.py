"""Provider adapters. Each adapter translates a common chat request to one provider's API
and classifies failures so the router can decide whether to retry, fall back, or stop."""
from dataclasses import dataclass

import requests


class GatewayError(Exception):
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    INVALID_CREDENTIALS = "invalid_credentials"
    UNSUPPORTED = "unsupported"
    BAD_REQUEST = "bad_request"
    INVALID_OUTPUT = "invalid_output"

    RETRYABLE = {RATE_LIMITED, TRANSIENT, TIMEOUT}

    def __init__(self, kind, message="", retry_after=None):
        super().__init__(f"{kind}: {message}"[:500])
        self.kind = kind
        self.message = message[:500]
        self.retry_after = retry_after

    @property
    def retryable(self):
        return self.kind in self.RETRYABLE


@dataclass
class ChatResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    tokens_reported: bool = False


def classify_http_error(status, body):
    text = (body or "")[:2000].lower()
    if status in (401, 403):
        return GatewayError.INVALID_CREDENTIALS
    if status == 402 or "insufficient_quota" in text or "exceeded your current quota" in text or "quota exceeded" in text:
        return GatewayError.QUOTA_EXHAUSTED
    if status == 429:
        return GatewayError.RATE_LIMITED
    if status == 404:
        return GatewayError.UNSUPPORTED
    if status in (408, 504):
        return GatewayError.TIMEOUT
    if status >= 500:
        return GatewayError.TRANSIENT
    if status == 400 and any(w in text for w in ("not supported", "unsupported", "does not support")):
        return GatewayError.UNSUPPORTED
    return GatewayError.BAD_REQUEST


class BaseAdapter:
    def __init__(self, provider):
        self.provider = provider

    def _post(self, url, headers, payload):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.provider.timeout_seconds)
        except requests.Timeout as exc:
            raise GatewayError(GatewayError.TIMEOUT, str(exc))
        except requests.RequestException as exc:
            raise GatewayError(GatewayError.TRANSIENT, f"connection failed: {exc.__class__.__name__}")
        if resp.status_code >= 400:
            retry_after = resp.headers.get("retry-after")
            raise GatewayError(classify_http_error(resp.status_code, resp.text), f"HTTP {resp.status_code}: {resp.text[:300]}",
                               retry_after=float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else None)
        try:
            return resp.json()
        except ValueError:
            raise GatewayError(GatewayError.INVALID_OUTPUT, "provider returned non-JSON response")

    def _get(self, url, headers):
        try:
            resp = requests.get(url, headers=headers, timeout=min(self.provider.timeout_seconds, 15))
        except requests.RequestException as exc:
            raise GatewayError(GatewayError.TRANSIENT, f"connection failed: {exc.__class__.__name__}")
        if resp.status_code >= 400:
            raise GatewayError(classify_http_error(resp.status_code, resp.text), f"HTTP {resp.status_code}")
        return resp.json()

    def chat(self, model_identifier, messages, *, json_mode=False, max_tokens=1024):
        raise NotImplementedError

    def health_check(self):
        """Return a short description on success; raise GatewayError on failure."""
        raise NotImplementedError


class OllamaAdapter(BaseAdapter):
    def chat(self, model_identifier, messages, *, json_mode=False, max_tokens=1024):
        payload = {"model": model_identifier, "messages": messages, "stream": False, "options": {"num_predict": max_tokens}}
        if json_mode:
            payload["format"] = "json"
        data = self._post(f"{self.provider.base_url.rstrip('/')}/api/chat", {}, payload)
        if "message" not in data:
            raise GatewayError(GatewayError.INVALID_OUTPUT, "missing message in Ollama response")
        pin, pout = data.get("prompt_eval_count"), data.get("eval_count")
        return ChatResult(data["message"].get("content", ""), pin or 0, pout or 0, pin is not None and pout is not None)

    def health_check(self):
        data = self._get(f"{self.provider.base_url.rstrip('/')}/api/tags", {})
        names = [m.get("name") for m in data.get("models", [])]
        return f"{len(names)} local models: {', '.join(names[:8])}"

    def list_models(self):
        data = self._get(f"{self.provider.base_url.rstrip('/')}/api/tags", {})
        return [m.get("name") for m in data.get("models", [])]


class OpenAICompatibleAdapter(BaseAdapter):
    """OpenAI, xAI, NVIDIA, Gemini's OpenAI-compatible endpoint and other compatible APIs."""

    def _headers(self):
        key = self.provider.get_api_key()
        if not key:
            raise GatewayError(GatewayError.INVALID_CREDENTIALS, "no API credential configured")
        return {"Authorization": f"Bearer {key}"}

    def chat(self, model_identifier, messages, *, json_mode=False, max_tokens=1024):
        payload = {"model": model_identifier, "messages": messages, "max_tokens": max_tokens}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(f"{self.provider.base_url.rstrip('/')}/chat/completions", self._headers(), payload)
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise GatewayError(GatewayError.INVALID_OUTPUT, "unexpected completion format")
        usage = data.get("usage") or {}
        return ChatResult(text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), bool(usage))

    def health_check(self):
        data = self._get(f"{self.provider.base_url.rstrip('/')}/models", self._headers())
        return f"{len(data.get('data', []))} models listed"


class AnthropicAdapter(BaseAdapter):
    VERSION = "2023-06-01"

    def _headers(self):
        key = self.provider.get_api_key()
        if not key:
            raise GatewayError(GatewayError.INVALID_CREDENTIALS, "no API credential configured")
        return {"x-api-key": key, "anthropic-version": self.VERSION}

    def chat(self, model_identifier, messages, *, json_mode=False, max_tokens=1024):
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        convo = [m for m in messages if m["role"] != "system"]
        payload = {"model": model_identifier, "max_tokens": max_tokens, "messages": convo}
        if system:
            payload["system"] = system
        data = self._post(f"{self.provider.base_url.rstrip('/')}/v1/messages", self._headers(), payload)
        try:
            text = "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")
        except (KeyError, TypeError):
            raise GatewayError(GatewayError.INVALID_OUTPUT, "unexpected messages format")
        usage = data.get("usage") or {}
        return ChatResult(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0), bool(usage))

    def health_check(self):
        data = self._get(f"{self.provider.base_url.rstrip('/')}/v1/models", self._headers())
        return f"{len(data.get('data', []))} models listed"


ADAPTERS = {
    "ollama": OllamaAdapter,
    "openai": OpenAICompatibleAdapter,
    "gemini": OpenAICompatibleAdapter,
    "xai": OpenAICompatibleAdapter,
    "nvidia": OpenAICompatibleAdapter,
    "openai_compatible": OpenAICompatibleAdapter,
    "anthropic": AnthropicAdapter,
}


def register_adapter(kind, cls):
    """Extension point: add a provider without touching the router or engine."""
    ADAPTERS[kind] = cls


def get_adapter(provider):
    cls = ADAPTERS.get(provider.kind)
    if cls is None:
        raise GatewayError(GatewayError.UNSUPPORTED, f"no adapter for provider kind {provider.kind}")
    return cls(provider)
