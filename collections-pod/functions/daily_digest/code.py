#input_type_name: DigestInput
#output_type_name: DigestResult
#function_name: daily_digest

"""Compose a collections status digest and send it to the team over the configured
notification channel (Telegram works out of the box). Run on a schedule for a daily
stand-up, or on demand from the app. Degrades to a logged interaction if no channel.
"""

from datetime import date

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class DigestInput(BaseModel):
    audience: str = "team"


class DigestResult(BaseModel):
    delivered: bool
    channel: str
    message: str


async def daily_digest(ctx: FunctionContext, data: DigestInput) -> DigestResult:
    pod = Pod.from_env()

    def q(sql):
        return pod.query(sql).to_dict()["items"]

    inv = q("select status, count(*) as n, coalesce(sum(amount),0) as amt from invoices group by status")
    by_status = {r["status"]: r for r in inv}
    active = by_status.get("ACTIVE", {})
    outstanding = active.get("amt", 0)
    overdue = q("select count(*) as n from invoices where status='ACTIVE' and days_overdue > 0")[0]["n"]
    pending = q("select count(*) as n from drafts where status='PENDING_REVIEW'")[0]["n"]
    legal = (by_status.get("LEGAL", {}) or {}).get("n", 0)
    today = date.today().isoformat()
    sent_today = q("select count(*) as n from drafts where status in ('SENT','AUTO_SENT') and substr(cast(sent_at as text),1,10)='" + today + "'")[0]["n"]

    cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]
    company = cfg.get("company_name") or "Collections"

    msg = (f"📊 {company} — daily collections digest\n"
           f"• Outstanding (active): ₹{int(outstanding):,}\n"
           f"• Overdue accounts: {overdue}\n"
           f"• Follow-ups sent today: {sent_today}\n"
           f"• Awaiting approval: {pending}\n"
           f"• In legal: {legal}")

    channel = (cfg.get("notify_channel") or "NONE").upper()
    delivered, note = False, "no channel configured"
    if channel == "TELEGRAM":
        token = cfg.get("telegram_bot_token")
        cid = cfg.get("telegram_team_chat_id")
        if token and cid:
            try:
                import requests
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": cid, "text": msg}, timeout=20)
                delivered, note = r.status_code < 300, f"telegram {r.status_code}"
            except Exception as exc:
                note = str(exc)[:200]
    elif channel in ("SLACK", "WHATSAPP"):
        try:
            op = "chat_post_message" if channel == "SLACK" else "whatsapp_send_message"
            acct = "workspace-slack" if channel == "SLACK" else "workspace-whatsapp"
            payload = {"channel": cfg.get("slack_channel") or "", "text": msg} if channel == "SLACK" else {"to": "", "text": msg}
            pod.connectors.execute(acct, op, payload)
            delivered, note = True, f"sent via {channel}"
        except Exception as exc:
            note = str(exc)[:200]

    pod.table("interactions").create({
        "kind": "NOTIFICATION_SENT", "channel": channel if channel != "NONE" else "SYSTEM", "direction": "OUTBOUND",
        "summary": "Daily digest: " + msg.replace("\n", " · ")[:240],
        "detail": {"delivered": delivered, "note": note}, "actor_label": "daily_digest",
        "level": "SUCCESS" if delivered else "INFO"})

    return DigestResult(delivered=delivered, channel=channel, message=msg)
