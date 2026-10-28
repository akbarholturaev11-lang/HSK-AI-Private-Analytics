# Owner-only HSK AI analytics refresh from ChatGPT

This feature is disabled until the HSK AI server change has been deployed and
the **same, independently generated random secret** has been stored in two
separate configuration systems:

1. Railway production service: `ADMIN_PWA_MANUAL_AGGREGATE_REFRESH_TOKEN`
   (48-128 characters; use a cryptographically random value).
2. This repository, Settings → Secrets and variables → Actions:
   `HSKAI_MANUAL_AGGREGATE_REFRESH_TOKEN` with the same value.

Do **not** paste the secret into this public repository, an issue, a PR, a chat,
or source control. Keep the R2 bucket private even though this sanitized
aggregate snapshot repository is now public.

## On-demand operation

After both deploy and secret provisioning, ChatGPT's connected GitHub tool can
create an issue in this repository whose exact title is:

`[HSK AI] Refresh analytics`

Only an issue authored by `akbarholturaev11-lang` (also checked against the
event actor) can start the privileged workflow. The request issue is closed on
successful refresh. Alternatively the owner may click "Run workflow" on
`.github/workflows/manual-refresh-request.yml`.

The workflow invokes the token-authenticated HSK AI production endpoint. The
backend **recomputes verified overall/overview weekly and monthly metrics**
even while existing 24-hour caches are valid, then writes private R2 with a
Redis-coordinated lease. The normal `sync-r2.yml` workflow is dispatched after
successful source refresh to copy only validated aggregate snapshots into the
public GitHub analytics archive.

A successful **request** workflow alone does not prove the GitHub archive is
current; confirm the subsequent `sync-r2.yml` run is successful, then read
`data/status.json`, `index.json`, `overview/latest.json` and their
`generated_at`/`expires_at` values.

## Failure behavior

- Missing/invalid secrets, wrong origin and untrusted issue authors: denied.
- Missing Redis distributed coordination or R2: backend fails closed.
- Failed source recomputation: old snapshot is left intact where possible; no
  fabricated or user-level data is published.
- If weekly succeeds but monthly fails, the endpoint returns an error and the
  automatic GitHub mirror dispatch is skipped. The successful weekly source
  refresh might already have been committed to private R2 and is not rolled
  back; retry only after inspecting the source error.
- This does not enable the previously disabled read-only MCP transport. The
  operation uses the existing GitHub connector issue-write capability.

## Deployment dependencies

The server endpoint is implemented separately in HSK-AI-Bot and must be merged,
CI tested and deployed **before** enabling this workflow. It is disabled by
default and intentionally does not accept browser cookies or arbitrary
platform/period parameters. No automatic Railway configuration or deployment is
performed by this repository.
