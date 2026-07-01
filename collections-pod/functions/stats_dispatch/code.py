#input_type_name: DispatchInput
#output_type_name: DispatchResult
#function_name: stats_dispatch

"""Stats-sharing dispatcher for the Schedule tab. Composes a collections digest from a
per-channel selection of metrics and delivers it over Slack, Telegram, or WhatsApp.

Three modes:
  - "manual" — send one channel now (channel=…), or every enabled channel (channel=None).
  - "test"   — send a short test message so the operator can verify a channel's wiring.
  - "due"    — the schedule sweep: send every enabled channel whose freq/time matches now
               and that hasn't already fired this window (guarded by last_sent_at).

Delivery is Lemma-native for Slack (connector) and bring-your-own-token for Telegram
(Bot API) and WhatsApp (Meta Graph Cloud API) — the same tokens the pod already stores
in pod_config. A channel with no destination/credentials records the digest instead of
sending, so the pod never hard-fails a demo.
"""

from datetime import date, datetime, timezone
from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


CHANNELS = ("SLACK", "TELEGRAM", "WHATSAPP")


class DispatchInput(BaseModel):
    mode: str = "manual"              # manual | test | due
    channel: Optional[str] = None     # SLACK | TELEGRAM | WHATSAPP (None = all enabled, for manual)


class ChannelResult(BaseModel):
    channel: str
    delivered: bool
    detail: str


class DispatchResult(BaseModel):
    mode: str
    sent: int
    results: list[ChannelResult]
    message: str = ""


# ---------------------------------------------------------------- metrics

def _metrics(pod):
    def q(sql):
        return pod.query(sql).to_dict()["items"]
    inv = q("select status, count(*) as n, coalesce(sum(amount),0) as amt from invoices where coalesce(demo,false)=false group by status")
    by_status = {r["status"]: r for r in inv}
    active = by_status.get("ACTIVE", {})
    today = date.today().isoformat()
    return {
        "outstanding": int(active.get("amt", 0) or 0),
        "overdue": q("select count(*) as n from invoices where status='ACTIVE' and days_overdue > 0 and coalesce(demo,false)=false")[0]["n"],
        "pending": q("select count(*) as n from drafts where status='PENDING_REVIEW'")[0]["n"],
        "legal": int((by_status.get("LEGAL", {}) or {}).get("n", 0) or 0),
        "sent_today": q("select count(*) as n from drafts where status in ('SENT','AUTO_SENT') and substr(cast(sent_at as text),1,10)='" + today + "'")[0]["n"],
        "promises": q("select count(*) as n from promises where status='OPEN'")[0]["n"],
        "disputes": int((by_status.get("DISPUTED", {}) or {}).get("n", 0) or 0),
        "top": q("select invoice_no, amount from invoices where status='ACTIVE' and coalesce(demo,false)=false order by amount desc limit 3"),
    }


def _compose(pod, cfg, ch):
    """Build the digest text honoring the channel's inc_* metric flags."""
    m = _metrics(pod)
    company = cfg.get("company_name") or "Collections"
    lines = [f"📊 {company} — collections digest"]
    if ch.get("inc_outstanding", True):
        lines.append(f"• Outstanding (active): ₹{m['outstanding']:,}")
    if ch.get("inc_overdue", True):
        lines.append(f"• Overdue accounts: {m['overdue']}")
    if ch.get("inc_sent_today", True):
        lines.append(f"• Follow-ups sent today: {m['sent_today']}")
    if ch.get("inc_pending", True):
        lines.append(f"• Awaiting approval: {m['pending']}")
    if ch.get("inc_legal", True):
        lines.append(f"• In legal: {m['legal']}")
    if ch.get("inc_promises", False):
        lines.append(f"• Open promises to pay: {m['promises']}")
    if ch.get("inc_disputes", False):
        lines.append(f"• Disputed: {m['disputes']}")
    if ch.get("inc_top_accounts", False) and m["top"]:
        top = ", ".join(f"{r['invoice_no']} ₹{int(r['amount']):,}" for r in m["top"])
        lines.append(f"• Top overdue: {top}")
    if len(lines) == 1:
        lines.append("• (no metrics selected)")
    return "\n".join(lines)


# ---------------------------------------------------------------- delivery

def _dest(cfg, channel, row):
    d = (row or {}).get("destination")
    if d:
        return d
    if channel == "SLACK":
        return cfg.get("slack_channel") or ""
    if channel == "TELEGRAM":
        return cfg.get("telegram_team_chat_id") or ""
    if channel == "WHATSAPP":
        return cfg.get("whatsapp_to") or ""
    return ""


