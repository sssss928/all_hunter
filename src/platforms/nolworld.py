#!/usr/bin/env python3
# encoding=utf-8
"""NOL World / Global Interpark automation for the MyHunter nodriver runtime.

This module ports the operational parts of the NOL Ticket Monitor extension to
Python: event-code discovery, ticket-pool queries, detail-page monitoring,
pre-sale and queue state handling, date/time selection, protected verification
handoff, legacy Global Interpark seat selection, and the newer NOL onestop flow.

Chrome-extension concerns such as side-panel rendering, chrome.storage,
licensing/admin screens, and browser notifications intentionally map to
MyHunter's existing settings, logging, sound, Discord, and Telegram services.
No credential from the source extension is embedded here.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import html
import json
import random
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import xml.etree.ElementTree as ET

import requests
from zendriver import cdp

from core.state_machine import WorkflowStateMachine
from dom import DOMController
import util
from nodriver_common import (
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
)


__all__ = [
    "PLAY_SEQUENCES",
    "find_place_code_in_text",
    "build_nolworld_login_url",
    "merge_concert_codes",
    "parse_concert_codes_from_url",
    "parse_remain_seat_xml",
    "resolve_concert_codes",
    "fetch_remain_seats_for_seq",
    "fetch_seat_map_and_blocks",
    "fetch_ticket_pool_data",
    "async_fetch_ticket_pool_data",
    "reset_nolworld_state",
    "nodriver_nolworld_main",
]


NOL_DOMAINS = (
    "world.nol.com",
    "nol.com",
    "interpark.com",
    "globalinterpark.com",
)
GLOBAL_INTERPARK_ROOT = "https://gpoticket.globalinterpark.com"
REMAIN_SEAT_PATH = "/Global/Play/Book/Lib/BookInfoXml.asp"
SEAT_MAP_PATH = "/Global/Play/Book/BookSeatView.asp"
BIZ_CODE = "10965"
PLAY_SEQUENCES = tuple(f"{index:03d}" for index in range(1, 11))
DEFAULT_HTTP_TIMEOUT = 8.0
DEFAULT_BOOKING_TIMEOUT_MINUTES = 10.0
SOLD_OUT_TEXT = (
    "sold out",
    "not available",
    "ended",
    "coming soon",
    "준비중",
    "매진",
)
SOLD_OUT_TEXT += ("매진", "판매 종료", "售罄", "已結束")


@dataclass
class NolWorldState:
    """Small, throttled state machine shared by the single MyHunter browser."""

    machine: WorkflowStateMachine = field(
        default_factory=lambda: WorkflowStateMachine("checking")
    )
    last_url: str = ""
    next_action_at: float = 0.0
    last_reload_at: float = 0.0
    availability_baselined: bool = False
    buy_was_available: bool = False
    buy_attempts: int = 0
    notice_attempts: int = 0
    success_notified: bool = False
    last_queue_signature: tuple[Any, ...] | None = None
    last_action: str = ""
    last_action_at: float = 0.0
    last_error_at: float = 0.0
    last_pool_at: float = 0.0
    booking_started_at: float = 0.0
    detail_url: str = ""
    cycle_count: int = 0
    login_submitted: bool = False
    verification_handoff: bool = False
    verification_started_at: float = 0.0
    pool_task: asyncio.Task | None = None
    pool_result: dict[str, Any] | None = None
    seen_urls: set[str] = field(default_factory=set)

    @property
    def phase(self) -> str:
        return self.machine.current

    @phase.setter
    def phase(self, value: str) -> None:
        self.machine.transition(value)

    def reset_for_url(self, url: str) -> None:
        self.last_url = url
        self.machine.reset(_phase_from_url(url))
        self.next_action_at = 0.0
        self.availability_baselined = False
        self.buy_was_available = False
        self.buy_attempts = 0
        self.notice_attempts = 0
        self.last_queue_signature = None
        self.last_action = ""
        if self.phase == "checking":
            self.detail_url = url
            self.booking_started_at = 0.0
            self.login_submitted = False
            self.verification_handoff = False
            self.verification_started_at = 0.0


_state = NolWorldState()
_main_lock: asyncio.Lock | None = None


def _get_main_lock() -> asyncio.Lock:
    global _main_lock
    if _main_lock is None:
        _main_lock = asyncio.Lock()
    return _main_lock


def reset_nolworld_state() -> None:
    """Reset runtime state; primarily useful for tests and browser restarts."""

    global _state, _main_lock
    if _state.pool_task and not _state.pool_task.done():
        _state.pool_task.cancel()
    _state = NolWorldState()
    _main_lock = None


def parse_concert_codes_from_url(url: str | None) -> dict[str, str | None] | None:
    """Parse ``goodsCode`` and ``placeCode`` from supported NOL event URLs."""

    if not url:
        return None
    try:
        parsed = urlparse(url)
    except (TypeError, ValueError):
        return None

    places_match = re.search(r"/places/(\d+)/products/(\d+)", parsed.path)
    if places_match:
        return {
            "placeCode": places_match.group(1),
            "goodsCode": places_match.group(2),
        }

    goods_match = re.search(r"/products/(\d+)", parsed.path)
    if not goods_match:
        return None

    query = parse_qs(parsed.query)
    place_code = (query.get("placeCode") or [None])[0]
    if place_code and not str(place_code).isdigit():
        place_code = None
    return {"goodsCode": goods_match.group(1), "placeCode": place_code}


def build_nolworld_login_url(return_url: str) -> str:
    """Build the official NOL email-login URL for a target event page."""

    parsed = urlparse(return_url or "https://world.nol.com/en/ticket")
    language = "en"
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and re.fullmatch(r"[a-z]{2}(?:-[A-Z]{2})?", path_parts[0]):
        language = path_parts[0]
    target = urlunparse(parsed) if parsed.netloc else (
        f"https://world.nol.com{parsed.path or '/en/ticket'}"
    )
    query = urlencode({"returnUrl": target})
    return f"https://world.nol.com/{language}/login?{query}"


def find_place_code_in_text(text: str | None) -> str | None:
    """Find a serialized ``placeCode`` in RSC/JSON/script text."""

    if not text:
        return None
    match = re.search(r"""["']placeCode["']\s*:\s*["'](\d+)["']""", text)
    return match.group(1) if match else None


def merge_concert_codes(
    primary: dict[str, Any] | None,
    fallback: dict[str, Any] | None,
) -> dict[str, str | None] | None:
    """Merge explicit codes with URL-derived fallback codes."""

    if not primary and not fallback:
        return None
    primary = primary or {}
    fallback = fallback or {}
    return {
        "goodsCode": primary.get("goodsCode") or fallback.get("goodsCode"),
        "placeCode": primary.get("placeCode") or fallback.get("placeCode"),
    }


