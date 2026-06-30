# Implementation Plan — Collections Agent

Build bottom-up; verify each layer before wiring the next. Pod: `collections-agent`
(id `019f0c73-8f07-7151-beee-d7d96c136d4f`, org `followup-lemma`).

> Progress notes:
> - **Grounding:** the local stack can't run Kreuzberg (image registry-gated), so the
>   semantic search index is unavailable. The drafter grounds by **direct file read by
>   path** of the `/knowledge/*.md` docs (works without Kreuzberg). Recorded in design §6.
> - **Gmail grant deferred:** `dispatch_followup`'s `connector:gmail` grant is removed
>   until the Gmail auth-config exists (Task 9); the in-app path works now and the
>   connector call is guarded behind `email_enabled` + try/except.

- [x] 1. Scaffold the pod bundle
  - `collections-pod/pod.json` written; pod shell created on backend; CLI context set
  - _Requirements: all (foundation)_

- [x] 2. Tables + runtime config
  - [x] 2.1 Authored `clients`, `invoices`, `drafts`, `interactions`, `pod_config`, `followup_queue`, `promises` (all shared)
  - [x] 2.2 Imported all 7 tables; verified columns + FK linkage + `query run`
  - [x] 2.3 Seeded `pod_config` singleton with safe defaults (email off, notify NONE)
  - _Requirements: 1, 3, 7, 10, 13, 16_

- [ ] 3. Knowledge files (RAG)
  - [x] 3.1 Authored `/knowledge` folder metadata; wrote `collection_policy.md`, `payment_terms.md`, `contracts/northwind_msa.md`
  - [x] 3.2 Uploaded the docs; verified `files cat` returns full text (direct-read grounding). NOTE: semantic indexing FAILED (Kreuzberg unavailable) — grounding uses direct read instead of search.
  - _Requirements: 4_

- [ ] 4. Deterministic functions
  - [x] 4.1 `classify_and_score` — verified; expanded to return full draft context (so the drafter gets authoritative facts, not fetched ones)
  - [x] 4.2 `validate_draft` — verified PASS + hallucination FAIL
  - [x] 4.3 `make_fallback_draft` — verified
  - [x] 4.4 `dispatch_followup` — verified in-app SENT + count bump + interaction
  - [x] 4.5 `flag_legal` — verified (sets LEGAL + logs)
  - [x] 4.6 `notify` — verified (degrades to logged when no channel)
  - [x] 4.7 `scan_overdue` — verified (enqueues overdue; refreshes stage)
  - [x] 4.8 `metrics_snapshot` — verified (totals/stage/risk/status)
  - [x] 4.9 `complete_queue_item`, `reject_draft` — workflow terminal helpers (verified in-flow)
  - [x] 4.10 `requeue_stuck` — self-heal: bounded retry then deterministic fallback so every invoice converges (verified: recovered 2 transient failures)
  - _Requirements: 2, 3, 5, 7, 8, 11, 15, 17_

- [ ] 5. Agents
  - [x] 5.1 `collections-drafter` — verified accurate (facts passed in; echoes source fields; passes validation). Grounding via direct file read.
  - [ ] 5.2 `reply-triager` — pending (Phase 3, with inbound)
  - [ ] 5.3 `digest-writer` — pending (Phase 3, with notifications)
  - _Requirements: 4, 14, 15_

- [x] 6. Workflow `collections-run`
  - [x] 6.1 Graph authored (classify → escalated?→legal → drafter → validate → fail?→fallback → route → review FORM/auto-dispatch); `workflows validate` clean
  - [x] 6.2 Imported; verified ALL branches via seeded fan-out: 5 auto-sent, 3 pending, 2 legal
  - [x] 6.3 Human approval verified: submit approve → run resumes → draft SENT. Waiting runs ↔ pending drafts 1:1 (no orphans)
  - _Requirements: 2, 5, 6, 8, 9_

- [ ] 7. Schedules & choreography
  - [x] 7.2 `followup-dispatch` DATASTORE on `followup_queue` INSERT → `collections-run` (verified reactive fan-out)
  - [ ] 7.1 `daily-scan` TIME → scan (wrapper workflow)
  - [ ] 7.4 `heal` TIME → requeue_stuck (wrapper workflow) for hands-off self-healing
  - [x] 7.3 Verified fan-out; test schedules to be paused after verification
  - _Requirements: 8, 11, 16, 17_

- [ ] 8. Operator app `collections-app`
  - [ ] 8.1 Write `DESIGN.md` (page map, scenarios, first-30s, states, tone)
  - [ ] 8.2 Scaffold Vite app; wire SDK + AuthGuard + live hooks
  - [ ] 8.3 Dashboard (KPIs from `metrics_snapshot`, stage/risk charts, live feed)
  - [ ] 8.4 Invoices queue (filters, delivery pills) + per-invoice actions (process/regenerate/tone)
  - [ ] 8.5 Client detail with interaction timeline
  - [ ] 8.6 Approvals inbox (workflow FORM render + submit)
  - [ ] 8.7 Legal queue
  - [ ] 8.8 Sent log + Audit feed
  - [ ] 8.9 Settings (pod_config editor)
  - [ ] 8.10 Build, walk every scenario in the browser, view it, deploy
  - _Requirements: 9, 10, 12, 13_

- [ ] 9. Connectors & surfaces (full reach)
  - [ ] 9.1 Gmail auth-config + account; re-add `connector:gmail` grant to `dispatch_followup`; wire send; README setup
  - [ ] 9.2 Gmail inbound WEBHOOK schedule → reply workflow (`reply-triager` → `handle_reply`)
  - [ ] 9.3 WhatsApp/Slack surface + `notify` channel (re-add connector grant); daily-digest schedule; legal ping
  - [ ] 9.4 Promise-to-pay: `handle_reply` creates promise; `promise-recheck` schedule
  - _Requirements: 7, 8, 14, 15, 16_

- [ ] 10. Seed, hero moment, and end-to-end verification
  - [ ] 10.1 `seed/seed.sh` — clients/invoices across stages, one in approval, one legal, knowledge uploads
  - [ ] 10.2 Run the success-criteria scenario end-to-end; confirm hero moment on open
  - [ ] 10.3 Write `README.md` runbook (setup, connector auth, seed, smoke test)
  - _Requirements: 17, 18_
