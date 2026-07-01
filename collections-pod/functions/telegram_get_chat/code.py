#input_type_name: TgInput
#output_type_name: TgResult
#function_name: telegram_get_chat

"""Helper for the Telegram setup wizard. Given a bot token, validates it (getMe) and
returns the recent chats that have messaged the bot (getUpdates) so the operator can
pick a chat id with one click instead of hand-copying it from a raw API URL.
"""

from pydantic import BaseModel
from lemma_sdk import FunctionContext


class TgInput(BaseModel):
    bot_token: str


class Chat(BaseModel):
    id: str
    name: str
    type: str


class TgResult(BaseModel):
    ok: bool
    bot: str
    chats: list[Chat]
    detail: str


async def telegram_get_chat(ctx: FunctionContext, data: TgInput) -> TgResult:
    import requests
    token = (data.bot_token or "").strip()
    if not token:
        return TgResult(ok=False, bot="", chats=[], detail="Enter a bot token first.")
    try:
        me = requests.get(f"https://api.telegram.org/bot{token}/getMe", timeout=15).json()
        if not me.get("ok"):
            return TgResult(ok=False, bot="", chats=[], detail="Invalid bot token.")
        botname = me["result"].get("username", "bot")
        upd = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15).json()
        seen, chats = set(), []
        for u in (upd.get("result") or []):
            msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            ch = msg.get("chat") or {}
            cid = ch.get("id")
            if cid is None or cid in seen:
                continue
            seen.add(cid)
            nm = ch.get("title") or (" ".join(filter(None, [ch.get("first_name"), ch.get("last_name")]))) or ch.get("username") or str(cid)
            chats.append(Chat(id=str(cid), name=nm, type=ch.get("type", "")))
        detail = f"Found {len(chats)} chat(s)." if chats else "Token valid. Now send a message to your bot (or add it to a group and post), then click Find again."
        return TgResult(ok=True, bot=botname, chats=chats, detail=detail)
    except Exception as exc:
        return TgResult(ok=False, bot="", chats=[], detail=str(exc)[:200])
