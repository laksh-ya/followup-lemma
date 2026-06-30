#!/usr/bin/env bash
# Seed the collections-agent pod: knowledge docs + demo records, then kick the pipeline.
# Requires active CLI context (LEMMA_ORG_ID + LEMMA_POD_ID) and auth.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "==> uploading knowledge documents"
lemma files mkdir /knowledge 2>/dev/null || true
lemma files mkdir /knowledge/contracts 2>/dev/null || true
lemma files upload "$HERE/knowledge/collection_policy.md" /knowledge/collection_policy.md 2>/dev/null || true
lemma files upload "$HERE/knowledge/payment_terms.md" /knowledge/payment_terms.md 2>/dev/null || true
lemma files upload "$HERE/knowledge/contracts/northwind_msa.md" /knowledge/contracts/northwind_msa.md 2>/dev/null || true

echo "==> seeding clients + invoices + config"
python3 "$HERE/seed.py"

echo "==> scanning overdue invoices (kicks collections-run per invoice via the datastore trigger)"
lemma functions run scan_overdue --data '{"force": true}' >/dev/null
echo "==> seeded. The pipeline is processing in the background; drafts/approvals/legal flags appear within ~1 min."
