# Canon Component Discovery

A Playwright-based crawler that maps AEM component usage across Canon shop and content pages, producing a visual HTML report used to inform EDS block development via [da.live](https://da.live).

## Reports

Each report below is a thin HTML shell — run the matching pattern (or `--index`, see below) to (re)generate its data before opening it.

### USA
| Report | Description |
|---|---|
| [usa-pdp.html](usa-pdp.html) | All PDPs, specifically the "Overview" tab content |
| [usa-business.html](usa-business.html) | All "/business" pages |
| [usa-consumer-1.html](usa-consumer-1.html) | Content pages — `/newsroom` and `/learning` excluded |
| [usa-consumer-2.html](usa-consumer-2.html) | Content pages — `/newsroom` excluded |
| [usa-learning.html](usa-learning.html) | Learning pages — random sample of 80 |
| [usa-newsroom.html](usa-newsroom.html) | Newsroom pages — random sample of 30 |
| [usa-cvi.html](usa-cvi.html) | CVI site pages |

### CA
| Report | Description |
|---|---|
| [ca-pdp.html](ca-pdp.html) | All PDPs — product overview section |
| [ca-overview.html](ca-overview.html) | All PDPs — product overview section (unfiltered) |

Or open [index.html](index.html) for a landing page linking to every pattern's report at once.

---

## Intent

The Canon shop and content pages are built on AEM with a set of reusable components. As part of migrating to Edge Delivery Services (EDS), these components need to be recreated as EDS blocks. This tool crawls live pages, identifies which AEM component classes appear (and how often), and screenshots each one in context — giving the team a concrete visual reference for every variation a block needs to handle and a frequency-based view of where to prioritise effort.

---

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Running

All runs require a `--pattern` flag that selects which site/region/page-type to crawl.

```bash
# Default — first 20 pages
python3 analyze.py --pattern usa-pdp

# Crawl a specific number of pages
python3 analyze.py --pattern usa-pdp --limit 50

# Crawl every page in the sitemap
python3 analyze.py --pattern usa-pdp --limit all

# Randomly sample 300 pages from the full sitemap URL pool
python3 analyze.py --pattern ca-pdp --sample 300

# Crawl 12 pages at a time instead of the default 6 (large runs finish faster;
# see "Concurrency" below for tuning guidance)
python3 analyze.py --pattern usa-pdp --sample 1800 --concurrency 12

# Test a single URL without touching the sitemap
python3 analyze.py --pattern usa-pdp --url https://www.usa.canon.com/shop/p/dp-v2730

# Custom output file name
python3 analyze.py --pattern ca-pdp --out canada-report.html

# Generate the report without starting the local server / opening a browser
python3 analyze.py --pattern usa-pdp --no-serve

# List all available patterns
python3 analyze.py --list-patterns

# Generate index.html linking to every pattern's report (see below)
python3 analyze.py --index
```

Output for a pattern named `usa-pdp`:
- **`usa-pdp.html`** — the report shell (loads its data client-side, see below)
- **`dist/usa-pdp/summary.json`** — the high-level breakdown the report loads on open (components, occurrence/page counts, per-page results)
- **`dist/usa-pdp/{component}/data.json`** — that component's screenshot list (path + page URL per shot), fetched only when its gallery is opened
- **`dist/usa-pdp/{component}/`** — one subfolder per component class, each containing WebP screenshots (+ a `_thumbs/` subfolder)

`dist/{pattern}/` is cleared and recreated at the start of every run for that pattern — other patterns' `dist/` folders are untouched.

Because the report data lives in separate JSON files rather than being baked into the HTML, the report template (`REPORT_CSS`/`REPORT_JS`/`_build_html` in `analyze.py`) can be edited and reloaded in the browser against a previous run's `dist/` output, without re-running the crawl.

### Local server

By default, after crawling, `analyze.py` starts a local HTTP server rooted at the current directory and opens the report in your browser (`http://127.0.0.1:<port>/...`). This is required because the report's `fetch()` calls for `summary.json`/`data.json` are blocked by Chrome when a report is opened directly as a `file://` page. The server keeps running (`Ctrl+C` to stop) so galleries can still be opened lazily after the crawl finishes. Pass `--no-serve` to skip this (e.g. for scripted/batch runs).

### Index page

`python3 analyze.py --index` writes `index.html` — a landing page listing every pattern in `patterns.json` (skips crawling entirely). Like the reports, it's a thin shell: it fetches `patterns.json` for the pattern list, then for each one checks whether `dist/{pattern}/summary.json` exists — if so it links to `{pattern}.html` with live stats (pages crawled, component count, generated time); if not, it shows the pattern as not-yet-generated along with the command to run. Because this check happens client-side on load, `index.html` itself never needs to be regenerated as you crawl more patterns — just reload it.

---

## Patterns (`patterns.json`)

Each entry in `patterns.json` defines one crawl target. The Claude Code `/pattern` skill (`.claude/skills/pattern/`) can walk you through adding a new one interactively.

### Flat format (single source)

```json
"usa-pdp": {
  "label": "USA — PDPs",
  "sitemap": "https://www.usa.canon.com/shop/media/sitemap/sitemap.xml",
  "url_filter": "/shop/p/",
  "container": "#pdp-description",
  "component_root": ".ccMaxWidth",
  "mode": "children",
  "pre_click": ["#tab-description"]
}
```

| Field | Required | Description |
|---|---|---|
| `label` | no | Display name used in the report title |
| `sitemap` | yes | Full URL of the sitemap (index or regular) |
| `url_filter` | yes | Substring — only URLs containing this are crawled |
| `url_exclude` | no | List of substrings — URLs matching any of these are skipped |
| `url_rewrite` | no | Object of rewrite rules applied to sitemap URLs before crawling. Currently supports `"trailing_slash": ".html"` — replaces a trailing `/` with the given suffix |
| `container` | yes | CSS selector for the section to search within. Can also be a list of selectors tried in order — the first one found on the page is used |
| `component_root` | yes | CSS selector for the component wrapper(s) |
| `mode` | no | `"children"` (default) or `"elements"` — see below |
| `identify_by` | no | `"class"` (default) — name components from their joined CSS class list — or `"attribute"` — name them from the value of `identify_attr` |
| `identify_attr` | no | Attribute to read the component name from when `identify_by` is `"attribute"` (e.g. `automation-testid`) |
| `exclude_selectors` | no | List of selectors — any matched component inside one of these (checked via `closest()`) is skipped entirely, e.g. nav/footer/banner regions |
| `top_level_only` | no | `elements` mode only. When true, a match is skipped if an ancestor also matches `component_root` — keeps only the outermost of a nested pair |
| `nested_captures` | no | `elements` mode only. Per-component drill-down rules — see [Nested captures](#nested-captures) |
| `child_selectors` | no | Additional sub-elements to capture (see below) |
| `pre_click` | no | List of CSS selectors to click before analysis — use for tabs or accordions that gate content |

### Multi-source format

When content may live in different containers on different pages, use `sources` — an array of source objects (each one a "capture group"). Each is tried in order and all results are combined. Any field valid in the flat format above (`container`, `component_root`, `mode`, `exclude_selectors`, `nested_captures`, etc.) can be set per-source here too.

```json
"usa-cvi": {
  "label": "USA — CVI",
  "sitemap": "https://www.cvi.canon.com/sitemap.xml",
  "url_filter": "/",
  "url_rewrite": { "trailing_slash": ".html" },
  "sources": [
    { "container": "#to-main-content", "component_root": ".aem-Grid", "mode": "children" }
  ]
}
```

### Shared defaults

A top-level `"defaults"` key (a sibling of the pattern entries, not a pattern itself — hidden from `--list-patterns`) can hold `exclude_selectors` and/or `nested_captures` shared across every capture group (every source, whether from the flat format or a `sources` array):

```json
{
  "defaults": {
    "exclude_selectors": ["footer", "#sub-nav"],
    "nested_captures": { "grid-wrapper": { "selector": "...", "limit": 2 } }
  },
  "usa-pdp": { "...": "..." }
}
```

Each source merges its own `exclude_selectors`/`nested_captures` on top of `defaults` rather than replacing them:
- `exclude_selectors` — concatenated (defaults first, then the source's own entries as additions)
- `nested_captures` — merged by key: a source can override one named rule from defaults (by re-specifying that key) or add brand-new ones; any key it doesn't mention still falls through to defaults

A source that defines neither field just inherits `defaults` as-is.

---

## Modes

### `children` (default)

Finds all `component_root` elements inside `container`, then collects their **immediate `div` children** as the components. Good for wrapper patterns like `#pdp-description > .ccMaxWidth > [components]`.

Special rules applied during child collection:
- **`variable-spacing-wrapper`** — the wrapper itself is skipped; its own immediate div children are used instead.
- **`rte-textImage-cmp`** — regardless of other classes on the element, it is always classified as `rte-textImage-cmp`.
- **`aem-GridColumn` / `aem-GridColumn--*`** — these AEM layout classes are stripped from the component name.

### `elements`

Each element matching `component_root` inside `container` **is** the component. No child traversal. Good for patterns like `#overview-product .pagebuilder-column-group`, or attribute-based matching like `[automation-testid]` (see `identify_by`/`identify_attr` above).

---

## Child selectors

`child_selectors` lets you capture additional sub-elements within each matched component as separate screenshot entries. Only applies in `elements` mode.

```json
"child_selectors": [
  { "selector": "[data-content-type='html']", "name": "html" }
]
```

| Field | Description |
|---|---|
| `selector` | CSS selector searched inside each matched component |
| `name` | Fixed component name to assign. If omitted, the element's own class list is used. |

---

## Nested captures

`nested_captures` (`elements` mode only) defines drill-down rules keyed by a component's identified name — for components that wrap other meaningful components and need more than a single flat screenshot.

```json
"nested_captures": {
  "grid-wrapper": {
    "selector": "[data-testid=\"grid-layout\"] > * > *",
    "limit": 2,
    "name": "grid-wrapper item"
  },
  "spacing-wrapper": {
    "selector": ":scope > *",
    "limit": 1,
    "skip_self": true,
    "name": "spacing-wrapper item"
  }
}
```

| Field | Required | Description |
|---|---|---|
| `selector` | yes | CSS selector (relative to the matched component) for the candidate children to drill into |
| `limit` | no | Max number of matches to capture. Omit for no limit |
| `skip_self` | no | When true, the wrapper itself isn't captured — only what's found via `selector` (a pure-wrapper "unwrap", e.g. a spacing/layout div with no visual identity of its own) |
| `name` | no | Fallback name for a captured child that has no `identify_attr` value of its own |

Rules compose automatically: a captured child is named by its own `identify_attr` value if it has one, otherwise by the rule's `name`. If that resulting name is itself a key in `nested_captures`, that child's own rule is applied recursively.

---

## Screenshots

- Saved as WebP (lossless, smaller than PNG with no quality loss) to `dist/{pattern}/{component-name}/{page-slug}.webp`
- A separate small, lossy-compressed thumbnail is generated for each screenshot at `dist/{pattern}/{component-name}/_thumbs/{page-slug}.webp`, used by the report's gallery grid so it doesn't have to load full-size images
- If a component appears multiple times on one page: `{slug}-1.webp`, `{slug}-2.webp`, …
- Elements that are not visible or have zero size are skipped
- The `title` component is never screenshotted
- Before screenshotting, the following UI elements are hidden to avoid noise: cookie banners, chat buttons, product media blocks, sticky nav, etc.
- The full page is scrolled before any screenshots are taken so lazy-loaded images are present

---

## Report

The report itself (`{pattern}.html`) is a static shell with no crawl data baked in — everything shown is fetched client-side from `dist/{pattern}/summary.json` and, lazily, `dist/{pattern}/{component}/data.json`. See [Local server](#local-server) for why it needs to be served rather than opened directly.

- **Summary chips** — total pages crawled, unique components found, pages with/without components
- **Sortable table** — one row per component, with count and sample screenshot thumbnail
- **Modal** — click a component's screenshot button to fetch and open a thumbnail grid of every screenshot for that component; clicking a thumbnail switches to a single-image viewer (prev/next, caption, and a link to the live page that screenshot was captured from) starting at that image, with a link back to the grid
- **Pages with components** accordion — expandable list of crawled pages that had components
- **Pages without components** accordion — expandable list of pages where nothing was found

---

## Crawl behaviour

### Single browser session

The sitemap fetch and all page crawls run inside **one browser instance and one browser context** (shared cookies/session), which is important for avoiding bot detection — see [Concurrency](#concurrency) for how multiple pages share it at once. This prevents the server from seeing two separate cold browser sessions in quick succession, which is a common bot-detection trigger. The sitemap XML is fetched with `wait_until="commit"` (response body only, no JavaScript execution) to minimise fingerprinting during that phase.

### Concurrency

Pages are crawled several at a time (in separate tabs of the same shared browser context, not separate sessions) rather than one at a time — controlled by `--concurrency` (default: 6). Raising it speeds up large runs roughly linearly (e.g. a 1,800-page `--sample` at the default concurrency of 6 takes roughly a sixth as long as crawling one page at a time); lowering it trades speed for a gentler request rate if you see more `ERROR`/`⚠` lines or timeouts than usual, which can indicate the target site is rate-limiting or flagging the crawl. `--url` and small `--limit` runs aren't meaningfully affected either way since there's little to parallelize.

### Cookie consent

The OneTrust consent banner is automatically accepted on each page before any interaction or screenshotting takes place. This ensures it cannot intercept clicks on tabs or other pre-click targets.

### Context refresh

To prevent accumulated browser state (cookies, service workers, cached scripts) from causing pages to fail after hundreds of navigations, the browser context is replaced with a fresh one every **100 pages** (i.e. between each batch of up to 100 concurrently-crawled pages). Cookie consent is re-handled automatically on the first page of each new context.
