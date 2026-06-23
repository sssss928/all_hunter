#encoding=utf-8
# =============================================================================
# KKTIX Platform Module - Performance Optimized
# Extracted from nodriver_tixcraft.py during modularization (Phase 1)
# Optimizations:
#   - DOM caching and batch operations
#   - Parallel async operations
#   - Smart retry mechanism with exponential backoff
#   - State management optimization
#   - Efficient selector caching
# =============================================================================

import asyncio
import json
import os
import random
import re
import time
import urllib.parse
from functools import lru_cache
from typing import Optional, Any

from zendriver import cdp

import util
from nodriver_common import (
    check_and_handle_pause,
    nodriver_check_checkbox,
    play_sound_while_ordering,
    send_discord_notification,
    send_telegram_notification,
    write_question_to_file,
    CONST_FROM_TOP_TO_BOTTOM,
    CONST_MAXBOT_ANSWER_ONLINE_FILE,
    CONST_MAXBOT_INT28_FILE,
)

__all__ = [
    "nodriver_kktix_signin",
    "nodriver_kktix_paused_main",
    "nodriver_kktix_travel_price_list",
    "nodriver_kktix_assign_ticket_number",
    "nodriver_kktix_reg_captcha",
    "debug_kktix_page_state",
    "nodriver_kktix_date_auto_select",
    "nodriver_kktix_events_press_next_button",
    "nodriver_kktix_check_guest_modal",
    "nodriver_kktix_press_next_button",
    "nodriver_kktix_check_ticket_page_status",
    "nodriver_kktix_reg_new_main",
    "check_kktix_got_ticket",
    "nodriver_kktix_main",
    "nodriver_kktix_booking_main",
    "nodriver_kktix_confirm_order_button",
    "nodriver_kktix_order_member_code",
]

# ============================================================================
# STATE MANAGEMENT - 優化版本
# ============================================================================

class KKTIXState:
    """KKTIX 狀態管理類別 - 替換全域 _state 字典"""
    
    def __init__(self):
        self.fail_list = []
        self.start_time = None
        self.done_time = None
        self.elapsed_time = None
        self.is_popup_checkout = False
        self.played_sound_ticket = False
        self.played_sound_order = False
        self.got_ticket_detected = False
        self.success_actions_done = False
        self.reg_execution_count = 0
        self.alert_handler_registered = False
        self.alert_needs_reload = False
        self.guest_modal_checked = False
        self.guest_modal_last_check_time = 0
        self.guest_modal_status_logged = False
        self.last_ticket_already_selected_log_value = None
        self.printed_completed = False
        self.last_homepage_redirect_time = 0
        self.queue_log_time = 0
        self.last_signin_redirect_time = 0
        
        # 效能優化: DOM 快取
        self.dom_cache = {}
        self.cache_timestamp = {}

    def clear_cache(self):
        """清除 DOM 快取"""
        self.dom_cache.clear()
        self.cache_timestamp.clear()
    
    def set_cache(self, key: str, value: Any, ttl: float = 0.25):
        """設定快取 (ttl: 過期時間秒數)"""
        self.dom_cache[key] = value
        self.cache_timestamp[key] = time.time() + ttl
    
    def get_cache(self, key: str) -> Optional[Any]:
        """獲取快取 (若過期則返回 None)"""
        if key in self.dom_cache:
            if time.time() < self.cache_timestamp.get(key, 0):
                return self.dom_cache[key]
            else:
                try:
                    del self.dom_cache[key]
                    del self.cache_timestamp[key]
                except Exception:
                    pass
        return None
    
    def reset_for_new_session(self):
        """重置為新的購票流程"""
        self.fail_list = []
        self.played_sound_ticket = False
        self.reg_execution_count = 0
        self.last_ticket_already_selected_log_value = None
        self.clear_cache()

# 全域狀態實例
_state = KKTIXState()

# ============================================================================
# 優化的選擇器和 DOM 操作
# ============================================================================

@lru_cache(maxsize=32)
def _get_selector_key(selector: str) -> str:
    """生成選擇器快取鍵"""
    return f"selector:{selector}"

async def _cached_query_selector(tab, selector: str, cache_ttl: float = 0.25):
    """快取的選擇器查詢"""
    cache_key = _get_selector_key(selector)
    cached = _state.get_cache(cache_key)
    
    if cached is not None:
        return cached
    
    try:
        result = await tab.query_selector(selector)
        if result:
            _state.set_cache(cache_key, result, cache_ttl)
        return result
    except Exception:
        return None

async def _cached_query_selector_all(tab, selector: str, cache_ttl: float = 0.25):
    """快取的多選擇器查詢"""
    cache_key = f"{_get_selector_key(selector)}:all"
    cached = _state.get_cache(cache_key)
    
    if cached is not None:
        return cached
    
    try:
        result = await tab.query_selector_all(selector)
        if result:
            _state.set_cache(cache_key, result, cache_ttl)
        return result
    except Exception:
        return None

async def _smart_sleep(duration: float = 0.1, jitter: bool = True):
    """智能延遲 - 支援 jitter 避免雷鳴羊群問題"""
    if jitter and duration > 0:
        actual_duration = duration + random.uniform(-duration * 0.1, duration * 0.1)
    else:
        actual_duration = duration
    await asyncio.sleep(max(actual_duration, 0.01))

# ============================================================================
# 優化的重試機制
# ============================================================================

async def _retry_with_backoff(
    func,
    max_retries: int = 3,
    base_delay: float = 0.3,
    max_delay: float = 2.0,
    backoff_factor: float = 1.5
):
    """指數退避重試機制"""
    for attempt in range(max_retries):
        try:
            if asyncio.iscoroutinefunction(func):
                return await func()
            else:
                return func()
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            delay = min(base_delay * (backoff_factor ** attempt), max_delay)
            delay += random.uniform(0, delay * 0.1)  # 添加抖動
            await asyncio.sleep(delay)
    return None

# ============================================================================
# 核心函數 - 優化版本
# ============================================================================

