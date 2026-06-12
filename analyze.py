"""
components/analyze.py — Crawl Canon shop PDP pages, extract immediate child
div classes inside #pdp-description > .ccMaxWidth, and produce an HTML report.

Classification rules:
  - variable-spacing-wrapper divs are unwrapped; their immediate div children
    are treated as the actual components
  - Any element whose class list includes rte-textImage-cmp is recorded as
    just "rte-textImage-cmp" (other classes on that element are ignored)

Screenshots:
  - Saved to dist/{component}/{page-slug}.png
  - Multiple of the same component on one page: {slug}-1.png, {slug}-2.png …
  - "title" components are excluded from screenshots
  - dist/ is cleared and recreated on every run

Usage:
    python3 analyze.py                    # first 20 /shop/p/ pages (default)
    python3 analyze.py --limit 100
    python3 analyze.py --out report.html
"""

import argparse
import asyncio
import json
import random
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

try:
    from playwright_stealth import Stealth
    async def _stealth(page):
        await Stealth().apply_stealth_async(page)
except ImportError:
    async def _stealth(page):
        pass

PATTERNS_FILE = Path(__file__).parent / "patterns.json"


def load_patterns() -> dict:
    if not PATTERNS_FILE.exists():
        raise FileNotFoundError(f"patterns.json not found at {PATTERNS_FILE}")
    with open(PATTERNS_FILE) as f:
        return json.load(f)


def get_pattern(key: str) -> dict:
    patterns = load_patterns()
    if key not in patterns:
        available = ", ".join(patterns)
        raise SystemExit(f"ERROR: pattern '{key}' not found. Available: {available}")
    return patterns[key]
BROWSER_UA  = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SKIP_SCREENSHOT  = {"title", "breadcrumb"}
HIDE_SELECTORS   = ["#usntA40Toggle", "#Chat_Image_Button", "#onetrust-consent-sdk", ".wrap-media-product-info", ".page-anchors-top", ".tabsContainer", ".product.media", ".product-info-main.pdp-info", "#embedded-messaging", ".inc_pdp_block", ".header.aem-GridColumn"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _scroll_page(page) -> None:
    """
    Scroll down the full page in viewport-sized steps so lazy-loaded images
    have a chance to load, then return to the top before screenshotting.
    """
    viewport_h = await page.evaluate("window.innerHeight")
    total_h    = await page.evaluate("document.body.scrollHeight")
    position   = 0
    while position < total_h:
        position  = min(position + viewport_h, total_h)
        await page.evaluate(f"window.scrollTo(0, {position})")
        await page.wait_for_timeout(250)
        total_h = await page.evaluate("document.body.scrollHeight")
    await page.evaluate("window.scrollTo(0, 0)")
    await page.wait_for_timeout(400)


async def _hide_elements(page) -> None:
    """Inject CSS to hide fixed UI chrome that would appear in screenshots."""
    css = ", ".join(HIDE_SELECTORS) + " { display: none !important; }"
    await page.add_style_tag(content=css)


def _classify(cls_list: list[str]) -> str:
    """Return the canonical component name for a list of CSS classes."""
    filtered = [c for c in cls_list
                if c != "aem-GridColumn" and not c.startswith("aem-GridColumn--")]
    if "rte-textImage-cmp" in filtered:
        return "rte-textImage-cmp"
    if not filtered:
        return "(no class)"
    return " ".join(filtered)


def _comp_to_folder(comp: str) -> str:
    """Return a filesystem-safe folder name for a component class string."""
    return comp.replace(" ", "_").replace("(", "").replace(")", "")


def _url_slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1] or "index"


def _apply_url_rewrite(urls: list[str], rewrite: dict) -> list[str]:
    suffix = rewrite.get("trailing_slash")
    if not suffix:
        return urls
    result = []
    for u in urls:
        stripped = u.rstrip("/")
        last_segment = stripped.rsplit("/", 1)[-1]
        if "." not in last_segment:
            result.append(stripped + suffix)
        else:
            result.append(u)
    return result


def clear_dist(dist_dir: Path) -> None:
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True)
    print(f"  dist/ cleared and recreated at {dist_dir.resolve()}\n")


# ---------------------------------------------------------------------------
# Sitemap helpers
# ---------------------------------------------------------------------------

async def _fetch_text(context, url: str) -> str:
    page = await context.new_page()
    await _stealth(page)
    try:
        resp = await page.goto(url, wait_until="commit", timeout=30_000)
        raw  = await resp.body()
        return raw.decode("utf-8", errors="replace")
    finally:
        await page.close()


def _locs(xml: str) -> list[str]:
    soup = BeautifulSoup(xml, "lxml-xml")
    locs = [t.get_text(strip=True) for t in soup.find_all("loc")]
    if not locs:
        soup = BeautifulSoup(xml, "html.parser")
        locs = [t.get_text(strip=True) for t in soup.find_all("loc")]
    return [u for u in locs if u.startswith("http")]


