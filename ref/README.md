# Finance Collections Agent

An AI-powered accounts receivable system that generates escalation-aware follow-up emails for overdue invoices — automatically, consistently, and with a full audit trail.

**FastAPI · LangGraph · LiteLLM · Celery + Redis · Next.js 15**

[**Demo Video →**](https://drive.google.com/drive/u/0/folders/1RHEfpEfq5Wn40YZlYUmcjUFHud0su1lf) · [**GitHub →**](https://github.com/laksh-ya/finance-follow-up)

---

![Dashboard](docs/screenshots/dashboard.png)

---

## The Problem

Finance teams chase overdue invoices manually. The result: inconsistent tone, no audit trail, delayed escalations, hours burned on copy-paste emails. The challenge isn't generating one email — it's generating the right email, for the right client, at the right escalation level, every day, without hallucinating invoice data or sending a stern final notice to someone who's 3 days late.

---

## Architecture

![Architecture](docs/screenshots/architecture.png)

The system is built as a **modular pipeline** across 7 layers:

- **Ingestion** — CSV, Excel, or live Google Sheets sync
- **FastAPI backend** — REST API with auth, rate limiting, input sanitization
- **Celery + Redis** — async task queue with priority routing and Celery Beat scheduler
- **LangGraph pipeline** — 10-node deterministic state machine per invoice (see below)
- **LiteLLM gateway** — provider-agnostic LLM interface (Groq, Gemini, OpenAI, Ollama, Together)
- **Email dispatch** — three-mode: mock / SMTP sandbox / live Resend
- **Next.js dashboard** — 8-page operator UI with live feed, human review queue, observability

---

## The Pipeline

Every overdue invoice runs through a deterministic LangGraph state machine. The pipeline never fails silently — exhausted retries fall to a deterministic template, failed Celery tasks land in a dead-letter queue with a manual retry button.

```
load_invoice → classify_stage → build_prompt → call_llm → validate_output
                                                                   │
                                              valid?  ──→ confidence_check
                                              retry?  ──→ build_prompt (max 2x)
                                              failed? ──→ fallback_template
                                                                   │
                                           HIGH conf ──→ dispatch_email
                                           LOW / S4  ──→ human_queue
                                                                   │
                                                             write_audit
```

**Why LangGraph over alternatives:** the escalation logic maps cleanly to graph edges. The `InvoiceState` TypedDict is the explicit contract between all 10 nodes — no hidden state, no implicit passing. Conditional routing at two decision points (validation result, confidence score) without nested if-else chains. The retry loop is bounded by state (`retry_count <= 2`) and cannot run indefinitely. Compared to CrewAI/AutoGen — this isn't a multi-agent problem and adding that overhead would obscure the pipeline logic.

---

## Escalation Logic

| Stage | Days Overdue | Tone | Dispatch |
|---|---|---|---|
| Stage 1 | 1–7 | Warm & friendly | Auto |
| Stage 2 | 8–14 | Polite but firm | Auto |
| Stage 3 | 15–21 | Formal & serious | Auto (high-priority queue) |
| Stage 4 | 22–30 | Stern & urgent | Human review |
| Escalated | 30+ | No auto email | Flagged — no further automation |

Stage 4 emails always route to human review when human-in-the-loop is on. 30+ day invoices skip email generation entirely — pipeline sets status to DISPUTED and queues for manual handling.

---

## Email Generation & Validation

The LLM receives a structured system prompt with 10 strict rules, a stage-specific user prompt with all invoice fields injected, and a required JSON output schema.

**Anti-hallucination enforcement** is the core reliability mechanism. The LLM must echo `invoice_id_used`, `client_name_used`, `amount_used`, and `days_overdue_used` back in its output — these are cross-checked against the source record in code. Any mismatch fails validation and triggers a retry. The hallucination fields were added after noticing batch runs would occasionally transpose invoice IDs — cross-validating in code rather than relying on prompt instructions alone eliminated the failure mode.

Validation pipeline per response:

1. Pydantic parse — strict field types, length constraints, valid tone enum
2. Hallucination check — 4-field cross-validation against source invoice
3. Placeholder detection — rejects `[INSERT`, `TBD`, `PLACEHOLDER` in body
4. Retry loop — up to 2 re-generations on failure
5. Fallback template — deterministic output when retries exhaust, always routed to human review

Every email includes: client name, invoice number, amount, due date, days overdue, and a dynamic payment link — from data, never invented.

---

## LLM — Choice & Rationale

**Default:** `groq/llama-3.3-70b-versatile` via LiteLLM

| Criterion | Why Groq + Llama 3.3 70B |
|---|---|
| Cost | Free tier, no credit card — zero barrier for evaluation |
| Speed | ~1–1.5s end-to-end via Groq's LPU hardware — live regeneration feels instant |
| JSON output mode | Native `response_format: json_object` support — maps directly onto `GeneratedEmail` Pydantic schema |
| Quality | Tested across 330+ invoice pipeline runs (Langfuse) — strong adherence to exact-field-echo requirement |
| Context window | 128k — system prompt + invoice context + output schema with headroom |
| Portability | LiteLLM means zero code changes to switch provider |

Llama 3.3 70B outperforms 8B specifically on the exact-field-echo requirement that the hallucination check depends on. The latency delta on Groq's infrastructure is negligible.

**Alternatives considered:**

- `llama-3.1-8b-instant` — faster but less reliable JSON schema adherence on multi-field structured outputs
- `gemini-1.5-flash` — comparable, slightly higher hallucination rate on exact-field-echo in testing
- `gpt-4o-mini` — best quality but requires billing setup; unnecessary for this task

**Switching providers is two env vars:**

```bash
LLM__PROVIDER=gemini
LLM__MODEL=gemini/gemini-1.5-flash-latest
LLM__API_KEY=your_key
```

Works for Groq, Gemini, OpenAI, Together AI, or any Ollama model locally. The model picker in Settings switches the active provider without a restart.

---

## Prompt Design

System prompt structure (abbreviated):

```
You are an enterprise finance collections assistant.

STRICT RULES:
1. Never hallucinate invoice data. Use ONLY values provided.
2. invoice_id_used MUST equal exactly: {invoice_id}
3. client_name_used MUST equal exactly: {client_name}
4. amount_used MUST equal exactly: {amount}
5. days_overdue_used MUST equal exactly: {days_overdue}
6. Output ONLY valid JSON. No markdown. No preamble.
7. Tone MUST match: {tone_requirement}
8. Never mention legal action unless stage is STAGE_4.
... (10 rules total)
```

Each of the 4 stages has a user prompt template with the full invoice context injected. Tone, key message, and CTA differ per stage — warm and assumptive at Stage 1, credit-term consequences at Stage 3, legal escalation language only at Stage 4.

---

## Human Review Queue

![Review Queue](docs/screenshots/review-queue.png)

Low-confidence drafts, Stage 4 notices, and escalated invoices land here. The queue groups items by reason (escalated 30+ days, Stage 4 final notice, low confidence) so reviewers triage by urgency.

Actions per item:

- **Approve & send** — dispatches as-is, logged as human-approved
- **Edit first** — inline editor, edited version sent, original preserved in audit
- **New draft** — re-runs pipeline, previous draft superseded
- **Discard** — no email sent, invoice flagged
- **Legal** — sets account status, removes from email automation

---

## Email Delivery Modes

![Mailtrap](docs/screenshots/mailtrap.png)

Three modes, switchable live from Settings:

**Mock** — emails generated and logged, nothing sent. Default for development.

**Sandbox** — routes to a Mailtrap SMTP inbox. Real rendering, real deliverability metadata, client never receives anything. Used for the demo and all test runs.

**Live** — dispatches via Resend API. Requires a verified sender domain with SPF, DKIM, and DMARC.

The SMTP layer handles port variants automatically (465/587/2525) — works with Gmail app passwords, Zoho, Mailtrap, or any standard SMTP without code changes.

Every dispatch attempt is persisted with provider name, message ID, model used, token count, and delivery status.

![Sent Emails](docs/screenshots/sent-emails.png)

---

## Google Sheets Integration

Invoices ingested via CSV/Excel upload or live Google Sheets sync. The Sheets integration is bidirectional — pulls invoice rows on a 30-minute cron, writes audit entries back to a separate sheet, and updates invoice status/stage after dispatch.

A finance team can manage invoices entirely in Sheets and use this dashboard for monitoring and review only.

![Audit Sheets](docs/screenshots/audit-sheets.png)

---

## Queue Architecture

Celery Beat fires the daily scan; workers consume from priority queues independently. Stage 3+ invoices route to `high_priority` — they don't queue behind Stage 1 reminders.

```
Beat (9am cron) ──→ scan_and_enqueue
                          │
              ┌───────────┼──────────┐
              ▼           ▼          ▼
        high_priority   default   scheduler
       (Stage 3/4)    (Stage 1/2)
              │
          Worker pool
              │
      process_invoice
      ├── 3 retries, exponential backoff (60s → 120s → 240s)
      └── exhausted → dead_letter table (manually retryable)
```

**Celery Eager mode** (`CELERY_EAGER=true`) runs tasks synchronously in the API process — no Redis needed for local development.

---

## Observability

![Monitoring](docs/screenshots/monitoring.png)

**Langfuse** traces every LLM call — raw prompt, raw response, token counts, latency, validation outcome. 330 traces across all runs, $0.07 total cost at the time of this recording.

![Langfuse](docs/screenshots/langfuse.png)

The Monitoring page surfaces: total tokens, average latency, LLM calls vs fallbacks, dead-letter queue with retry, and direct links to Langfuse, Mailtrap inbox, Neon database, and Upstash Redis dashboards.

---

## Security

| Risk | Implementation |
|---|---|
| **Prompt injection** | All client-supplied fields pass through a validator stripping `ignore previous instructions`, `system:`, `{{`, `<\|` before reaching the prompt. JSON output mode prevents injection via response path. |
| **Hallucination** | LLM must echo all invoice fields in output. Cross-checked against source record in code. Retry loop + deterministic fallback + confidence threshold + human routing. |
| **API key exposure** | All secrets via `.env` and `pydantic_settings.BaseSettings`. `.env.example` committed with placeholders. `.env` in `.gitignore`. LLM keys never reach the browser. |
| **Unauthorised access** | `X-API-Key` header required on all API routes. Auth failures logged to activity feed — brute-force attempts visible. SlowAPI rate limits per endpoint. CORS locked to configured origins. |
| **PII in logs** | Email addresses masked in all log output (`r***@domain.com`). Full email body in DB only, never in stdout. |
| **Email spoofing** | Default is mock. Sandbox catches everything in Mailtrap. Live mode (Resend) requires domain verification with SPF, DKIM, DMARC. |

---

## Settings & Runtime Controls

![Settings](docs/screenshots/settings.png)

Everything live-configurable without restart:

- LLM provider, model, API key, confidence threshold
- Human-in-the-loop toggle
- Auto-dispatch toggle
- Email mode (mock / sandbox / live)
- **Test connections** — round-trips a real call to LLM, email, Redis, and Sheets individually with inline status + latency

---

## Running It

### Zero dependencies (2 min)

```bash
git clone https://github.com/laksh-ya/finance-follow-up
cd finance-follow-up
cp .env.example .env
make install
make dev
```

Frontend → http://localhost:3000 · API docs → http://localhost:8000/docs

Default: mock LLM, logged emails, inline tasks, SQLite. No accounts needed.

Upload `sample_data/invoices_sample.csv` → click **Process All** → watch the pipeline run across all escalation stages.

### Real LLM + sandbox email

```bash
# add to .env
LLM__PROVIDER=groq
LLM__MODEL=groq/llama-3.3-70b-versatile
LLM__API_KEY=gsk_...            # console.groq.com — free, no card

EMAIL_MODE=sandbox
MAILTRAP_HOST=sandbox.smtp.mailtrap.io
MAILTRAP_PORT=2525
MAILTRAP_USER=...               # mailtrap.io — free inbox
MAILTRAP_PASS=...
EMAIL_FROM=Finance Collections <collections@example.com>
```

### Real async queue

```bash
CELERY_EAGER=false
REDIS_URL=rediss://default:token@host.upstash.io:6379   # upstash.com — free serverless Redis

make worker    # separate terminal
make beat      # separate terminal
make flower    # optional — Celery UI at :5555
```

### Postgres (optional, SQLite works fine for dev)

```bash
DATABASE_URL=postgresql+psycopg2://user:pass@host/db?sslmode=require
# Neon (neon.tech) or Supabase — both free tier
```

### LLM tracing

```bash
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
# cloud.langfuse.com — free 50k events/month
```

### Google Sheets sync

1. Create a GCP service account → download `credentials.json` → place in `backend/`
2. Enable Google Sheets API on the project
3. Share both sheets (invoice + audit) with the service account email as Editor

```bash
SHEETS_ENABLED=true
SHEETS_CREDENTIALS_PATH=credentials.json
SHEETS_INVOICE_ID=your_sheet_id   # from URL: /d/{ID}/edit
SHEETS_AUDIT_ID=your_audit_sheet_id
```

### Full Docker stack

```bash
make docker-up
# Redis · Backend · Worker · Beat · Flower (:5555) · Frontend
```

### Production (Render + Vercel)

- **Backend**: Render Web Service — `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- **Worker**: Render Background Worker — `celery -A app.tasks.celery_app worker -Q high_priority,default,scheduler -c 4`
- **Beat**: Render Background Worker — `celery -A app.tasks.celery_app beat`
- **Frontend**: Vercel — root `frontend/`, set `NEXT_PUBLIC_API_URL` + `NEXT_PUBLIC_API_KEY`
- Set `CORS_ORIGINS` on backend to the Vercel frontend URL

---

## All Make Commands

```bash
make help          # all targets
make install       # backend + frontend deps
make dev           # both servers
make backend / frontend / worker / beat / flower
make seed          # seed demo invoices
make reset         # wipe DB
make test-llm      # test LLM connectivity
make test-email    # send test email
make docker-up / docker-down
make typecheck     # TypeScript check
make stop          # kill all services
```

---

## Screenshot Index

| File | What it shows |
|---|---|
| `dashboard.png` | KPI cards, stage breakdown, live activity feed |
| `review-queue.png` | Human review queue — escalated + stage 4 grouped |
| `review-queue-detail.png` | Stage 4 final notices with approve/edit/reject actions |
| `sent-emails.png` | Sent emails page — full body, provider, confidence badge |
| `mailtrap.png` | Mailtrap sandbox inbox — multiple emails received |
| `mailtrap-email.png` | Mailtrap individual email — rendered body |
| `monitoring.png` | LLM health, dead-letter queue, external dashboard links |
| `settings.png` | Model picker, confidence threshold, email mode |
| `settings-email.png` | Email delivery mode selector |
| `settings-connections.png` | Test connections panel |
| `langfuse.png` | Langfuse — 330 traces, $0.07 cost |
| `langfuse-early.png` | Langfuse — early run, 64 traces |
| `audit-sheets.png` | Google Sheets audit writeback |
| `sheets-invoices.png` | Google Sheets invoice source |
| `architecture.png` | Full architecture diagram |
| `onboarding.png` | Welcome modal — data source + HIL setup |
| `dashboard-empty.png` | Empty state with getting started guide |
| `upstash-redis.png` | Upstash Redis dashboard (Mumbai, free tier) |
| `gcp-service-account.png` | GCP service account for Sheets sync |