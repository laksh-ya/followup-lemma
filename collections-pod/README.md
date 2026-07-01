# Collections Agent — pod runbook

An AI accounts-receivable desk: it tracks customers and their overdue invoices,
classifies each by escalation stage, drafts a stage-appropriate follow-up grounded in
your policy docs, validates it against the source record (no hallucinated figures),
then auto-sends the safe ones and routes risky / final notices to a human. A team of
roles works one shared workspace; legal handles 30+ day accounts; replies are
understood and acted on.

App: deployed at `lemma apps open collections-app` (cloud: `https://collections-app.apps.lemma.work`).

> **Which pod?** App slugs are globally unique, and `lemma pods select` only lasts one
> shell. Set a persistent default so imports/deploys always hit the right pod:
> `lemma config set-default-org <org> && lemma config set-default-pod <pod>`. Verify
> with `lemma pods list` (the active one shows `yes`) before `lemma pods import` / `lemma apps deploy`.

## Interface (6 tabs + utility icons)

**Tabs:** Overview · Invoices · Approvals · Replies · Customers · Schedule.
**Top-right icons:** 📖 Setup guide · 💬 Ask agent · ⚙ Settings · ☾ theme.
The pod **boots empty** — load data via Google Sheets / CSV / manual, or click
**Data ▾ → 🌱 Seed random mails** to generate a sample book of customers, invoices & drafts.

| Team | Where | What they get |
|---|---|---|
| **Collector** | Invoices, Customers, Replies | The working queue; the AI has already triaged + drafted. |
| **Finance lead** | Approvals | Approve/edit/reject AI drafts (final notices never auto-send). Hit **Legal** to escalate — the account moves to the Legal & recovery section and the escalation is posted to Slack **#legal**. |
| **Finance lead** | Schedule | Send the daily stats digest to Slack **#daily-stats** (official connector) and Telegram; pick what to send and when; manual + scheduled. |
| **Admin** | Settings ⚙ | Behavior, mail mode (Gmail), Slack #legal channel, Telegram agent — all live. |
| **Anyone** | Ask 💬 (app) · Slack #chat · Telegram | Chat with the concierge about the book in plain language. |

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

In the app: **Run scan** triages overdue invoices; the **Data ▾** menu imports from
**Google Sheets**, imports/exports CSV, adds an invoice, or resets real data. Demo
data lives under the **🧪 Mock Mailbox** (seed / seed replies / clear).

## Loading real data

Three paths, all landing as real (`demo=false`) invoices deduped by customer email:

1. **Google Sheets** (primary) — Data ▾ → *Import from Google Sheets*. Reads on your
   connected Google account via the `google_sheets` connector; no keys in the app:
   ```bash
   lemma connectors auth-configs create google_sheets --name workspace-sheets
   lemma connectors connect-requests create google_sheets --auth-config-id <id>   # authorize in browser
   ```
   Paste the sheet link + A1 range; header row required (same columns as CSV below).
2. **CSV / Excel** — Data ▾ → *Import CSV / Excel*.
3. **Manual** — Data ▾ → *Add invoice*.

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

## Slack integration — three channels

One `slack` connector (LEMMA provider) powers three distinct flows. **All posts pin a
fixed workspace account** (`pod_config.slack_account_id`) rather than the invoking user —
so shared-channel posts and the unattended scheduled digest work regardless of who (or
what) triggers them. Connect once:

```bash
lemma connectors auth-configs create slack --name slack
lemma connectors connect-requests create slack --auth-config-id <id>   # authorize in browser
lemma connectors accounts list --app slack                             # copy the account id → pod_config.slack_account_id
```

| Channel | Purpose | How it's wired |
|---|---|---|
| **#legal** | Legal escalations. Hitting **Legal** on a draft (Approvals) posts the escalated mail here and moves the account to Legal & recovery. | `app_action` (`_notify`) → `chat_post_message`. Channel id in Settings ⚙. |
| **#daily-stats** | The daily stats digest — manual or scheduled. | `stats_dispatch` → `chat_post_message`. Channel id in the Schedule tab. |
| **#chat** | Two-way chat with the pod (concierge agent answers from live data). | A Slack **surface** routing `#chat` → `collections-concierge`. |

Invite the Slack app to each channel, then paste channel ids where noted. For **#chat**,
the surface needs a one-time webhook paste:

