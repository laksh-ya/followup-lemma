#input_type_name: TestInput
#output_type_name: TestResult
#function_name: test_channel

"""Send a test message so the operator can verify mail / notification config from the
Settings screen before relying on it. kind='email' uses the configured mail mode;
kind='notify' uses the configured notification channel.
"""

from typing import Optional

from pydantic import BaseModel
from lemma_sdk import FunctionContext, Pod


class TestInput(BaseModel):
    kind: str                 # email | notify
    to: Optional[str] = None  # test recipient (email) or chat_id (notify)


class TestResult(BaseModel):
    ok: bool
    detail: str


async def test_channel(ctx: FunctionContext, data: TestInput) -> TestResult:
    pod = Pod.from_env()
    cfg = (pod.records.list("pod_config", limit=1).to_dict()["items"] or [{}])[0]

    if data.kind == "email":
        mode = (cfg.get("mail_mode") or "IN_APP").upper()
        to = data.to or (cfg.get("email_from") or "").split("<")[-1].strip(">") or "test@example.com"
        subject = "Collections Agent — test email"
        body = "This is a test message from your Collections Agent mail configuration."
        if mode != "GMAIL" or not cfg.get("email_enabled"):
            return TestResult(ok=True, detail="IN_APP mode: nothing sent (record-only). Switch to Gmail and connect an account to send for real.")
        try:
            pod.connectors.execute("workspace-gmail", "gmail_send_email",
                {"recipient_email": to, "subject": subject, "body": body})
            return TestResult(ok=True, detail=f"Gmail: sent to {to}")
        except Exception as exc:
            return TestResult(ok=False, detail=f"Connect a Gmail account first (lemma connectors). {str(exc)[:200]}")

    # notify test — Slack connector only
    channel = (cfg.get("notify_channel") or "NONE").upper()
    if channel == "NONE":
        return TestResult(ok=False, detail="No notification channel selected.")
    if channel == "SLACK":
        target = data.to or cfg.get("slack_channel") or ""
        if not target:
            return TestResult(ok=False, detail="Set a Slack channel id in Settings first.")
        try:
            pod.connectors.execute("slack", "chat_post_message",
                {"body": {"channel": target, "text": "✅ Collections Agent test notification."}})
            return TestResult(ok=True, detail=f"Slack: sent to {target}")
        except Exception as exc:
            return TestResult(ok=False, detail=f"Connect a Slack account first (lemma connectors). {str(exc)[:200]}")
    return TestResult(ok=False, detail=f"Unsupported channel: {channel}")
