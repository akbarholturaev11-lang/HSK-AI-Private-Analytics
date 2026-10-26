# HSK AI — Private Analytics

This **private** repository provides ChatGPT Plus with verified, privacy-filtered
statistics from HSK AI Admin PWA's Cloudflare R2 aggregate cache.

## Organized analytics

Data is grouped by **section**, **reporting window**, and **UTC observation timestamp**:

```text
index.json                         ← global navigation and source/availability flags
indexes/2026-10.json               ← all recorded October 2026 snapshots
overview/latest.json               ← latest *fresh weekly* overview, if available
overview/periods/weekly.json       ← most recent *fresh* weekly overview
overview/history/2026/10/
  2026-10-26T010159760210Z_weekly.json
users/latest.json
users/history/2026/10/...
sales/latest.json
sales/history/2026/10/...
payments/latest.json
payments/history/2026/10/...
courses/latest.json
courses/history/2026/10/...
marketing/README.md                ← no verified marketing data yet
data/latest.json                   ← backward-compatible legacy weekly overview
data/status.json                   ← R2 source freshness state
```

**Crucial interpretation:** the date in the historical file name is when the
aggregate was **generated**, NOT a one-day total. Weekly reports use the previous
rolling seven days; monthly reports use the previous 30 days. All-time snapshots
may contain some current/rolling-window measures. Do **not** add overlapping
windows, claim that historical values are live or invent gaps in the calendar.

Available categories are **overview**, **users**, **sales**, **payments**, and
**courses**. All category metrics derive strictly from the approved 16 counters
of the Admin PWA overview projection. "Sales" provides current paid Pro and
period-scoped paying-account counts, not monetary revenue or a conversion rate.
"Payments" provides payment-record counts, not distinct buyers. Marketing does
not have an approved source yet and is **not_instrumented** in `index.json`.

The root index links monthly index files; each monthly index lists complete
history file paths and their period start/end evidence. Historical files are
immutable: identical snapshots are reused; conflicting values at the same
timestamp fail the run. The `latest.json` aliases disappear on expiration,
while historical observations remain for longitudinal analysis.

## Reading data from ChatGPT Plus

Give the GitHub connector permission to access this private repo. For example:

> Open `index.json` in `akbarholturaev11-lang/HSK-AI-Private-Analytics`.
> For October, open `indexes/2026-10.json` and the relevant category snapshots.
> Compare only compatible metrics and reporting windows, distinguish historical
> observations from live data, and identify missing days rather than filling them in.

## Synchronization and credentials

- Source bucket: `hsk-ai-statistics`.
- Pinned, verified overview cache namespace in `.github/workflows/sync-r2.yml`.
- GitHub Actions runs every six hours UTC, or manually. This **does not** cause
  a new database aggregate to be computed. The Admin PWA R2 cache is demand-driven.
- Private repository Actions secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`,
  `R2_SECRET_ACCESS_KEY`.
- Prefer a **read-only** R2 key scoped to this one bucket. Never commit credentials
  or raw R2 snapshots.
- Run tests: `python -m unittest discover -s tests -v`.

## Limits

- The initial archive can only contain snapshots observed after archive
  activation; past unsaved dates cannot be reconstructed from present data.
- No person-level identifiers, feedback text, receipts, user rows, tokens,
  unverified attribution, marketing or revenue metrics are exported.
- Archives persist, so repository size will increase over time; review the
  retention/indexing design before it grows to tens of thousands of files.
- R2 or credential failures stop the workflow without publishing fabricated data.
