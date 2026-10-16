"""FastAPI 入口：LINE webhook + 提醒排程。

啟動：uvicorn line_assistant.app:create_app --factory --port 8000
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from .assistant import Assistant
from .config import Settings
from .line_api import LineClient, verify_signature
from .storage import Storage

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)
REMINDER_POLL_SECONDS = 30

HELP_TEXT = """我是你的 LINE 私人小助手，直接用自然語言跟我說就好，例如：
・明天早上 8 點提醒我帶健保卡
・我有哪些提醒？／取消提醒 3
・記一下：車停在 B2 柱子 C17
・我車停哪？
・今天台北會下雨嗎？

指令：
/help 顯示這段說明
/reset 清除對話記憶（筆記與提醒會保留）
/id 顯示你的 LINE user ID"""


async def deliver_due_reminders(storage: Storage, line: LineClient) -> int:
    sent = 0
    for r in storage.due_reminders():
        try:
            await line.push(r["user_id"], f"⏰ 提醒：{r['text']}")
        except Exception:
            log.exception("push reminder #%s failed; will retry", r["id"])
            continue
        storage.mark_reminder_sent(r["id"])
        sent += 1
    return sent


async def _reminder_loop(storage: Storage, line: LineClient) -> None:
    while True:
        try:
            await deliver_due_reminders(storage, line)
        except Exception:
            log.exception("reminder loop error")
        await asyncio.sleep(REMINDER_POLL_SECONDS)


def create_app(settings: Settings = None, storage: Storage = None, line: LineClient = None,
               assistant: Assistant = None, run_scheduler: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    storage = storage or Storage(settings.db_path)
    line = line or LineClient(settings.line_channel_access_token)
    assistant = assistant or Assistant(settings, storage)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(_reminder_loop(storage, line)) if run_scheduler else None
        yield
        if task:
            task.cancel()
        await line.aclose()

    app = FastAPI(title="LINE 私人小助手", lifespan=lifespan)

    async def send(user_id: str, reply_token: str, text: str) -> None:
        # reply token 有效時間短，Claude 想太久或已用過就改用 push
        try:
            await line.reply(reply_token, text)
        except Exception:
            log.warning("reply failed, falling back to push", exc_info=True)
            await line.push(user_id, text)

    async def handle_text(user_id: str, reply_token: str, text: str) -> None:
        cmd = text.strip().lower()
        if cmd in ("/help", "help", "說明"):
            return await send(user_id, reply_token, HELP_TEXT)
        if cmd == "/reset":
            storage.clear_messages(user_id)
            return await send(user_id, reply_token, "好，對話記憶已清除（筆記與提醒都還在）。")
        try:
            await line.show_loading(user_id)
        except Exception:
            log.debug("loading animation failed", exc_info=True)
        try:
            answer = await assistant.reply(user_id, text)
        except Exception:
            log.exception("assistant failed")
            answer = "我這邊出了點問題，請稍後再試。"
        await send(user_id, reply_token, answer)

    async def handle_event(event: dict) -> None:
        source = event.get("source", {})
        user_id = source.get("userId")
        reply_token = event.get("replyToken")
        if source.get("type") != "user" or not user_id or not reply_token:
            return  # 私人助手只處理一對一聊天

        message = event.get("message", {})
        if event.get("type") == "message" and message.get("type") == "text" \
                and message.get("text", "").strip().lower() == "/id":
            return await send(user_id, reply_token, f"你的 LINE user ID：\n{user_id}")

        if settings.allowed_user_ids and user_id not in settings.allowed_user_ids:
            log.info("ignored message from non-allowed user %s", user_id)
            return
        if not settings.allowed_user_ids:
            return await send(user_id, reply_token,
                              "助手尚未設定擁有者。請把下面這串 ID 設為環境變數 ALLOWED_USER_IDS 後重新啟動：\n"
                              f"{user_id}")

        if event.get("type") == "follow":
            return await send(user_id, reply_token, HELP_TEXT)
        if event.get("type") != "message":
            return
        if message.get("type") == "text":
            await handle_text(user_id, reply_token, message["text"])
        else:
            await send(user_id, reply_token, "目前我只看得懂文字訊息喔。")

    async def process(events: list) -> None:
        for event in events:
            try:
                await handle_event(event)
            except Exception:
                log.exception("failed to handle event")

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/callback")
    async def callback(request: Request, background: BackgroundTasks,
                       x_line_signature: str = Header(default="")):
        body = await request.body()
        if not verify_signature(settings.line_channel_secret, body, x_line_signature):
            raise HTTPException(status_code=400, detail="invalid signature")
        payload = await request.json()
        # 先回 200 給 LINE，再在背景處理（Claude 回覆可能需要數秒）
        background.add_task(process, payload.get("events", []))
        return "OK"

    return app