```bash
lemma surfaces upsert slack --agent collections-concierge --account <slack-account-id>
lemma surfaces channels slack --channel-id <C…> --channel-name chat --agent collections-concierge
lemma surfaces setup slack     # prints the webhook URL to paste into the Slack app's Event Subscriptions
```

## Telegram — chat + stats

- **Chat** — a Lemma **managed surface** (`collections-concierge`), no token/webhook:
  ```bash
  lemma surfaces upsert telegram --agent collections-concierge
  lemma surfaces setup telegram      # prints the bot link to message
  ```
  Already ACTIVE in this pod.
- **Stats digest** — the Telegram *connector* is surface-only (no send operation), so
  proactive stats posts use a **BotFather bot token** (stored in `pod_config`); the
  Schedule tab's Telegram card has a **Find chat id** button (`telegram_get_chat`).

## Stats digest (Schedule tab)

Pick *what to send* (metric checklist) and *when to send* (freq + time + weekday) per
destination (Slack #daily-stats, Telegram), with **Send now** / **Send test** and a
**Send now to all enabled** button. The `stats-dispatch` schedule (cron `*/30 * * * *`,
ships **paused**) checks each destination's window and fires the due ones:

```bash
lemma schedules resume stats-dispatch     # pause again: lemma schedules pause stats-dispatch
```

## Chat with the agent — in-app Ask, Slack #chat, Telegram

The `collections-concierge` agent answers questions about the live book (overdue
accounts, a customer's status, the legal queue, pending approvals) through three front
doors: the in-app **💬 Ask** tab (`lemma-agent-thread`), **Slack #chat**, and **Telegram**
(both surfaces routed to the same agent). Read-only Q&A; it reports, it doesn't act.

## Inbound replies (closing the loop)

The Replies tab shows classified customer replies (promise-to-pay / dispute / paid /
question). With a Gmail mailbox connected, a WEBHOOK schedule on new mail feeds the
`reply-triager` agent which classifies and updates the account. For a demo without a
mailbox, use **Data ▾ → 💬 Seed customer replies**.

## Schedules

- `followup-dispatch` (DATASTORE, active) — each enqueued invoice runs the pipeline.
- `daily-scan` (TIME 09:00, paused) — resume for hands-off daily scanning.
- `heal` (TIME, paused) — resume for automatic retry of transient failures.
- `stats-dispatch` (TIME `*/30 * * * *`, paused) — the stats-sharing sweep (see Schedule tab).

```bash
lemma schedules resume daily-scan
lemma schedules resume heal
lemma schedules resume stats-dispatch
```

## Deploy to lemma.work (cloud)

```bash
lemma pods export ./collections-pod            # structure round-trips
lemma servers select cloud && lemma auth login
lemma pods create collections-agent
lemma pods import ./collections-pod
bash ./collections-pod/seed/seed.sh            # file contents + records don't bundle — re-seed
lemma apps deploy collections-app ./collections-pod/apps/collections-app/index.html --yes
lemma surfaces upsert telegram --agent collections-concierge                       # Telegram chat surface
lemma surfaces upsert slack --agent collections-concierge --account <slack-acct>   # Slack #chat surface
```

After importing, reconnect connectors (Gmail `workspace-gmail`, Slack `slack`, Google
Sheets `workspace-sheets`), then set `pod_config.slack_account_id` to the connected Slack
account id and paste the #legal / #daily-stats channel ids in Settings / Schedule.

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

Re-do connector auth on cloud (Gmail `workspace-gmail`, Slack `slack`, Google Sheets
`workspace-sheets`) and re-enter channel ids / destinations in Settings and the Schedule tab.

## Verify (smoke test)

```bash
lemma functions run seed_demo --data '{"enqueue":true}'                          # sample customers + invoices, pipeline runs
# wait ~1 min, then:
lemma query run "select status,count(*) from drafts group by status"            # AUTO_SENT + PENDING_REVIEW
lemma functions run stats_dispatch --data '{"mode":"test","channel":"SLACK"}'    # posts a test digest to #daily-stats
lemma functions run app_action --data '{"action":"notify","note":"legal","config":{"message":"test"}}'  # posts to #legal
```

Both Slack posts should return `delivered: true` / `sent via Slack` once
`pod_config.slack_account_id` is set and the channel ids are in place. Seeded rows show in
the normal tabs (one shared workspace — no sandbox). Reset everything with
`lemma functions run reset_pod --data '{"confirm":true}'`.