def _rsc_url(page_url: str) -> str:
    parsed = urlparse(page_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["_rsc"] = ["il0lm"]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def fetch_place_code_from_page(
    page_url: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    session: requests.Session | None = None,
) -> str | None:
    """Resolve a missing place code through the event page's RSC response."""

    if not page_url:
        return None
    parsed = urlparse(page_url)
    place_code = (parse_qs(parsed.query).get("placeCode") or [None])[0]
    if place_code and str(place_code).isdigit():
        return str(place_code)

    client = session or requests
    try:
        response = client.get(
            _rsc_url(page_url),
            headers={"Accept": "text/x-component", "RSC": "1"},
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException:
        return None
    return find_place_code_in_text(response.text)


def resolve_concert_codes(
    goods_code: str | None = None,
    place_code: str | None = None,
    page_url: str = "",
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> dict[str, str] | None:
    """Resolve a complete NOL code pair from explicit data, URL, and RSC."""

    merged = merge_concert_codes(
        {"goodsCode": goods_code, "placeCode": place_code},
        parse_concert_codes_from_url(page_url),
    )
    if not merged or not merged.get("goodsCode"):
        return None
    if not merged.get("placeCode"):
        merged["placeCode"] = fetch_place_code_from_page(page_url, timeout=timeout)
    if not merged.get("placeCode"):
        return None
    return {
        "goodsCode": str(merged["goodsCode"]),
        "placeCode": str(merged["placeCode"]),
    }


def parse_remain_seat_xml(xml_text: str) -> list[dict[str, Any]]:
    """Parse Global Interpark's repeated ``Table`` XML payload safely."""

    if not xml_text or "<Table" not in xml_text:
        return []
    cleaned = re.sub(r"<\?xml[^>]*\?>", "", xml_text).strip()
    try:
        root = ET.fromstring(f"<Root>{cleaned}</Root>")
    except ET.ParseError:
        return []

    rows: list[dict[str, Any]] = []
    for table in root.iter("Table"):
        def text_of(tag: str) -> str:
            element = table.find(tag)
            return html.unescape(element.text or "").strip() if element is not None else ""

        name = text_of("SeatGradeName")
        grade = text_of("SeatGrade")
        remain_text = text_of("RemainCnt")
        price_text = text_of("SalesPrice")
        if not any((name, grade, remain_text, price_text)):
            continue
        try:
            remain = int(re.sub(r"[^\d-]", "", remain_text) or "0")
        except ValueError:
            remain = 0
        try:
            price = int(re.sub(r"[^\d-]", "", price_text) or "0")
        except ValueError:
            price = 0
        rows.append(
            {
                "grade": grade,
                "name": name,
                "remain": max(0, remain),
                "price": max(0, price),
            }
        )
    return rows


def fetch_remain_seats_for_seq(
    goods_code: str,
    place_code: str,
    play_seq: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    session: requests.Session | None = None,
) -> list[dict[str, Any]] | None:
    """Fetch ticket-grade availability for one performance sequence."""

    params = {
        "Flag": "RemainSeat",
        "GoodsCode": goods_code,
        "PlaceCode": place_code,
        "LanguageType": "G2001",
        "MemBizCode": BIZ_CODE,
        "PlaySeq": play_seq,
        "Tiki": "N",
        "TmgsOrNot": "D2003",
        "SessionId": "x",
    }
    client = session or requests
    response = client.get(
        f"{GLOBAL_INTERPARK_ROOT}{REMAIN_SEAT_PATH}",
        params=params,
        timeout=timeout,
    )
    response.raise_for_status()
    rows = parse_remain_seat_xml(response.text)
    return rows or None


def fetch_seat_map_and_blocks(
    goods_code: str,
    place_code: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> dict[str, Any]:
    """Fetch the legacy seat-map image and discover block identifiers."""

    params = {
        "GoodsCode": goods_code,
        "PlaceCode": place_code,
        "PlaySeq": "001",
        "SessionId": "x",
        "LanguageType": "G2001",
    }
    seat_map_image = None
    block_codes: list[str] = []
    try:
        response = requests.get(
            f"{GLOBAL_INTERPARK_ROOT}{SEAT_MAP_PATH}",
            params=params,
            timeout=timeout,
        )
        response.raise_for_status()
        body = response.text
        seen: set[str] = set()
        for call in re.findall(r"GetBlockSeatList\([^)]*'(\d+)'\)", body):
            if call not in seen:
                seen.add(call)
                block_codes.append(call)
        image_match = re.search(r"""src=["'](https?://[^"']+\.gif)["']""", body)
        if image_match:
            seat_map_image = html.unescape(image_match.group(1))
    except requests.RequestException:
        pass
    return {"seatMapImage": seat_map_image, "blockCodes": block_codes}


def fetch_ticket_pool_data(
    goods_code: str,
    place_code: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
    max_workers: int = 4,
) -> dict[str, Any]:
    """Fetch all ticket-pool sequences concurrently, preserving sequence order."""

    sequence_rows: dict[str, dict[str, Any]] = {}

    def fetch_one(play_seq: str) -> tuple[str, list[dict[str, Any]] | None]:
        return play_seq, fetch_remain_seats_for_seq(
            goods_code,
            place_code,
            play_seq,
            timeout=timeout,
        )

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, 10))) as executor:
        futures = {executor.submit(fetch_one, seq): seq for seq in PLAY_SEQUENCES}
        for future in as_completed(futures):
            play_seq = futures[future]
            try:
                _, seats = future.result()
                if seats:
                    sequence_rows[play_seq] = {"play_seq": play_seq, "seats": seats}
            except Exception as exc:
                sequence_rows[play_seq] = {
                    "play_seq": play_seq,
                    "error": str(exc),
                }

    map_data = fetch_seat_map_and_blocks(
        goods_code,
        place_code,
        timeout=timeout,
    )
    return {
        "goods_code": goods_code,
        "place_code": place_code,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "play_sequences": [
            sequence_rows[seq] for seq in PLAY_SEQUENCES if seq in sequence_rows
        ],
        "seat_map_image": map_data["seatMapImage"],
        "block_codes": map_data["blockCodes"],
    }


async def async_fetch_ticket_pool_data(
    goods_code: str,
    place_code: str,
    *,
    timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> dict[str, Any]:
    """Non-blocking wrapper for use inside MyHunter's browser event loop."""

    return await asyncio.to_thread(
        fetch_ticket_pool_data,
        goods_code,
        place_code,
        timeout=timeout,
    )


def _phase_from_url(url: str) -> str:
    lowered = (url or "").lower()
    if "/auth-web/login" in lowered or re.search(
        r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?login(?:[/?]|$)",
        lowered,
    ):
        return "login"
    if "/waiting" in lowered:
        return "queuing"
    if "/gates" in lowered or "logingate" in lowered:
        return "pre_sale"
    if any(part in lowered for part in ("/book", "/onestop", "/seat", "/schedule")):
        return "booking"
    if "/products/" in lowered or "/ticket/" in lowered:
        return "checking"
    return "unknown"


def _is_nol_url(url: str) -> bool:
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(hostname == domain or hostname.endswith("." + domain) for domain in NOL_DOMAINS)


def _config(config_dict: dict[str, Any]) -> dict[str, Any]:
    source = config_dict.get("nolworld", {})
    interval = source.get("check_interval", 5)
    try:
        interval = max(3.0, min(float(interval), 120.0))
    except (TypeError, ValueError):
        interval = 5.0
    try:
        block_delay_ms = max(200, min(int(source.get("block_delay_ms", 600)), 5000))
    except (TypeError, ValueError):
        block_delay_ms = 600
    try:
        pool_minutes = max(
            1,
            min(int(source.get("ticket_pool_refresh_minutes", 15)), 1440),
        )
    except (TypeError, ValueError):
        pool_minutes = 15
    try:
        session_timeout_minutes = max(
            5.0,
            min(
                float(
                    source.get(
                        "session_timeout_minutes",
                        DEFAULT_BOOKING_TIMEOUT_MINUTES,
                    )
                ),
                10.0,
            ),
        )
    except (TypeError, ValueError):
        session_timeout_minutes = DEFAULT_BOOKING_TIMEOUT_MINUTES
    return {
        "check_interval": interval,
        "pre_sale_mode": bool(source.get("pre_sale_mode", False)),
        "lock_only_mode": bool(source.get("lock_only_mode", False)),
        # Older settings do not have this key, so they automatically receive
        # the reliable first-run Notice/Buy behavior.
        "auto_notice_and_buy": bool(source.get("auto_notice_and_buy", True)),
        "block_delay_ms": block_delay_ms,
        "custom_blocks": _split_values(source.get("custom_blocks", "")),
        "ticket_pool_enabled": bool(source.get("ticket_pool_enabled", True)),
        "ticket_pool_refresh_minutes": pool_minutes,
        "security_handoff": bool(source.get("security_handoff", True)),
        "session_timeout_seconds": session_timeout_minutes * 60,
    }


def _split_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if not value:
        return []
    text = str(value)
    parsed = util.parse_keyword_string_to_array(text)
    if parsed:
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [
        item.strip().strip("\"'")
        for item in re.split(r"[,;\n]+", text)
        if item.strip().strip("\"'")
    ]


def _preferred_dates(config_dict: dict[str, Any]) -> list[str]:
    values = _split_values(
        config_dict.get("date_auto_select", {}).get("date_keyword", "")
    )
    for target in config_dict.get("nolworld", {}).get("schedule_targets", []) or []:
        if not isinstance(target, dict):
            continue
        value = str(target.get("date", "")).strip()
        if value:
            values.append(value)
    normalized: list[str] = []
    for value in values:
        digits = re.sub(r"\D", "", value)
        if len(digits) == 8:
            normalized.append(digits)
        normalized.append(value)
    return list(dict.fromkeys(normalized))


def _preferred_schedule_targets(config_dict: dict[str, Any]) -> list[dict[str, str]]:
    targets: list[dict[str, str]] = []
    for item in config_dict.get("nolworld", {}).get("schedule_targets", []) or []:
        if not isinstance(item, dict):
            continue
        date_value = re.sub(r"\D", "", str(item.get("date", "")))
        if len(date_value) != 8:
            continue
        time_value = str(item.get("time", "")).strip()
        targets.append(
            {
                "date": date_value,
                "time": time_value,
                "label": str(item.get("label", "")).strip(),
            }
        )
    return targets


def _preferred_tiers(config_dict: dict[str, Any]) -> list[str]:
    return _split_values(
        config_dict.get("area_auto_select", {}).get("area_keyword", "")
    )


def _preferred_seat_types(config_dict: dict[str, Any]) -> list[str]:
    values = config_dict.get("nolworld", {}).get("seat_types", []) or []
    normalized: list[str] = []
    for value in values:
        text = str(value).strip().upper()
        if "STAND" in text:
            normalized.append("STANDING")
        elif "SEAT" in text or "RESERVED" in text:
            normalized.append("SEATED")
    return list(dict.fromkeys(normalized))


def _preferred_seat_zones(config_dict: dict[str, Any]) -> list[str]:
    return _split_values(config_dict.get("nolworld", {}).get("seat_zones", ""))


def _selection_mode(config_dict: dict[str, Any], section: str) -> str:
    mode = str(config_dict.get(section, {}).get("mode", "from top to bottom"))
    return mode if mode in {
        "from top to bottom",
        "from bottom to top",
        "center",
        "random",
    } else "from top to bottom"


def _choose_index(length: int, mode: str) -> int:
    if length <= 1:
        return 0
    if mode == "from bottom to top":
        return length - 1
    if mode == "center":
        return length // 2
    if mode == "random":
        return random.randrange(length)
    return 0


PAGE_SNAPSHOT_JS = r"""
(() => {
  const href = location.href;
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' ||
        Number.parseFloat(style.opacity || '1') === 0) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const isBuyText = text => {
    const value = (text || '').trim().toLowerCase();
    return value.includes('buy now') || value === 'buy' ||
      value.includes('购票') || value.includes('購票') ||
      value.includes('購入') || value.includes('구매') ||
      /buy\s*now|立即购买|立即購買|今すぐ購入/i.test(value);
  };
  const findBuy = () => {
    const selectors = [
      '[class*="pos_fixed"][class*="bottom_0"] button',
      '[class*="pos_fixed"][class*="bottom-0"] button',
      '[class*="grid-area_purchase-button"] button',
      'button.nds-e-rectangle-button--variant_filled_primary'
    ];
    for (const selector of selectors) {
      for (const el of document.querySelectorAll(selector)) {
        if (visible(el) && isBuyText(el.textContent)) return el;
      }
    }
    for (const el of document.querySelectorAll('button,a[role="button"],a[class*="button"]')) {
      if (visible(el) && isBuyText(el.textContent)) return el;
    }
    return null;
  };
  let state = 'error';
  if (href.includes('/auth-web/login') || /\/[a-z]{2}(?:-[A-Z]{2})?\/login(?:[/?]|$)/.test(href) ||
      (document.querySelector('input[name="email"]') &&
       document.querySelector('input[name="password"]'))) {
    state = 'login';
  } else if (href.includes('/waiting') ||
      document.querySelector('[class*="Wait_pageWrap"],[class*="Wait_content"]')) {
    state = 'queue';
  } else if (href.includes('/gates') ||
      document.querySelector('button[class*="_primary_"]')) {
    state = 'preSale';
  } else if (href.includes('/onestop') || href.includes('/Book') ||
      href.includes('/book') || document.getElementById('divBookMain') ||
      document.getElementById('formBook') ||
      document.querySelector('[class*="Schedule_pageWrap"],[class*="Seat_pageWrap"]')) {
    state = 'booking';
  } else if (href.includes('nol.com') &&
      (href.includes('/products/') || href.includes('/ticket/'))) {
    state = 'detail';
  }
  const buy = findBuy();
  const noticeDialog = [...document.querySelectorAll(
    '.nds-e-modal-bottom-sheet__container,[role="dialog"],[role="alertdialog"]'
  )].find(dialog => visible(dialog) && (
    dialog.querySelector('.nds-e-modal-bottom-sheet__actionButton,' +
      '.nds-e-modal-bottom-sheet__closeButton') ||
    /\bnotice\b|公告|공지/i.test(dialog.textContent || '')
  ));
  const noticeButton = noticeDialog?.querySelector(
    '.nds-e-modal-bottom-sheet__actionButton,' +
    '.nds-e-modal-bottom-sheet__closeButton,' +
    'button[aria-label="Close"],button[aria-label="닫기"]'
  );
  const queue = {
    waitingOrder: null,
    waitingPeople: null,
    bookingRate: null,
    progressPercent: null
  };
  const order = document.querySelector('[class*="StatusBox_mainText"] strong');
  if (order) queue.waitingOrder = Number.parseInt(order.textContent.replace(/\D/g, ''), 10) || null;
  for (const row of document.querySelectorAll('[class*="StatusBox_row"]')) {
    const left = row.querySelector('[class*="StatusBox_columnLeft"]');
    const right = row.querySelector('[class*="StatusBox_columnRight"]');
    if (!left || !right) continue;
    const label = left.textContent.trim().toLowerCase();
    const value = right.textContent.trim();
    if (label.includes('waiting') || label.includes('대기')) {
      queue.waitingPeople = Number.parseInt(value.replace(/\D/g, ''), 10) || null;
    }
    if (label.includes('rate') || label.includes('접속률')) queue.bookingRate = value;
  }
  const progress = document.querySelector('[class*="StatusBox_barActive"]');
  if (progress) {
    const match = (progress.style.width || '').match(/([\d.]+)%/);
    if (match) queue.progressPercent = Number.parseFloat(match[1]);
  }
  const preSaleButton = document.querySelector('button[class*="_primary_"]');
  let placeCode = null;
  for (const script of document.querySelectorAll('script')) {
    const match = (script.textContent || '').match(/["']placeCode["']\s*:\s*["'](\d+)["']/);
    if (match) { placeCode = match[1]; break; }
  }
  const codesMatch = href.match(/places\/(\d+)\/products\/(\d+)/);
  const goodsMatch = href.match(/products\/(\d+)/);
  return {
    state,
    url: href,
    title: (document.querySelector('[class*="grid-area_basic-info"] h1') ||
            document.querySelector('h1'))?.textContent?.trim() || document.title || '',
    buy: buy ? {
      found: true,
      disabled: Boolean(buy.disabled || buy.classList.contains('disabled')),
      text: (buy.textContent || '').trim()
    } : {found: false, disabled: true, text: ''},
    notice: {
      found: Boolean(noticeDialog),
      buttonFound: Boolean(noticeButton && visible(noticeButton)),
      text: (noticeDialog?.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 160)
    },
    preSale: preSaleButton ? {
      found: true,
      disabled: Boolean(preSaleButton.disabled),
      text: (preSaleButton.textContent || '').trim()
    } : {found: false, disabled: true, text: ''},
    queue,
    login: {
      emailFound: Boolean(document.querySelector('input[name="email"],input[type="email"]')),
      passwordFound: Boolean(document.querySelector('input[name="password"],input[type="password"]')),
      challengeReady: Boolean(document.querySelector(
        'input[name="cf-turnstile-response"]'
      )?.value)
    },
    goodsCode: codesMatch ? codesMatch[2] : (goodsMatch ? goodsMatch[1] : null),
    placeCode: codesMatch ? codesMatch[1] : placeCode
  };
})()
"""


LOGIN_JS = r"""
(() => {
  const credentials = __CREDENTIALS__;
  window.__myhunterNolLoginState = window.__myhunterNolLoginState || {};
  const email = document.querySelector('input[name="email"],input[type="email"]');
  const password = document.querySelector(
    'input[name="password"],input[type="password"][autocomplete="current-password"]'
  );
  if (!email || !password) return {action: 'login_form_missing'};

  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number.parseFloat(style.opacity || '1') !== 0 &&
      rect.width > 0 && rect.height > 0;
  };
  const setValue = (input, value) => {
    const prototype = input instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
    if (setter) setter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('input', {
      bubbles: true,
      data: value,
      inputType: 'insertText'
    }));
    input.dispatchEvent(new Event('change', {bubbles: true}));
  };
  const fillGradually = (input, value, fieldName) => {
    const current = String(input.value || '');
    if (current === value) return true;
    const base = value.startsWith(current) ? current : '';
    const next = value.slice(0, Math.min(value.length, base.length + 4));
    input.focus();
    setValue(input, next);
    window.__myhunterNolLoginState[fieldName] = next.length;
    return next === value;
  };

  if (!fillGradually(email, credentials.account, 'email')) {
    return {action: 'login_email_filling', filled: false, delay: 0.25};
  }
  if (!fillGradually(password, credentials.password, 'password')) {
    return {action: 'login_password_filling', filled: false, delay: 0.25};
  }

  const challenge = document.querySelector('input[name="cf-turnstile-response"]');
  const challengeReady = !challenge || Boolean(challenge.value);
  if (!challengeReady) {
    return {action: 'login_waiting_turnstile', filled: true};
  }

  const form = email.closest('form') || password.closest('form');
  if (!form) return {action: 'login_form_missing', filled: true};
  const candidates = [
    ...form.querySelectorAll('button,input[type="submit"],[role="button"]'),
    ...document.querySelectorAll('button,input[type="submit"],[role="button"]')
  ].filter(item => visible(item) && !item.disabled && !item.getAttribute('aria-disabled'));
  const submit = candidates.find(item => {
    const text = (
      item.innerText || item.value || item.getAttribute('aria-label') || ''
    ).trim();
    return item.type === 'submit' || /log\s*in|login|sign\s*in|continue|next/i.test(text);
  });
  if (submit) {
    submit.scrollIntoView({block: 'center', behavior: 'instant'});
    submit.click();
  } else if (typeof form.requestSubmit === 'function') form.requestSubmit();
  else {
    password.focus();
    password.dispatchEvent(new KeyboardEvent('keydown', {
      key: 'Enter',
      code: 'Enter',
      bubbles: true
    }));
  }
  return {action: 'login_submitted', filled: true};
})()
"""


DIALOG_HOOK_JS = r"""
(() => {
  if (window.__myhunterNolDialogHooked) {
    return {installed: true, lastDialog: window.__myhunterNolLastDialog || null};
  }
  const record = (type, message) => {
    window.__myhunterNolLastDialog = {
      type,
      message: String(message || ''),
      timestamp: Date.now()
    };
  };
  window.__myhunterNolOriginalAlert = window.alert?.bind(window);
  window.__myhunterNolOriginalConfirm = window.confirm?.bind(window);
  window.alert = message => record('alert', message);
  window.confirm = message => {
    record('confirm', message);
    return true;
  };
  window.__myhunterNolDialogHooked = true;
  return {installed: true, lastDialog: null};
})()
"""


NOTICE_DISMISS_JS = r"""
(() => {
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number.parseFloat(style.opacity || '1') !== 0 &&
      rect.width > 0 && rect.height > 0;
  };
  const dialogs = [...document.querySelectorAll(
    '.nds-e-modal-bottom-sheet__container,[role="dialog"],[role="alertdialog"]'
  )].filter(visible);
  const dialog = dialogs.find(item =>
    item.querySelector('.nds-e-modal-bottom-sheet__actionButton,' +
      '.nds-e-modal-bottom-sheet__closeButton') ||
    /\bnotice\b|公告|공지/i.test(item.textContent || '')
  );
  if (!dialog) return {clicked: false, found: false, error: 'not-found'};
  const candidates = [
    ...dialog.querySelectorAll(
      '.nds-e-modal-bottom-sheet__actionButton,' +
      '.nds-e-modal-bottom-sheet__closeButton,' +
      'button[aria-label="Close"],button[aria-label="닫기"],button'
    )
  ].filter(visible);
  const button = candidates.find(item =>
    /close|confirm|ok|關閉|关闭|확인|닫기/i.test(
      `${item.textContent || ''} ${item.getAttribute('aria-label') || ''}`
    )
  ) || candidates[0];
  if (!button) return {clicked: false, found: true, error: 'button-not-found'};
  const text = (button.textContent || button.getAttribute('aria-label') || '').trim();
  button.scrollIntoView({block: 'center', behavior: 'instant'});
  button.click();
  return {clicked: true, found: true, text};
})()
"""


BUY_TARGET_JS = r"""
(() => {
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number.parseFloat(style.opacity || '1') !== 0 &&
      rect.width > 0 && rect.height > 0;
  };
  const isBuy = text =>
    /buy\s*now|\bbuy\b|购票|購票|購入|구매|立即购买|立即購買|今すぐ購入/i.test(text || '');
  let button = null;
  const selectors = [
    '[class*="pos_fixed"][class*="bottom_0"] button',
    '[class*="pos_fixed"][class*="bottom-0"] button',
    '[class*="grid-area_purchase-button"] button',
    'button.nds-e-rectangle-button--variant_filled_primary',
    'button,a[role="button"],a[class*="button"]'
  ];
  for (const selector of selectors) {
    button = [...document.querySelectorAll(selector)].find(
      el => visible(el) && isBuy(el.textContent)
    );
    if (button) break;
  }
  if (!button) return {found: false, error: 'not-found'};
  const text = (button.textContent || '').trim();
  if (button.disabled || button.classList.contains('disabled')) {
    return {found: false, error: 'disabled', text};
  }
  if (/sold out|매진|ended/i.test(text)) {
    return {found: false, error: 'sold-out', text};
  }
  if (!window.__myhunterNolOpenPatched) {
    const originalOpen = window.open?.bind(window);
    window.open = function(url, target, features) {
      if (url && /interpark\.com|globalinterpark\.com|nol\.com|\/Book|\/book|\/waiting|\/gates/.test(url)) {
        location.href = url;
        return window;
      }
      return originalOpen ? originalOpen(url, target, features) : null;
    };
    window.__myhunterNolOpenPatched = true;
  }
  button.scrollIntoView({block: 'center', behavior: 'instant'});
  const rect = button.getBoundingClientRect();
  return {
    found: true,
    text,
    x: Math.round(rect.left + rect.width / 2),
    y: Math.round(rect.top + rect.height / 2)
  };
})()
"""


BUY_CLICK_JS = r"""
(() => {
  const visible = el => {
    if (!el || !el.isConnected) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      rect.width > 0 && rect.height > 0;
  };
  const isBuy = text => /buy\s*now|\bbuy\b|购票|購票|購入|구매|立即购买|立即購買|今すぐ購入/i.test(text || '');
  let button = null;
  for (const el of document.querySelectorAll('button,a[role="button"],a[class*="button"]')) {
    if (visible(el) && isBuy(el.textContent)) { button = el; break; }
  }
  if (!button) return {clicked: false, error: 'not-found'};
  const text = (button.textContent || '').trim();
  if (button.disabled || button.classList.contains('disabled')) {
    return {clicked: false, error: 'disabled', text};
  }
  if (/sold out|매진|ended/i.test(text)) {
    return {clicked: false, error: 'sold-out', text};
  }
  if (!window.__myhunterNolOpenPatched) {
    const originalOpen = window.open?.bind(window);
    window.open = function(url, target, features) {
      if (url && /interpark\.com|globalinterpark\.com|nol\.com|\/Book|\/book|\/waiting|\/gates/.test(url)) {
        location.href = url;
        return window;
      }
      return originalOpen ? originalOpen(url, target, features) : null;
    };
    window.__myhunterNolOpenPatched = true;
  }
  button.scrollIntoView({block: 'center', behavior: 'instant'});
  const rect = button.getBoundingClientRect();
  const eventInit = {
    bubbles: true, cancelable: true, view: window,
    clientX: rect.left + rect.width / 2,
    clientY: rect.top + rect.height / 2
  };
  button.dispatchEvent(new PointerEvent('pointerdown', eventInit));
  button.dispatchEvent(new MouseEvent('mousedown', eventInit));
  button.dispatchEvent(new PointerEvent('pointerup', eventInit));
  button.dispatchEvent(new MouseEvent('mouseup', eventInit));
  button.dispatchEvent(new MouseEvent('click', eventInit));
  return {clicked: true, text};
})()
"""


PRESALE_CLICK_JS = r"""
(() => {
  const button = document.querySelector('button[class*="_primary_"]');
  if (!button) return {clicked: false, error: 'not-found'};
  if (button.disabled) return {clicked: false, error: 'disabled', text: button.textContent.trim()};
  button.click();
  return {clicked: true, text: button.textContent.trim()};
})()
"""


ONESTOP_SCHEDULE_JS = r"""
(() => {
  const config = __CONFIG__;
  const visible = el => {
    if (!el) return false;
    const style = getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      rect.width > 0 && rect.height > 0;
  };
  const selected = el =>
    el?.getAttribute('aria-selected') === 'true' ||
    el?.getAttribute('aria-pressed') === 'true' ||
    el?.getAttribute('aria-current') === 'date' ||
    el?.dataset?.selected === 'true' ||
    /\bselected\b|\bactive\b|\bchecked\b/i.test(
      `${el?.className || ''} ${el?.parentElement?.className || ''}`
    );
  const normalizeTime = raw => {
    const text = String(raw || '').replace(/\s+/g, ' ').trim();
    let match = text.match(/\b(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b/i);
    if (match) {
      let hour = Number.parseInt(match[1], 10);
      const minute = Number.parseInt(match[2] || '0', 10);
      const suffix = match[3].toUpperCase();
      if (suffix === 'PM' && hour < 12) hour += 12;
      if (suffix === 'AM' && hour === 12) hour = 0;
      return String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0');
    }
    match = text.match(/\b([01]?\d|2[0-3]):([0-5]\d)\b/);
    if (match) return String(Number.parseInt(match[1], 10)).padStart(2, '0') + ':' + match[2];
    return text.toLowerCase();
  };
  const timeMatches = (button, preferredTimes) => {
    if (!preferredTimes.length) return true;
    const text = [
      button.textContent,
      button.getAttribute('aria-label'),
      button.getAttribute('title'),
      button.dataset?.time,
      button.value
    ].filter(Boolean).join(' ');
    const normalized = normalizeTime(text);
    return preferredTimes.some(value => {
      const preferred = normalizeTime(value);
      return normalized === preferred || text.toLowerCase().includes(String(value).toLowerCase());
    });
  };
  const monthNames = {
    jan: 1, january: 1, feb: 2, february: 2, mar: 3, march: 3,
    apr: 4, april: 4, may: 5, jun: 6, june: 6, jul: 7, july: 7,
    aug: 8, august: 8, sep: 9, sept: 9, september: 9,
    oct: 10, october: 10, nov: 11, november: 11, dec: 12, december: 12
  };
  const normalizeDate = (raw, fallbackYear, fallbackMonth) => {
    const text = String(raw || '').replace(/\s+/g, ' ').trim();
    let match = text.match(/(20\d{2})\D{0,3}(\d{1,2})\D{0,3}(\d{1,2})/);
    if (match) {
      return match[1] + match[2].padStart(2, '0') +
        match[3].padStart(2, '0');
    }
    match = text.match(
      /\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b\D*(\d{1,2})(?:\D+(20\d{2}))?/i
    );
    if (match) {
      const namedMonth = monthNames[match[1].toLowerCase()];
      return String(match[3] || fallbackYear) +
        String(namedMonth).padStart(2, '0') + match[2].padStart(2, '0');
    }
    const dayMatch = text.match(/\b([0-3]?\d)\b/);
    if (fallbackYear && fallbackMonth && dayMatch) {
      return fallbackYear + fallbackMonth + dayMatch[1].padStart(2, '0');
    }
    return '';
  };
  const monthNode = document.querySelector('[class*="EntCalendar_month"]');
  const monthText = monthNode ? monthNode.textContent.trim() : '';
  let monthMatch = monthText.match(/(20\d{2})\D{0,3}(\d{1,2})/);
  let year = monthMatch ? monthMatch[1] : '';
  let month = monthMatch ? monthMatch[2].padStart(2, '0') : '';
  if (!monthMatch) {
    monthMatch = monthText.match(
      /\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b\D*(20\d{2})/i
    );
    if (monthMatch) {
      year = monthMatch[2];
      month = String(
        monthNames[monthMatch[1].toLowerCase()]
      ).padStart(2, '0');
    }
  }
  const dateButtons = [...document.querySelectorAll(
    '[class*="EntCalendar_dateItem"] button[class*="EntCalendar_dateButton"],' +
    'button[class*="EntCalendar_dateButton"]'
  )].filter(button => !button.disabled && visible(button));
  const dates = dateButtons.map(button => {
    const raw = [
      button.dataset?.date,
      button.dataset?.value,
      button.value,
      button.getAttribute('aria-label'),
      button.getAttribute('title'),
      button.textContent
    ].filter(Boolean).join(' ');
    return {
      button,
      value: normalizeDate(raw, year, month),
      selected: selected(button),
      label: (button.textContent || '').replace(/\s+/g, ' ').trim()
    };
  }).filter(item => item.value.length === 8);
  let candidates = dates;
  if (config.dates.length) {
    const preferred = dates.filter(item => config.dates.some(value =>
      item.value === String(value).replace(/\D/g, '') ||
      item.label.toLowerCase().includes(String(value).toLowerCase())
    ));
    if (preferred.length) candidates = preferred;
    else if (!config.dateFallback) return {action: 'no_matching_date', available: dates.map(item => item.value)};
  }
  if (!candidates.length) return {action: 'no_date', available: []};
  let index = config.dateIndex;
  if (index >= candidates.length) index = candidates.length - 1;
  const target = candidates[Math.max(0, index)];
  if (!target.selected) {
    target.button.scrollIntoView({block: 'center', behavior: 'instant'});
    target.button.click();
    return {action: 'date_selected', date: target.value};
  }

  const timeButtons = [...document.querySelectorAll(
    'button[class*="TimeBlock_timeButton"],' +
    '[role="button"][class*="TimeBlock_timeButton"]'
  )]
    .filter(button => !button.disabled && visible(button));
  const preferredTimes = (config.scheduleTargets || [])
    .filter(item => String(item.date || '').replace(/\D/g, '') === target.value)
    .map(item => item.time || item.label || '')
    .filter(Boolean);
  const timeCandidates = timeButtons.filter(button => timeMatches(button, preferredTimes));
  const selectedTime = timeCandidates.find(selected) || timeButtons.find(selected);
  if (!timeButtons.length) {
    return {action: 'schedule_waiting_time', date: target.value, times: 0};
  }
  if (!timeCandidates.length && preferredTimes.length) {
    return {action: 'no_matching_time', date: target.value, available: timeButtons.map(button => (button.textContent || '').trim())};
  }
  if (timeButtons.length && !selectedTime) {
    const targetTime = timeCandidates[0] || timeButtons[0];
    targetTime.scrollIntoView({block: 'center', behavior: 'instant'});
    targetTime.click();
    return {
      action: 'time_selected',
      date: target.value,
      time: (targetTime.textContent || '').trim()
    };
  }

  const footerButtons = [...document.querySelectorAll(
    '[class*="ScheduleContent_footerButton"] button:not([disabled]),' +
    'button[class*="EntButton_primary"]:not([disabled])'
  )].filter(visible);
  const next = footerButtons.find(button =>
    /next|continue|다음|下一步|繼續|继续/i.test((button.textContent || '').trim())
  ) || footerButtons[footerButtons.length - 1];
  if (next) {
    next.scrollIntoView({block: 'center', behavior: 'instant'});
    next.click();
    return {
      action: 'schedule_submitted',
      date: target.value,
      time: (selectedTime?.textContent || '').trim(),
      times: timeButtons.length
    };
  }
  return {
    action: 'schedule_waiting_next',
    date: target.value,
    time: (selectedTime?.textContent || '').trim(),
    times: timeButtons.length
  };
})()
"""


BOOKING_STEP_JS = r"""
(() => {
  const config = __CONFIG__;
  const documents = [];
  const visit = (doc, win) => {
    if (!doc || documents.some(item => item.doc === doc)) return;
    documents.push({doc, win});
    for (const frame of doc.querySelectorAll('iframe,frame')) {
      try { if (frame.contentDocument) visit(frame.contentDocument, frame.contentWindow); } catch (_) {}
    }
  };
  visit(document, window);
  const visible = el => {
    if (!el) return false;
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      rect.width > 0 && rect.height > 0;
  };
  const matches = (text, keywords) => {
    if (!keywords.length) return true;
    const haystack = (text || '').toLowerCase();
    return keywords.some(group =>
      String(group).toLowerCase().split(/\s+/).filter(Boolean)
        .every(word => haystack.includes(word))
    );
  };
  const click = el => {
    if (!el) return false;
    el.scrollIntoView?.({block: 'center', behavior: 'instant'});
    el.click();
    return true;
  };
  const selected = el =>
    el?.getAttribute('aria-selected') === 'true' ||
    el?.getAttribute('aria-pressed') === 'true' ||
    el?.dataset?.selected === 'true' ||
    /\bselected\b|\bactive\b|\bchecked\b/i.test(
      `${el?.className || ''} ${el?.parentElement?.className || ''}`
    );
  const textOf = el => [
    el?.textContent,
    el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('title'),
    el?.getAttribute?.('data-name'),
    el?.getAttribute?.('data-block'),
    el?.getAttribute?.('data-grade')
  ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  const normalizeSeatType = text => {
    const value = String(text || '').toLowerCase();
    if (/standing|floor|stand/i.test(value)) return 'STANDING';
    if (/seated|reserved|seat/i.test(value)) return 'SEATED';
    return '';
  };
  const parseRgb = value => {
    const match = String(value || '').match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/i);
    return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
  };
  const isPurpleAction = el => {
    if (!el || el.disabled || !visible(el)) return false;
    const style = el.ownerDocument.defaultView.getComputedStyle(el);
    const rgb = parseRgb(style.backgroundColor) || parseRgb(style.borderColor);
    if (!rgb) return false;
    const [red, green, blue] = rgb;
    return blue >= 120 && red >= 80 && blue > green + 25 && red > green + 10;
  };
  const clickSeatSelectionCompleted = doc => {
    const buttons = [...doc.querySelectorAll('button,[role="button"],a')].filter(visible);
    const completed = buttons.find(button =>
      /seat\s*selection\s*completed|selection\s*completed|complete|done|next|선택\s*완료/i
        .test(textOf(button))
    );
    if (completed) return click(completed);
    const primary = buttons.find(isPurpleAction);
    return primary ? click(primary) : false;
  };
  const isSeatConflict = text =>
    /already\s+(?:been\s+)?selected|seat\s+has\s+already|seat\s+was\s+already|already\s+taken|-1|座位.*(?:已|被)|已被選擇|선택/i
      .test(text || '');
  const clearSelectedSeats = (doc, win) => {
    for (const seat of doc.querySelectorAll(
      'span.SeatY[name="Seats"][value="Y"],circle.js-seat.selected'
    )) {
      try { seat.click(); } catch (_) {}
    }
    try {
      if (typeof win.fnSeatUpdate === 'function') win.fnSeatUpdate();
      else if (typeof win.fnCancel === 'function') win.fnCancel();
    } catch (_) {}
  };
  if (/\/onestop\/(payment|order)/i.test(location.href)) return {action: 'payment'};

  const nativeDialog = window.__myhunterNolLastDialog;
  if (nativeDialog?.message) {
    window.__myhunterNolLastDialog = null;
    if (isSeatConflict(nativeDialog.message)) {
      for (const {doc, win} of documents) clearSelectedSeats(doc, win);
      return {action: 'seat_conflict_recovered', message: nativeDialog.message};
    }
    return {action: 'dialog_dismissed', message: nativeDialog.message};
  }

  for (const {doc, win} of documents) {
    const dialogs = [...doc.querySelectorAll(
      '[role="dialog"],[role="alertdialog"],.modal,.popup,.layerPopup'
    )].filter(visible);
    for (const dialog of dialogs) {
      const message = (dialog.textContent || '').replace(/\s+/g, ' ').trim();
      if (!message) continue;
      const closeButton = [...dialog.querySelectorAll('button,a')].find(element =>
        visible(element) &&
        /close|confirm|ok|확인|닫기|關閉|確定/i.test(
          (element.textContent || '') + ' ' + (element.getAttribute('aria-label') || '')
        )
      );
      if (closeButton) click(closeButton);
      if (isSeatConflict(message)) {
        clearSelectedSeats(doc, win);
        return {action: 'seat_conflict_recovered', message: message.slice(0, 160)};
      }
      if (closeButton) {
        return {action: 'dialog_dismissed', message: message.slice(0, 160)};
      }
    }
  }

  const priceLayer = document.querySelector('[class*="PriceContent_layerWrap"]');
  if (priceLayer) {
    const input = priceLayer.querySelector('.joint-stepper__input');
    const increment = priceLayer.querySelector('.joint-stepper__incrementButton');
    const wanted = Math.max(1, config.numSeats);
    let current = Number.parseInt(input?.value || '0', 10) || 0;
    while (increment && !increment.disabled && current < wanted) {
      increment.click();
      current += 1;
    }
    const order = priceLayer.querySelector('button[class*="EntButton_primary"]:not([disabled])');
    if (order) {
      order.click();
      return {action: 'order_submitted', count: current};
    }
    return {action: 'price_waiting', count: current};
  }

  for (const {doc, win} of documents) {
    const slider = doc.querySelector(
      '.captchSliderInner,' +
      '.captchSlider,' +
      '[class*="captchSlider"],' +
      '[class*="captchaSlider"],' +
      '[class*="sliderCaptcha"],' +
      '[class*="SliderCaptcha"]'
    );
    if (slider && visible(slider)) {
      return {
        action: 'slider_captcha',
        type: 'slider',
        text: (slider.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 120)
      };
    }

    const hcaptcha = doc.querySelector(
      'iframe[src*="hcaptcha"],' +
      'iframe[data-hcaptcha-widget-id],' +
      '.h-captcha,' +
      'div[id^="hcaptcha"]'
    );
    if (hcaptcha && visible(hcaptcha)) {
      return {action: 'protected_challenge', type: 'hcaptcha'};
    }

    const captchaImage = doc.querySelector(
      '[class*="ModalCaptchaText_captchaImage"] img,' +
      '#imgCaptcha,.capchaLayer img,' +
      'img[src*="captcha" i],img[alt*="captcha" i]'
    );
    const captchaInput = doc.querySelector(
      '[class*="ModalCaptchaText"] input,#txtCaptcha,' +
      'input[name="Captcha"],input[name*="captcha" i]'
    );
    if (captchaImage && captchaInput && visible(captchaImage)) {
      return {
        action: 'captcha',
        image: captchaImage.currentSrc || captchaImage.src || '',
        inputId: captchaInput.id || '',
        inputName: captchaInput.name || ''
      };
    }

    const configuredTypes = config.seatTypes || [];
    const typeOrder = configuredTypes;
    const typeButtons = [...doc.querySelectorAll(
      'button,[role="button"],[role="tab"],a'
    )].filter(element => {
      const text = textOf(element);
      return visible(element) && text.length <= 80 && normalizeSeatType(text);
    });
    if (configuredTypes.length && typeButtons.length) {
      const activeTypeButton = typeButtons.find(selected);
      const activeType = normalizeSeatType(textOf(activeTypeButton));
      const preferredType = typeOrder.find(type =>
        typeButtons.some(button => normalizeSeatType(textOf(button)) === type)
      );
      if (preferredType && activeType !== preferredType) {
        const targetType = typeButtons.find(button =>
          normalizeSeatType(textOf(button)) === preferredType
        );
        if (targetType && click(targetType)) {
          return {action: 'seat_type_selected', seatType: preferredType, text: textOf(targetType)};
        }
      }
    }

    const zoneKeywords = (config.zones && config.zones.length)
      ? config.zones
      : (config.customBlocks && config.customBlocks.length)
        ? config.customBlocks
        : config.tiers;
    const ignoredZoneText = /seat\s*selection\s*completed|selection\s*completed|previous|next|standing|reserved|seated|seat\s*type/i;
    const zoneCandidates = [...doc.querySelectorAll(
      'button,[role="button"],[role="option"],li,a,div[class*="Item"],div[class*="item"]'
    )].filter(element => {
      const text = textOf(element);
      if (!visible(element) || !text || text.length > 120 || ignoredZoneText.test(text)) return false;
      if (zoneKeywords.length) return matches(text, zoneKeywords);
      return /\d{2,3}|floor|side|section|block|구역|區|區域/i.test(text);
    });
    const activeZone = zoneCandidates.find(selected);
    if (zoneCandidates.length && !activeZone) {
      const targetZone = zoneCandidates[Math.min(config.areaIndex, zoneCandidates.length - 1)];
      if (targetZone && click(targetZone)) {
        return {action: 'zone_selected', text: textOf(targetZone)};
      }
    }

    const purpleButtons = [...doc.querySelectorAll('button,[role="button"],a')]
      .filter(element => isPurpleAction(element) && !/previous|back/i.test(textOf(element)));
    if (purpleButtons.length) {
      const button = purpleButtons[0];
      click(button);
      const completed = clickSeatSelectionCompleted(doc);
      return {
        action: completed ? 'seat_selection_completed' : 'seat_button_clicked',
        text: textOf(button)
      };
    }
    if (zoneCandidates.length && activeZone) {
      const activeIndex = zoneCandidates.indexOf(activeZone);
      const nextZone = zoneCandidates[activeIndex + 1];
      if (nextZone && click(nextZone)) {
        return {action: 'zone_selected', text: textOf(nextZone)};
      }
    }
    if (configuredTypes.length > 1 && typeButtons.length) {
      const activeTypeButton = typeButtons.find(selected);
      const activeType = normalizeSeatType(textOf(activeTypeButton));
      const activeTypeIndex = Math.max(0, typeOrder.indexOf(activeType));
      const nextType = typeOrder[(activeTypeIndex + 1) % typeOrder.length];
      const targetType = typeButtons.find(button =>
        normalizeSeatType(textOf(button)) === nextType
      );
      if (targetType && activeType !== nextType && click(targetType)) {
        return {action: 'seat_type_selected', seatType: nextType, text: textOf(targetType)};
      }
    }

    const legacyDates = [...doc.querySelectorAll(
      'a[name="CellPlayDate"],[onclick*="PlayDate"],[onclick*="fnSelectPlayDate"]'
    )].filter(visible);
    if (legacyDates.length) {
      let candidates = legacyDates.filter(el => matches(
        (el.textContent || '') + ' ' + (el.getAttribute('onclick') || ''),
        config.dates
      ));
      if (!candidates.length && config.dateFallback) candidates = legacyDates;
      if (candidates.length) {
        const target = candidates[Math.min(config.dateIndex, candidates.length - 1)];
        const isSelected = target.getAttribute('aria-selected') === 'true' ||
          /\bsel1\b|\bselected\b|\bactive\b/i.test(target.className || '');
        if (isSelected) {
          // Continue below so the selected performance can advance to Next.
        } else {
        const handler = target.getAttribute('onclick') || '';
        try {
          if (handler && win) win.eval(handler);
          else target.click();
        } catch (_) { target.click(); }
        return {action: 'date_selected', text: target.textContent.trim()};
        }
      }
      if (!candidates.length) {
        return {action: 'no_matching_date', count: legacyDates.length};
      }
    }

    const playSeq = doc.querySelector('#PlaySeq,select[name="PlaySeq"]');
    if (playSeq && playSeq.tagName === 'SELECT' && !playSeq.value) {
      const option = [...playSeq.options].find(item => item.value);
      if (option) {
        playSeq.value = option.value;
        playSeq.dispatchEvent(new Event('change', {bubbles: true}));
        return {action: 'time_selected', value: option.value};
      }
    }
    if (playSeq && playSeq.tagName === 'SELECT' && playSeq.value) {
      const next = doc.querySelector(
        '#LargeNextBtnLink,#SmallNextBtnLink,a[href*="fnNextStep"],' +
        'a[onclick*="fnNextStep"],img[src*="btn_next"]'
      );
      if (next) {
        click(next.closest?.('a') || next);
        return {action: 'schedule_submitted', playSeq: playSeq.value};
      }
      try {
        if (typeof win.fnNextStep === 'function') {
          const result = win.fnNextStep('P');
          return {action: 'schedule_submitted', playSeq: playSeq.value, result: String(result)};
        }
      } catch (_) {}
    }

    const seatCount = doc.querySelector('select[name="SeatCount"]');
    if (seatCount) {
      const options = [...seatCount.options].filter(option => Number.parseInt(option.value, 10) > 0);
      let target = options.find(option => Number.parseInt(option.value, 10) === config.numSeats);
      if (!target && options.length) {
        target = options.reduce((best, option) =>
          Number.parseInt(option.value, 10) > Number.parseInt(best.value, 10) ? option : best
        );
      }
      if (target) {
        seatCount.value = target.value;
        seatCount.dispatchEvent(new Event('change', {bubbles: true}));
        try { if (typeof win.fnSelectPrice === 'function') win.fnSelectPrice(seatCount); } catch (_) {}
        try {
          if (typeof win.fnNextStep === 'function') win.fnNextStep('P');
          else click(doc.querySelector('#LargeNextBtnLink,#SmallNextBtnLink,a[href*="fnNextStep"]'));
        } catch (_) {}
        return {action: 'count_selected', count: Number.parseInt(target.value, 10)};
      }
    }

    const gradeRows = [...doc.querySelectorAll(
      '[id="GradeRow"] span[onclick*="fnSwapGrade"],[id="GradeDetail"][seatgradename]'
    )];
    if (gradeRows.length) {
      let candidates = gradeRows.filter(row => matches(
        (row.textContent || '') + ' ' + (row.getAttribute('seatgradename') || ''),
        config.tiers
      ));
      if (!candidates.length && config.areaFallback) candidates = gradeRows;
      if (candidates.length) {
        const target = candidates[Math.min(config.areaIndex, candidates.length - 1)];
        const handler = target.getAttribute('onclick') || '';
        const indexMatch = handler.match(/fnSwapGrade\((\d+)\)/);
        try {
          if (indexMatch && typeof win.fnSwapGrade === 'function') {
            win.fnSwapGrade(Number.parseInt(indexMatch[1], 10));
          } else if (handler) {
            win.eval(handler);
          } else {
            target.click();
          }
        } catch (_) { target.click(); }
        return {action: 'grade_selected', text: target.textContent.trim()};
      }
    }

    const areas = [...doc.querySelectorAll(
      'map area,[id="GradeDetail"] a[href*="fnBlockSeatUpdate"]'
    )];
    if (areas.length) {
      let candidates = areas.filter(area => {
        const text = [
          area.title, area.alt, area.getAttribute('href'), area.getAttribute('onclick')
        ].filter(Boolean).join(' ');
        return matches(text, config.customBlocks.length ? config.customBlocks : config.tiers);
      });
      if (!candidates.length && config.areaFallback) candidates = areas;
      if (candidates.length) {
        const target = candidates[Math.min(config.areaIndex, candidates.length - 1)];
        const handler = target.getAttribute('href') || target.getAttribute('onclick') || '';
        try {
          if (handler.toLowerCase().startsWith('javascript:')) win.eval(handler.slice(11));
          else if (handler && !handler.startsWith('http')) win.eval(handler);
          else target.click();
        } catch (_) { target.click(); }
        return {action: 'block_selected', text: target.title || target.alt || handler};
      }
    }

    let availableSeats = [...doc.querySelectorAll(
      'span.SeatN[name="Seats"][value="N"],circle.js-seat:not(.disabled):not(.Disabled)'
    )].filter(visible);
    const selectedSeats = [...doc.querySelectorAll(
      'span.SeatY[name="Seats"][value="Y"],circle.js-seat.selected'
    )].filter(visible);
    if (selectedSeats.length) {
      let result = null;
      try {
        if (typeof win.fnNextStep === 'function') result = win.fnNextStep('P');
        else {
          const next = doc.querySelector(
            '#LargeNextBtnLink,#SmallNextBtnLink,a[href*="fnNextStep"],a[onclick*="fnNextStep"]'
          );
          if (next) click(next);
        }
      } catch (_) {}
      return {
        action: 'seat_next_retried',
        count: selectedSeats.length,
        result: result === null ? null : String(result)
      };
    }
    if (config.customBlocks.length || config.tiers.length) {
      const filtered = availableSeats.filter(seat => matches(
        [
          seat.id, seat.getAttribute('title'), seat.getAttribute('aria-label'),
          seat.getAttribute('data-block'), seat.getAttribute('data-grade')
        ].filter(Boolean).join(' '),
        config.customBlocks.length ? config.customBlocks : config.tiers
      ));
      if (filtered.length) availableSeats = filtered;
      else if (!config.areaFallback) availableSeats = [];
    }
    if (availableSeats.length) {
      const selected = availableSeats.slice(0, config.numSeats);
      selected.forEach(click);
      const submit = doc.querySelector(
        'button[class*="EntButton_primary"]:not([disabled]),#LargeNextBtnLink,#SmallNextBtnLink'
      );
      if (submit) click(submit);
      else {
        try { if (typeof win.fnNextStep === 'function') win.fnNextStep('P'); } catch (_) {}
      }
      return {
        action: 'seats_selected',
        count: selected.length,
        seats: selected.map(seat => seat.id || seat.title || '')
      };
    }
  }
  return {action: 'waiting'};
})()
"""


async def _evaluate(
    tab: Any,
    javascript: str,
    config_dict: dict[str, Any],
) -> Any:
    try:
        return await tab.evaluate(javascript)
    except Exception as exc:
        error_text = str(exc)
        if "dialog" in error_text.lower():
            try:
                await tab.send(cdp.page.handle_java_script_dialog(accept=True))
                return await tab.evaluate(javascript)
            except Exception:
                pass
        now = time.monotonic()
        if now - _state.last_error_at >= 5:
            util.create_debug_logger(config_dict).log(
                f"[NOL] JavaScript evaluation failed: {exc}"
            )
            _state.last_error_at = now
        return None


async def _run_login_step(
    tab: Any,
    config_dict: dict[str, Any],
) -> dict[str, Any]:
    accounts = config_dict.get("accounts", {})
    account = str(accounts.get("nolworld_account", "")).strip()
    password = str(accounts.get("nolworld_password", ""))
    if not account or not password:
        return {"action": "login_credentials_missing"}

    result = await _evaluate(
        tab,
        LOGIN_JS.replace(
            "__CREDENTIALS__",
            json.dumps({"account": account, "password": password}),
        ),
        config_dict,
    )
    if not isinstance(result, dict):
        return {"action": "login_waiting"}
    action = str(result.get("action", "login_waiting"))
    if action == "login_submitted":
        if not _state.login_submitted:
            print(f"[NOL][LOGIN] submitted for {account[:3]}***")
        _state.login_submitted = True
    elif action == "login_waiting_turnstile":
        if _state.last_action != action:
            print("[NOL][LOGIN] credentials filled; waiting for Turnstile.")
    return result


def _is_available(snapshot: dict[str, Any]) -> bool:
    buy = snapshot.get("buy") or {}
    text = str(buy.get("text", "")).lower()
    return bool(
        buy.get("found")
        and not buy.get("disabled")
        and not any(marker in text for marker in SOLD_OUT_TEXT)
    )


async def _notify_success(
    config_dict: dict[str, Any],
    *,
    detail: str,
) -> None:
    if _state.success_notified:
        return
    _state.success_notified = True
    print(f"[NOL][SUCCESS] {detail}")
    if config_dict.get("advanced", {}).get("play_sound", {}).get("order", True):
        play_sound_while_ordering(config_dict)
    send_discord_notification(config_dict, "order", "NOL World")
    send_telegram_notification(config_dict, "order", "NOL World")


async def _run_booking_step(
    tab: Any,
    url: str,
    config_dict: dict[str, Any],
    ocr: Any,
    platform_config: dict[str, Any],
) -> None:
    await _evaluate(tab, DIALOG_HOOK_JS, config_dict)
    date_values = _preferred_dates(config_dict)
    tier_values = _preferred_tiers(config_dict)
    zone_values = _preferred_seat_zones(config_dict)
    payload = {
        "dates": date_values,
        "scheduleTargets": _preferred_schedule_targets(config_dict),
        "tiers": tier_values,
        "seatTypes": _preferred_seat_types(config_dict),
        "zones": zone_values,
        "customBlocks": platform_config["custom_blocks"],
        "numSeats": max(1, min(int(config_dict.get("ticket_number", 1)), 4)),
        "dateFallback": bool(config_dict.get("date_auto_fallback", False)),
        "areaFallback": bool(config_dict.get("area_auto_fallback", False)),
        "dateIndex": _choose_index(
            max(1, len(date_values)),
            _selection_mode(config_dict, "date_auto_select"),
        ),
        "areaIndex": _choose_index(
            max(
                1,
                len(zone_values)
                or len(tier_values)
                or len(platform_config["custom_blocks"]),
            ),
            _selection_mode(config_dict, "area_auto_select"),
        ),
    }

    if "/onestop/schedule" in url.lower():
        result = await _evaluate(
            tab,
            ONESTOP_SCHEDULE_JS.replace("__CONFIG__", json.dumps(payload)),
            config_dict,
        )
    else:
        result = await _evaluate(
            tab,
            BOOKING_STEP_JS.replace("__CONFIG__", json.dumps(payload)),
            config_dict,
        )
    if not isinstance(result, dict):
        return

    action = str(result.get("action", "waiting"))
    now = time.monotonic()
    if action != _state.last_action or now - _state.last_action_at >= 5:
        print(f"[NOL][{_state.phase.upper()}] {action}: {result}")
        _state.last_action = action
        _state.last_action_at = now

    if action in {"captcha", "slider_captcha", "protected_challenge"}:
        if not _state.verification_handoff:
            _state.verification_handoff = True
            _state.verification_started_at = now
            print(
                f"[NOL][SECURITY HANDOFF] Protected verification ({action}) is waiting "
                "in the browser. The workflow state is preserved and will "
                "resume automatically after legitimate completion."
            )
        _state.next_action_at = now + 1.0
        return

    if _state.verification_handoff:
        elapsed = max(0.0, now - _state.verification_started_at)
        print(
            "[NOL][SECURITY HANDOFF] Verification cleared; "
            f"resuming after {elapsed:.1f}s."
        )
        _state.verification_handoff = False
        _state.verification_started_at = 0.0

    controller = DOMController(tab)
    if action == "date_selected":
        synchronized = await controller.wait_for_mutation(
            "document.querySelectorAll("
            "'button[class*=\"TimeBlock_timeButton\"],"
            "[role=\"button\"][class*=\"TimeBlock_timeButton\"]'"
            ").length > 0",
            timeout=2.5,
        )
        _state.next_action_at = time.monotonic() + (
            0.05 if synchronized else 0.4
        )
        return
    if action == "time_selected":
        synchronized = await controller.wait_for_mutation(
            "[...document.querySelectorAll('button')].some(button => "
            "!button.disabled && /next|continue|다음|下一步|繼續|继续/i.test("
            "button.textContent || ''))",
            timeout=2.5,
        )
        _state.next_action_at = time.monotonic() + (
            0.05 if synchronized else 0.4
        )
        return

    if action in {"order_submitted", "payment"}:
        await _notify_success(
            config_dict,
            detail=f"{action}, seats={result.get('seats') or result.get('count') or ''}",
        )
        if action == "payment" or platform_config["lock_only_mode"]:
            _state.phase = "done"
    elif action == "seat_conflict_recovered":
        _state.success_notified = False
        _state.next_action_at = now + 0.2
        return
    delay_ms = platform_config["block_delay_ms"]
    _state.next_action_at = now + max(0.2, delay_ms / 1000)


async def _click_buy_button(
    tab: Any,
    config_dict: dict[str, Any],
) -> dict[str, Any]:
    """Click Buy with a browser-level mouse event, then fall back to DOM events."""

    target = await _evaluate(tab, BUY_TARGET_JS, config_dict)
    if not isinstance(target, dict) or not target.get("found"):
        return target if isinstance(target, dict) else {
            "clicked": False,
            "error": "target-unavailable",
        }

    try:
        x = float(target["x"])
        y = float(target["y"])
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mouseMoved",
                x=x,
                y=y,
            )
        )
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mousePressed",
                x=x,
                y=y,
                button=cdp.input_.MouseButton("left"),
                click_count=1,
            )
        )
        await asyncio.sleep(0.05)
        await tab.send(
            cdp.input_.dispatch_mouse_event(
                type_="mouseReleased",
                x=x,
                y=y,
                button=cdp.input_.MouseButton("left"),
                click_count=1,
            )
        )
        return {
            "clicked": True,
            "text": target.get("text", ""),
            "method": "cdp",
        }
    except Exception as exc:
        util.create_debug_logger(config_dict).log(
            f"[NOL][CLICK] CDP Buy click failed; using DOM fallback: {exc}"
        )
        fallback = await _evaluate(tab, BUY_CLICK_JS, config_dict)
        if isinstance(fallback, dict):
            fallback["method"] = "dom"
            return fallback
        return {"clicked": False, "error": "fallback-failed"}