def _deliver(pod, cfg, channel, dest, msg):
    """(delivered, detail). Records instead of sending when creds/destination missing."""
    if not dest:
        return (False, "no destination set (recorded only)")
    try:
        if channel == "SLACK":
            pod.connectors.execute("slack", "chat_post_message", {"body": {"channel": dest, "text": msg}})
            return (True, f"sent to Slack {dest}")
        if channel == "TELEGRAM":
            token = cfg.get("telegram_bot_token")
            if not token:
                return (False, "Telegram bot token not set in Settings")
            import requests
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": dest, "text": msg}, timeout=20)
            return (r.status_code < 300, f"telegram {r.status_code}")
        if channel == "WHATSAPP":
            token = cfg.get("whatsapp_token")
            phone_id = cfg.get("whatsapp_phone_id")
            if not token or not phone_id:
                return (False, "WhatsApp phone id / token not set in Settings")
            import requests
            r = requests.post(
                f"https://graph.facebook.com/v20.0/{phone_id}/messages",
                headers={"Authorization": f"Bearer {token}"},
                json={"messaging_product": "whatsapp", "to": dest, "type": "text",
                      "text": {"body": msg}}, timeout=20)
            return (r.status_code < 300, f"whatsapp {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        return (False, str(exc)[:200])
    return (False, f"unknown channel {channel}")


def _log(pod, channel, delivered, msg, detail):
    pod.table("interactions").create({
        "kind": "NOTIFICATION_SENT", "channel": channel if delivered else "SYSTEM",
        "direction": "OUTBOUND",
        "summary": f"Stats → {channel}: " + msg.replace("\n", " · ")[:220],
        "detail": {"delivered": delivered, "info": detail}, "actor_label": "stats_dispatch",
        "level": "SUCCESS" if delivered else "INFO"})


def _rows_by_channel(pod):
    rows = pod.records.list("stats_channels", limit=50).to_dict()["items"]
    return {str(r.get("channel", "")).upper(): r for r in rows}


def _is_due(row, now):
    if not row.get("enabled"):
        return False
    freq = (row.get("freq") or "OFF").upper()
    if freq == "OFF":
        return False
    dow = now.weekday()  # 0=Mon
    if freq == "WEEKDAYS" and dow >= 5:
        return False
    if freq == "WEEKLY" and dow != int(row.get("weekly_dow") or 0):
        return False
    if now.hour != int(row.get("send_hour") or 9):
        return False
    # fire once within the 30-min slot that contains send_minute
    if (now.minute // 30) != (int(row.get("send_minute") or 0) // 30):
        return False
    last = row.get("last_sent_at")
    if last:
        try:
            lt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if lt.date() == now.date() and lt.hour == now.hour:
                return False
        except Exception:
            pass
    return True


async def stats_dispatch(ctx: FunctionContext, data: DispatchInput) -> DispatchResult:
    pod = Pod.from_env()
    cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]
    rows = _rows_by_channel(pod)
    mode = (data.mode or "manual").lower()
    results, sent, last_msg = [], 0, ""

    def run_one(channel, row):
        nonlocal sent, last_msg
        if mode == "test":
            msg = "✅ Collections Agent — test stats message. Your " + channel.title() + " channel is wired."
        else:
            msg = _compose(pod, cfg, row or {})
        last_msg = msg
        dest = _dest(cfg, channel, row)
        delivered, detail = _deliver(pod, cfg, channel, dest, msg)
        _log(pod, channel, delivered, msg, detail)
        if delivered:
            sent += 1
            if row and row.get("id") and mode == "due":
                pod.table("stats_channels").update(row["id"], {
                    "last_sent_at": datetime.now(timezone.utc).isoformat()})
        results.append(ChannelResult(channel=channel, delivered=delivered, detail=detail))

    if mode == "due":
        now = datetime.now(timezone.utc)
        for channel in CHANNELS:
            row = rows.get(channel)
            if row and _is_due(row, now):
                run_one(channel, row)
    else:
        targets = [data.channel.upper()] if data.channel else [c for c in CHANNELS if (rows.get(c) or {}).get("enabled")]
        for channel in targets:
            if channel in CHANNELS:
                run_one(channel, rows.get(channel))

    return DispatchResult(mode=mode, sent=sent, results=results, message=last_msg)
