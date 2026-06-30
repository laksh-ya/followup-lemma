#input_type_name: NotifyInput
#output_type_name: NotifyResult
#function_name: notify

"""Send a notification to a team/legal audience (daily digest, legal escalation) or a
client (strict notice) over the pod's configured channel. Degrades gracefully: if no
channel is configured (or the connector isn't wired yet), it records the notification
as an interaction without failing the calling flow.
"""

from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class NotifyInput(BaseModel):
    audience: str = "team"          # team | legal | client
    message: str
    channel: Optional[str] = None   # override; else pod_config.notify_channel
    invoice_id: Optional[str] = None
    client_id: Optional[str] = None
    to: Optional[str] = None        # explicit recipient (phone/handle) for client notices


class NotifyResult(BaseModel):
    delivered: bool
    channel: str
    note: str


async def notify(ctx: FunctionContext, data: NotifyInput) -> NotifyResult:
    pod = Pod.from_env()
    cfg_items = pod.records.list("pod_config", limit=1).to_dict()["items"]
    cfg = cfg_items[0] if cfg_items else {}
    channel = (data.channel or cfg.get("notify_channel") or "NONE").upper()

    delivered = False
    note = ""

    if channel in ("SLACK", "WHATSAPP"):
        try:
            # Wired in Phase 3 (connectors). Until a connector account exists this
            # raises and we fall through to a logged-but-undelivered notification.
            if channel == "SLACK":
                pod.connectors.execute("workspace-slack", "chat_post_message",
                                       {"text": data.message})
            else:
                pod.connectors.execute("workspace-whatsapp", "whatsapp_send_message",
                                       {"to": data.to or "", "text": data.message})
            delivered = True
            note = f"sent via {channel}"
        except Exception as exc:
            note = f"{channel} not deliverable yet: {str(exc)[:200]}"
    else:
        note = "no channel configured"

    pod.table("interactions").create({
        "invoice_id": data.invoice_id,
        "client_id": data.client_id,
        "kind": "NOTIFICATION_SENT",
        "channel": channel if channel in ("SLACK", "WHATSAPP") else "SYSTEM",
        "direction": "OUTBOUND",
        "summary": f"[{data.audience}] {data.message[:200]}",
        "detail": {"delivered": delivered, "channel": channel, "note": note, "to": data.to},
        "actor_label": "notify",
        "level": "SUCCESS" if delivered else "WARN",
    })

    return NotifyResult(delivered=delivered, channel=channel, note=note)
