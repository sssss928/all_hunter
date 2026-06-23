from __future__ import annotations

from pathlib import Path
import asyncio
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from platforms import nolworld  # noqa: E402
import settings  # noqa: E402


def test_parse_concert_codes_from_supported_urls() -> None:
    assert nolworld.parse_concert_codes_from_url(
        "https://world.nol.com/ticket/places/240001/products/24012345"
    ) == {"placeCode": "240001", "goodsCode": "24012345"}
    assert nolworld.parse_concert_codes_from_url(
        "https://world.nol.com/ticket/genre/CONCERT/products/24012345?placeCode=240001"
    ) == {"placeCode": "240001", "goodsCode": "24012345"}
    assert nolworld.parse_concert_codes_from_url(
        "https://world.nol.com/ticket/genre/CONCERT/products/24012345"
    ) == {"placeCode": None, "goodsCode": "24012345"}
    assert nolworld.parse_concert_codes_from_url("https://world.nol.com/") is None


def test_find_and_merge_concert_codes() -> None:
    assert (
        nolworld.find_place_code_in_text(
            r'0:{"goodsCode":"24012345","placeCode":"240001"}'
        )
        == "240001"
    )
    assert nolworld.merge_concert_codes(
        {"goodsCode": "24012345", "placeCode": None},
        {"goodsCode": "fallback", "placeCode": "240001"},
    ) == {"goodsCode": "24012345", "placeCode": "240001"}


def test_build_nolworld_login_url_preserves_event_target() -> None:
    result = nolworld.build_nolworld_login_url(
        "https://world.nol.com/en/ticket/places/240001/products/24012345?foo=bar"
    )
    assert result.startswith("https://world.nol.com/en/login?")
    assert (
        "returnUrl=https%3A%2F%2Fworld.nol.com%2Fen%2Fticket"
        "%2Fplaces%2F240001%2Fproducts"
        "%2F24012345%3Ffoo%3Dbar"
    ) in result


def test_parse_remain_seat_xml() -> None:
    payload = """
    <Table>
      <SeatGrade>1</SeatGrade>
      <SeatGradeName>VIP &amp; Soundcheck</SeatGradeName>
      <RemainCnt>12</RemainCnt>
      <SalesPrice>198000</SalesPrice>
    </Table>
    <Table>
      <SeatGrade>2</SeatGrade>
      <SeatGradeName>R Seat</SeatGradeName>
      <RemainCnt>0</RemainCnt>
      <SalesPrice>165,000</SalesPrice>
    </Table>
    """
    assert nolworld.parse_remain_seat_xml(payload) == [
        {
            "grade": "1",
            "name": "VIP & Soundcheck",
            "remain": 12,
            "price": 198000,
        },
        {
            "grade": "2",
            "name": "R Seat",
            "remain": 0,
            "price": 165000,
        },
    ]


def test_ticket_pool_fetches_sequences_and_keeps_order(monkeypatch) -> None:
    def fake_fetch(goods_code, place_code, play_seq, **_kwargs):
        assert goods_code == "G1"
        assert place_code == "P1"
        return [{"grade": play_seq, "name": "VIP", "remain": 1, "price": 100}]

    monkeypatch.setattr(nolworld, "fetch_remain_seats_for_seq", fake_fetch)
    monkeypatch.setattr(
        nolworld,
        "fetch_seat_map_and_blocks",
        lambda *_args, **_kwargs: {
            "seatMapImage": "https://example.invalid/map.gif",
            "blockCodes": ["101"],
        },
    )

    result = nolworld.fetch_ticket_pool_data("G1", "P1", max_workers=3)

    assert [row["play_seq"] for row in result["play_sequences"]] == list(
        nolworld.PLAY_SEQUENCES
    )
    assert result["block_codes"] == ["101"]


def test_default_settings_include_nolworld() -> None:
    config = settings.get_default_config()
    assert config["nolworld"]["check_interval"] >= 3
    assert config["nolworld"]["ticket_pool_enabled"] is True
    assert config["nolworld"]["security_handoff"] is True
    assert config["nolworld"]["auto_notice_and_buy"] is True
    assert config["nolworld"]["session_timeout_minutes"] == 10
    assert config["accounts"]["nolworld_account"] == ""
    assert config["accounts"]["nolworld_password"] == ""


def test_nolworld_interval_does_not_fall_back_to_general_reload() -> None:
    config = settings.get_default_config()
    config["advanced"]["auto_reload_page_interval"] = 99
    config["nolworld"]["check_interval"] = 4
    assert nolworld._config(config)["check_interval"] == 4