async def nodriver_kktix_check_queue_page(tab, config_dict):
    """偵測 KKTIX 等候室頁面 - 優化版本"""
    debug = util.create_debug_logger(config_dict)

    is_queue_page = False
    wait_text = ""
    
    try:
        result = await tab.evaluate('''
            (function() {
                const cfTime = document.querySelector('#cf-time');
                if (cfTime) return { isQueue: true, waitText: cfTime.textContent.trim() };
                
                const heading = document.querySelector('main h1');
                if (heading && heading.textContent.indexOf('目前網站人流眾多') >= 0) {
                    return { isQueue: true, waitText: '' };
                }
                return { isQueue: false, waitText: '' };
            })()
        ''')
        
        if isinstance(result, dict):
            is_queue_page = bool(result.get('isQueue', False))
            wait_text = result.get('waitText', '')
    except Exception:
        pass

    # 節流日誌輸出 (每 10 秒最多一次)
    if is_queue_page:
        current_time = time.time()
        if current_time - _state.queue_log_time > 10:
            _state.queue_log_time = current_time
            debug.log(f"[KKTIX QUEUE] 在等候室中，頁面會自動重新整理. {wait_text}".rstrip())

    return is_queue_page

async def nodriver_kktix_signin(tab, url, config_dict):
    """KKTIX 登入 - 優化版本"""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    debug.log("nodriver_kktix_signin:", url)

    # 解析 back_to 參數
    target_url = config_dict["homepage"]
    try:
        parsed_url = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qs(parsed_url.query)
        if 'back_to' in params and len(params['back_to']) > 0:
            target_url = params['back_to'][0]
    except Exception as exc:
        debug.log(f"[KKTIX SIGNIN] 解析 back_to 失敗: {exc}")

    # 檢查等候室
    if await nodriver_kktix_check_queue_page(tab, config_dict):
        return False

    await _smart_sleep(0.35)

    kktix_account = config_dict["accounts"]["kktix_account"]
    kktix_password = config_dict["accounts"]["kktix_password"].strip()

    has_redirected = False
    if len(kktix_account) > 4:
        try:
            # 並行填寫帳號和密碼
            account_task = tab.query_selector("#user_login")
            password_task = tab.query_selector("#user_password")
            
            account, password = await asyncio.gather(
                account_task,
                password_task,
                return_exceptions=True
            )

            if account and not isinstance(account, Exception):
                await account.send_keys(kktix_account)
                await _smart_sleep(0.15)

            if password and not isinstance(password, Exception):
                await password.send_keys(kktix_password)
                await _smart_sleep(0.15)

            # 點擊登入按鈕
            await tab.evaluate('''
                const loginBtn = document.querySelector('input[type="submit"][value="登入"]');
                if (loginBtn) loginBtn.click();
            ''')

            # 使用智能輪詢檢查登入完成
            max_wait = 10
            check_interval = 0.3
            max_attempts = int(max_wait / check_interval)
            login_completed = False

            for attempt in range(max_attempts):
                if await check_and_handle_pause(config_dict):
                    return False

                try:
                    current_url = await tab.evaluate('window.location.href')
                    if '/users/sign_in' not in current_url:
                        login_completed = True
                        debug.log(f"[KKTIX SIGNIN] 登入完成 ({attempt * check_interval:.1f}s): {current_url}")
                        break
                except Exception:
                    pass

                if attempt < max_attempts - 1:
                    await _smart_sleep(check_interval)

            if not login_completed:
                debug.log(f"[KKTIX SIGNIN] 登入逾時 ({max_wait}s)")

            # 檢查是否需要手動重導
            try:
                current_url = await tab.evaluate('window.location.href')
                if current_url and ('kktix.com/' in current_url or 'kktix.cc/' in current_url):
                    if '/users/sign_in' not in current_url:
                        if (current_url.endswith('/') or '/users/' in current_url) and target_url != current_url:
                            debug.log(f"[KKTIX SIGNIN] 重導到: {target_url}")
                            await tab.get(target_url)
                            await _smart_sleep(1.75)
                            has_redirected = True
            except Exception as redirect_error:
                debug.log(f"[KKTIX SIGNIN] 重導失敗: {redirect_error}")

        except Exception as e:
            debug.log(f"[KKTIX SIGNIN] 錯誤: {e}")

    return has_redirected

async def nodriver_kktix_redirect_to_signin_if_guest(tab, url, config_dict):
    """偵測訪客登入狀態 - 優化版本"""
    if len(config_dict["accounts"]["kktix_account"]) <= 4:
        return False

    debug = util.create_debug_logger(config_dict)
    
    try:
        is_guest = bool(await tab.evaluate(
            "!!document.querySelector('li.not-signed-in:not(.hidden)')"
        ))
    except Exception:
        return False
    
    if not is_guest:
        return False

    # 節流重導 (根據設定間隔)
    current_time = time.time()
    redirect_interval = config_dict["advanced"].get("auto_reload_page_interval", 3)
    if redirect_interval <= 0:
        redirect_interval = 3
    
    if current_time - _state.last_signin_redirect_time > redirect_interval:
        _state.last_signin_redirect_time = current_time
        sign_in_url = "https://kktix.com/users/sign_in?back_to=" + urllib.parse.quote(url, safe='')
        debug.log("[KKTIX] 偵測到訪客工作階段，重導至登入頁面")
        try:
            await tab.get(sign_in_url)
        except Exception as exc:
            debug.log(f"[KKTIX] 重導至登入頁面失敗: {exc}")

    return True


async def nodriver_kktix_paused_main(tab, url, config_dict):
    """Keep the login flow responsive while the ticket-selection loop is paused."""
    if "/users/sign_in?" in url:
        return await nodriver_kktix_signin(tab, url, config_dict)
    return False


