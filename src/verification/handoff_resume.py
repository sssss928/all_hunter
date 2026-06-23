"""Preserve workflow state while a protected verification requires a user."""

from __future__ import annotations

from dataclasses import dataclass
import time


@dataclass(frozen=True)
class HandoffEvent:
    action: str
    kind: str
    active: bool
    elapsed_seconds: float
    url: str


class HandoffCoordinator:
    """Detect enter/wait/resume transitions without interacting with a challenge."""

    def __init__(self, notice_interval: float = 15.0) -> None:
        self.notice_interval = max(1.0, notice_interval)
        self.active = False
        self.kind = ""
        self.url = ""
        self.started_at = 0.0
        self.last_notice_at = 0.0

    def observe(
        self,
        *,
        active: bool,
        kind: str,
        url: str,
        now: float | None = None,
    ) -> HandoffEvent:
        now = time.monotonic() if now is None else now
        if active and not self.active:
            self.active = True
            self.kind = kind
            self.url = url
            self.started_at = now
            self.last_notice_at = now
            action = "entered"
        elif active:
            action = "waiting"
            if now - self.last_notice_at >= self.notice_interval:
                action = "reminder"
                self.last_notice_at = now
        elif self.active:
            action = "resumed"
            self.active = False
        else:
            action = "clear"
        elapsed = max(0.0, now - self.started_at) if self.started_at else 0.0
        return HandoffEvent(
            action=action,
            kind=self.kind or kind,
            active=self.active,
            elapsed_seconds=elapsed,
            url=self.url or url,
        )
