# LINE 私人小助手

用 LINE 跟 Claude 聊天的私人助理，只回應你自己（白名單）。

能做的事：

- **聊天問答**：翻譯、改寫、擬訊息、整理重點，會記得最近 20 輪對話。
- **提醒**：「明天早上 8 點提醒我帶健保卡」，時間到用 LINE 推播通知；可查看、取消。
- **筆記**：「記一下：車停在 B2 C17」→ 之後問「我車停哪？」。
- **網路搜尋**：天氣、新聞、營業時間等即時資訊（可關閉）。

指令：`/help` 說明、`/reset` 清除對話記憶（筆記與提醒保留）、`/id` 顯示自己的 LINE user ID。

## 架構

```
LINE App ──► LINE 平台 ──POST /callback──► FastAPI (app.py)
                                              │ 驗證簽章、白名單
                                              ▼
                                       Assistant (assistant.py) ──► Claude API
                                              │  工具：提醒／筆記 (tools.py)、web_search
                                              ▼
                                       SQLite (storage.py)
                                              ▲
                          每 30 秒檢查到期提醒 ─┘──► LINE push
```

| 檔案 | 用途 |
| --- | --- |
| `app.py` | webhook 入口、指令、白名單、提醒排程 |
| `assistant.py` | system prompt、Claude 呼叫與工具迴圈 |
| `tools.py` | 提醒與筆記工具（定義 + 實作） |
| `storage.py` | SQLite：對話、筆記、提醒 |
| `line_api.py` | LINE 簽章驗證、reply / push、讀取中動畫、長訊息切段 |
| `config.py` | 環境變數設定 |

## 設定步驟

### 1. 建立 LINE Messaging API channel

1. 到 [LINE Developers Console](https://developers.line.biz/console/) 建立 Provider，再建立 **Messaging API** channel。
2. **Basic settings** 頁複製 **Channel secret**。
3. **Messaging API** 頁最下方發行 **Channel access token (long-lived)**。
4. 同頁把 **Auto-reply messages** 與 **Greeting messages** 關掉（在 LINE Official Account Manager 裡），**Use webhook** 打開。

### 2. 取得 Claude API key

到 [Claude Console](https://platform.claude.com/) 建立 API key。

### 3. 設定環境變數

| 變數 | 必填 | 說明 |
| --- | --- | --- |
| `LINE_CHANNEL_SECRET` | ✓ | Channel secret |
| `LINE_CHANNEL_ACCESS_TOKEN` | ✓ | Channel access token |
| `ANTHROPIC_API_KEY` | ✓ | Claude API key |
| `ALLOWED_USER_IDS` | ✓ | 允許使用的 LINE user ID，多個用逗號分隔（取得方式見下方） |
| `ASSISTANT_DB_PATH` | | SQLite 檔路徑，預設 `line_assistant.db` |
| `ASSISTANT_TIMEZONE` | | 預設 `Asia/Taipei` |
| `ASSISTANT_MODEL` | | 預設 `claude-opus-5-5` |
| `ASSISTANT_EFFORT` | | `low`（預設，回得快、便宜）／`medium`／`high` |
| `ASSISTANT_HISTORY_TURNS` | | 記住幾輪對話，預設 20 |
| `ASSISTANT_WEB_SEARCH` | | 設 `0` 關閉網路搜尋 |

### 4. 啟動

```bash
pip install -r requirements.txt
uvicorn line_assistant.app:create_app --factory --host 0.0.0.0 --port 8000
```

本機測試可以用 [ngrok](https://ngrok.com/)：`ngrok http 8000`，把 `https://xxxx.ngrok-free.app/callback`
貼到 LINE Console 的 **Webhook URL**，按 **Verify**。

### 5. 綁定自己

1. 先**不設** `ALLOWED_USER_IDS` 啟動，用手機加 bot 好友並傳任何訊息，bot 會回覆你的 user ID。
2. 把那串 `U…` 設成 `ALLOWED_USER_IDS`，重新啟動。之後陌生人傳訊息 bot 都不會回應。

## 部署注意

- 提醒靠程序內的排程，**伺服器必須一直開著**。會自動休眠的免費方案（如 Render free）睡著時提醒會延遲到被喚醒才送。
- SQLite 檔要放在**持久化磁碟**上，否則重新部署會清空筆記與提醒（Render / Fly.io / Railway 都可掛 volume，把 `ASSISTANT_DB_PATH` 指到那裡）。
- 只跑**一個**程序（不要 `--workers 2`），否則提醒可能重複送出。
- LINE 免費方案每月推播（push）有則數上限；一般回覆（reply）不計入。提醒與「Claude 想太久導致 reply token 過期」時會用到 push。

## 費用與隱私

- 每則訊息呼叫一次以上 Claude API，依 token 計費；預設 `effort=low` 並啟用 prompt cache 壓低成本。
- 對話、筆記、提醒存在你自己的 SQLite 檔。訊息內容會送到 Claude API 與 LINE 平台處理。
- 啟用了 server-side refusal fallback（`fallbacks: "default"`），主要模型婉拒時由 API 自動改用備援模型回答。

## 測試

```bash
python -m pytest tests/test_line_assistant.py
```

測試用假的 LINE 與 Claude client，不會打外部 API。