async def nodriver_kktix_travel_price_list(tab, config_dict, kktix_area_auto_select_mode, kktix_area_keyword):
    """優化版本 - 批次 DOM 查詢和快取"""
    if await check_and_handle_pause(config_dict):
        return True, False, None

    debug = util.create_debug_logger(config_dict)
    ticket_number = config_dict["ticket_number"]

    areas = None
    pending_tickets = None
    is_ticket_number_assigned = False

    # 批次評估所有票券訊息
    ticket_price_list = None
    try:
        ticket_price_list = await tab.evaluate('''
            (function() {
                let rows = Array.from(document.querySelectorAll('div.display-table-row'));
                if (rows.length === 0) {
                    rows = Array.from(document.querySelectorAll('div.ticket-item'));
                }

                let inputIndex = 0;
                return rows.map((row, rowIndex) => {
                    const input = row.querySelector('input');
                    const hasInput = !!input;
                    const rowInputIndex = hasInput ? inputIndex : null;
                    if (hasInput) inputIndex += 1;

                    return {
                        index: rowIndex,
                        html: row.innerHTML || "",
                        text: row.textContent || row.innerText || "",
                        hasInput: hasInput,
                        inputValue: input ? input.value : "0",
                        inputIndex: rowInputIndex
                    };
                });
            })()
        ''')
        ticket_price_list = util.parse_nodriver_result(ticket_price_list)
        if not isinstance(ticket_price_list, list):
            ticket_price_list = None
    except Exception as exc:
        ticket_price_list = None
        debug.log(f"[KKTIX] 取得票券列表失敗: {exc}")

    is_dom_ready = True
    price_list_count = len(ticket_price_list) if ticket_price_list else 0

    if price_list_count > 0:
        areas = []
        pending_tickets = []

        # 解析區域關鍵字 (AND 邏輯)
        kktix_area_keyword_array = [kw.strip() for kw in kktix_area_keyword.split(' ') if kw.strip()]
        kktix_area_keyword_array = [util.format_keyword_string(kw) for kw in kktix_area_keyword_array]

        debug.log(f'[KKTIX AREA] 關鍵字 (AND 邏輯): {kktix_area_keyword_array}')

        for i, ticket_info in enumerate(ticket_price_list):
            row_text = ""
            original_text = ""
            row_html = ticket_info.get('html', '') if isinstance(ticket_info, dict) else ""
            row_input = None
            current_ticket_number = "0"
            
            try:
                if ticket_info:
                    row_text = ticket_info.get('text', '') or util.remove_html_tags(row_html)
                    original_text = ' '.join(row_text.split())
                    current_ticket_number = ticket_info.get('inputValue', '0')
                    if ticket_info.get('hasInput'):
                        row_input = ticket_info.get('inputIndex')
            except Exception as exc:
                is_dom_ready = False
                debug.log(f"[KKTIX] 票券解析錯誤: {exc}")
                break

            if len(row_text) > 0:
                # 檢查售罄狀態
                sold_out_keywords = ['暫無票', '已售完', 'Sold Out', 'sold out', '完売']
                is_sold_out = any(kw in row_text for kw in sold_out_keywords)

                if is_sold_out:
                    row_text = ""
                    continue

                # 檢查未開賣狀態 (保留供後續關鍵字匹配)
                not_yet_open_keywords = [
                    '未開賣', '尚未開賣', '尚未開始', '即將開賣',
                    'Not Started', 'not started', 'まだ発売'
                ]
                has_not_yet_open_status = any(kw in row_text for kw in not_yet_open_keywords)

                # 沒有輸入欄位且不是未開賣票券的篩選
                if len(row_text) > 0 and row_input is None and not has_not_yet_open_status:
                    row_text = ""
                    continue

            # 排除關鍵字檢查
            if len(row_text) > 0:
                if util.reset_row_text_if_match_keyword_exclude(config_dict, row_text):
                    row_text = ""

            # 清理停用詞
            if len(row_text) > 0:
                row_text = util.format_keyword_string(row_text)

            # 檢查剩餘票數
            if len(row_text) > 0 and ticket_number > 1:
                ticket_count = 999
                
                # 從 HTML 中提取剩餘票數
                if ' danger' in row_html and '剩' in row_text and '張' in row_text:
                    match = re.search(r'剩\\s*(\\d+)\\s*張', row_html)
                    if match:
                        try:
                            ticket_count = int(match.group(1))
                        except Exception:
                            ticket_count = 999
                        debug.log(f"[KKTIX] 剩餘票券: {ticket_count}")
                
                if ticket_count < ticket_number:
                    row_text = ""
                    debug.log(f"[KKTIX] 跳過 (票數不足): 需要 {ticket_number}, 只有 {ticket_count}")

            # 關鍵字匹配
            if len(row_text) > 0:
                if len(current_ticket_number) > 0 and current_ticket_number != "0":
                    is_ticket_number_assigned = True
                    break

                is_match_area = False
                if len(kktix_area_keyword_array) == 0:
                    is_match_area = True
                else:
                    is_match_area = all(kw in row_text for kw in kktix_area_keyword_array)

                if is_match_area:
                    if row_input is not None:
                        areas.append(row_input)
                        debug.log(f"[KKTIX AREA] 符合票券: {original_text[:80]}")
                        if kktix_area_auto_select_mode == CONST_FROM_TOP_TO_BOTTOM:
                            break
                    else:
                        pending_tickets.append({
                            'index': i,
                            'text': original_text[:60],
                            'keywords': kktix_area_keyword_array
                        })
                        debug.log(f"[KKTIX AREA] 待開賣票券: {original_text[:80]}")

    if debug.enabled and areas:
        debug.log(f"[KKTIX AREA] 符合票券數: {len(areas) if areas else 0}")

    if await check_and_handle_pause(config_dict):
        return True, False, None

    return is_dom_ready, is_ticket_number_assigned, areas