def test_nolworld_is_wired_into_ui_and_release() -> None:
    settings_html = (SRC / "www" / "settings.html").read_text(encoding="utf-8")
    settings_js = (SRC / "www" / "settings.js").read_text(encoding="utf-8")
    spec = (ROOT / "build_scripts" / "nodriver_tixcraft.spec").read_text(
        encoding="utf-8"
    )

    assert "https://world.nol.com" in settings_html
    assert 'id="nolworld_account"' in settings_html
    assert 'id="nolworld_password"' in settings_html
    assert 'id="nol_auto_notice_and_buy"' in settings_html
    assert 'id="auto-reload-page-interval-row"' in settings_html
    assert "platform === 'nolworld'" in settings_js
    assert "key: 'nolworld'" in settings_js
    assert "'platforms.nolworld'" in spec


def test_nolworld_booking_scripts_cover_recovery_paths() -> None:
    assert "schedule_waiting_next" in nolworld.ONESTOP_SCHEDULE_JS
    assert "schedule_waiting_time" in nolworld.ONESTOP_SCHEDULE_JS
    assert "schedule_submitted" in nolworld.ONESTOP_SCHEDULE_JS
    assert "monthNames" in nolworld.ONESTOP_SCHEDULE_JS
    assert "seat_conflict_recovered" in nolworld.BOOKING_STEP_JS
    assert "seat_next_retried" in nolworld.BOOKING_STEP_JS
    assert "slider_captcha" in nolworld.BOOKING_STEP_JS
    assert ".captchSliderInner" in nolworld.BOOKING_STEP_JS
    assert "protected_challenge" in nolworld.BOOKING_STEP_JS
    assert "requestSubmit" in nolworld.LOGIN_JS


def test_nolworld_has_no_runtime_activation_gate() -> None:
    source = (SRC / "platforms" / "nolworld.py").read_text(encoding="utf-8")
    forbidden = (
        "validateLicense",
        "licenseCode",
        "nol-bot-license",
        "deviceId",
        "redeem-code",
    )
    assert not any(marker in source for marker in forbidden)


def test_nolworld_protected_verification_uses_handoff_not_bypass() -> None:
    source = (SRC / "platforms" / "nolworld.py").read_text(encoding="utf-8")
    assert "SECURITY HANDOFF" in source
    assert "CAPTCHA_SUBMIT_JS" not in source
    assert "turnstile-callback" not in source
    assert "cf-turnstile-response" in source  # readiness detection only
    assert "slider_captcha" in source  # slider detection and handoff only


class FakeTab:
    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.reload_count = 0
        self.get_urls = []
        self.evaluated = []

    async def evaluate(self, javascript):
        self.evaluated.append(javascript)
        if javascript == nolworld.PAGE_SNAPSHOT_JS:
            return self.snapshots.pop(0)
        if javascript == nolworld.NOTICE_DISMISS_JS:
            return {"clicked": True, "found": True, "text": "Close"}
        if javascript == nolworld.BUY_TARGET_JS:
            return {"found": True, "x": 100, "y": 200, "text": "Buy Now"}
        if javascript == nolworld.BUY_CLICK_JS:
            return {"clicked": True, "text": "Buy Now"}
        if "new MutationObserver" in javascript:
            return True
        raise AssertionError("unexpected JavaScript evaluation")

    async def reload(self):
        self.reload_count += 1

    async def get(self, url):
        self.get_urls.append(url)


class FakeLoginTab(FakeTab):
    async def evaluate(self, javascript):
        if javascript == nolworld.PAGE_SNAPSHOT_JS:
            return self.snapshots.pop(0)
        if "credentials.account" in javascript and "requestSubmit" in javascript:
            return {"action": "login_submitted", "filled": True}
        raise AssertionError("unexpected JavaScript evaluation")


def test_nolworld_login_fills_and_submits_credentials() -> None:
    nolworld.reset_nolworld_state()
    config = settings.get_default_config()
    config["accounts"]["nolworld_account"] = "person@example.com"
    config["accounts"]["nolworld_password"] = "secret"
    tab = FakeLoginTab(
        [
            {
                "state": "login",
                "buy": {"found": False, "disabled": True, "text": ""},
                "queue": {},
                "login": {
                    "emailFound": True,
                    "passwordFound": True,
                    "challengeReady": True,
                },
            }
        ]
    )

    result = asyncio.run(
        nolworld.nodriver_nolworld_main(
            tab,
            "https://world.nol.com/en/auth-web/login?returnUrl=%2Fen%2Fticket",
            config,
        )
    )

    assert result["phase"] == "login"
    assert nolworld._state.login_submitted is True


