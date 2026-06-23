# NOL World / Global Interpark 實作說明

`src/platforms/nolworld.py` 將 NOL World 與 Global Interpark 的購票流程整合到 MyHunter / zendriver。模組不需要激活碼，也不連線至私人授權伺服器。

## 執行流程

1. `login`：使用「自動填表單」中的 NOL World Email 與密碼填入官方登入頁。Cloudflare Turnstile 等互動式驗證由使用者完成，驗證完成後程式自動送出登入。
2. `checking`：在活動詳情頁依 `nolworld.check_interval` 檢查 Buy Now。NOL 不使用通用的 `advanced.auto_reload_page_interval`。
3. `pre_sale`：監看倒數／Gate 控制，不刷新官方頁面；按鈕可用後自動前進。
4. `queuing`：保留官方排隊頁並讀取進度，不重新整理頁面。
5. `booking`：依序選擇日期、時間、票價／區域、座位、張數與下一步。
6. `done`：進入訂單／付款階段後停止 NOL 自動操作並保留瀏覽器。

## 可靠性處理

- 日期、時間與 Next 採分步狀態轉移，等待前一個畫面狀態完成後才執行下一步。
- 支援新版 onestop 與舊版 Global Interpark frame 流程。
- 偵測「座位已被選擇／已被他人取得」等對話框後，自動關閉、取消失效座位並重新掃描。
- 已選座位但頁面沒有前進時，會重試 `fnNextStep('P')` 或 Next 按鈕。
- 進入購票頁後開始時限計時；達到 `nolworld.session_timeout_minutes` 仍未進入訂單／付款時，回到活動頁重新排隊。
- 票池查詢使用背景執行緒，不阻塞瀏覽器事件迴圈。

## 安全驗證 Handoff / Resume

- `nolworld.security_handoff=true` 時，偵測圖片 CAPTCHA、Turnstile 或其他受保護驗證後保留目前頁面與流程狀態。
- 程式不刷新購票頁、不丟失日期／時間／座位狀態；驗證合法完成後自動續跑。
- OCR 正規化與 confidence 工具只用於授權測試 fixture，不對真實網站安全驗證自動送出答案。

## 建議設定

| 設定 | 建議值 | 說明 |
|---|---:|---|
| `nolworld.check_interval` | `5` 秒 | 活動詳情頁監控速度與穩定性的平衡值 |
| `nolworld.block_delay_ms` | `600` ms | 日期、區域、座位與 Next 操作之間的節流 |
| `nolworld.ticket_pool_refresh_minutes` | `15` 分鐘 | 背景票池更新，不影響 Buy Now 監控 |
| `nolworld.session_timeout_minutes` | `10` 分鐘 | 購票時限結束後重新開始一輪 |
| `nolworld.auto_notice_and_buy` | `true` | 關閉 Notice 並於 Buy Now 可用時立即點擊 |
| `nolworld.security_handoff` | `true` | 保留安全驗證前後的流程狀態並自動續跑 |
| `nolworld.lock_only_mode` | 依需求 | 進入訂單／付款後保留瀏覽器給使用者操作 |

請遵守 NOL World 的服務條款與所在地法律；不要使用多帳號、多重排隊或規避網站安全控制。