async def nodriver_kktix_assign_ticket_number(tab, config_dict, kktix_area_keyword):
    """優化版本 - 減少重複查詢"""
    if await check_and_handle_pause(config_dict):
        return True, False, False

    debug = util.create_debug_logger(config_dict)

    ticket_number_str = str(config_dict["ticket_number"])
    auto_select_mode = config_dict["area_auto_select"]["mode"]
    is_fallback_selection = (kktix_area_keyword == "")

    is_ticket_number_assigned = False
    matched_blocks = None
    is_dom_ready = True
    
    is_dom_ready, is_ticket_number_assigned, matched_blocks = await nodriver_kktix_travel_price_list(
        tab, config_dict, auto_select_mode, kktix_area_keyword
    )

    target_area = None
    is_need_refresh = False
    
    if is_dom_ready:
        if not is_ticket_number_assigned:
            target_area = util.get_target_item_from_matched_list(matched_blocks, auto_select_mode)

        if not matched_blocks or len(matched_blocks) == 0:
            is_need_refresh = True

    if target_area is not None:
        try:
            target_index = target_area

            # 單次 JS 評估 - 設定並驗證
            assign_result = await tab.evaluate(f'''
                (function() {{
                    let inputs = document.querySelectorAll('div.display-table-row input');
                    if (inputs.length === 0) {{
                        inputs = document.querySelectorAll('div.ticket-item input.number-step-input-core');
                    }}
                    const targetInput = inputs[{target_index}];

                    if (!targetInput) {{
                        return {{ success: false, error: "找不到輸入欄位" }};
                    }}

                    let parentRow = targetInput.closest('div.display-table-row') || targetInput.closest('div.ticket-item');
                    let ticketName = parentRow ? 
                        parentRow.textContent.replace(/\\s+/g, ' ').trim() : "未知票種";

                    const currentValue = targetInput.value;

                    if (currentValue === "0") {{
                        targetInput.focus();
                        targetInput.value = "{ticket_number_str}";
                        targetInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        targetInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        targetInput.dispatchEvent(new Event('blur', {{ bubbles: true }}));

                        if (window.angular) {{
                            const scope = window.angular.element(targetInput).scope();
                            if (scope) scope.$apply();
                        }}

                        return {{ success: true, assigned: true, value: "{ticket_number_str}", ticketName: ticketName }};
                    }} else {{
                        return {{ success: true, assigned: false, value: currentValue, alreadySet: true, ticketName: ticketName }};
                    }}
                }})();
            ''')

            assign_result = util.parse_nodriver_result(assign_result)

            if assign_result and assign_result.get('success'):
                await _smart_sleep(0.1)

                ticket_name = assign_result.get('ticketName', '未知票種')
                if assign_result.get('assigned'):
                    clean_ticket_name = ' '.join(ticket_name.split())
                    selection_type = "後備" if is_fallback_selection else "關鍵字符合"
                    debug.log(f"[KKTIX AREA SELECT] 已選擇: {clean_ticket_name} ({selection_type})")
                    is_ticket_number_assigned = True
                elif assign_result.get('alreadySet'):
                    debug.log(f"[KKTIX AREA SELECT] 已設定票數")
                    is_ticket_number_assigned = True

        except Exception as exc:
            debug.log(f"[KKTIX AREA] 設定票數失敗: {exc}")

    if await check_and_handle_pause(config_dict):
        return True, False, False

    return is_dom_ready, is_ticket_number_assigned, is_need_refresh

async def nodriver_kktix_reg_captcha(tab, config_dict, fail_list, registrationsNewApp_div):
    """驗證碼處理 - 優化版本"""
    debug = util.create_debug_logger(config_dict)

    answer_list = []
    success = False

    # 批次檢查元素
    elements_check = await tab.evaluate('''
        (function() {
            return {
                hasQuestion: !!document.querySelector('div.custom-captcha-inner p'),
                hasInput: !!document.querySelector('div.custom-captcha-inner > div > div > input'),
                hasButtons: document.querySelectorAll('div.register-new-next-button-area > button').length,
                questionText: document.querySelector('div.custom-captcha-inner p')?.innerText || ''
            };
        })();
    ''')
    elements_check = util.parse_nodriver_result(elements_check)

    is_question_popup = False
    if elements_check and elements_check.get('hasQuestion'):
        question_text = elements_check.get('questionText', '')

        if len(question_text) > 0:
            is_question_popup = True
            write_question_to_file(question_text)

            answer_list = util.get_answer_list_from_user_guess_string(config_dict, CONST_MAXBOT_ANSWER_ONLINE_FILE)
            if len(answer_list) == 0 and config_dict["advanced"]["auto_guess_options"]:
                answer_list = util.get_answer_list_from_question_string(None, question_text, config_dict)

            inferred_answer_string = ""
            for answer_item in answer_list:
                if answer_item not in fail_list:
                    inferred_answer_string = answer_item
                    break

            if len(answer_list) > 0:
                answer_list = list(dict.fromkeys(answer_list))

            debug.log(f"[KKTIX] 推論答案: {inferred_answer_string}")
            debug.log(f"[KKTIX] 問題: {question_text}")

            # 優化的填寫流程 - 使用指數退避
            if len(inferred_answer_string) > 0 and elements_check.get('hasInput'):
                async def fill_captcha():
                    await _smart_sleep(random.uniform(0.3, 0.8))
                    
                    fill_result = await tab.evaluate(f'''
                        (function() {{
                            const input = document.querySelector('div.custom-captcha-inner > div > div > input');
                            if (!input || input.disabled || input.readOnly) {{
                                return {{ success: false, error: "輸入欄位不可用" }};
                            }}

                            input.focus();
                            input.value = "{inferred_answer_string}";
                            input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            input.blur();

                            return {{ success: true, value: input.value }};
                        }})();
                    ''')
                    
                    return util.parse_nodriver_result(fill_result)

                try:
                    fill_result = await _retry_with_backoff(fill_captcha, max_retries=2)
                    
                    if fill_result and fill_result.get('success'):
                        debug.log(f"[KKTIX] 驗證碼已填寫")
                        
                        await _smart_sleep(random.uniform(0.5, 1.0))
                        button_clicked = await nodriver_kktix_press_next_button(tab, config_dict)
                        
                        if button_clicked:
                            success = True
                            await _smart_sleep(random.uniform(0.5, 1.0))
                            fail_list.append(inferred_answer_string)
                except Exception as exc:
                    debug.log(f"[KKTIX] 驗證碼處理失敗: {exc}")

    return fail_list, is_question_popup, success

