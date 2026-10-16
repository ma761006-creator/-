"""Claude 對話核心：組合歷史、執行工具迴圈、回傳要送給 LINE 的文字。

歷史只保存每輪的純文字（使用者訊息與最終回覆），不重播 thinking 區塊；
system prompt 與 tools 在整個程序生命週期內固定，讓 prompt cache 穩定命中。
"""
import logging
from datetime import datetime

import anthropic

from .config import Settings
from .storage import Storage
from .tools import TOOL_DEFINITIONS, ToolError, ToolExecutor

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
WEEKDAYS = "一二三四五六日"

SYSTEM_PROMPT = """你是使用者在 LINE 上的私人小助手，用繁體中文（台灣用語）回覆。

回覆方式：
- 這是手機聊天，回答要精簡、口語，先講結論；需要條列時用「・」或數字，不要用 Markdown 標題、表格或粗體符號（LINE 不會渲染）。
- 每則使用者訊息開頭的 [現在時間 …] 是系統附上的，用來換算「明天」「下週一」「兩小時後」等時間，不必複述。

你能做的事：
- 設定、查看、取消提醒（時間到會用 LINE 推播通知）。設定成功後簡短複述時間與內容。
- 幫忙記筆記、搜尋筆記、刪除筆記。
- 需要即時資訊（天氣、新聞、營業時間、匯率等）時使用網路搜尋，並附上來源名稱。
- 一般問答、翻譯、改寫、擬訊息、整理重點。

醫療、法律、財務相關問題可以提供一般性資訊，但涉及個人診斷或處置時提醒使用者諮詢專業人員。
不確定使用者的意思時，先簡短反問，不要猜。"""


class Assistant:
    def __init__(self, settings: Settings, storage: Storage, client: anthropic.AsyncAnthropic = None):
        self.settings = settings
        self.storage = storage
        self.client = client or anthropic.AsyncAnthropic()
        self.executor = ToolExecutor(storage, settings.tz)
        self.tools = list(TOOL_DEFINITIONS)
        if settings.enable_web_search:
            self.tools.append({
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 3,
                "user_location": {"type": "approximate", "country": "TW", "timezone": settings.timezone},
            })

    def _stamp(self, text: str) -> str:
        now = datetime.now(self.settings.tz)
        return f"[現在時間 {now:%Y-%m-%d %H:%M}（週{WEEKDAYS[now.weekday()]}）{self.settings.timezone}]\n{text}"

    async def _create(self, messages: list):
        return await self.client.beta.messages.create(
            model=self.settings.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=messages,
            output_config={"effort": self.settings.effort},
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )

    def _run_tools(self, user_id: str, content) -> list:
        results = []
        for block in content:
            if block.type != "tool_use":
                continue
            try:
                output = self.executor.run(user_id, block.name, dict(block.input))
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": output})
            except (ToolError, TypeError) as e:
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": str(e), "is_error": True})
        return results

    async def reply(self, user_id: str, text: str) -> str:
        user_text = self._stamp(text)
        messages = self.storage.recent_messages(user_id, self.settings.history_turns)
        messages.append({"role": "user", "content": user_text})

        try:
            answer = await self._loop(user_id, messages)
        except anthropic.RateLimitError:
            return "我現在有點忙不過來（API 流量限制），請過一分鐘再試一次。"
        except anthropic.APIConnectionError:
            return "暫時連不上 AI 服務，請稍後再試。"
        except anthropic.APIStatusError as e:
            log.exception("Claude API error (request_id=%s)", getattr(e, "request_id", None))
            return f"AI 服務發生錯誤（{e.status_code}），請稍後再試。"

        self.storage.add_message(user_id, "user", user_text)
        self.storage.add_message(user_id, "assistant", answer)
        return answer

    async def _loop(self, user_id: str, messages: list) -> str:
        for _ in range(MAX_TOOL_ROUNDS):
            resp = await self._create(messages)

            if resp.stop_reason == "refusal":
                return "抱歉，這個請求我沒辦法協助。"
            if resp.stop_reason in ("tool_use", "pause_turn"):
                messages.append({"role": "assistant", "content": resp.content})
                if resp.stop_reason == "tool_use":
                    messages.append({"role": "user", "content": self._run_tools(user_id, resp.content)})
                continue

            text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
            if resp.stop_reason == "max_tokens":
                text += "\n…（回覆太長被截斷了）"
            return text or "（我想了一下，但沒有產生回覆，請換個方式再問一次。）"

        return "這個請求步驟太多了，我先停在這裡。可以把問題拆小一點再問我嗎？"
