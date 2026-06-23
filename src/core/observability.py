"""Structured observability without leaking credentials or session values."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import time
from typing import Any, Callable
from uuid import uuid4


_SECRET_MARKERS = (
    "password",
    "passwd",
    "token",
    "secret",
    "cookie",
    "authorization",
    "webhook",
)


def _redact(value: Any, key: str = "") -> Any:
    if any(marker in key.lower() for marker in _SECRET_MARKERS):
        return "***"
    if isinstance(value, dict):
        return {str(k): _redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


@dataclass
class StructuredLogger:
    component: str
    sink: Callable[[str], None] = print
    enabled: bool = True
    trace_id: str = field(default_factory=lambda: uuid4().hex)

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        payload = {
            "timestamp": time.time(),
            "trace_id": self.trace_id,
            "component": self.component,
            "event": event,
            **_redact(fields),
        }
        if self.enabled:
            self.sink(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return payload