async def nodriver_kktix_date_auto_select(tab, config_dict):
    """日期自動選擇 - 優化版本"""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)

    if not config_dict["date_auto_select"]["enable"]:
        debug.log("[KKTIX DATE SELECT] 主開關已關閉")
        return False

    auto_select_mode = config_dict["date_auto_select"]["mode"]
    date_keyword = config_dict["date_auto_select"]["date_keyword"].strip()
    date_auto_fallback = config_dict.get('date_auto_fallback', False)

    # 使用並行查詢優化 - 同時檢查多個可能性
    session_list = None
    direct_button = None
    event_list_container = None
    
    try:
        session_list, direct_button, event_list_container = await asyncio.gather(
            tab.query_selector_all('div.event-list ul.clearfix > li'),
            tab.query_selector('.tickets > a.btn-point'),
            tab.query_selector('div.event-list'),
            return_exceptions=True
        )
        
        # 過濾異常
        session_list = session_list if not isinstance(session_list, Exception) and session_list else None
        direct_button = direct_button if not isinstance(direct_button, Exception) else None
        event_list_container = event_list_container if not isinstance(event_list_container, Exception) else None
        
    except Exception:
        pass

    # 單一工作階段頁面檢查
    if not event_list_container and direct_button:
        debug.log("[KKTIX DATE] 單場次頁面偵測 (無日期選擇)")
        return False

    if not session_list or len(session_list) == 0:
        debug.log("[KKTIX DATE] 無工作階段列表")
        return False

    debug.log(f"[KKTIX DATE] 找到 {len(session_list)} 場次")

    # 批次處理工作階段
    formated_session_list = []
    formated_session_list_text = []

    for session_item in session_list:
        try:
            date_text = None
            
            # 優先級 1: span.timezoneSuffix
            try:
                date_elem = await session_item.query_selector('span.timezoneSuffix')
                if date_elem:
                    date_text = await date_elem.get_html()
                    date_text = util.remove_html_tags(date_text).strip()
            except Exception:
                pass

            # 後備: .event-info > a > p
            if not date_text:
                try:
                    date_elem = await session_item.query_selector('.event-info > a > p')
                    if date_elem:
                        date_text = await date_elem.get_html()
                        date_text = util.remove_html_tags(date_text).strip()
                except Exception:
                    pass

            button_elem = await session_item.query_selector('div.content > a.btn-point')

            if date_text and button_elem:
                if not util.reset_row_text_if_match_keyword_exclude(config_dict, date_text):
                    formated_session_list.append(button_elem)
                    formated_session_list_text.append(date_text)
                    debug.log(f"[KKTIX DATE] 可用場次: {date_text}")
        except Exception as exc:
            debug.log(f"[KKTIX DATE] 工作階段處理錯誤: {exc}")
            continue

    if len(formated_session_list) == 0:
        debug.log("[KKTIX DATE] 沒有符合的場次")
        return False

    # 關鍵字優先匹配
    matched_blocks = None

    if not date_keyword:
        matched_blocks = formated_session_list
    else:
        matched_blocks = []
        try:
            keyword_array = json.loads("[" + date_keyword + "]")

            for keyword_index, keyword_item_set in enumerate(keyword_array):
                for i, session_text in enumerate(formated_session_list_text):
                    normalized_session = re.sub(r'\s+', ' ', session_text)
                    is_match = False

                    if isinstance(keyword_item_set, str):
                        normalized_keyword = re.sub(r'\s+', ' ', keyword_item_set)
                        is_match = normalized_keyword in normalized_session
                    elif isinstance(keyword_item_set, list):
                        normalized_keywords = [re.sub(r'\s+', ' ', kw) for kw in keyword_item_set]
                        is_match = all(kw in normalized_session for kw in normalized_keywords)

                    if is_match:
                        matched_blocks = [formated_session_list[i]]
                        debug.log(f"[KKTIX DATE KEYWORD] 關鍵字 #{keyword_index + 1} 符合")
                        break

                if matched_blocks:
                    break

        except Exception as e:
            debug.log(f"[KKTIX DATE KEYWORD] 解析錯誤: {e}")

    # 後備機制
    if (not matched_blocks or len(matched_blocks) == 0) and date_keyword and date_auto_fallback:
        debug.log("[KKTIX DATE FALLBACK] 觸發自動後備")
        matched_blocks = formated_session_list

    # 點擊選擇的場次
    target_button = util.get_target_item_from_matched_list(matched_blocks, auto_select_mode)

    is_date_clicked = False
    if target_button:
        try:
            await target_button.click()
            is_date_clicked = True
            debug.log("[KKTIX DATE SELECT] 場次選擇完成")
        except Exception as exc:
            debug.log(f"[KKTIX DATE SELECT] 點擊失敗: {exc}")

    return is_date_clicked

async def nodriver_kktix_press_next_button(tab, config_dict=None):
    """點擊下一步按鈕 - 優化版本，減少重複評估"""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)

    for retry_count in range(2):  # 減少重試次數
        try:
            if retry_count > 0:
                await _smart_sleep(0.3)

            result = await tab.evaluate('''
                (function() {
                    const buttons = document.querySelectorAll('div.register-new-next-button-area > button');
                    if (buttons.length === 0) {
                        return { success: false, error: '找不到按鈕' };
                    }

                    const targetButton = buttons[buttons.length - 1];
                    const buttonText = targetButton.innerText || targetButton.textContent || '';
                    const isDisabled = targetButton.disabled || 
                                      targetButton.classList.contains('disabled');
                    const isProcessing = ['查詢空位中', '處理中', '請稍候'].some(t => buttonText.includes(t));

                    if (isDisabled && isProcessing) {
                        return { success: true, processing: true, buttonText: buttonText };
                    }
                    if (isDisabled) {
                        return { success: false, error: '按鈕已禁用', buttonText: buttonText };
                    }

                    // 觸發事件
                    const event = new MouseEvent('click', { bubbles: true, cancelable: true, view: window });
                    targetButton.scrollIntoView({ behavior: 'instant', block: 'center' });
                    targetButton.focus();
                    targetButton.dispatchEvent(event);

                    return { success: true, clicked: true, buttonText: buttonText };
                })();
            ''')

            result = util.parse_nodriver_result(result)

            if result and result.get('success'):
                button_text = result.get('buttonText', '').strip()
                
                if result.get('processing'):
                    debug.log(f"[KKTIX] 處理中: [{button_text}]")
                    await _smart_sleep(1.0)
                    return True
                else:
                    debug.log(f"[KKTIX] 已點擊下一步")
                    await _smart_sleep(0.2)
                    return True
            else:
                if retry_count < 1:
                    await _smart_sleep(0.5)
                    continue

        except Exception as exc:
            debug.log(f"[KKTIX] 點擊錯誤 (重試 {retry_count + 1}): {exc}")

    debug.log("[KKTIX] 點擊失敗")
    return False

