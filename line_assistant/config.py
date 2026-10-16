import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo


def _split_ids(raw: str) -> frozenset:
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    line_channel_secret: str
    line_channel_access_token: str
    allowed_user_ids: frozenset = field(default_factory=frozenset)
    db_path: str = "line_assistant.db"
    timezone: str = "Asia/Taipei"
    model: str = "claude-opus-5-5"
    effort: str = "low"
    history_turns: int = 20
    enable_web_search: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            line_channel_secret=os.environ.get("LINE_CHANNEL_SECRET", ""),
            line_channel_access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""),
            allowed_user_ids=_split_ids(os.environ.get("ALLOWED_USER_IDS", "")),
            db_path=os.environ.get("ASSISTANT_DB_PATH", "line_assistant.db"),
            timezone=os.environ.get("ASSISTANT_TIMEZONE", "Asia/Taipei"),
            model=os.environ.get("ASSISTANT_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("ASSISTANT_EFFORT", "low"),
            history_turns=int(os.environ.get("ASSISTANT_HISTORY_TURNS", "20")),
            enable_web_search=os.environ.get("ASSISTANT_WEB_SEARCH", "1") != "0",
        )