async def _collect_pdp_urls(
    context, limit: int | None, sitemap_url: str,
    url_filter: str, url_exclude: list[str] | None = None,
) -> list[str]:
    """Fetch sitemap(s) using an existing browser context and return matching URLs."""
    print(f"Fetching sitemap: {sitemap_url} …")
    xml = await _fetch_text(context, sitemap_url)

    if "<sitemapindex" in xml:
        child_sitemaps = _locs(xml)
        print(f"  Sitemap index — {len(child_sitemaps)} child sitemaps")
        all_urls: list[str] = []
        for child_url in child_sitemaps:
            print(f"  {child_url.split('/')[-1]} … ", end="", flush=True)
            try:
                child_locs = _locs(await _fetch_text(context, child_url))
                all_urls.extend(child_locs)
                pdp_count  = sum(1 for u in all_urls if url_filter in u)
                print(f"{len(child_locs)} URLs  (matching so far: {pdp_count})")
                if limit is not None and pdp_count >= limit:
                    break
            except Exception as e:
                print(f"FAILED ({e})")
    else:
        all_urls = _locs(xml)

    def _keep(u: str) -> bool:
        if url_filter not in u:
            return False
        if url_exclude and any(ex in u for ex in url_exclude):
            return False
        return True

    pdp_urls = [u for u in all_urls if _keep(u)]
    seen: set[str] = set()
    unique: list[str] = []
    for u in pdp_urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    if url_exclude:
        print(f"  Excluded patterns: {', '.join(url_exclude)}")
    if limit is None:
        print(f"\n  {len(unique)} unique {url_filter} URLs\n")
    else:
        using = min(limit, len(unique))
        print(f"\n  {len(unique)} unique {url_filter} URLs — using {using}\n")
    return unique if limit is None else unique[:limit]


# ---------------------------------------------------------------------------
# Page analysis + screenshots
# ---------------------------------------------------------------------------

async def _get_component_elements(cc_handle) -> list:
    """
    Return (element_handle, comp_name) pairs from the immediate div children
    of cc_handle, unwrapping any variable-spacing-wrapper divs one level.
    """
    pairs: list[tuple] = []
    children = await cc_handle.query_selector_all(":scope > div")
    for child in children:
        cls_list = await child.evaluate("el => Array.from(el.classList)")
        if "variable-spacing-wrapper" in cls_list:
            inner = await child.query_selector_all(":scope > div")
            for el in inner:
                inner_cls = await el.evaluate("el => Array.from(el.classList)")
                pairs.append((el, _classify(inner_cls)))
        else:
            pairs.append((child, _classify(cls_list)))
    return pairs


async def _analyze_page(context, url: str, dist_dir: Path,
                        sources: list[dict], pre_click: list[str] | None = None) -> list[str]:
    page = await context.new_page()
    await _stealth(page)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2_000)
        consent = await page.query_selector("#onetrust-accept-btn-handler")
        if consent:
            await consent.click()
            await page.wait_for_timeout(500)
        for sel in (pre_click or []):
            el = await page.query_selector(sel)
            if el:
                await el.click()
                await page.wait_for_timeout(150)
        await _scroll_page(page)
        await _hide_elements(page)

        all_pairs: list[tuple] = []
        for source in sources:
            container      = source["container"]
            component_root = source["component_root"]
            mode           = source.get("mode", "children")
            child_selectors = source.get("child_selectors") or []

            root = await page.query_selector(container)
            if not root:
                print(f"      ⚠  {container} not found")
                continue

            if mode == "elements":
                els = await root.query_selector_all(component_root)
                if not els:
                    print(f"      ⚠  no {component_root} elements found inside {container}")
                    continue
                pairs: list[tuple] = []
                for el in els:
                    cls_list = await el.evaluate("el => Array.from(el.classList)")
                    pairs.append((el, _classify(cls_list)))
                    for child_def in child_selectors:
                        sel  = child_def["selector"]
                        name = child_def.get("name")
                        for child_el in await el.query_selector_all(sel):
                            if name:
                                comp_name = name
                            else:
                                child_cls = await child_el.evaluate("el => Array.from(el.classList)")
                                comp_name = _classify(child_cls)
                            pairs.append((child_el, comp_name))
            else:
                ccs = await root.query_selector_all(component_root)
                if not ccs:
                    print(f"      ⚠  {component_root} not found inside {container}")
                    continue
                pairs = []
                for cc in ccs:
                    pairs.extend(await _get_component_elements(cc))
                if not pairs:
                    print(f"      ⚠  {component_root} found but contains no immediate div children")
                    continue

            all_pairs.extend(pairs)

        pairs = all_pairs
        if not pairs:
            return []

        # Count per component folder to decide whether to use index in filenames
        slug = _url_slug(url)
        folder_counts: dict[str, int] = defaultdict(int)
        for _, comp in pairs:
            folder_counts[_comp_to_folder(comp)] += 1

        comp_idx: dict[str, int] = defaultdict(int)
        for el, comp in pairs:
            folder_name = _comp_to_folder(comp)
            comp_idx[folder_name] += 1

            if comp not in SKIP_SCREENSHOT:
                comp_dir = dist_dir / folder_name
                comp_dir.mkdir(parents=True, exist_ok=True)

                n       = comp_idx[folder_name]
                total   = folder_counts[folder_name]
                fname   = f"{slug}-{n}.png" if total > 1 else f"{slug}.png"

                try:
                    if not await el.is_visible():
                        continue
                    box = await el.bounding_box()
                    if not box or box["width"] == 0 or box["height"] == 0:
                        continue
                    await el.scroll_into_view_if_needed()
                    await el.screenshot(path=str(comp_dir / fname))
                except Exception as e:
                    print(f"      Screenshot failed ({comp}): {e}")

        return [comp for _, comp in pairs]

    except Exception as e:
        print(f"    ERROR: {e}")
        return []
    finally:
        await page.close()


