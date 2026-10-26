# Marketing analytics — pending verified data

This category is intentionally **not instrumented** in the first release.

The currently approved Cloudflare R2 source is the HSK AI Admin PWA **overview**
projection. It does not provide independently verified campaign attribution,
traffic source conversion, marketing spend, or A/B experiment outcomes.

No marketing JSON snapshot is generated from unrelated user or payment counts.
The root `index.json` records `marketing.status = not_instrumented`.

To activate this section, audit a real marketing aggregate source, define
observation windows and attribution evidence, add a strict output allowlist,
and cover it with source/provenance regression tests.
