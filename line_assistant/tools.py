"""助手可呼叫的本地工具：提醒與筆記。每個工具都綁定目前對話的 LINE 使用者。"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from .storage import Storage, utcnow

TIME_FORMAT = "%Y-%m-%d %H:%M"


def _obj(properties: dict, required: list) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOL_DEFINITIONS = [
    {
        "name": "add_reminder",
        "description": "在指定時間用 LINE 推播提醒使用者。時間是使用者當地時間，格式 YYYY-MM-DD HH:MM。"
                       "使用者說「明天早上」「30 分鐘後」等相對時間時，請依訊息開頭提供的現在時間換算。",
        "strict": True,
        "input_schema": _obj(
            {
                "when": {"type": "string", "description": "當地時間，YYYY-MM-DD HH:MM"},
                "text": {"type": "string", "description": "提醒內容"},
            },
            ["when", "text"],
        ),
    },
    {
        "name": "list_reminders",
        "description": "列出尚未送出的提醒（含編號與時間）。",
        "strict": True,
        "input_schema": _obj({}, []),
    },
    {
        "name": "cancel_reminder",
        "description": "依編號取消一個尚未送出的提醒。不確定編號時先呼叫 list_reminders。",
        "strict": True,
        "input_schema": _obj({"reminder_id": {"type": "integer"}}, ["reminder_id"]),
    },
    {
        "name": "save_note",
        "description": "把使用者要你記住的事存成筆記（例如：停車位置、密碼提示以外的生活資訊、待辦、靈感）。",
        "strict": True,
        "input_schema": _obj({"content": {"type": "string"}}, ["content"]),
    },
    {
        "name": "search_notes",
        "description": "以關鍵字搜尋筆記；query 給空字串則列出最近的筆記。",
        "strict": True,
        "input_schema": _obj({"query": {"type": "string"}}, ["query"]),
    },
    {
        "name": "delete_note",
        "description": "依編號刪除一則筆記。",
        "strict": True,
        "input_schema": _obj({"note_id": {"type": "integer"}}, ["note_id"]),
    },
]


class ToolError(Exception):
    pass


class ToolExecutor:
    def __init__(self, storage: Storage, tz: ZoneInfo):
        self.storage = storage
        self.tz = tz

    def _local(self, iso_utc: str) -> str:
        return datetime.fromisoformat(iso_utc).astimezone(self.tz).strftime(TIME_FORMAT)

    def run(self, user_id: str, name: str, args: dict) -> str:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ToolError(f"未知的工具：{name}")
        return json.dumps(handler(user_id, **args), ensure_ascii=False)

    def _tool_add_reminder(self, user_id: str, when: str, text: str) -> dict:
        try:
            due = datetime.strptime(when.strip(), TIME_FORMAT).replace(tzinfo=self.tz)
        except ValueError:
            raise ToolError(f"時間格式錯誤：{when!r}，請用 YYYY-MM-DD HH:MM")
        if due <= utcnow():
            raise ToolError(f"{when} 已經過去了，請給未來的時間")
        rid = self.storage.add_reminder(user_id, due, text)
        return {"ok": True, "reminder_id": rid, "when": due.strftime(TIME_FORMAT), "text": text}

    def _tool_list_reminders(self, user_id: str) -> dict:
        items = self.storage.pending_reminders(user_id)
        return {"reminders": [{"id": r["id"], "when": self._local(r["due_at"]), "text": r["text"]} for r in items]}

    def _tool_cancel_reminder(self, user_id: str, reminder_id: int) -> dict:
        if not self.storage.cancel_reminder(user_id, reminder_id):
            raise ToolError(f"找不到尚未送出的提醒 #{reminder_id}")
        return {"ok": True, "cancelled": reminder_id}

    def _tool_save_note(self, user_id: str, content: str) -> dict:
        return {"ok": True, "note_id": self.storage.add_note(user_id, content)}

    def _tool_search_notes(self, user_id: str, query: str) -> dict:
        notes = self.storage.search_notes(user_id, query.strip())
        return {"notes": [{"id": n["id"], "content": n["content"], "saved": self._local(n["created_at"])} for n in notes]}

    def _tool_delete_note(self, user_id: str, note_id: int) -> dict:
        if not self.storage.delete_note(user_id, note_id):
            raise ToolError(f"找不到筆記 #{note_id}")
        return {"ok": True, "deleted": note_id}
