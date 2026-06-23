# 機制 15：受保護驗證偵測與 Handoff / Resume

**最後更新**：2026-06-23

## 原則

Cloudflare Turnstile、圖片 CAPTCHA、hCaptcha、滑塊及其他官方安全控制只做
偵測、狀態保留與續跑，不點擊驗證元件、不注入 token、不使用外部解題服務。

## 偵測層級

`detect_cloudflare_challenge()` 使用有限頻率檢查：

1. CDP target 是否包含 `challenges.cloudflare.com`。
2. 頁面是否存在 Turnstile iframe 或 `.cf-turnstile`。
3. HTML 是否包含全頁驗證中的明確指標。

主迴圈最多每 3 秒檢查一次；進入 handoff 後每 0.5 秒保留事件迴圈，
避免原本 50 ms 主迴圈反覆掃描整份 DOM。

## 狀態流程

```text
RUNNING
  -> SECURITY_HANDOFF (偵測到受保護驗證)
  -> WAITING          (保留 URL、排隊、日期與購票狀態)
  -> RESUMED          (官方驗證合法完成)
  -> 原平台流程
```

`verification.HandoffCoordinator` 會產生 `entered`、`waiting`、`reminder`、
`resumed` 與 `clear` 事件。結構化日誌只記錄驗證類型、平台、耗時與 URL，
不記錄 cookie、token、密碼或驗證答案。

## 嵌入式登入驗證

Cityline 與 NOL World 登入頁的嵌入式 Turnstile 不會在帳密填入前攔截平台
adapter；程式先填入使用者已授權的帳密，再等待官方驗證完成並自動送出。

## 相容函數

`handle_cloudflare_challenge()` 保留舊名稱避免既有呼叫端中斷，但現在只輪詢
驗證是否已合法完成，不再執行座標點擊、模板匹配或頁面強制刷新。