async def nodriver_kktix_check_ticket_page_status(tab, config_dict=None):
    """檢查票券頁面狀態 - 優化版本"""
    debug = util.create_debug_logger(config_dict)

    try:
        page_state = await tab.evaluate('''
            () => {
                const ticketArea = document.querySelector('#registrationsNewApp') || document.body;
                const ticketUnits = Array.from(ticketArea.querySelectorAll('.ticket-unit'));

                if (ticketUnits.length === 0) {
                    return { allSoldOut: false, allNotYetOpen: false };
                }

                const notYetOpenKws = ['尚未開賣', '未開賣', '尚未開始', 'Not Started', 'まだ発売'];
                const soldOutKws = ['售完', '已售完', 'Sold Out', '完売'];

                let notYetCount = 0, soldOutCount = 0;

                for (const unit of ticketUnits) {
                    const qty = unit.querySelector('.ticket-quantity')?.textContent || '';
                    if (notYetOpenKws.some(kw => qty.includes(kw))) notYetCount++;
                    else if (soldOutKws.some(kw => qty.includes(kw))) soldOutCount++;
                }

                const total = ticketUnits.length;
                return {
                    allSoldOut: soldOutCount === total && total > 0,
                    allNotYetOpen: notYetCount === total && total > 0,
                    stats: { total, notYetOpen: notYetCount, soldOut: soldOutCount }
                };
            }
        ''')

        page_state = util.parse_nodriver_result(page_state)

        if page_state and (page_state.get('allSoldOut') or page_state.get('allNotYetOpen')):
            status = "全部售完" if page_state.get('allSoldOut') else "全部未開賣"
            debug.log(f"[KKTIX STATUS] {status}, 將重新載入")
            return True

    except Exception as exc:
        debug.log(f"[KKTIX STATUS] 檢查失敗: {exc}")

    return False

async def nodriver_kktix_reg_new_main(tab, config_dict, fail_list, played_sound_ticket):
    """購票主流程 - 優化版本"""
    if await check_and_handle_pause(config_dict):
        return fail_list, played_sound_ticket

    debug = util.create_debug_logger(config_dict)

    _state.reg_execution_count += 1
    if _state.reg_execution_count % 10 == 0:  # 每 10 次執行才記錄一次
        debug.log(f"[KKTIX REG] 執行次數: {_state.reg_execution_count}")

    if not config_dict["area_auto_select"]["enable"]:
        debug.log("[KKTIX AREA SELECT] 主開關已關閉")
        return fail_list, played_sound_ticket

    area_keyword = config_dict["area_auto_select"]["area_keyword"].strip()
    auto_select_mode = config_dict["area_auto_select"]["mode"]
    area_auto_fallback = config_dict.get('area_auto_fallback', False)

    try:
        registrationsNewApp_div = await tab.query_selector('#registrationsNewApp')
    except Exception:
        registrationsNewApp_div = None

    if registrationsNewApp_div:
        is_dom_ready = True
        is_need_refresh = await nodriver_kktix_check_ticket_page_status(tab, config_dict)

        if len(area_keyword) > 0:
            area_keyword_array = []
            try:
                area_keyword_array = json.loads("[" + area_keyword + "]")
            except Exception:
                area_keyword_array = []

            is_need_refresh_final = True

            for area_keyword_item in area_keyword_array:
                is_dom_ready, is_ticket_number_assigned, is_need_refresh_tmp = await nodriver_kktix_assign_ticket_number(
                    tab, config_dict, area_keyword_item
                )

                if not is_dom_ready:
                    break

                if not is_need_refresh_tmp:
                    is_need_refresh_final = False

                if is_ticket_number_assigned:
                    break

            # 後備邏輯
            if not is_ticket_number_assigned and is_need_refresh_final:
                if area_auto_fallback:
                    debug.log("[KKTIX AREA FALLBACK] 觸發自動後備")
                    is_dom_ready, is_ticket_number_assigned, is_need_refresh = await nodriver_kktix_assign_ticket_number(
                        tab, config_dict, ""
                    )

        else:
            is_dom_ready, is_ticket_number_assigned, is_need_refresh = await nodriver_kktix_assign_ticket_number(
                tab, config_dict, ""
            )

        if is_dom_ready and is_ticket_number_assigned:
            if await check_and_handle_pause(config_dict):
                return fail_list, played_sound_ticket

            # 填寫會員序號
            await nodriver_kktix_order_member_code(tab, config_dict)

            if not played_sound_ticket:
                if config_dict["advanced"]["play_sound"]["ticket"]:
                    play_sound_while_ordering(config_dict)
            played_sound_ticket = True

            # 驗證碼處理
            fail_list, is_question_popup, button_clicked = await nodriver_kktix_reg_captcha(
                tab, config_dict, fail_list, registrationsNewApp_div
            )

            if await check_and_handle_pause(config_dict):
                return fail_list, played_sound_ticket

            if not is_question_popup:
                await nodriver_kktix_check_guest_modal(tab, config_dict, force_check=True)

                # 嘗試點擊下一步
                if not button_clicked and config_dict["kktix"].get("auto_press_next_step_button", True):
                    await nodriver_kktix_press_next_button(tab, config_dict)

        elif is_need_refresh:
            played_sound_ticket = False
            debug.log("[KKTIX] 無符合票券，刷新頁面")
            await _smart_sleep(config_dict["advanced"].get("auto_reload_page_interval", 3))
            try:
                await tab.reload()
            except Exception:
                pass

    return fail_list, played_sound_ticket

def check_kktix_got_ticket(url, config_dict):
    """檢查是否成功購票"""
    debug = util.create_debug_logger(config_dict)
    
    is_kktix_got_ticket = False
    if '/events/' in url and '/registrations/' in url and "-" in url and '/registrations/new' not in url and '#/booking' not in url:
        is_kktix_got_ticket = True
        debug.log(f"[KKTIX] 成功頁面: {url}")

    return is_kktix_got_ticket

