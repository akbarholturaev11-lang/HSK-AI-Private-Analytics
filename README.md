# HSK AI — Private Analytics Bridge

This **private** repository mirrors allowlisted aggregated HSK AI statistics from the private Cloudflare R2 bucket `hsk-ai-statistics` for use with the GitHub connector in ChatGPT Plus.

## What it does
- GitHub Actions checks R2 up to four times daily (this does **not** refresh the database).
- Reads only the R2 **basic overall statistics** namespace, validates the R2 cache envelope and source, and exports a strictly allowlisted subset.
- Writes `data/latest.json`, `data/summary.md`, and `data/status.json` if snapshots are fresh. Expired snapshots are never published as fresh.
- Never exports raw user accounts, identifiers, receipts, feedback, secrets or arbitrary R2 objects.

## Enable scheduled synchronization
1. Under this repository's **Settings → Secrets and variables → Actions**, create repository secrets: `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`. Use a **read-only Cloudflare R2 token** limited to `hsk-ai-statistics`, not the Railway write token.
2. The bucket and **24-character basic-statistics namespace** are pinned in the workflow. Verify they match the R2 console.
3. Open **Actions → Private R2 statistics sync → Run workflow**. Inspect `data/status.json` after completion.
4. Authorize this private repository in ChatGPT's GitHub connector. Ask ChatGPT to read `data/status.json` and `data/latest.json`, report `generated_at`/`expires_at` and analyze only actual available figures.

## Limits
The HSK AI R2 cache is **lazy**. GitHub sync reads snapshots already created when the Admin PWA statistics page was queried. It does not compute, refresh or query production data. Monthly/all-time output appears only if those variants have been cached. If all are missing/stale, status will be `unavailable`.

This repository must remain PRIVATE. Do not copy its data or workflow tokens into public HSK-AI-Bot.
