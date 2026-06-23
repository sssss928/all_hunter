"""Small state-machine primitive shared by platform adapters."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Iterable, Mapping
from uuid import uuid4


@dataclass(frozen=True)
class StateTransition:
    trace_id: str
    previous: str
    current: str
    reason: str
    timestamp: float


class WorkflowStateMachine:
    """Track explicit state transitions and reject invalid edges when requested."""

    def __init__(
        self,
        initial: str,
        *,
        allowed: Mapping[str, Iterable[str]] | None = None,
        strict: bool = False,
        history_limit: int = 100,
    ) -> None:
        self.trace_id = uuid4().hex
        self.current = str(initial)
        self.allowed = {
            str(source): frozenset(str(target) for target in targets)
            for source, targets in (allowed or {}).items()
        }
        self.strict = strict
        self.history: deque[StateTransition] = deque(maxlen=history_limit)

    def transition(self, target: str, reason: str = "") -> StateTransition:
        target = str(target)
        previous = self.current
        permitted = self.allowed.get(previous)
        if (
            self.strict
            and target != previous
            and permitted is not None
            and target not in permitted
        ):
            raise ValueError(f"invalid transition: {previous} -> {target}")
        self.current = target
        event = StateTransition(
            trace_id=self.trace_id,
            previous=previous,
            current=target,
            reason=str(reason or "state assignment"),
            timestamp=time.time(),
        )
        if target != previous or reason:
            self.history.append(event)
        return event

    def reset(self, initial: str) -> None:
        self.trace_id = uuid4().hex
        self.current = str(initial)
        self.history.clear()