def test_first_available_check_clicks_buy_now_by_default() -> None:
    nolworld.reset_nolworld_state()
    config = settings.get_default_config()
    config["nolworld"]["ticket_pool_enabled"] = False
    tab = FakeTab(
        [
            {
                "state": "detail",
                "buy": {"found": True, "disabled": False, "text": "Buy Now"},
                "queue": {},
            }
        ]
    )

    result = asyncio.run(
        nolworld.nodriver_nolworld_main(
            tab,
            "https://world.nol.com/ticket/places/240001/products/24012345",
            config,
        )
    )

    assert result["clicked"] is True
    assert result["phase"] == "buying"
    assert tab.reload_count == 0


def test_notice_is_closed_before_buy_is_attempted() -> None:
    nolworld.reset_nolworld_state()
    config = settings.get_default_config()
    config["nolworld"]["ticket_pool_enabled"] = False
    tab = FakeTab(
        [
            {
                "state": "detail",
                "notice": {
                    "found": True,
                    "buttonFound": True,
                    "text": "Notice Close",
                },
                "buy": {"found": True, "disabled": False, "text": "Buy Now"},
                "queue": {},
            },
            {
                "state": "detail",
                "notice": {"found": False, "buttonFound": False, "text": ""},
                "buy": {"found": True, "disabled": False, "text": "Buy Now"},
                "queue": {},
            },
        ]
    )
    url = "https://world.nol.com/en/ticket/places/240001/products/24012345"

    first = asyncio.run(nolworld.nodriver_nolworld_main(tab, url, config))
    assert first["notice"]["clicked"] is True
    assert nolworld.BUY_TARGET_JS not in tab.evaluated

    nolworld._state.next_action_at = 0
    second = asyncio.run(nolworld.nodriver_nolworld_main(tab, url, config))
    assert second["clicked"] is True
    assert tab.evaluated.index(nolworld.NOTICE_DISMISS_JS) < tab.evaluated.index(
        nolworld.BUY_TARGET_JS
    )


def test_buy_click_is_retried_when_detail_page_does_not_advance() -> None:
    nolworld.reset_nolworld_state()
    config = settings.get_default_config()
    config["nolworld"]["ticket_pool_enabled"] = False
    snapshot = {
        "state": "detail",
        "notice": {"found": False, "buttonFound": False, "text": ""},
        "buy": {"found": True, "disabled": False, "text": "Buy Now"},
        "queue": {},
    }
    tab = FakeTab([snapshot, snapshot, snapshot])
    url = "https://world.nol.com/en/ticket/places/240001/products/24012345"

    first = asyncio.run(nolworld.nodriver_nolworld_main(tab, url, config))
    assert first["clicked"] is True

    nolworld._state.next_action_at = 0
    second = asyncio.run(nolworld.nodriver_nolworld_main(tab, url, config))
    assert second["phase"] == "checking"

    nolworld._state.next_action_at = 0
    third = asyncio.run(nolworld.nodriver_nolworld_main(tab, url, config))
    assert third["clicked"] is True
    assert nolworld._state.buy_attempts == 2


def test_queue_page_is_polled_without_reload() -> None:
    nolworld.reset_nolworld_state()
    config = settings.get_default_config()
    config["nolworld"]["ticket_pool_enabled"] = False
    tab = FakeTab(
        [
            {
                "state": "queue",
                "buy": {"found": False, "disabled": True, "text": ""},
                "queue": {
                    "waitingOrder": 120,
                    "waitingPeople": 350,
                    "bookingRate": "12/s",
                    "progressPercent": 66.5,
                },
            }
        ]
    )

    result = asyncio.run(
        nolworld.nodriver_nolworld_main(
            tab,
            "https://world.nol.com/waiting",
            config,
        )
    )

    assert result["phase"] == "queuing"
    assert tab.reload_count == 0


def test_expired_booking_session_restarts_from_event_page(monkeypatch) -> None:
    nolworld.reset_nolworld_state()
    config = settings.get_default_config()
    config["homepage"] = (
        "https://world.nol.com/en/ticket/places/240001/products/24012345"
    )
    config["nolworld"]["ticket_pool_enabled"] = False
    config["nolworld"]["session_timeout_minutes"] = 5
    tab = FakeTab(
        [
            {
                "state": "booking",
                "buy": {"found": False, "disabled": True, "text": ""},
                "queue": {},
            }
        ]
    )

    nolworld._state.detail_url = config["homepage"]
    nolworld._state.booking_started_at = 1.0
    monkeypatch.setattr(nolworld.time, "monotonic", lambda: 302.0)

    result = asyncio.run(
        nolworld.nodriver_nolworld_main(
            tab,
            "https://gpoticket.globalinterpark.com/Global/Play/Book/BookMain.asp",
            config,
        )
    )

    assert result["session_restarted"] is True
    assert tab.get_urls == [config["homepage"]]
