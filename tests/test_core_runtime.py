from __future__ import annotations

import json

import pytest

from core.observability import StructuredLogger
from core.retry import RetryPolicy
from core.state_machine import WorkflowStateMachine
from ocr.pipeline import choose_consensus, normalize_ocr_text
from platforms.registry import PLATFORM_MODULES, resolve_platform
from verification.handoff_resume import HandoffCoordinator


def test_retry_policy_is_bounded() -> None:
    policy = RetryPolicy(
        max_attempts=3,
        initial_delay=0.5,
        multiplier=2,
        max_delay=1.5,
        jitter=0,
    )
    assert [policy.delay_for(index) for index in (1, 2, 3, 4)] == [
        0.5,
        1.0,
        1.5,
        1.5,
    ]
    assert policy.allows(3) is True
    assert policy.allows(4) is False


def test_state_machine_records_and_validates_transitions() -> None:
    machine = WorkflowStateMachine(
        "init",
        allowed={"init": {"login"}, "login": {"done"}},
        strict=True,
    )
    event = machine.transition("login", "page ready")
    assert event.previous == "init"
    assert event.current == "login"
    assert machine.history[-1].reason == "page ready"
    with pytest.raises(ValueError):
        machine.transition("payment")


def test_structured_logger_redacts_sensitive_values() -> None:
    lines: list[str] = []
    logger = StructuredLogger("test", sink=lines.append)
    payload = logger.emit(
        "login",
        account="person@example.com",
        password="secret",
        nested={"access_token": "abc"},
    )
    decoded = json.loads(lines[0])
    assert payload["password"] == "***"
    assert decoded["nested"]["access_token"] == "***"
    assert decoded["account"] == "person@example.com"


def test_handoff_preserves_enter_wait_resume_state() -> None:
    handoff = HandoffCoordinator(notice_interval=10)
    entered = handoff.observe(
        active=True,
        kind="turnstile",
        url="https://example.test/",
        now=10,
    )
    waiting = handoff.observe(
        active=True,
        kind="turnstile",
        url="https://example.test/",
        now=15,
    )
    resumed = handoff.observe(
        active=False,
        kind="turnstile",
        url="https://example.test/",
        now=18,
    )
    assert entered.action == "entered"
    assert waiting.action == "waiting"
    assert resumed.action == "resumed"
    assert resumed.elapsed_seconds == 8


def test_ocr_fixture_normalization_and_consensus() -> None:
    assert normalize_ocr_text("S D M E D Z") == "SDMEDZ"
    answer, confidence = choose_consensus(
        ["S D M E D Z", "SDMEDZ", "SDM EDZ", "ZRZ"]
    )
    assert answer == "SDMEDZ"
    assert confidence == 0.75


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://kktix.com/events/example", "kktix"),
        ("https://tixcraft.com/activity/detail/1", "tixcraft"),
        ("https://ticketmaster.sg/activity/detail/1", "tixcraft"),
        ("https://www.famiticket.com.tw/", "famiticket"),
        ("https://ticket.ibon.com.tw/", "ibon"),
        ("https://kham.com.tw/application/UTK01/UTK0101_.aspx", "kham"),
        ("https://ticketplus.com.tw/activity/1", "ticketplus"),
        ("https://www.cityline.com/Events.html", "cityline"),
        ("https://premier.hkticketing.com/", "hkticketing"),
        ("https://world.nol.com/en/ticket", "nolworld"),
        ("https://tickets.funone.io/", "funone"),
        ("https://go.fansi.me/", "fansigo"),
        ("https://www.facebook.com/login.php?next=x", "facebook"),
    ],
)
def test_platform_registry_routes_supported_hosts(
    url: str,
    expected: str,
) -> None:
    assert resolve_platform(url) == expected


def test_platform_registry_does_not_route_query_string_domains() -> None:
    assert (
        resolve_platform(
            "https://example.com/?next=https://world.nol.com/en/ticket"
        )
        is None
    )
    assert len(PLATFORM_MODULES) == 12


def test_platform_registry_caches_hot_loop_resolution() -> None:
    resolve_platform.cache_clear()
    url = "https://world.nol.com/en/ticket/places/1/products/2"
    for _ in range(1000):
        assert resolve_platform(url) == "nolworld"
    cache = resolve_platform.cache_info()
    assert cache.misses == 1
    assert cache.hits == 999
