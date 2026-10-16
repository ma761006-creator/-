import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from line_assistant.app import create_app, deliver_due_reminders
from line_assistant.assistant import Assistant
from line_assistant.config import Settings
from line_assistant.line_api import split_text, verify_signature
from line_assistant.storage import Storage, utcnow
from line_assistant.tools import ToolError, ToolExecutor

SECRET = "test-secret"
OWNER = "Uowner"


def sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()


class FakeLine:
    def __init__(self):
        self.replies, self.pushes = [], []
        self.fail_reply = False

    async def reply(self, token, text):
        if self.fail_reply:
            raise RuntimeError("expired token")
        self.replies.append((token, text))

    async def push(self, user_id, text):
        self.pushes.append((user_id, text))

    async def show_loading(self, user_id, seconds=60):
        pass

    async def aclose(self):
        pass


def text_block(t):
    return SimpleNamespace(type="text", text=t)


def tool_block(id_, name, input_):
    return SimpleNamespace(type="tool_use", id=id_, name=name, input=input_)


class FakeMessages:
    """依序回傳預先排好的回應，並記錄每次請求。"""
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def fake_client(*responses):
    msgs = FakeMessages(responses)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def resp(stop_reason, *content):
    return SimpleNamespace(stop_reason=stop_reason, content=list(content))


@pytest.fixture
def settings(tmp_path):
    return Settings(line_channel_secret=SECRET, line_channel_access_token="tok",
                    allowed_user_ids=frozenset({OWNER}), db_path=str(tmp_path / "t.db"),
                    enable_web_search=False)


@pytest.fixture
def storage(settings):
    return Storage(settings.db_path)


# --- LINE helpers -------------------------------------------------------------
def test_verify_signature():
    body = b'{"events":[]}'
    assert verify_signature(SECRET, body, sign(body))
    assert not verify_signature(SECRET, body, sign(b"other"))
    assert not verify_signature("", body, sign(body))


def test_split_text_respects_limits():
    assert split_text("hi") == ["hi"]
    parts = split_text("a" * 12000, limit=5000)
    assert [len(p) for p in parts] == [5000, 5000, 2000]
    parts = split_text("x\n" * 20000, limit=100, max_parts=5)
    assert len(parts) == 5 and all(len(p) <= 100 for p in parts)
    assert parts[-1].endswith("已截斷）")


