# Project Overview

This project crawls a site's pages (driven by `patterns.json`) and generates a self-contained-per-run HTML report cataloging component usage — one row per component with occurrence/page counts and a screenshot gallery — to inform EDS block development.

## Main workflow

1. Configure one or more crawl targets in [`patterns.json`](patterns.json) — see the README's [Patterns](README.md#patterns-patternsjson), [Modes](README.md#modes), and [Nested captures](README.md#nested-captures) sections for the field reference.
2. Run `python3 analyze.py --pattern <pattern>`.
3. Review the generated `<pattern>.html` report — it opens automatically via a local server (see [Local server](README.md#local-server)).
4. Optionally run `python3 analyze.py --index` for a landing page linking to every pattern's report.

The crawler fetches a sitemap, visits matching pages in a single browser session, identifies components inside a chosen container (by CSS class or by attribute), screenshots each occurrence as WebP, and writes `dist/<pattern>/summary.json` plus one `dist/<pattern>/{component}/data.json` per component for the report to load client-side (see [Screenshots](README.md#screenshots) and [Report](README.md#report)).

## Key files

- [`analyze.py`](analyze.py) — crawler, screenshot capture, JSON data output, and the report/index HTML templates (`REPORT_CSS`/`REPORT_JS`, `_build_html`, `_build_index_html`).
- [`patterns.json`](patterns.json) — crawl target configuration, including the shared `defaults` block (see README's [Shared defaults](README.md#shared-defaults)).
- [`README.md`](README.md) — setup, running, pattern format, modes, nested captures, screenshots, report/index behavior, local server, crawl behaviour.
- [`requirements.txt`](requirements.txt) — Python dependencies.

## Development guidance

This tool is scoped to Canon's shop and content sites — README examples intentionally reference real Canon patterns (`usa-pdp`, `ca-pdp`, etc.) rather than generic placeholders; keep new examples concrete in the same way. Report output (`*.html`, `dist/`) is generated; don't hand-edit it. `patterns.json` is user-owned crawl configuration — don't modify it unless asked. After changes to `analyze.py`, sanity-check with `python3 -c "import ast; ast.parse(open('analyze.py').read())"` before relying on a real crawl to catch syntax errors.
