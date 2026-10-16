"""LINE Messaging API 最小封裝：簽章驗證、reply、push、讀取中動畫。"""
import base64
import hashlib
import hmac
from typing import List

import httpx

API_BASE = "https://api.line.me/v2/bot"
MAX_TEXT_LEN = 5000      # LINE 單則文字訊息上限
MAX_MESSAGES = 5         # 單次 reply / push 最多 5 則


def verify_signature(channel_secret: str, body: bytes, signature: str) -> bool:
    if not channel_secret or not signature:
        return False
    digest = hmac.new(channel_secret.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def split_text(text: str, limit: int = MAX_TEXT_LEN, max_parts: int = MAX_MESSAGES) -> List[str]:
    """把長文字切成 LINE 可接受的多則訊息，盡量在換行處切開。"""
    text = text.strip() or "（沒有內容）"
    parts: List[str] = []
    while text and len(parts) < max_parts:
        if len(text) <= limit:
            parts.append(text)
            text = ""
            break
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:  # 超過 5 則的部分截斷並註明
        parts[-1] = parts[-1][: limit - 20].rstrip() + "\n…（內容過長已截斷）"
    return parts


class LineClient:
    def __init__(self, access_token: str, http: httpx.AsyncClient = None):
        self._http = http or httpx.AsyncClient(timeout=15.0)
        self._headers = {"Authorization": f"Bearer {access_token}"}

    async def _post(self, path: str, payload: dict) -> httpx.Response:
        resp = await self._http.post(f"{API_BASE}{path}", json=payload, headers=self._headers)
        resp.raise_for_status()
        return resp

    @staticmethod
    def _text_messages(text: str) -> List[dict]:
        return [{"type": "text", "text": t} for t in split_text(text)]

    async def reply(self, reply_token: str, text: str) -> None:
        await self._post("/message/reply", {"replyToken": reply_token, "messages": self._text_messages(text)})

    async def push(self, user_id: str, text: str) -> None:
        await self._post("/message/push", {"to": user_id, "messages": self._text_messages(text)})

    async def show_loading(self, user_id: str, seconds: int = 60) -> None:
        """一對一聊天中顯示「輸入中」動畫（5–60 秒，回覆送出時自動消失）。"""
        await self._post("/chat/loading/start", {"chatId": user_id, "loadingSeconds": seconds})

    async def aclose(self) -> None:
        await self._http.aclose()