async def _restart_booking_cycle(
    tab: Any,
    config_dict: dict[str, Any],
) -> bool:
    target = _state.detail_url or str(config_dict.get("homepage", "")).strip()
    if not target or not _is_nol_url(target):
        return False
    _state.cycle_count += 1
    print(
        f"[NOL][SESSION] booking window expired; "
        f"starting cycle #{_state.cycle_count + 1}."
    )
    try:
        await tab.get(target)
    except Exception as exc:
        util.create_debug_logger(config_dict).log(
            f"[NOL][SESSION] failed to return to event page: {exc}"
        )
        return False
    _state.last_url = ""
    _state.phase = "checking"
    _state.booking_started_at = 0.0
    _state.availability_baselined = False
    _state.buy_was_available = False
    _state.buy_attempts = 0
    _state.notice_attempts = 0
    _state.verification_handoff = False
    _state.verification_started_at = 0.0
    _state.success_notified = False
    _state.last_action = ""
    _state.next_action_at = time.monotonic() + 1.0
    return True


def _pool_summary(pool: dict[str, Any]) -> str:
    total = 0
    available_sequences = 0
    for sequence in pool.get("play_sequences", []):
        seats = sequence.get("seats") or []
        count = sum(max(0, int(item.get("remain", 0))) for item in seats)
        if count:
            available_sequences += 1
            total += count
    return f"{total} seats across {available_sequences} play sequence(s)"