CONTEXT_REFRESH_EVERY = 100


async def _new_context(browser):
    return await browser.new_context(
        user_agent=BROWSER_UA, viewport={"width": 1440, "height": 900}, locale="en-US",
    )


async def collect_and_crawl(
    dist_dir: Path,
    sources: list[dict],
    *,
    sitemap_url: str = "",
    url_filter: str = "/shop/p/",
    url_exclude: list[str] | None = None,
    limit: int | None = 20,
    sample: int | None = None,
    url_override: str = "",
    pre_click: list[str] | None = None,
    url_rewrite: dict | None = None,
) -> tuple[list[str], list[dict]]:
    """Fetch sitemap and crawl pages in a single browser session to avoid bot detection."""
    results: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="chrome", headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await _new_context(browser)

        if url_override:
            urls = [url_override.strip()]
            print(f"Single-URL mode: {urls[0]}\n")
        else:
            if sample is not None:
                all_urls = await _collect_pdp_urls(context, None, sitemap_url, url_filter, url_exclude)
                all_urls = _apply_url_rewrite(all_urls, url_rewrite or {})
                urls = random.sample(all_urls, min(sample, len(all_urls)))
                print(f"  Random sample: {len(urls)} of {len(all_urls)} URLs\n")
            else:
                urls = await _collect_pdp_urls(context, limit, sitemap_url, url_filter, url_exclude)
                urls = _apply_url_rewrite(urls, url_rewrite or {})
            if not urls:
                await browser.close()
                return [], []

        print(f"Crawling {len(urls)} page(s) …\n{'─'*60}")
        for i, url in enumerate(urls, 1):
            if i > 1 and (i - 1) % CONTEXT_REFRESH_EVERY == 0:
                await context.close()
                context = await _new_context(browser)
                print(f"  (context refreshed)\n")
            print(f"  [{i:>3}/{len(urls)}] {url}")
            components = await _analyze_page(context, url, dist_dir, sources, pre_click)
            results.append({"url": url, "components": components})
            if components:
                print(f"           {len(components)} component(s): {', '.join(components)}")
            else:
                print(f"           — no components found")

        await browser.close()

    return urls, results


# ---------------------------------------------------------------------------
# Screenshot index
# ---------------------------------------------------------------------------

def scan_screenshots(dist_dir: Path) -> dict[str, list[str]]:
    """
    Walk dist/ and return {folder_name: [relative_path, ...]} for every
    component folder that contains PNG files.  Paths are relative to the
    HTML file (e.g. "dist/rte-textImage-cmp/some-product.png").
    """
    result: dict[str, list[str]] = {}
    if not dist_dir.exists():
        return result
    for comp_dir in sorted(dist_dir.iterdir()):
        if not comp_dir.is_dir():
            continue
        images = sorted(f for f in comp_dir.glob("*.png"))
        if images:
            result[comp_dir.name] = [
                f"{dist_dir}/{comp_dir.name}/{img.name}" for img in images
            ]
    return result


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

