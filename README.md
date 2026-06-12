# Canon Component Discovery

Crawls Canon pages via sitemap, finds components inside a target container, screenshots each one, and produces a self-contained HTML report.

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
python3 analyze.py --pattern usa-shop

# Crawl a specific number of pages
python3 analyze.py --pattern usa-shop --limit 50

# Crawl every page in the sitemap
python3 analyze.py --pattern usa-shop --limit all

# Test a single URL without touching the sitemap
python3 analyze.py --pattern usa-shop --url https://www.usa.canon.com/shop/p/dp-v2730

# Custom output file name
python3 analyze.py --pattern ca-shop --out canada-report.html

# List all available patterns
python3 analyze.py --list-patterns
```

Output for a pattern named `usa-shop`:
- **`usa-shop.html`** — the HTML report
- **`dist/usa-shop/`** — one subfolder per component class, each containing PNG screenshots

`dist/` is cleared and rebuilt on every run.

---

## Patterns (`patterns.json`)

Each entry in `patterns.json` defines one crawl target.

### Flat format (single source)

```json
"usa-shop": {
  "label": "USA — Shop PDPs",
  "sitemap": "https://www.usa.canon.com/shop/media/sitemap/sitemap.xml",
  "url_filter": "/shop/p/",
  "container": "#pdp-description",
  "component_root": ".ccMaxWidth",
  "mode": "children"
}
```

| Field | Required | Description |
|---|---|---|
| `label` | no | Display name used in the report title |
| `sitemap` | yes | Full URL of the sitemap (index or regular) |
| `url_filter` | yes | Substring — only URLs containing this are crawled |
| `container` | yes | CSS selector for the section to search within |
| `component_root` | yes | CSS selector for the component wrapper(s) |
| `mode` | no | `"children"` (default) or `"elements"` — see below |
| `child_selectors` | no | Additional sub-elements to capture (see below) |

### Multi-source format

When content may live in different containers on different pages, use `sources` — an array of source objects. Each is tried in order and all results are combined.

```json
"usa-content": {
  "label": "USA — Content Pages",
  "sitemap": "https://www.usa.canon.com/content/canon/en.sitemap.consumer.xml",
  "url_filter": "/",
  "sources": [
    { "container": "#to-main-content", "component_root": ".ccMaxWidth", "mode": "children" },
    { "container": "#to-main-content", "component_root": ".aem-Grid",   "mode": "children" }
  ]
}
```

---

## Modes

### `children` (default)

Finds all `component_root` elements inside `container`, then collects their **immediate `div` children** as the components. Good for wrapper patterns like `#pdp-description > .ccMaxWidth > [components]`.

Special rules applied during child collection:
- **`variable-spacing-wrapper`** — the wrapper itself is skipped; its own immediate div children are used instead.
- **`rte-textImage-cmp`** — regardless of other classes on the element, it is always classified as `rte-textImage-cmp`.
- **`aem-GridColumn` / `aem-GridColumn--*`** — these AEM layout classes are stripped from the component name.

### `elements`

Each element matching `component_root` inside `container` **is** the component. No child traversal. Good for patterns like `#overview-product .pagebuilder-column-group`.

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

## Screenshots

- Saved to `dist/{pattern}/{component-name}/{page-slug}.png`
- If a component appears multiple times on one page: `{slug}-1.png`, `{slug}-2.png`, …
- Elements that are not visible or have zero size are skipped
- The `title` component is never screenshotted
- Before screenshotting, the following UI elements are hidden to avoid noise: cookie banners, chat buttons, product media blocks, sticky nav, etc.
- The full page is scrolled before any screenshots are taken so lazy-loaded images are present

---

## Report

The HTML report is self-contained (no external dependencies).

- **Summary chips** — total pages crawled, unique components found, pages with/without components
- **Sortable table** — one row per component, with count and sample screenshot thumbnail
- **Modal** — click any component row to cycle through all its screenshots with captions
- **Pages with components** accordion — expandable list of crawled pages that had components
- **Pages without components** accordion — expandable list of pages where nothing was found