async def _maintain_ticket_pool(
    snapshot: dict[str, Any],
    url: str,
    platform_config: dict[str, Any],
    config_dict: dict[str, Any],
) -> None:
    if not platform_config["ticket_pool_enabled"]:
        return
    now = time.monotonic()
    interval = platform_config["ticket_pool_refresh_minutes"] * 60

    if _state.pool_task and _state.pool_task.done():
        try:
            _state.pool_result = _state.pool_task.result()
            print(f"[NOL][POOL] {_pool_summary(_state.pool_result)}")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            util.create_debug_logger(config_dict).log(f"[NOL][POOL] refresh failed: {exc}")
        finally:
            _state.pool_task = None

    if _state.pool_task or now - _state.last_pool_at < interval:
        return
    codes = await asyncio.to_thread(
        resolve_concert_codes,
        snapshot.get("goodsCode"),
        snapshot.get("placeCode"),
        url,
        timeout=3.0,
    )
    if not codes:
        return
    _state.last_pool_at = now
    _state.pool_task = asyncio.create_task(
        async_fetch_ticket_pool_data(codes["goodsCode"], codes["placeCode"])
    )


async def nodriver_nolworld_main(
    tab: Any,
    url: str,
    config_dict: dict[str, Any],
    ocr: Any = None,
) -> dict[str, Any]:
    """Advance the NOL World state machine once without blocking the main loop."""

    if not tab or not _is_nol_url(url):
        return {"phase": "not_nol", "handled": False}

    lock = _get_main_lock()
    if lock.locked():
        return {"phase": _state.phase, "handled": True, "busy": True}

    async with lock:
        now = time.monotonic()
        if url != _state.last_url:
            _state.reset_for_url(url)
            print(f"[NOL] page phase={_state.phase}: {url}")

        if _state.phase == "done":
            return {"phase": "done", "handled": True}
        if now < _state.next_action_at:
            return {"phase": _state.phase, "handled": True, "throttled": True}

        platform_config = _config(config_dict)
        snapshot = await _evaluate(tab, PAGE_SNAPSHOT_JS, config_dict)
        if not isinstance(snapshot, dict):
            _state.next_action_at = now + 1
            return {"phase": _state.phase, "handled": True, "snapshot": False}

        snapshot_state = snapshot.get("state")
        if snapshot_state == "login":
            _state.phase = "login"
        elif snapshot_state == "queue":
            _state.phase = "queuing"
        elif snapshot_state == "preSale":
            _state.phase = "pre_sale"
        elif snapshot_state == "booking":
            _state.phase = "booking"
        elif snapshot_state == "detail" and _state.phase not in {"buying", "booking"}:
            _state.phase = "checking"
            _state.detail_url = url
            _state.booking_started_at = 0.0

        if _state.phase == "login":
            result = await _run_login_step(tab, config_dict)
            action = str(result.get("action", "login_waiting"))
            now = time.monotonic()
            if action != _state.last_action or now - _state.last_action_at >= 10:
                print(f"[NOL][LOGIN] {action}")
                _state.last_action = action
                _state.last_action_at = now
            try:
                login_delay = float(result.get("delay", 0))
            except (TypeError, ValueError):
                login_delay = 0
            if login_delay <= 0:
                login_delay = 0.8 if action == "login_submitted" else 1.0
            _state.next_action_at = now + login_delay

        elif _state.phase == "checking":
            notice = snapshot.get("notice") or {}
            if (
                platform_config["auto_notice_and_buy"]
                and notice.get("found")
            ):
                result = await _evaluate(tab, NOTICE_DISMISS_JS, config_dict)
                _state.notice_attempts += 1
                if isinstance(result, dict) and result.get("clicked"):
                    print(
                        f"[NOL][NOTICE] closed: "
                        f"{result.get('text', '') or 'Notice'}"
                    )
                    _state.notice_attempts = 0
                    # Wait on the actual overlay mutation instead of relying
                    # only on a fixed animation delay.
                    overlay_cleared = await DOMController(
                        tab
                    ).wait_for_mutation(
                        "!document.querySelector("
                        "'.nds-e-modal-bottom-sheet__container')",
                        timeout=1.2,
                    )
                    _state.next_action_at = time.monotonic() + (
                        0.05 if overlay_cleared else 0.45
                    )
                else:
                    _state.next_action_at = now + min(
                        1.5,
                        0.35 + _state.notice_attempts * 0.15,
                    )
                return {
                    "phase": _state.phase,
                    "handled": True,
                    "notice": result,
                }

            await _maintain_ticket_pool(
                snapshot,
                url,
                platform_config,
                config_dict,
            )
            available = _is_available(snapshot)
            _state.availability_baselined = True
            _state.buy_was_available = available

            if available and platform_config["auto_notice_and_buy"]:
                result = await _click_buy_button(tab, config_dict)
                if isinstance(result, dict) and result.get("clicked"):
                    _state.buy_attempts += 1
                    print(
                        f"[NOL][CLICK] Buy Now #{_state.buy_attempts} "
                        f"({result.get('method', 'unknown')}): "
                        f"{result.get('text', '')}"
                    )
                    _state.phase = "buying"
                    _state.next_action_at = now + 0.45
                    return {"phase": _state.phase, "handled": True, "clicked": True}

            if not platform_config["pre_sale_mode"] and (
                now - _state.last_reload_at >= platform_config["check_interval"]
            ):
                try:
                    await tab.reload()
                    _state.last_reload_at = now
                    _state.next_action_at = now + 0.8
                except Exception as exc:
                    util.create_debug_logger(config_dict).log(
                        f"[NOL] detail-page reload failed: {exc}"
                    )
                    _state.next_action_at = now + 1
            else:
                _state.next_action_at = now + min(
                    1.0,
                    platform_config["check_interval"],
                )

        elif _state.phase == "pre_sale":
            pre_sale = snapshot.get("preSale") or {}
            if pre_sale.get("found") and not pre_sale.get("disabled"):
                result = await _evaluate(tab, PRESALE_CLICK_JS, config_dict)
                if isinstance(result, dict) and result.get("clicked"):
                    print(f"[NOL][PRE-SALE] clicked: {result.get('text', '')}")
                    _state.phase = "queuing"
            _state.next_action_at = now + 0.5

        elif _state.phase == "queuing":
            queue = snapshot.get("queue") or {}
            signature = (
                queue.get("waitingOrder"),
                queue.get("waitingPeople"),
                queue.get("bookingRate"),
                queue.get("progressPercent"),
            )
            if signature != _state.last_queue_signature:
                print(
                    "[NOL][QUEUE] "
                    f"position={signature[0]}, waiting={signature[1]}, "
                    f"rate={signature[2]}, progress={signature[3]}"
                )
                _state.last_queue_signature = signature
            # Queue pages are intentionally never reloaded.
            _state.next_action_at = now + 1

        elif _state.phase in {"buying", "booking"}:
            if snapshot_state == "detail":
                # React or the closing modal overlay may consume a click.
                # Retry quickly without refreshing the official event page.
                _state.phase = "checking"
                _state.buy_was_available = False
                if _state.buy_attempts >= 10:
                    _state.buy_attempts = 0
                    _state.next_action_at = now + 1.5
                else:
                    _state.next_action_at = now + 0.35
            else:
                _state.phase = "booking"
                _state.buy_attempts = 0
                if _state.booking_started_at <= 0:
                    _state.booking_started_at = now
                    print(
                        "[NOL][SESSION] booking timer started "
                        f"({platform_config['session_timeout_seconds'] / 60:g} min)."
                    )
                elif (
                    now - _state.booking_started_at
                    >= platform_config["session_timeout_seconds"]
                ):
                    restarted = await _restart_booking_cycle(tab, config_dict)
                    return {
                        "phase": _state.phase,
                        "handled": True,
                        "session_restarted": restarted,
                    }
                await _run_booking_step(
                    tab,
                    url,
                    config_dict,
                    ocr,
                    platform_config,
                )
        else:
            _state.next_action_at = now + 1

        return {
            "phase": _state.phase,
            "handled": True,
            "snapshot": snapshot,
        }