async def nodriver_kktix_main(tab, url, config_dict):
    """KKTIX 主執行函數 - 優化版本"""
    debug = util.create_debug_logger(config_dict)

    # 全域警告處理 - 自動關閉售罄警告
    async def handle_kktix_alert(event):
        if os.path.exists(CONST_MAXBOT_INT28_FILE):
            return

        debug.log(f"[KKTIX ALERT] 警告: '{event.message}'")

        dangerous_keywords = ["取消", "不保留"]
        is_dangerous = any(kw in event.message for kw in dangerous_keywords)
        should_accept = not is_dangerous

        sold_out_keywords = ["售完", "已售完", "別人搶先一步", "已無可配座位", "失敗"]
        is_sold_out = any(kw in event.message for kw in sold_out_keywords)
        
        if is_sold_out:
            _state.alert_needs_reload = True

        for attempt in range(2):
            try:
                await tab.send(cdp.page.handle_java_script_dialog(accept=should_accept))
                debug.log(f"[KKTIX ALERT] 已{'接受' if should_accept else '關閉'}")
                break
            except Exception:
                if attempt < 1:
                    await _smart_sleep(0.1)

    # 註冊警告處理 (只一次)
    if not _state.alert_handler_registered:
        try:
            tab.add_handler(cdp.page.JavascriptDialogOpening, handle_kktix_alert)
            _state.alert_handler_registered = True
        except Exception:
            pass

    # 登入流程
    if '/users/sign_in?' in url:
        await nodriver_kktix_signin(tab, url, config_dict)
        try:
            url = await tab.evaluate('window.location.href')
        except Exception:
            pass

    if '/users/sign_in?' not in url:
        # 等候頁面 (自動重新載入)
        await nodriver_kktix_check_queue_page(tab, config_dict)

        if '#/booking' in url:
            await nodriver_kktix_booking_main(tab, config_dict)
        elif '/registrations/new' in url:
            if _state.alert_needs_reload:
                _state.alert_needs_reload = False
                _state.played_sound_ticket = False
                try:
                    await tab.reload()
                except Exception:
                    pass
                return False

            await nodriver_kktix_check_guest_modal(tab, config_dict)

            if await nodriver_kktix_dismiss_failure_modal(tab, config_dict):
                _state.played_sound_ticket = False
                _state.clear_cache()
                try:
                    await tab.reload()
                except Exception as exc:
                    debug.log(f"[KKTIX] 售罄提示關閉後重新載入失敗: {exc}")
                return False
            
            _state.start_time = time.time()

            is_dom_ready = False
            try:
                html_body = await tab.get_content()
                if html_body and len(html_body) > 10240 and "registrationsNewApp" in html_body:
                    is_dom_ready = True
            except Exception:
                pass

            if not is_dom_ready:
                _state.reset_for_new_session()
                await nodriver_kktix_check_queue_page(tab, config_dict)
            else:
                if await nodriver_kktix_redirect_to_signin_if_guest(tab, url, config_dict):
                    return False

                await nodriver_check_checkbox(tab, '#person_agree_terms:not(:checked)')

                if config_dict["kktix"]["auto_fill_ticket_number"]:
                    _state.fail_list, _state.played_sound_ticket = await nodriver_kktix_reg_new_main(
                        tab, config_dict, _state.fail_list, _state.played_sound_ticket
                    )
                    _state.done_time = time.time()

        else:
            if '/events/' in url and len(url.split('/')) <= 5:
                # 活動頁面 - 日期選擇或購票
                is_date_selected = False
                if config_dict["date_auto_select"]["enable"]:
                    is_date_selected = await nodriver_kktix_date_auto_select(tab, config_dict)

                if not is_date_selected and config_dict["kktix"].get("auto_press_next_step_button", True):
                    await nodriver_kktix_events_press_next_button(tab, config_dict)

            _state.reset_for_new_session()

    # 檢查是否成功
    is_kktix_got_ticket = False
    if not _state.got_ticket_detected:
        is_kktix_got_ticket = check_kktix_got_ticket(url, config_dict)
        if is_kktix_got_ticket:
            _state.got_ticket_detected = True
    elif _state.got_ticket_detected:
        is_kktix_got_ticket = True

    is_quit_bot = False
    if is_kktix_got_ticket:
        is_quit_bot = True

        if not _state.success_actions_done:
            if _state.start_time and _state.done_time:
                elapsed = _state.done_time - _state.start_time
                debug.log(f"[KKTIX] 購票完成, 用時: {elapsed:.3f} 秒")

            if not _state.played_sound_order:
                if config_dict["advanced"]["play_sound"]["order"]:
                    play_sound_while_ordering(config_dict)
                send_discord_notification(config_dict, "order", "KKTIX")
                send_telegram_notification(config_dict, "order", "KKTIX")
                _state.played_sound_order = True

            _state.success_actions_done = True

    return is_quit_bot

# 簡化版本的支援函數 (完整版保留原始 docstring)

async def nodriver_kktix_events_press_next_button(tab, config_dict=None):
    """點擊活動頁面的購票按鈕"""
    if await check_and_handle_pause(config_dict):
        return False
    try:
        result = await tab.evaluate('''
            (function() {
                const button = document.querySelector('.tickets > a.btn-point');
                if (button) {
                    button.scrollIntoView({ behavior: 'instant', block: 'center' });
                    button.click();
                    return { success: true };
                }
                return { success: false };
            })()
        ''')
        return util.parse_nodriver_result(result).get('success', False)
    except Exception:
        return False

async def nodriver_kktix_check_guest_modal(tab, config_dict, force_check=False):
    """檢查及關閉訪客模態"""
    if await check_and_handle_pause(config_dict):
        return False

    debug = util.create_debug_logger(config_dict)
    
    is_first_check = not _state.guest_modal_checked
    current_time = time.time()
    
    if _state and not force_check and not is_first_check:
        if current_time - _state.guest_modal_last_check_time < 2:
            return False
    
    _state.guest_modal_last_check_time = current_time

    try:
        if is_first_check:
            await _smart_sleep(0.4)
            _state.guest_modal_checked = True

        modal_state = await tab.evaluate('''
            (function() {
                const modal = document.querySelector('#guestModal');
                if (!modal) return { status: 'missing' };
                const style = window.getComputedStyle(modal);
                return { status: style.display !== 'none' ? 'visible' : 'hidden' };
            })()
        ''')
        
        modal_state = util.parse_nodriver_result(modal_state)

        if modal_state and modal_state.get('status') == 'visible':
            debug.log("[KKTIX GUEST MODAL] 偵測到訪客模態")
            try:
                await tab.evaluate('''
                    const dismissBtn = document.querySelector('#guestModal button[data-dismiss="modal"]');
                    if (dismissBtn) dismissBtn.click();
                ''')
                await _smart_sleep(0.4)
                return True
            except Exception as exc:
                debug.log(f"[KKTIX GUEST MODAL] 關閉失敗: {exc}")

    except Exception as exc:
        debug.log(f"[KKTIX GUEST MODAL] 錯誤: {exc}")

    return False


