"""Redaction of secrets in logs, audit details and displayed payloads."""
import re

SENSITIVE_KEYS = re.compile(r"(api[_-]?key|secret|password|token|authorization|credential)", re.I)
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),          # OpenAI / Anthropic style keys
    re.compile(r"xai-[A-Za-z0-9]{16,}"),
    re.compile(r"nvapi-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),         # Google keys
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
]


def redact_text(text):
    if not isinstance(text, str):
        return text
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def redact(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if SENSITIVE_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return redact_text(value)