REPORT_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif;
    font-size: 13px; color: #1e293b; background: #f8fafc;
}
.top-bar {
    background: #0f172a; padding: 14px 32px; position: sticky; top: 0; z-index: 10;
}
.top-bar h1 { color: #f1f5f9; font-size: 15px; font-weight: 700; }
.top-bar .meta { color: #64748b; font-size: 11px; margin-top: 2px; }
.content { padding: 24px 32px; }

/* Summary chips */
.summary-chips { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }
.chip {
    border-radius: 6px; padding: 8px 16px; font-weight: 600; font-size: 13px;
    display: flex; flex-direction: column; align-items: center; gap: 2px;
}
.chip .chip-num { font-size: 22px; font-weight: 800; }
.chip .chip-lbl { font-size: 11px; font-weight: 500; }

/* Controls */
.controls { display:flex; gap:10px; margin-bottom:12px; flex-wrap:wrap; align-items:center; }
.controls input {
    padding: 5px 10px; border: 1px solid #e2e8f0;
    border-radius: 6px; font-size: 12px; flex: 1; min-width: 200px;
}
.visible-count { color:#64748b; font-size:12px; }

/* Table */
.table-wrap { overflow-x: auto; margin-bottom: 32px; }
table {
    width: 100%; border-collapse: collapse;
    background: #fff; border: 1px solid #e2e8f0;
    border-radius: 8px; overflow: hidden;
}
thead th {
    background: #f1f5f9; text-align: left; padding: 9px 12px;
    font-weight: 600; font-size: 12px; border-bottom: 2px solid #e2e8f0;
    white-space: nowrap; cursor: pointer; user-select: none;
}
thead th:hover { background: #e2e8f0; }
thead th.sorted-asc::after  { content: " ▲"; font-size:10px; }
thead th.sorted-desc::after { content: " ▼"; font-size:10px; }
thead th.no-sort { cursor: default; }
thead th.no-sort:hover { background: #f1f5f9; }
tbody tr { border-bottom: 1px solid #f1f5f9; }
tbody tr:hover td { background: #f8fafc; }
tbody tr.hidden { display: none; }
td { padding: 8px 12px; vertical-align: top; }
td.comp-cell { font-family: monospace; font-size: 12px; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.total-row td { font-weight: 700; background: #f1f5f9; border-top: 2px solid #e2e8f0; }

/* Coverage bar */
.cov-wrap { display:flex; align-items:center; gap:8px; min-width:120px; }
.cov-bar-bg { flex:1; background:#e2e8f0; border-radius:3px; height:6px; }
.cov-bar { background:#3b82f6; border-radius:3px; height:6px; }
.cov-pct { font-size:11px; color:#64748b; width:36px; text-align:right; flex-shrink:0; }

/* Screenshot button */
.shot-btn {
    background: #eff6ff; border: 1px solid #bfdbfe; color: #1e40af;
    border-radius: 4px; padding: 2px 7px; font-size: 11px; cursor: pointer;
    margin-left: 8px; white-space: nowrap; vertical-align: middle;
}
.shot-btn:hover { background: #dbeafe; }

/* Pages detail row */
tr.pages-row > td {
    background: #f8fafc; padding: 6px 16px 10px 28px;
    border-bottom: 1px solid #e2e8f0;
}
tr.pages-row:hover > td { background: #f1f5f9; }
.pages-grid {
    display: grid; grid-template-columns: repeat(2, 1fr);
    gap: 3px 16px; margin-top: 4px;
}
.page-link {
    color: #3b82f6; text-decoration: none; font-size: 11px;
    font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.page-link:hover { text-decoration: underline; }
details.pages-more summary {
    cursor: pointer; color: #64748b; font-size: 11px; user-select: none;
}
details.pages-more summary:hover { color: #334155; }
details.pages-more[open] summary { margin-bottom: 4px; }

/* Accordions */
details.accordion { margin-bottom: 14px; }
details.accordion > summary {
    cursor: pointer; list-style: none;
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 16px; background: #f1f5f9;
    border: 1px solid #e2e8f0; border-radius: 8px;
    font-size: 13px; font-weight: 600; color: #334155;
    user-select: none;
}
details.accordion > summary::-webkit-details-marker { display: none; }
details.accordion[open] > summary { border-radius: 8px 8px 0 0; border-bottom-color: #e2e8f0; }
details.accordion > summary:hover { background: #e2e8f0; }
details.accordion > summary::after { content: '▶'; font-size: 10px; color: #94a3b8; }
details.accordion[open] > summary::after { content: '▼'; }
.accordion-body {
    border: 1px solid #e2e8f0; border-top: none;
    border-radius: 0 0 8px 8px; padding: 14px;
    background: #fff;
}
.summary-count { color: #64748b; font-size: 12px; font-weight: 500; margin-right: 8px; }

/* Per-page cards */
.page-card {
    background: #fff; border: 1px solid #e2e8f0; border-radius: 8px;
    margin-bottom: 10px; overflow: hidden;
}
.page-card-header {
    padding: 8px 14px; background: #f1f5f9; border-bottom: 1px solid #e2e8f0;
    font-family: monospace; font-size: 12px;
    display: flex; justify-content: space-between; align-items: center;
}
.page-card-header a {
    color: #1e40af; text-decoration: none;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.page-card-header a:hover { text-decoration: underline; }
.page-card-body { padding: 8px 14px; display: flex; flex-wrap: wrap; gap: 6px; }
.comp-tag {
    background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe;
    border-radius: 4px; padding: 2px 8px; font-size: 11px; font-family: monospace;
}
.empty-links { display: flex; flex-direction: column; gap: 5px; }
.empty-link {
    color: #3b82f6; text-decoration: none;
    font-size: 12px; font-family: monospace;
}
.empty-link:hover { text-decoration: underline; }

/* Modal */
.modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,0.78);
    z-index: 1000; display: flex; align-items: center; justify-content: center;
}
.modal-overlay[hidden] { display: none; }
.modal-box {
    background: #fff; border-radius: 10px; padding: 24px 24px 18px;
    max-width: 92vw; max-height: 92vh;
    position: relative; min-width: 360px;
    display: flex; flex-direction: column; gap: 10px;
}
.modal-close {
    position: absolute; top: 10px; right: 14px;
    background: none; border: none; font-size: 20px; line-height: 1;
    cursor: pointer; color: #64748b;
}
.modal-close:hover { color: #0f172a; }
.modal-title {
    font-weight: 700; font-size: 13px; font-family: monospace;
    color: #1e293b; padding-right: 28px;
}
.modal-img-wrap {
    display: flex; align-items: center; gap: 10px; flex: 1; min-height: 0;
}
.modal-img {
    flex: 1; max-width: 100%; max-height: 70vh;
    object-fit: contain; border: 1px solid #e2e8f0; border-radius: 6px;
    display: block; min-width: 0;
}
.modal-nav {
    background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 6px;
    padding: 10px 14px; cursor: pointer; font-size: 22px; color: #334155;
    flex-shrink: 0; line-height: 1;
}
.modal-nav:hover:not(:disabled) { background: #e2e8f0; }
.modal-nav:disabled { opacity: 0.25; cursor: default; }
.modal-footer { text-align: center; }
.modal-caption { font-family: monospace; font-size: 12px; color: #475569; }
.modal-counter { font-size: 11px; color: #94a3b8; margin-top: 2px; }
"""

REPORT_JS = """
// ── Table filtering & sorting ────────────────────────────────────────────────
const rows     = Array.from(document.querySelectorAll('tbody tr[data-comp]'));
const subRows  = Array.from(document.querySelectorAll('tbody tr.pages-row'));
const searchEl = document.getElementById('search');
const countEl  = document.getElementById('visible-count');

function applyFilters() {
    const q = searchEl.value.toLowerCase();
    let visible = 0;
    rows.forEach(row => {
        const show = !q || row.dataset.comp.toLowerCase().includes(q);
        row.classList.toggle('hidden', !show);
        if (show) visible++;
    });
    subRows.forEach(row => {
        const parent = document.querySelector(`tr[data-comp="${row.dataset.parent}"]`);
        row.classList.toggle('hidden', !parent || parent.classList.contains('hidden'));
    });
    countEl.textContent = visible + ' components';
}
searchEl.addEventListener('input', applyFilters);

let sortCol = 'pages', sortDir = -1;
document.querySelectorAll('thead th[data-col]').forEach(th => {
    th.addEventListener('click', () => {
        if (sortCol === th.dataset.col) { sortDir *= -1; }
        else { sortCol = th.dataset.col; sortDir = sortCol === 'comp' ? 1 : -1; }
        document.querySelectorAll('thead th').forEach(t =>
            t.classList.remove('sorted-asc', 'sorted-desc'));
        th.classList.add(sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
        sortTable();
    });
});

function sortTable() {
    const tbody    = document.querySelector('tbody');
    const totalRow = document.querySelector('tr.total-row');
    rows.slice().sort((a, b) => {
        if (sortCol === 'comp') return sortDir * a.dataset.comp.localeCompare(b.dataset.comp);
        return sortDir * ((parseFloat(a.dataset[sortCol]) || 0) - (parseFloat(b.dataset[sortCol]) || 0));
    }).forEach(row => {
        tbody.appendChild(row);
        subRows.filter(sr => sr.dataset.parent === row.dataset.comp)
               .forEach(sr => tbody.appendChild(sr));
    });
    if (totalRow) tbody.appendChild(totalRow);
    applyFilters();
}

// ── Screenshot modal ─────────────────────────────────────────────────────────
const SCREENSHOTS = JSON.parse(document.getElementById('screenshot-data').textContent);
const modal       = document.getElementById('screenshot-modal');
const modalImg    = modal.querySelector('.modal-img');
const modalTitle  = modal.querySelector('.modal-title');
const modalCap    = modal.querySelector('.modal-caption');
const modalCtr    = modal.querySelector('.modal-counter');
const modalPrev   = modal.querySelector('.modal-prev');
const modalNext   = modal.querySelector('.modal-next');

let _images = [], _idx = 0;

function openModal(folder) {
    _images = SCREENSHOTS[folder] || [];
    if (!_images.length) return;
    _idx = 0;
    modalTitle.textContent = folder.replace(/_/g, ' ');
    _renderModal();
    modal.removeAttribute('hidden');
}

function _renderModal() {
    const src = _images[_idx];
    modalImg.src = src;
    modalCap.textContent = src.split('/').pop().replace(/\\.png$/i, '');
    modalCtr.textContent = `${_idx + 1} / ${_images.length}`;
    modalPrev.disabled = _idx === 0;
    modalNext.disabled = _idx === _images.length - 1;
}

function modalNav(dir) {
    _idx = Math.max(0, Math.min(_images.length - 1, _idx + dir));
    _renderModal();
}

function closeModal() { modal.setAttribute('hidden', ''); }

modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });

document.addEventListener('keydown', e => {
    if (modal.hasAttribute('hidden')) return;
    if (e.key === 'Escape')      closeModal();
    if (e.key === 'ArrowLeft')   modalNav(-1);
    if (e.key === 'ArrowRight')  modalNav(1);
});
"""


def _build_html(
    components: list[tuple[str, list[str]]],
    page_results: list[dict],
    total_pages: int,
    screenshots_by_comp: dict[str, list[str]],
    region: str = "",
) -> str:
    now         = datetime.now().strftime("%Y-%m-%d %H:%M")
    region_label = region.upper() if region else "Canon Shop"
    pages_with_data   = sum(1 for r in page_results if r["components"])
    total_occurrences = sum(len(pages) for _, pages in components)

    # ── Summary chips ────────────────────────────────────────────────────────
    chips_html = f"""
        <div class="chip" style="background:#eff6ff;color:#1e40af;">
            <span class="chip-num">{total_pages}</span>
            <span class="chip-lbl">Pages Crawled</span>
        </div>
        <div class="chip" style="background:#f0fdf4;color:#166534;">
            <span class="chip-num">{pages_with_data}</span>
            <span class="chip-lbl">Pages with Components</span>
        </div>
        <div class="chip" style="background:#f1f5f9;color:#334155;">
            <span class="chip-num">{len(components)}</span>
            <span class="chip-lbl">Unique Components</span>
        </div>
        <div class="chip" style="background:#fef9c3;color:#854d0e;">
            <span class="chip-num">{total_occurrences}</span>
            <span class="chip-lbl">Total Occurrences</span>
        </div>"""

    # ── Component table rows ─────────────────────────────────────────────────
    rows_html = ""
    for comp, pages in components:
        comp_esc    = _esc(comp)
        folder_name = _comp_to_folder(comp)
        page_cnt    = len(pages)
        pct         = page_cnt / total_pages * 100 if total_pages else 0
        occ         = sum(r["components"].count(comp) for r in page_results)

        cov_html = (
            f'<div class="cov-wrap">'
            f'<div class="cov-bar-bg"><div class="cov-bar" style="width:{pct:.1f}%"></div></div>'
            f'<span class="cov-pct">{pct:.0f}%</span>'
            f'</div>'
        )

        shot_count = len(screenshots_by_comp.get(folder_name, []))
        shot_btn   = (
            f'<button class="shot-btn" onclick="openModal(\'{folder_name}\')">'
            f'&#128247; {shot_count}</button>'
            if shot_count else ""
        )

        rows_html += (
            f'<tr data-comp="{comp_esc}" data-pages="{page_cnt}" data-occ="{occ}">'
            f'<td class="comp-cell">{comp_esc}{shot_btn}</td>'
            f'<td class="num">{occ}</td>'
            f'<td class="num">{page_cnt}</td>'
            f'<td>{cov_html}</td>'
            f'</tr>\n'
        )

        def page_link(u: str) -> str:
            path = u.split("usa.canon.com")[-1] if "usa.canon.com" in u else u
            return (f'<a href="{_esc(u)}" target="_blank" rel="noopener" '
                    f'class="page-link" title="{_esc(u)}">{_esc(path)}</a>')

        preview   = pages[:5]
        extra     = pages[5:]
        prev_html = "\n".join(page_link(u) for u in preview)
        expand    = ""
        if extra:
            extra_html = "\n".join(page_link(u) for u in extra)
            expand = (
                f'<details class="pages-more">'
                f'<summary>+{len(extra):,} more</summary>'
                f'<div class="pages-grid">{extra_html}</div>'
                f'</details>'
            )
        rows_html += (
            f'<tr class="pages-row" data-parent="{comp_esc}">'
            f'<td colspan="4"><div class="pages-grid">{prev_html}</div>{expand}</td>'
            f'</tr>\n'
        )

    rows_html += (
        f'<tr class="total-row"><td>TOTAL</td>'
        f'<td class="num">{total_occurrences}</td>'
        f'<td class="num">{total_pages}</td><td></td></tr>\n'
    )

    # ── Pages section — two accordions ──────────────────────────────────────
    found    = [r for r in page_results if r["components"]]
    not_found = [r for r in page_results if not r["components"]]

    def page_card(r: dict) -> str:
        url  = r["url"]
        path = url.split("usa.canon.com")[-1] if "usa.canon.com" in url else url
        tags = "".join(f'<span class="comp-tag">{_esc(c)}</span>' for c in r["components"])
        return (
            f'<div class="page-card">'
            f'<div class="page-card-header">'
            f'<a href="{_esc(url)}" target="_blank" rel="noopener">{_esc(path)}</a>'
            f'<span style="color:#64748b;font-size:11px;flex-shrink:0;margin-left:12px">'
            f'{len(r["components"])} component(s)</span>'
            f'</div>'
            f'<div class="page-card-body">{tags}</div>'
            f'</div>'
        )

    found_body    = "\n".join(page_card(r) for r in found) if found else "<p style='color:#94a3b8;font-size:12px'>None</p>"
    no_found_body = (
        '<div class="empty-links">'
        + "\n".join(
            f'<a href="{_esc(r["url"])}" class="empty-link" target="_blank" rel="noopener">'
            f'{_esc(r["url"].split("usa.canon.com")[-1] if "usa.canon.com" in r["url"] else r["url"])}'
            f'</a>'
            for r in not_found
        )
        + "</div>"
    ) if not_found else "<p style='color:#94a3b8;font-size:12px'>None</p>"

    pages_section = f"""
<details class="accordion" open>
  <summary>
    Pages with components found
    <span class="summary-count">{len(found)} page{"s" if len(found) != 1 else ""}</span>
  </summary>
  <div class="accordion-body">{found_body}</div>
</details>
<details class="accordion">
  <summary>
    Pages with no components found
    <span class="summary-count">{len(not_found)} page{"s" if len(not_found) != 1 else ""}</span>
  </summary>
  <div class="accordion-body">{no_found_body}</div>
</details>"""

    # ── Screenshot data embedded as JSON ────────────────────────────────────
    shot_json = json.dumps(screenshots_by_comp, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{region_label} — Canon Component Discovery</title>
<style>{REPORT_CSS}</style>
</head>
<body>

<div class="top-bar">
  <h1>{region_label} — Canon Component Discovery</h1>
  <div class="meta">Generated {now} &nbsp;·&nbsp; {total_pages} PDP pages crawled &nbsp;·&nbsp; #pdp-description &gt; .ccMaxWidth immediate children</div>
</div>

<div class="content">
  <div class="summary-chips">{chips_html}</div>

  <div class="controls">
    <input id="search" type="text" placeholder="Search components…">
    <span class="visible-count" id="visible-count">{len(components)} components</span>
  </div>

  <div class="table-wrap">
  <table id="comp-table">
    <thead>
      <tr>
        <th data-col="comp">Component Class</th>
        <th data-col="occ" class="sorted-desc" style="text-align:right">Occurrences</th>
        <th data-col="pages" style="text-align:right">Pages</th>
        <th class="no-sort">Coverage</th>
      </tr>
    </thead>
    <tbody>
{rows_html}
    </tbody>
  </table>
  </div>

  {pages_section}
</div>

<!-- Screenshot modal -->
<div id="screenshot-modal" class="modal-overlay" hidden>
  <div class="modal-box">
    <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    <div class="modal-title"></div>
    <div class="modal-img-wrap">
      <button class="modal-nav modal-prev" onclick="modalNav(-1)">&#8249;</button>
      <img class="modal-img" src="" alt="">
      <button class="modal-nav modal-next" onclick="modalNav(1)">&#8250;</button>
    </div>
    <div class="modal-footer">
      <div class="modal-caption"></div>
      <div class="modal-counter"></div>
    </div>
  </div>
</div>

<script id="screenshot-data" type="application/json">{shot_json}</script>
<script>{REPORT_JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover component classes on Canon shop PDP pages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 analyze.py --pattern usa-shop
  python3 analyze.py --pattern ca-shop --limit 50
  python3 analyze.py --pattern usa-shop --limit all
  python3 analyze.py --pattern ca-shop --sample 300
  python3 analyze.py --pattern usa-shop --url https://www.usa.canon.com/shop/p/dp-v2730
  python3 analyze.py --list-patterns
        """,
    )
    parser.add_argument("--pattern", default="",
                        help="Pattern key from patterns.json (e.g. usa-shop, ca-shop)")
    parser.add_argument("--list-patterns", action="store_true",
                        help="Print all available patterns and exit")
    parser.add_argument("--url", default="",
                        help="Analyze a single URL instead of fetching from the sitemap")
    parser.add_argument("--limit", default="20",
                        help="Pages to crawl: a number, or 'all' (default: 20)")
    parser.add_argument("--sample", default="",
                        help="Randomly sample N pages from the full sitemap URL pool")
    parser.add_argument("--out", default="",
                        help="Output HTML file (default: {pattern}.html)")
    args = parser.parse_args()

    # ── --list-patterns ──────────────────────────────────────────────────────
    if args.list_patterns:
        patterns = load_patterns()
        print(f"Available patterns ({PATTERNS_FILE}):\n")
        for key, cfg in patterns.items():
            print(f"  {key:<20} {cfg.get('label', '')}")
            print(f"  {'':20} sitemap:        {cfg.get('sitemap', '')}")
            print(f"  {'':20} url_filter:     {cfg.get('url_filter', '')}")
            print(f"  {'':20} container:      {cfg.get('container', '')}")
            print(f"  {'':20} component_root: {cfg.get('component_root', '')}\n")
        raise SystemExit(0)

    if not args.pattern:
        parser.error("--pattern is required (use --list-patterns to see options)")

    pattern = get_pattern(args.pattern)
    label       = pattern.get("label", args.pattern)
    sitemap_url = pattern["sitemap"]
    url_filter  = pattern["url_filter"]
    url_exclude = pattern.get("url_exclude", [])
    url_rewrite = pattern.get("url_rewrite", {})
    pre_click   = pattern.get("pre_click", [])

    if "sources" in pattern:
        sources = pattern["sources"]
    else:
        sources = [{
            "container":      pattern["container"],
            "component_root": pattern["component_root"],
            "mode":           pattern.get("mode", "children"),
            "child_selectors": pattern.get("child_selectors", []),
        }]

    out_path = Path(args.out) if args.out else Path(f"{args.pattern}.html")
    dist_dir = Path("dist") / args.pattern

    print(f"Pattern: {args.pattern}  ({label})")
    print(f"Preparing {dist_dir}/ …")
    clear_dist(dist_dir)

    if args.url:
        urls, results = asyncio.run(collect_and_crawl(
            dist_dir, sources, url_override=args.url, pre_click=pre_click,
        ))
    elif args.sample:
        try:
            sample = int(args.sample)
        except ValueError:
            print(f"ERROR: --sample must be a number, got '{args.sample}'")
            raise SystemExit(1)
        urls, results = asyncio.run(collect_and_crawl(
            dist_dir, sources,
            sitemap_url=sitemap_url, url_filter=url_filter, url_exclude=url_exclude,
            sample=sample, pre_click=pre_click, url_rewrite=url_rewrite,
        ))
    else:
        raw = args.limit.strip().lower()
        if raw == "all":
            limit: int | None = None
        else:
            try:
                limit = int(raw)
            except ValueError:
                print(f"ERROR: --limit must be a number or 'all', got '{args.limit}'")
                raise SystemExit(1)

        urls, results = asyncio.run(collect_and_crawl(
            dist_dir, sources,
            sitemap_url=sitemap_url, url_filter=url_filter, url_exclude=url_exclude,
            limit=limit, pre_click=pre_click, url_rewrite=url_rewrite,
        ))
        if not urls:
            print(f"No URLs matching '{url_filter}' found — exiting.")
            raise SystemExit(1)

    # Aggregate: component -> deduplicated list of pages it appears on
    component_pages: dict[str, list[str]] = defaultdict(list)
    for r in results:
        seen_on_page: set[str] = set()
        for comp in r["components"]:
            if comp not in seen_on_page:
                component_pages[comp].append(r["url"])
                seen_on_page.add(comp)

    sorted_components = sorted(component_pages.items(), key=lambda x: -len(x[1]))

    print(f"\n{'─'*60}")
    print(f"  {'COMPONENT':<50} {'PAGES':>6}")
    print(f"  {'─'*57}")
    for comp, pages in sorted_components:
        print(f"  {comp:<50} {len(pages):>6}")

    screenshots_by_comp = scan_screenshots(dist_dir)

    out_path.write_text(_build_html(sorted_components, results, len(urls), screenshots_by_comp, label))
    print(f"\nReport       → {out_path}  (open with: open {out_path})")
    print(f"Screenshots  → {dist_dir.resolve()}/")
    total_shots = sum(len(v) for v in screenshots_by_comp.values())
    print(f"               {total_shots} screenshot(s) across {len(screenshots_by_comp)} component folder(s)")


if __name__ == "__main__":
    main()