async def nodriver_kktix_dismiss_failure_modal(tab, config_dict):
    """Dismiss sold-out/race-condition Bootstrap modals in one DOM operation."""
    debug = util.create_debug_logger(config_dict)
    try:
        result = await tab.evaluate('''
            (function() {
                const failureTexts = [
                    '已售完', '售完', '別人搶先一步', '已無可配座位',
                    '無可配座位', '已被購買', '購票失敗', '失敗', '錯誤'
                ];
                const buttonTexts = ['確定', 'OK', 'Ok', '知道了', '我知道了', 'close', 'Close'];
                const modals = document.querySelectorAll('.modal.in, .modal.show');
                for (const modal of modals) {
                    const text = (modal.textContent || '').trim();
                    if (!failureTexts.some(item => text.includes(item))) continue;
                    for (const button of modal.querySelectorAll('button, a.btn')) {
                        const buttonText = (button.textContent || '').trim();
                        if (
                            buttonTexts.some(item => buttonText.includes(item)) ||
                            button.getAttribute('data-dismiss') === 'modal'
                        ) {
                            button.click();
                            return { found: true, clicked: true, text: text.slice(0, 80) };
                        }
                    }
                    return { found: true, clicked: false, text: text.slice(0, 80) };
                }
                return { found: false, clicked: false, text: '' };
            })()
        ''')
        result = util.parse_nodriver_result(result)
        if isinstance(result, dict) and result.get("found"):
            debug.log(f"[KKTIX MODAL] 偵測到失敗提示: {result.get('text', '')}")
            return True
    except Exception as exc:
        debug.log(f"[KKTIX MODAL] 檢查失敗提示時發生錯誤: {exc}")
    return False


async def nodriver_kktix_booking_main(tab, config_dict):
    """座位選擇頁面自動化"""
    debug = util.create_debug_logger(config_dict)
    ret = False

    try:
        # 關閉資訊模態
        modal_visible = await tab.evaluate('''
            (function() {
                const m = document.querySelector('#infoModal');
                return m && window.getComputedStyle(m).display !== 'none';
            })()
        ''')
        
        if modal_visible:
            info_btn = await tab.query_selector('#infoModal .modal-footer button')
            if info_btn:
                await info_btn.click()
                debug.log("[KKTIX BOOKING] 已關閉資訊模態")
                await _smart_sleep(0.4)
                return ret

        # 點擊座位確認
        confirm_btn = await tab.query_selector('.btn-group-for-seat button.dropdown-toggle')
        if confirm_btn:
            is_open = await tab.evaluate('''
                (function() {
                    const g = document.querySelector('.btn-group-for-seat');
                    return g && g.classList.contains('open');
                })()
            ''')

            if not is_open:
                await confirm_btn.click()
                await _smart_sleep(0.2)

            done_btn = await tab.query_selector('a[ng-click="done()"]')
            if done_btn:
                await done_btn.click()
                debug.log("[KKTIX BOOKING] 座位已確認")
                ret = True
    except Exception as exc:
        debug.log(f"[KKTIX BOOKING] 錯誤: {exc}")

    return ret

async def nodriver_kktix_confirm_order_button(tab, config_dict):
    """點擊訂單確認按鈕"""
    debug = util.create_debug_logger(config_dict)
    ret = False

    try:
        confirm_button = await tab.query_selector('div.form-actions a.btn-primary')
        if confirm_button:
            is_enabled = await tab.evaluate('''
                (button) => {
                    return button && !button.disabled && button.offsetParent !== null;
                }
            ''', confirm_button)

            if is_enabled:
                await confirm_button.click()
                ret = True
                debug.log("[KKTIX] 訂單確認按鈕已點擊")
    except Exception as exc:
        debug.log(f"[KKTIX] 訂單確認失敗: {exc}")

    return ret

async def nodriver_kktix_order_member_code(tab, config_dict):
    """填寫會員序號"""
    debug = util.create_debug_logger(config_dict)

    if await check_and_handle_pause(config_dict):
        return False

    member_code = config_dict["advanced"].get("discount_code", "").strip()

    if not member_code:
        return False

    try:
        escaped_code = member_code.replace("\\", "\\\\").replace("'", "\\'")
        await _smart_sleep(0.2)

        result = await tab.evaluate(f'''
            (function() {{
                let filledCount = 0;
                const inputs = document.querySelectorAll('input.member-code');
                
                for (let input of inputs) {{
                    if (!input.value && !input.disabled) {{
                        input.value = '{escaped_code}';
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                        filledCount++;
                    }}
                }}
                
                return {{ success: filledCount > 0, filledCount: filledCount }};
            }})()
        ''')

        result = util.parse_nodriver_result(result)
        if result and result.get('success'):
            debug.log(f"[KKTIX MEMBER CODE] 已填寫 {result.get('filledCount')} 個欄位")
            return True

    except Exception as exc:
        debug.log(f"[KKTIX MEMBER CODE] 錯誤: {exc}")

    return False

async def debug_kktix_page_state(tab, show_debug=True):
    """收集 KKTIX 頁面狀態供除錯"""
    debug = util.create_debug_logger(enabled=show_debug)
    try:
        state = await tab.evaluate('''
            (function() {
                return {
                    url: window.location.href,
                    title: document.title,
                    readyState: document.readyState,
                    hasRegistrationDiv: !!document.querySelector('#registrationsNewApp'),
                    hasTicketAreas: document.querySelectorAll('div.display-table-row').length,
                    hasQuestion: !!document.querySelector('div.custom-captcha-inner p'),
                    questionText: document.querySelector('div.custom-captcha-inner p')?.innerText || '',
                    nextButtons: document.querySelectorAll('div.register-new-next-button-area > button').length,
                    timestamp: new Date().toISOString()
                };
            })();
        ''')

        state = util.parse_nodriver_result(state)

        if state and debug.enabled:
            debug.log("=== KKTIX 頁面狀態 ===")
            debug.log(f"URL: {state.get('url', 'N/A')}")
            debug.log(f"狀態: {state.get('readyState', 'N/A')}")
            debug.log(f"票券表單: {state.get('hasRegistrationDiv', False)}")
            debug.log(f"票券區域: {state.get('hasTicketAreas', 0)}")
            debug.log(f"驗證碼: {state.get('hasQuestion', False)}")
            debug.log(f"下一步按鈕: {state.get('nextButtons', 0)}")

        return state

    except Exception as exc:
        debug.log(f"[KKTIX DEBUG] 失敗: {exc}")
        return None
