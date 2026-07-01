# Collections Agent — pod runbook

An AI accounts-receivable desk: it tracks customers and their overdue invoices,
classifies each by escalation stage, drafts a stage-appropriate follow-up grounded in
your policy docs, validates it against the source record (no hallucinated figures),
then auto-sends the safe ones and routes risky / final notices to a human. A team of
roles works one shared workspace; legal handles 30+ day accounts; replies are
understood and acted on.

App: deployed at `lemma apps open collections-app` (local: `http://collections-app.127-0-0-1.sslip.io:8711`).

## Who uses it (and why)

| Team | Where | What they get |
|---|---|---|
| **Collector** | Invoices, Customers, Replies | The working queue; the AI has already triaged + drafted. |
| **Finance lead** | Approvals | Approve/edit/reject AI drafts; final notices never auto-send. |
| **Legal / recovery** | Legal | 30+ day & escalated accounts with full history + one-click notice. |
| **Admin** | Settings | Behavior, mail mode, notification channels — all live. |
| **Anyone** | Ask agent (app) / Telegram | Ask the concierge about the book in plain language. |

## Customer identity (the data foundation)

A customer is identified by **normalized email** (`clients.email_norm`, unique). Every
ingest — CSV upload, manual add, seed, or inbound reply — matches on that key, so the
same customer is **one record** across all their invoices and messages. The Customers
tab rolls everything up per customer.

## Quick start (local)

```bash
# 1. Model provider (the agent needs one). Groq is free:
lemma-stack config set LEMMA_DEFAULT_MODEL_TYPE openai_compat
lemma-stack config set LEMMA_OPENAI_API_KEY gsk_...
lemma-stack config set LEMMA_OPENAI_BASE_URL https://api.groq.com/openai/v1
lemma-stack config set LEMMA_OPENAI_DEFAULT_MODEL llama-3.3-70b-versatile
lemma-stack config set LEMMA_OPENAI_MODEL_NAMES llama-3.3-70b-versatile
lemma-stack restart

# 2. Create + import the pod
lemma pods create collections-agent
lemma pods import ./collections-pod         # tables → functions → agents → workflows → schedules → app

# 3. Upload the knowledge docs (grounding) + seed demo data
bash ./collections-pod/seed/seed.sh         # or click "Seed demo data" in the app

# 4. Open the app
lemma apps open collections-app
```

In the app: **Run scan** triages overdue invoices; the **Data ▾** menu seeds demo
data, seeds replies, imports/exports CSV, adds an invoice, or resets.

## CSV import format

Header row (only `invoice_no`, `client_email`, `due_date`, `amount` are required):

```
invoice_no,client_name,client_email,amount,currency,due_date,payment_link,status,company,phone,vip,payment_behavior,notes
INV-2025-100,Acme Corp,ap@acme.com,45000,INR,2025-05-01,https://pay/x,ACTIVE,Acme,+91...,false,AVERAGE,
```

- `due_date` = `YYYY-MM-DD`. `status` ∈ ACTIVE/PAID/DISPUTED/LEGAL/PAUSED (default ACTIVE).
- `payment_behavior` ∈ GOOD/AVERAGE/RISKY. `vip` = true/false.
- Customers are deduped by `client_email`.

## Email delivery modes (Settings → Email delivery)

Lemma-native only — no SMTP creds or API keys stored in the app:

- **IN_APP** (default) — record-only; drafts shown in-app. No email leaves. Best for demos.
- **GMAIL** — send through your Gmail account via the Lemma Gmail connector (delegated
  OAuth; the app never holds credentials):
  ```bash
  lemma connectors auth-configs create gmail --name workspace-gmail
  lemma connectors connect-requests create gmail --auth-config-id <id>   # authorize in browser
  ```
  The `connector:gmail use` grant is already in the bundle for `dispatch_followup`,
  `app_action`, and `test_channel`. Toggle **Send real email** on, pick GMAIL,
  **Save**, then **Send test**.

## Team & legal alerts (Settings → Team & legal alerts)

- **SLACK** — real delivery via the Lemma Slack connector:
  ```bash
  lemma connectors auth-configs create slack --name workspace-slack
  lemma connectors connect-requests create slack --auth-config-id <id>   # authorize in browser
  ```
  Set the team channel id (and optional legal channel id) in Settings, **Save**,
  **Send test**. Used for legal-escalation pings and the daily stats digest.
- **NONE** (default) — alerts are recorded as interactions, nothing sent.

## Chat with the agent — Telegram surface + in-app Ask

The `collections-concierge` agent answers questions about the live book (overdue
accounts, a customer's status, the legal queue, pending approvals). One agent, two
front doors:

- **In the app** — the **Ask agent** tab (official `lemma-agent-thread` component).
- **On Telegram** — a Lemma **managed surface** (system bot, no token, no webhook):
  ```bash
  lemma surfaces upsert telegram --agent collections-concierge
  lemma surfaces setup telegram      # prints the bot link to message
  ```
  Already ACTIVE in this pod. Read-only Q&A; it reports, it doesn't act.

## Inbound replies (closing the loop)

The Replies tab shows classified customer replies (promise-to-pay / dispute / paid /
question). With a Gmail mailbox connected, a WEBHOOK schedule on new mail feeds the
`reply-triager` agent which classifies and updates the account. For a demo without a
mailbox, use **Data ▾ → Seed customer replies**.

## Schedules

- `followup-dispatch` (DATASTORE, active) — each enqueued invoice runs the pipeline.
- `daily-scan` (TIME 09:00, paused) — resume for hands-off daily scanning.
- `heal` (TIME, paused) — resume for automatic retry of transient failures.

```bash
lemma schedules resume daily-scan
lemma schedules resume heal
```

## Deploy to lemma.work (cloud)

```bash
lemma pods export ./collections-pod            # structure round-trips
lemma servers select cloud && lemma auth login
lemma pods create collections-agent
lemma pods import ./collections-pod
bash ./collections-pod/seed/seed.sh            # file contents + records don't bundle — re-seed
lemma apps deploy collections-app ./collections-pod/apps/collections-app/index.html --yes
lemma surfaces upsert telegram --agent collections-concierge   # re-enable the chat surface
```

**Model runtime (cloud).** Agents run on a runtime profile. Groq's `llama-3.3-70b`
cannot do strict structured output through the cloud runtime, so the drafter is pinned
to `system:lemma` (Lemma credits) — it produces the structured draft reliably. To run
on your own provider instead, create a profile with a model that supports json_schema
structured output (e.g. `openai/gpt-oss-120b` on Groq) and set `agent_runtime.profile_id`
on the agents:

```bash
lemma runtime profiles create OPENAI_COMPATIBLE --name my-provider \
  --base-url <url> --api-key <key> --default-model <model> --model <model>
```

Re-do connector auth (Gmail/Slack) on cloud and re-enter channel ids in Settings.

## Verify (smoke test)

```bash
lemma functions run seed_demo --data '{"enqueue":true}'    # → customers + invoices, pipeline runs
# wait ~1 min, then:
lemma query run "select status,count(*) from drafts group by status"   # AUTO_SENT + PENDING_REVIEW
lemma query run "select status,count(*) from invoices group by status" # LEGAL appears for 30+ day
```

Expected hero state: ~6 auto-sent, ~3 awaiting approval, ~2 legal — visible the moment
the app opens.
