# MyHunter 1.2.0 重構與驗證報告

日期：2026-06-23

## 完成範圍

- 檢查並修正 `src/` 下所有 Python 程式。
- `src/platforms/` 實際包含 12 個售票平台模組；部分模組同時涵蓋多個網站家族，因此 UI 列出的支援網站數量較多。
- 重構 NOL World / Global Interpark 的 Notice、Buy Now、登入、日期、時間、Next、排隊、座位競爭失敗、10 分鐘工作階段與安全驗證交接。
- 更新設定介面、說明面板、設定範本、CI、Release、PyInstaller 與 source 發布流程。

## 主要架構改善

1. 平台路由
   - 由 hostname 判斷平台，避免 query string 造成誤判。
   - 使用 LRU cache，主迴圈相同 URL 的 1,000 次測試為 1 次解析、999 次快取命中。

2. 工作流程狀態
   - 新增可追蹤轉換歷程的狀態機。
   - 單一平台例外會記錄並有限退避，不會直接中止整個執行程式。

3. DOM 操作
   - 新增 MutationObserver 等待與可操作性檢查。
   - 點擊前確認可見、啟用、未被遮罩攔截，並在操作後確認狀態真的改變。
   - 固定延遲只作為回退，降低 React／SPA 尚未渲染完成造成的漏點與卡頓。

4. 可觀測性與安全
   - 新增 JSON 結構化日誌及 trace ID。
   - 密碼、token、cookie、授權標頭、webhook 等欄位會自動遮蔽。
   - 設定範本不含真實帳密或通知憑證。

## NOL World 改善

- 首次啟動與後續輪次都會處理 Notice，再等待遮罩消失後點擊 Buy Now。
- 日期、時間與 Next 分步確認；各步驟以 DOM 變化為主要等待條件。
- 滑動拼圖 `.captchSliderInner` 與 hCaptcha 等受保護驗證會被偵測並進入 handoff/resume；不執行自動拖滑破解。
- 排隊頁不重新整理；活動監控與座位區塊掃描採獨立間隔。
- NOL 模式隱藏通用 `Auto reload interval`，建議值為：
  - NOL check interval：5 秒
  - NOL block scan delay：600 ms
- 座位已被選走或競爭失敗時，關閉提示、移除失效選擇並重新掃描。
- 購票工作階段滿 10 分鐘後回到活動流程開始新一輪；排隊時間不計入。
- Turnstile、圖片 CAPTCHA、滑塊或其他官方保護驗證採 handoff/resume：保留頁面與流程狀態，合法完成後自動續跑。
- 付款保持由使用者確認，避免程式誤購或越過付款授權。

## 全平台程式清理

- 修正 103 個裸露 `except:`。
- 修正 57 個錯誤的 `not in`／`is not` 判斷形式。
- 移除 59 個未使用 import，縮短啟動載入並降低相依副作用。
- 修正布林值直接比較等 correctness 問題。
- 12 個平台模組已逐一實際 import 成功。
- 保留既有圖片 CAPTCHA/OCR 平台程式碼；TixCraft 另補上手動輸入完成後的可靠送出 helper。

## 驗證結果

- Python 測試：46 passed。
- Ruff correctness：All checks passed。
- Python compileall：通過。
- 設定頁 JavaScript 語法：通過。
- NOL 日期／時間／Next DOM 狀態測試：通過。
- GitHub Actions YAML：可由 PyYAML 正常解析。
- PyInstaller：
  - `nodriver_tixcraft.exe --help`：通過。
  - `settings.exe`：可提供 `/settings.html` 與設定 API。
- 實際發布包 UI：
  - 選擇 NOL homepage 後顯示 NOL 專屬控制。
  - Autofill 顯示 Email 與 Password。
  - NOL security handoff 預設啟用。
  - 通用自動刷新欄位在 NOL 模式隱藏。
  - OCR auto-submit 預設關閉。
  - 右側功能說明面板可正常開啟。

## 發布成品

- Windows x64 可執行包與 SHA-256。
- 完整 source ZIP 與 SHA-256。
- Source staging 會檢查必要檔案與最少檔案數，避免發布只含空資料夾的壓縮檔。
- GitHub Actions：
  - push / pull request 執行 CI。
  - 建立 `vX.Y.Z` tag 後自動建置 Windows 包與 source 包並發布 Release。

## 邊界與維護提醒

- 第三方售票網站可隨時調整 DOM、API、排隊或驗證機制，因此無法承諾未來永久零維護。
- 本次已對目前程式、公開頁面狀態與可重現測試完成驗證；正式活動、帳號限制、地區限制及付款仍以官方網站當下行為為準。
- 官方安全驗證不應以 token 注入或偽造方式繞過。Cloudflare 文件指出 Turnstile token 需由使用者端產生並由網站伺服器驗證，且具時效與單次使用限制。

## 工程參考

- Playwright actionability / auto-waiting：<https://playwright.dev/docs/actionability>
- Cloudflare Turnstile server-side validation：<https://developers.cloudflare.com/turnstile/get-started/server-side-validation/>
- Cloudflare Turnstile testing：<https://developers.cloudflare.com/turnstile/troubleshooting/testing/>