# --- tools --------------------------------------------------------------------
def test_reminder_tools_roundtrip(storage, settings):
    ex = ToolExecutor(storage, settings.tz)
    when = (datetime.now(settings.tz) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M")
    out = json.loads(ex.run(OWNER, "add_reminder", {"when": when, "text": "吃藥"}))
    assert out["ok"] and out["when"] == when

    listed = json.loads(ex.run(OWNER, "list_reminders", {}))["reminders"]
    assert listed == [{"id": out["reminder_id"], "when": when, "text": "吃藥"}]
    assert json.loads(ex.run("Uother", "list_reminders", {}))["reminders"] == []

    with pytest.raises(ToolError):
        ex.run("Uother", "cancel_reminder", {"reminder_id": out["reminder_id"]})
    ex.run(OWNER, "cancel_reminder", {"reminder_id": out["reminder_id"]})
    assert storage.pending_reminders(OWNER) == []


def test_reminder_rejects_past_and_bad_format(storage, settings):
    ex = ToolExecutor(storage, settings.tz)
    with pytest.raises(ToolError, match="過去"):
        ex.run(OWNER, "add_reminder", {"when": "2000-01-01 08:00", "text": "x"})
    with pytest.raises(ToolError, match="格式"):
        ex.run(OWNER, "add_reminder", {"when": "明天", "text": "x"})


def test_note_tools(storage, settings):
    ex = ToolExecutor(storage, settings.tz)
    ex.run(OWNER, "save_note", {"content": "車停在 B2 C17"})
    ex.run(OWNER, "save_note", {"content": "牛奶要買低脂"})
    hits = json.loads(ex.run(OWNER, "search_notes", {"query": "B2"}))["notes"]
    assert [n["content"] for n in hits] == ["車停在 B2 C17"]
    assert len(json.loads(ex.run(OWNER, "search_notes", {"query": ""}))["notes"]) == 2
    ex.run(OWNER, "delete_note", {"note_id": hits[0]["id"]})
    assert len(storage.search_notes(OWNER)) == 1


def test_recent_messages_starts_with_user(storage):
    for i in range(5):
        storage.add_message(OWNER, "user", f"u{i}")
        storage.add_message(OWNER, "assistant", f"a{i}")
    msgs = storage.recent_messages(OWNER, turns=2)
    assert [m["content"] for m in msgs] == ["u3", "a3", "u4", "a4"]


# --- reminders delivery -------------------------------------------------------
def test_deliver_due_reminders(storage):
    line = FakeLine()
    storage.add_reminder(OWNER, utcnow() - timedelta(minutes=1), "開會")
    storage.add_reminder(OWNER, utcnow() + timedelta(hours=1), "之後")
    assert asyncio.run(deliver_due_reminders(storage, line)) == 1
    assert line.pushes == [(OWNER, "⏰ 提醒：開會")]
    assert asyncio.run(deliver_due_reminders(storage, line)) == 0  # 不重送


# --- assistant loop -----------------------------------------------------------
def test_assistant_runs_tool_then_answers(storage, settings):
    client, msgs = fake_client(
        resp("tool_use", tool_block("t1", "save_note", {"content": "護照在抽屜"})),
        resp("end_turn", text_block("記好了！")),
    )
    a = Assistant(settings, storage, client=client)
    assert asyncio.run(a.reply(OWNER, "記一下護照在抽屜")) == "記好了！"
    assert storage.search_notes(OWNER, "護照")[0]["content"] == "護照在抽屜"

    second = msgs.calls[1]["messages"]
    assert second[-1]["content"][0]["type"] == "tool_result"
    assert second[-1]["content"][0].get("is_error") is None
    # 固定參數：fallback 與 model
    assert msgs.calls[0]["fallbacks"] == "default"
    assert msgs.calls[0]["model"] == "claude-opus-5-5"

    hist = storage.recent_messages(OWNER, 10)
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[0]["content"].startswith("[現在時間") and hist[1]["content"] == "記好了！"


def test_assistant_reports_tool_errors_to_model(storage, settings):
    client, msgs = fake_client(
        resp("tool_use", tool_block("t1", "cancel_reminder", {"reminder_id": 99})),
        resp("end_turn", text_block("找不到這個提醒")),
    )
    asyncio.run(Assistant(settings, storage, client=client).reply(OWNER, "取消 99"))
    result = msgs.calls[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True and "#99" in result["content"]


def test_assistant_history_is_append_only(storage, settings):
    client, msgs = fake_client(resp("end_turn", text_block("一")), resp("end_turn", text_block("二")))
    a = Assistant(settings, storage, client=client)
    asyncio.run(a.reply(OWNER, "第一句"))
    asyncio.run(a.reply(OWNER, "第二句"))
    first, second = msgs.calls[0]["messages"], msgs.calls[1]["messages"]
    assert second[: len(first)] == first
    assert msgs.calls[0]["system"] == msgs.calls[1]["system"]


def test_assistant_refusal(storage, settings):
    client, _ = fake_client(resp("refusal"))
    assert "沒辦法" in asyncio.run(Assistant(settings, storage, client=client).reply(OWNER, "x"))


# --- webhook ------------------------------------------------------------------
class StubAssistant:
    def __init__(self):
        self.calls = []

    async def reply(self, user_id, text):
        self.calls.append((user_id, text))
        return f"echo:{text}"


def make_client(settings, storage):
    line, stub = FakeLine(), StubAssistant()
    app = create_app(settings, storage, line, stub, run_scheduler=False)
    return TestClient(app), line, stub


def post(client, events):
    body = json.dumps({"events": events}).encode()
    return client.post("/callback", content=body, headers={"X-Line-Signature": sign(body)})


def text_event(user, text, token="rt"):
    return {"type": "message", "replyToken": token, "source": {"type": "user", "userId": user},
            "message": {"type": "text", "text": text}}


def test_webhook_rejects_bad_signature(settings, storage):
    client, _, _ = make_client(settings, storage)
    r = client.post("/callback", content=b'{"events":[]}', headers={"X-Line-Signature": "bad"})
    assert r.status_code == 400


def test_webhook_owner_message_gets_reply(settings, storage):
    client, line, stub = make_client(settings, storage)
    assert post(client, [text_event(OWNER, "你好")]).status_code == 200
    assert stub.calls == [(OWNER, "你好")]
    assert line.replies == [("rt", "echo:你好")]


def test_webhook_ignores_strangers_but_tells_id(settings, storage):
    client, line, stub = make_client(settings, storage)
    post(client, [text_event("Ustranger", "你好")])
    assert stub.calls == [] and line.replies == []
    post(client, [text_event("Ustranger", "/id")])
    assert "Ustranger" in line.replies[0][1]


def test_webhook_without_owner_configured(settings, storage, tmp_path):
    unset = Settings(line_channel_secret=SECRET, line_channel_access_token="tok",
                     db_path=str(tmp_path / "u.db"), enable_web_search=False)
    client, line, stub = make_client(unset, storage)
    post(client, [text_event("Unew", "hi")])
    assert stub.calls == [] and "ALLOWED_USER_IDS" in line.replies[0][1]


def test_webhook_reset_and_push_fallback(settings, storage):
    client, line, _ = make_client(settings, storage)
    storage.add_message(OWNER, "user", "舊訊息")
    post(client, [text_event(OWNER, "/reset")])
    assert storage.recent_messages(OWNER, 10) == []

    line.fail_reply = True
    post(client, [text_event(OWNER, "在嗎")])
    assert line.pushes == [(OWNER, "echo:在嗎")]
