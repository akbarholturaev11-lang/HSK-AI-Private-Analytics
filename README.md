# HSK AI — Private Analytics Bridge

A **private** GitHub mirror of allowlisted aggregate statistics from HSK AI Admin PWA's Cloudflare R2 cache. ChatGPT Plus can read the sanitized files using the GitHub connector.

## Current verified behavior

- Source: Admin PWA **overview** aggregate, not raw user records or SQL.
- Bucket: `hsk-ai-statistics`; pinned namespace: `aaa307fc4e280589c3d89a8d`.
- Output: `data/latest.json`, `data/overview_weekly.json`, `data/summary.md`, and `data/status.json`; monthly/all-time files are added only when their snapshots exist and validate.
- **16 strictly allowlisted counts** (accounts, activity, Pro, payment records, course engagement).
- Snapshot freshness: 24 hours after its actual `generated_at`. If expired or absent, the workflow removes `latest.json` rather than displaying stale data as fresh.
- GitHub Action: every six hours UTC or manual run, with privacy/freshness unit tests. No new SQL is triggered by the workflow.
- Scope: current aggregates only; financial revenue, conversion rates, detailed experiment analytics and AI cost are **not** exported in this first version.

## Reading in ChatGPT

Allow access to this private repo using the GitHub connector. Request:

> Read `data/status.json` and `data/latest.json` in `akbarholturaev11-lang/HSK-AI-Private-Analytics`. Check `generated_at` and `expires_at`, then analyze the reported counts without inventing missing statistics.

## Secrets

GitHub → Settings → Secrets and variables → Actions → Repository secrets:

- `R2_ACCOUNT_ID`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`

Recommended: use a **read-only** Cloudflare R2 token scoped to the bucket. Never commit credentials or raw snapshots. GitHub Actions gets repository write access only for the sanitized output.

## Important limitation

HSK AI's original Redis/R2 aggregate cache is demand-driven. GitHub's six-hour poll **does not refresh the source statistics**. To update a snapshot, visit the corresponding protected Admin PWA Overview after its 24-hour TTL expires, or implement a separately reviewed server-side aggregate refresh. Always check `data/status.json` first.

## Tests

`python -m unittest discover -s tests -v`

## Security

This repository must remain **PRIVATE**. No identifiers, messages, receipts, arbitrary R2 objects, access tokens, or user-level rows are exported. The bridge is read-only with respect to Cloudflare R2 and is not an MCP connector.
