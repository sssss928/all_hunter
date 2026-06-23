"""SPA-aware DOM interaction with state verification."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from core.retry import RetryPolicy


class DOMController:
    """Centralize bounded DOM queries, interactability checks, and mutation waits."""

    def __init__(self, tab: Any, retry_policy: RetryPolicy | None = None) -> None:
        self.tab = tab
        self.retry_policy = retry_policy or RetryPolicy()

    async def wait_for_mutation(
        self,
        predicate_js: str,
        *,
        timeout: float = 5.0,
    ) -> bool:
        script = """
        (() => new Promise(resolve => {
          const predicate = () => {
            try { return Boolean(__PREDICATE__); } catch (_) { return false; }
          };
          if (predicate()) return resolve(true);
          const observer = new MutationObserver(() => {
            if (predicate()) {
              observer.disconnect();
              resolve(true);
            }
          });
          observer.observe(document.documentElement, {
            childList: true, subtree: true, attributes: true
          });
          setTimeout(() => {
            observer.disconnect();
            resolve(predicate());
          }, __TIMEOUT__);
        }))()
        """
        result = await self.tab.evaluate(
            script.replace("__PREDICATE__", predicate_js).replace(
                "__TIMEOUT__",
                str(max(1, int(timeout * 1000))),
            )
        )
        return bool(result)

    async def click_when_ready(
        self,
        selector: str,
        *,
        expected_change_js: str | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        selector_json = json.dumps(selector)
        script = f"""
        (() => {{
          const el = document.querySelector({selector_json});
          if (!el || !el.isConnected) return {{clicked:false, reason:'selector_missing'}};
          const style = getComputedStyle(el);
          const rect = el.getBoundingClientRect();
          if (style.display === 'none' || style.visibility === 'hidden' ||
              style.pointerEvents === 'none' || rect.width <= 0 || rect.height <= 0) {{
            return {{clicked:false, reason:'not_interactable'}};
          }}
          if (el.disabled || el.getAttribute('aria-disabled') === 'true') {{
            return {{clicked:false, reason:'disabled'}};
          }}
          const x = rect.left + rect.width / 2;
          const y = rect.top + rect.height / 2;
          const top = document.elementFromPoint(x, y);
          if (top && top !== el && !el.contains(top)) {{
            return {{clicked:false, reason:'overlay_blocked'}};
          }}
          el.scrollIntoView({{block:'center', behavior:'instant'}});
          el.click();
          return {{clicked:true, reason:'clicked'}};
        }})()
        """
        deadline = asyncio.get_running_loop().time() + timeout
        attempt = 1
        last_result: dict[str, Any] = {
            "clicked": False,
            "reason": "timeout",
        }
        while self.retry_policy.allows(attempt):
            raw = await self.tab.evaluate(script)
            if isinstance(raw, dict):
                last_result = raw
            if last_result.get("clicked"):
                if not expected_change_js:
                    return last_result
                changed = await self.wait_for_mutation(
                    expected_change_js,
                    timeout=max(0.1, deadline - asyncio.get_running_loop().time()),
                )
                if changed:
                    last_result["state_changed"] = True
                    return last_result
                last_result = {
                    "clicked": False,
                    "reason": "state_not_updated",
                }
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(
                min(remaining, self.retry_policy.delay_for(attempt))
            )
            attempt += 1
        return last_result
