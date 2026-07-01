#input_type_name: NotifyInput
#output_type_name: NotifyResult
#function_name: notify

"""Send a team/legal notification over the configured channel.
  SLACK - real delivery via the Lemma Slack connector (delegated account)
Telegram is offered as a managed agent *surface* (two-way chat), not an outbound
push channel — see the README. Degrades gracefully: with no channel configured it
records the notification as an interaction without failing the caller.
"""

from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class NotifyInput(BaseModel):
    audience: str = "team"            # team | legal
    message: str
    chat_id: Optional[str] = None     # explicit channel override
    invoice_id: Optional[str] = None
    client_id: Optional[str] = None


class NotifyResult(BaseModel):
    delivered: bool
    channel: str
    note: str


def send_chat(cfg, audience, message, chat_id=None):
    """Returns (channel, delivered, note). Lemma-native Slack connector only."""
    channel = (cfg.get("notify_channel") or "NONE").upper()
    if channel == "SLACK":
        target = chat_id or (cfg.get("slack_legal_channel") if audience == "legal" else cfg.get("slack_channel")) or cfg.get("slack_channel") or ""
        if not target:
            return ("SLACK", False, "Slack channel id not set in Settings")
        try:
            Pod.from_env().connectors.execute("slack", "chat_post_message",
                {"channel": target, "text": message})
            return ("SLACK", True, "sent via Slack")
        except Exception as exc:
            return ("SLACK", False, "connect the Slack account first: " + str(exc)[:160])
    return ("NONE", False, "no channel configured")


async def notify(ctx: FunctionContext, data: NotifyInput) -> NotifyResult:
    pod = Pod.from_env()
    cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]
    channel, delivered, note = send_chat(cfg, data.audience, data.message, data.chat_id)

    pod.table("interactions").create({
        "invoice_id": data.invoice_id, "client_id": data.client_id,
        "kind": "NOTIFICATION_SENT",
        "channel": channel if channel in ("SLACK",) else "SYSTEM",
        "direction": "OUTBOUND",
        "summary": f"[{data.audience}] {data.message[:200]}",
        "detail": {"delivered": delivered, "channel": channel, "note": note},
        "actor_label": "notify", "level": "SUCCESS" if delivered else "WARN"})

    return NotifyResult(delivered=delivered, channel=channel, note=note)
