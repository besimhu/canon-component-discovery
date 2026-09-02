"""
components/analyze.py — Crawl a site's pages, identify components inside a
container per patterns.json, and produce an HTML report.

Component identification (per pattern, via `identify_by`):
  - "class"     (default) — the element's CSS class list, joined, is the name
  - "attribute" — the value of `identify_attr` (e.g. automation-testid) is
    the name

Other per-pattern knobs:
  - exclude_selectors — matched elements inside any of these are skipped
    entirely (checked via closest(), so nested matches are excluded too)
  - top_level_only — when set, an element is skipped if an ancestor also
    matches component_root (keeps only the outermost match in a nested pair)

Screenshots:
  - Saved to dist/{pattern}/{component}/{page-slug}.webp (+ a _thumbs/ subfolder)
  - Multiple of the same component on one page: {slug}-1.webp, {slug}-2.webp …
  - "title" components are excluded from screenshots
  - dist/{pattern}/ is cleared and recreated on every run

Usage:
    python3 analyze.py --pattern <name>   # first 20 matching pages (default)
    python3 analyze.py --pattern <name> --limit 100
    python3 analyze.py --pattern <name> --out report.html
"""

import argparse
import asyncio
import functools
import http.server
import json
import mimetypes
import random
import shutil
import time
import webbrowser
from collections import defaultdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from PIL import Image
from playwright.async_api import async_playwright

# Explicit, since http.server's MIME lookup otherwise depends on what's
# registered on the host OS/Python build.
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("image/webp", ".webp")

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
    available = {k: v for k, v in patterns.items() if k != "defaults"}
    if key not in available:
        raise SystemExit(f"ERROR: pattern '{key}' not found. Available: {', '.join(available)}")
    return available[key]


def get_defaults() -> dict:
    """Shared `exclude_selectors`/`nested_captures` from patterns.json's
    top-level "defaults" key, merged into every capture group (source) by
    `_merge_source_defaults` — see its docstring for merge semantics."""
    return load_patterns().get("defaults", {})


def _merge_source_defaults(source: dict, defaults: dict) -> dict:
    """
    Merge shared `defaults` into a single capture group (source) config:
      - exclude_selectors: concatenated — defaults first, then the source's
        own entries as additions.
      - nested_captures: merged by key — a source can override one named
        rule from defaults (by re-specifying that key) or add new ones,
        while any key it doesn't mention still falls through to defaults.
    """
    merged = dict(source)
    merged["exclude_selectors"] = [
        *defaults.get("exclude_selectors", []),
        *source.get("exclude_selectors", []),
    ]
    merged["nested_captures"] = {
        **defaults.get("nested_captures", {}),
        **source.get("nested_captures", {}),
    }
    return merged
BROWSER_UA  = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
SKIP_SCREENSHOT  = {"title", "breadcrumb"}
HIDE_SELECTORS   = ["#usntA40Toggle", "#Chat_Image_Button", "#onetrust-consent-sdk", ".wrap-media-product-info", ".page-anchors-top", ".tabsContainer", ".product.media", ".product-info-main.pdp-info", "#embedded-messaging", ".inc_pdp_block", ".page-header", ".header.aem-GridColumn", ".sections.nav-sections"]

# Screenshots are saved as lossless WebP (smaller than PNG with no quality
# loss). Thumbnails are a separate, small, lossy-compressed WebP render used
# for the gallery grid so it doesn't have to load full-size images.
SCREENSHOT_EXT     = "webp"
THUMB_DIRNAME      = "_thumbs"
THUMB_MAX_WIDTH    = 320
THUMB_QUALITY      = 70


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


CONTENT_WAIT_TIMEOUT_MS = 8_000


async def _wait_for_content(page, container_opts: list[str], component_root: str) -> None:
    """
    Wait until at least one of `container_opts` is present in the DOM *and*
    contains a `component_root` match. query_selector() alone checks the DOM
    at that exact instant — under concurrent crawling, tab content (e.g. a
    pre_click-revealed panel) can still be rendering, so an immediate check
    produces false "not found" negatives that vanish when the page is
    crawled alone. Swallows the timeout; callers still do their own
    query_selector afterward and report "not found" if it's genuinely absent.
    """
    try:
        await page.wait_for_function(
            "(args) => args.containers.some(c => { "
            "const r = document.querySelector(c); return r && r.querySelector(args.root); })",
            {"containers": container_opts, "root": component_root},
            timeout=CONTENT_WAIT_TIMEOUT_MS,
        )
    except Exception:
        pass


def _classify(cls_list: list[str]) -> str:
    """Return the canonical component name for a list of CSS classes."""
    filtered = [c for c in cls_list
                if c != "aem-GridColumn" and not c.startswith("aem-GridColumn--")]
    if "rte-textImage-cmp" in filtered:
        return "rte-textImage-cmp"
    if not filtered:
        return "(no class)"
    return " ".join(filtered)


def _save_screenshot(png_bytes: bytes, comp_dir: Path, fname: str) -> None:
    """Save `png_bytes` as a lossless WebP at comp_dir/fname, plus a small
    lossy WebP thumbnail at comp_dir/_thumbs/fname for the gallery grid."""
    img = Image.open(BytesIO(png_bytes)).convert("RGB")
    img.save(comp_dir / fname, "WEBP", lossless=True, method=6)

    thumb = img
    if thumb.width > THUMB_MAX_WIDTH:
        ratio = THUMB_MAX_WIDTH / thumb.width
        thumb = thumb.resize((THUMB_MAX_WIDTH, max(1, round(thumb.height * ratio))), Image.LANCZOS)
    thumb_dir = comp_dir / THUMB_DIRNAME
    thumb_dir.mkdir(exist_ok=True)
    thumb.save(thumb_dir / fname, "WEBP", quality=THUMB_QUALITY, method=6)


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


async def _expand_nested(
    el, comp_name: str, nested_captures: dict, identify_by: str, identify_attr: str,
) -> list[tuple]:
    """
    If `comp_name` has a rule in `nested_captures`, expand `el` into
    [el itself, unless skip_self] + [its drilled-down children, up to
    `limit`], recursing into any child whose own identified name also has a
    rule (e.g. a VerticalSectionSpacing whose first child happens to be a
    Grid follows the Grid rule too). Otherwise `el` is a plain leaf.

    A child is named by its own identify_attr value if it has one (this is
    what lets recursion trigger — e.g. a nested element with
    automation-testid="bonsai-Grid" is recognized as a Grid), falling back
    to the rule's static `name` when the attribute is absent.
    """
    rule = nested_captures.get(comp_name)
    if not rule:
        return [(el, comp_name)]

    pairs: list[tuple] = [] if rule.get("skip_self") else [(el, comp_name)]

    children = await el.query_selector_all(rule["selector"])
    limit = rule.get("limit")
    if limit is not None:
        children = children[:limit]

    for child in children:
        child_name = None
        if identify_by == "attribute":
            child_name = await child.get_attribute(identify_attr)
        if not child_name:
            child_name = rule.get("name", comp_name)
        pairs.extend(await _expand_nested(child, child_name, nested_captures, identify_by, identify_attr))

    return pairs


GOTO_RETRY_BACKOFF_MS = 4_000


class _WafBackoff:
    """
    Shared across every concurrent page task in one crawl. A single hot URL
    getting a transient 403 is handled by `_goto_with_retry`'s own quick
    retry — but the site's Akamai WAF can also escalate into a sustained
    block once a client's overall request volume/velocity trips its
    bot-rate heuristics, 403-ing most *subsequent* requests regardless of
    URL (each requested only once). A per-page retry can't ride that out,
    so once a burst of blocks is seen, this pauses the entire crawl — every
    task checks in before its next request — with exponential backoff,
    instead of racing through the rest of the URL list recording hundreds
    of false "blocked" results.
    """
    TRIP_THRESHOLD = 4        # blocks within WINDOW_S to trip a new cooldown
    WINDOW_S       = 30.0
    BASE_BACKOFF_S = 60.0
    MAX_BACKOFF_S  = 600.0
    RESET_AFTER_S  = 300.0    # calm period after which backoff resets to base

    def __init__(self):
        self.cooldown_until = 0.0
        self.backoff        = self.BASE_BACKOFF_S
        self.last_trip_at   = 0.0
        self._recent: list[float] = []

    async def wait_if_cooling_down(self) -> None:
        now = time.monotonic()
        if now < self.cooldown_until:
            await asyncio.sleep(self.cooldown_until - now)

    async def record_block(self, print_lock: asyncio.Lock, status) -> None:
        now = time.monotonic()
        self._recent = [t for t in self._recent if now - t < self.WINDOW_S] + [now]
        if len(self._recent) < self.TRIP_THRESHOLD or now < self.cooldown_until:
            return
        if now - self.last_trip_at > self.RESET_AFTER_S:
            self.backoff = self.BASE_BACKOFF_S
        pause = self.backoff
        self.cooldown_until = now + pause
        self.last_trip_at   = now
        self.backoff = min(self.backoff * 2, self.MAX_BACKOFF_S)
        async with print_lock:
            print(f"\n  ⏸  Site is rate-limiting this crawl (HTTP {status} × "
                  f"{len(self._recent)} in {self.WINDOW_S:.0f}s) — pausing "
                  f"{pause:.0f}s before continuing\n")
        await asyncio.sleep(pause)


async def _goto_with_retry(
    page, url: str, messages: list[str], block_tracker: _WafBackoff, print_lock: asyncio.Lock,
) -> bool:
    """
    Navigate to `url`, retrying once after a backoff if the response is a
    client/server error. The site's Akamai WAF can transiently 403 a URL
    that's been requested repeatedly in a short window (its own edge rate
    limiting, not a real block) — status clears again after a short cooldown
    — so a bare 403/5xx response gets a page-content check (e.g. an "Access
    Denied" edge page) rather than being trusted as a real "no components"
    result. Returns False (after recording a message) if still failing.
    """
    await block_tracker.wait_if_cooling_down()
    for attempt in range(2):
        resp = await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        status = resp.status if resp else None
        if status is None or status < 400:
            return True
        if attempt == 0:
            await page.wait_for_timeout(GOTO_RETRY_BACKOFF_MS)
    await block_tracker.record_block(print_lock, status)
    messages.append(f"      ⚠  HTTP {status} — request blocked/errored (not a missing-component result)")
    return False


async def _analyze_page(context, url: str, dist_dir: Path, sources: list[dict],
                        pre_click: list[str] | None,
                        block_tracker: _WafBackoff, print_lock: asyncio.Lock,
                        ) -> tuple[list[str], list[dict], list[str]]:
    """Returns (components, shots, messages) — `messages` are diagnostic
    lines (warnings/errors) the caller prints under its own lock, so output
    from concurrently-crawled pages can't interleave into misleading,
    duplicate-looking lines."""
    page = await context.new_page()
    await _stealth(page)
    messages: list[str] = []
    try:
        if not await _goto_with_retry(page, url, messages, block_tracker, print_lock):
            return [], [], messages
        await page.wait_for_timeout(2_000)
        # No cookie-banner accept-click needed: the context's init script
        # (see _new_context) keeps #onetrust-consent-sdk hidden from its
        # first paint, so it never renders and can't block later clicks.
        for sel in (pre_click or []):
            el = await page.query_selector(sel)
            if el:
                try:
                    await el.click(timeout=5_000)
                    await page.wait_for_timeout(150)
                except Exception:
                    pass
        await _scroll_page(page)
        await _hide_elements(page)

        all_pairs: list[tuple] = []
        for source in sources:
            container       = source["container"]
            container_opts  = container if isinstance(container, list) else [container]
            container_label = " or ".join(container_opts)
            component_root  = source["component_root"]
            mode            = source.get("mode", "children")
            child_selectors = source.get("child_selectors") or []

            # query_selector() below checks the DOM at this exact instant —
            # under concurrent crawling the container/its content can still
            # be rendering (especially after a pre_click reveal), so wait for
            # it first rather than reporting a false "not found".
            await _wait_for_content(page, container_opts, component_root)

            root = None
            for sel in container_opts:
                root = await page.query_selector(sel)
                if root:
                    break
            if not root:
                messages.append(f"      ⚠  {container_label} not found")
                continue

            if mode == "elements":
                els = await root.query_selector_all(component_root)
                if not els:
                    messages.append(f"      ⚠  no {component_root} elements found inside {container_label}")
                    continue
                identify_by       = source.get("identify_by", "class")
                identify_attr     = source.get("identify_attr", "")
                exclude_selectors = source.get("exclude_selectors") or []
                top_level_only    = source.get("top_level_only", False)
                nested_captures   = source.get("nested_captures") or {}

                pairs: list[tuple] = []
                for el in els:
                    if exclude_selectors and await el.evaluate(
                        "(el, sels) => sels.some(s => el.closest(s))", exclude_selectors
                    ):
                        continue
                    if top_level_only and await el.evaluate(
                        "(el, sel) => !!(el.parentElement && el.parentElement.closest(sel))",
                        component_root,
                    ):
                        continue

                    if identify_by == "attribute":
                        comp_name = await el.get_attribute(identify_attr) or "(no attr)"
                    else:
                        cls_list = await el.evaluate("el => Array.from(el.classList)")
                        comp_name = _classify(cls_list)

                    if nested_captures:
                        pairs.extend(await _expand_nested(el, comp_name, nested_captures, identify_by, identify_attr))
                    else:
                        pairs.append((el, comp_name))
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
                    messages.append(f"      ⚠  {component_root} not found inside {container_label}")
                    continue
                pairs = []
                for cc in ccs:
                    pairs.extend(await _get_component_elements(cc))
                if not pairs:
                    messages.append(f"      ⚠  {component_root} found but contains no immediate div children")
                    continue

            all_pairs.extend(pairs)

        pairs = all_pairs
        if not pairs:
            return [], [], messages

        # Count per component folder to decide whether to use index in filenames
        slug = _url_slug(url)
        folder_counts: dict[str, int] = defaultdict(int)
        for _, comp in pairs:
            folder_counts[_comp_to_folder(comp)] += 1

        shots: list[dict] = []
        comp_idx: dict[str, int] = defaultdict(int)
        for el, comp in pairs:
            folder_name = _comp_to_folder(comp)
            comp_idx[folder_name] += 1

            if comp not in SKIP_SCREENSHOT:
                comp_dir = dist_dir / folder_name
                comp_dir.mkdir(parents=True, exist_ok=True)

                n       = comp_idx[folder_name]
                total   = folder_counts[folder_name]
                fname   = f"{slug}-{n}.{SCREENSHOT_EXT}" if total > 1 else f"{slug}.{SCREENSHOT_EXT}"

                try:
                    if not await el.is_visible():
                        continue
                    box = await el.bounding_box()
                    if not box or box["width"] == 0 or box["height"] == 0:
                        continue
                    await el.scroll_into_view_if_needed()
                    png_bytes = await el.screenshot()
                    _save_screenshot(png_bytes, comp_dir, fname)
                    shots.append({
                        "folder":  folder_name,
                        "full":    f"{dist_dir}/{folder_name}/{fname}",
                        "thumb":   f"{dist_dir}/{folder_name}/{THUMB_DIRNAME}/{fname}",
                        "caption": Path(fname).stem,
                        "url":     url,
                    })
                except Exception as e:
                    messages.append(f"      Screenshot failed ({comp}): {e}")

        return [comp for _, comp in pairs], shots, messages

    except Exception as e:
        messages.append(f"    ERROR: {e}")
        return [], [], messages
    finally:
        await page.close()


CONTEXT_REFRESH_EVERY = 100
DEFAULT_CONCURRENCY   = 6


async def _new_context(browser):
    context = await browser.new_context(
        user_agent=BROWSER_UA, viewport={"width": 1440, "height": 900}, locale="en-US",
    )
    # Runs before any of the page's own scripts on every navigation in this
    # context, so the OneTrust banner is suppressed from its very first
    # paint — it never has a chance to render, let alone linger and block
    # later clicks (e.g. the pre_click tab button) under concurrent load.
    await context.add_init_script(
        "(() => { const s = document.createElement('style'); "
        "s.textContent = '#onetrust-consent-sdk { display: none !important; }'; "
        "document.documentElement.appendChild(s); })();"
    )
    return context


async def _crawl_one(
    context, url: str, dist_dir: Path, sources: list[dict], pre_click: list[str] | None,
    sem: asyncio.Semaphore, print_lock: asyncio.Lock, progress: list[int], total: int,
    block_tracker: _WafBackoff,
) -> dict:
    """Run one page through `_analyze_page`, gated by `sem` for concurrency, and
    print its result atomically (protected by `print_lock`) so output from
    concurrent pages doesn't interleave mid-line."""
    async with sem:
        components, shots, messages = await _analyze_page(
            context, url, dist_dir, sources, pre_click, block_tracker, print_lock,
        )
    async with print_lock:
        progress[0] += 1
        i = progress[0]
        print(f"  [{i:>3}/{total}] {url}")
        if components:
            print(f"           {len(components)} component(s): {', '.join(components)}")
        else:
            print(f"           — no components found")
        for msg in messages:
            print(msg)
    return {"url": url, "components": components, "shots": shots}


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
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[list[str], list[dict]]:
    """Fetch sitemap and crawl pages in a single browser session to avoid bot detection.

    Pages within each batch of CONTEXT_REFRESH_EVERY are crawled concurrently
    (bounded by `concurrency` tabs at a time, all sharing one context) rather
    than one at a time — the context is still refreshed between batches on
    the same schedule as before."""
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

        print(f"Crawling {len(urls)} page(s) at concurrency {concurrency} …\n{'─'*60}")
        print_lock    = asyncio.Lock()
        progress      = [0]
        block_tracker = _WafBackoff()
        for batch_start in range(0, len(urls), CONTEXT_REFRESH_EVERY):
            if batch_start > 0:
                await context.close()
                context = await _new_context(browser)
                print(f"  (context refreshed)\n")
            batch = urls[batch_start:batch_start + CONTEXT_REFRESH_EVERY]
            sem   = asyncio.Semaphore(max(1, concurrency))
            batch_results = await asyncio.gather(*(
                _crawl_one(context, url, dist_dir, sources, pre_click, sem, print_lock, progress, len(urls), block_tracker)
                for url in batch
            ))
            results.extend(batch_results)

        await browser.close()

    return urls, results


# ---------------------------------------------------------------------------
# JSON data outputs
# ---------------------------------------------------------------------------

def write_json_outputs(
    dist_dir: Path, label: str, total_pages: int, results: list[dict],
) -> tuple[list[tuple[str, list[str]]], dict[str, int], dict[str, list[dict]]]:
    """
    Write summary.json (high-level breakdown consumed by the report shell on
    load) and one {folder}/data.json per component (its screenshots, lazily
    fetched only when that component's gallery is opened).

    Returns (sorted_components, component_occurrences, shots_by_folder) so
    the caller can print the same data to the console.
    """
    component_pages: dict[str, list[str]] = defaultdict(list)
    component_occ: dict[str, int] = defaultdict(int)
    shots_by_folder: dict[str, list[dict]] = defaultdict(list)

    for r in results:
        seen_on_page: set[str] = set()
        for comp in r["components"]:
            component_occ[comp] += 1
            if comp not in seen_on_page:
                component_pages[comp].append(r["url"])
                seen_on_page.add(comp)
        for shot in r["shots"]:
            shots_by_folder[shot["folder"]].append({
                "full": shot["full"], "thumb": shot["thumb"],
                "caption": shot["caption"], "url": shot["url"],
            })

    sorted_components = sorted(component_pages.items(), key=lambda x: -len(x[1]))

    summary = {
        "label": label,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "total_pages": total_pages,
        "components": [
            {
                "name": comp,
                "folder": _comp_to_folder(comp),
                "occurrences": component_occ[comp],
                "pages": pages,
                "shot_count": len(shots_by_folder.get(_comp_to_folder(comp), [])),
            }
            for comp, pages in sorted_components
        ],
        "page_results": [
            {"url": r["url"], "components": r["components"]} for r in results
        ],
    }
    (dist_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False))

    for folder, shots in shots_by_folder.items():
        comp_dir = dist_dir / folder
        comp_dir.mkdir(parents=True, exist_ok=True)
        (comp_dir / "data.json").write_text(json.dumps({"shots": shots}, ensure_ascii=False))

    return sorted_components, component_occ, shots_by_folder


def _serve_report(out_path: Path) -> None:
    """Serve the current directory over HTTP and open the report in a
    browser, so its fetch() calls for summary.json / {folder}/data.json
    work (Chrome blocks fetch() against file:// pages)."""
    handler_cls = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(Path.cwd()))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    url  = f"http://127.0.0.1:{port}/{out_path.as_posix()}"
    print(f"\nServing report at {url}\nPress Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


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
.modal-page-link {
    display: inline-block; margin-top: 2px; font-size: 11px;
    color: #3b82f6; text-decoration: none;
}
.modal-page-link:hover { text-decoration: underline; }
.modal-counter { font-size: 11px; color: #94a3b8; margin-top: 2px; }

/* Modal — thumbnail grid */
.modal-grid {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 10px; width: 720px; max-height: 70vh; overflow-y: auto; padding: 2px;
}
.modal-grid[hidden] { display: none; }
.thumb-item {
    background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 6px;
    cursor: pointer; padding: 0; overflow: hidden; text-align: left;
}
.thumb-item:hover { border-color: #93c5fd; }
.thumb-item img {
    width: 100%; height: 90px; object-fit: cover; display: block;
}
.thumb-item .thumb-cap {
    display: block; padding: 5px 7px; font-size: 10px; font-family: monospace;
    color: #64748b; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* Modal — single-image viewer */
.modal-viewer { display: flex; flex-direction: column; gap: 10px; }
.modal-viewer[hidden] { display: none; }
.modal-back {
    background: none; border: none; color: #3b82f6; cursor: pointer;
    font-size: 12px; padding: 0; text-align: left; margin-bottom: -4px;
}
.modal-back:hover { text-decoration: underline; }
"""

REPORT_JS = """
function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Strip a URL down to its path (+query) for compact display, generically —
// works for whatever site was crawled rather than one hardcoded domain.
function shortPath(url) {
    try { const u = new URL(url); return u.pathname + u.search; } catch { return url; }
}

// ── Load summary.json and render everything ─────────────────────────────────
// The report ships as a thin static shell; all data lives in summary.json
// (high-level breakdown, loaded once) and one {folder}/data.json per
// component (screenshots, loaded lazily — see openModal below). This means
// the HTML/CSS/JS here can be edited and reloaded without re-running a crawl,
// as long as a previous run's dist/ output is still on disk.
let SUMMARY = null;
let sortCol = 'pages', sortDir = -1;
let searchQuery = '';

fetch(`${DIST_BASE}/summary.json`)
    .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(data => { SUMMARY = data; renderAll(); })
    .catch(err => {
        document.querySelector('.content').innerHTML =
            `<p style="color:#b91c1c">Failed to load ${DIST_BASE}/summary.json (${esc(err)}). ` +
            `Is this report being served over http:// rather than opened as a file?</p>`;
    });

function renderAll() {
    document.title = `${SUMMARY.label} — Component Discovery`;
    document.getElementById('report-title').textContent = `${SUMMARY.label} — Component Discovery`;
    document.getElementById('report-meta').textContent =
        `Generated ${SUMMARY.generated} · ${SUMMARY.total_pages} page(s) crawled`;
    renderChips();
    renderTable();
    renderPagesSection();
}

function renderChips() {
    const pagesWithData = SUMMARY.page_results.filter(r => r.components.length).length;
    const totalOcc = SUMMARY.components.reduce((s, c) => s + c.occurrences, 0);
    document.getElementById('summary-chips').innerHTML = `
        <div class="chip" style="background:#eff6ff;color:#1e40af;">
            <span class="chip-num">${SUMMARY.total_pages}</span>
            <span class="chip-lbl">Pages Crawled</span>
        </div>
        <div class="chip" style="background:#f0fdf4;color:#166534;">
            <span class="chip-num">${pagesWithData}</span>
            <span class="chip-lbl">Pages with Components</span>
        </div>
        <div class="chip" style="background:#f1f5f9;color:#334155;">
            <span class="chip-num">${SUMMARY.components.length}</span>
            <span class="chip-lbl">Unique Components</span>
        </div>
        <div class="chip" style="background:#fef9c3;color:#854d0e;">
            <span class="chip-num">${totalOcc}</span>
            <span class="chip-lbl">Total Occurrences</span>
        </div>`;
}

function pageLinkHtml(url) {
    return `<a href="${esc(url)}" target="_blank" rel="noopener" class="page-link" title="${esc(url)}">${esc(shortPath(url))}</a>`;
}

// ── Table: filtering & sorting ───────────────────────────────────────────────
function renderTable() {
    const tbody = document.querySelector('#comp-table tbody');
    tbody.innerHTML = '';

    let comps = SUMMARY.components.filter(c => !searchQuery || c.name.toLowerCase().includes(searchQuery));
    comps = comps.slice().sort((a, b) => {
        if (sortCol === 'comp') return sortDir * a.name.localeCompare(b.name);
        const av = sortCol === 'occ' ? a.occurrences : a.pages.length;
        const bv = sortCol === 'occ' ? b.occurrences : b.pages.length;
        return sortDir * (av - bv);
    });
    document.getElementById('visible-count').textContent = `${comps.length} components`;

    for (const c of comps) {
        const pct = SUMMARY.total_pages ? (c.pages.length / SUMMARY.total_pages * 100) : 0;
        const shotBtn = c.shot_count
            ? `<button class="shot-btn" onclick="openModal('${esc(c.folder)}')">&#128247; ${c.shot_count}</button>`
            : '';

        const row = document.createElement('tr');
        row.innerHTML = `
            <td class="comp-cell">${esc(c.name)}${shotBtn}</td>
            <td class="num">${c.occurrences}</td>
            <td class="num">${c.pages.length}</td>
            <td>
                <div class="cov-wrap">
                    <div class="cov-bar-bg"><div class="cov-bar" style="width:${pct.toFixed(1)}%"></div></div>
                    <span class="cov-pct">${pct.toFixed(0)}%</span>
                </div>
            </td>`;
        tbody.appendChild(row);

        const preview = c.pages.slice(0, 5), extra = c.pages.slice(5);
        const expand = extra.length
            ? `<details class="pages-more"><summary>+${extra.length.toLocaleString()} more</summary>
                 <div class="pages-grid">${extra.map(pageLinkHtml).join('')}</div></details>`
            : '';
        const pagesRow = document.createElement('tr');
        pagesRow.className = 'pages-row';
        pagesRow.innerHTML = `<td colspan="4"><div class="pages-grid">${preview.map(pageLinkHtml).join('')}</div>${expand}</td>`;
        tbody.appendChild(pagesRow);
    }

    const totalOcc = SUMMARY.components.reduce((s, c) => s + c.occurrences, 0);
    const totalRow = document.createElement('tr');
    totalRow.className = 'total-row';
    totalRow.innerHTML = `<td>TOTAL</td><td class="num">${totalOcc}</td><td class="num">${SUMMARY.total_pages}</td><td></td>`;
    tbody.appendChild(totalRow);
}

document.getElementById('search').addEventListener('input', e => {
    searchQuery = e.target.value.toLowerCase();
    if (SUMMARY) renderTable();
});

document.querySelectorAll('thead th[data-col]').forEach(th => {
    th.addEventListener('click', () => {
        if (sortCol === th.dataset.col) { sortDir *= -1; }
        else { sortCol = th.dataset.col; sortDir = sortCol === 'comp' ? 1 : -1; }
        document.querySelectorAll('thead th').forEach(t =>
            t.classList.remove('sorted-asc', 'sorted-desc'));
        th.classList.add(sortDir === 1 ? 'sorted-asc' : 'sorted-desc');
        if (SUMMARY) renderTable();
    });
});

// ── Pages section — two accordions ───────────────────────────────────────────
function renderPagesSection() {
    const found    = SUMMARY.page_results.filter(r => r.components.length);
    const notFound = SUMMARY.page_results.filter(r => !r.components.length);

    document.getElementById('found-count').textContent    = `${found.length} page${found.length !== 1 ? 's' : ''}`;
    document.getElementById('notfound-count').textContent = `${notFound.length} page${notFound.length !== 1 ? 's' : ''}`;

    document.getElementById('found-body').innerHTML = found.length ? found.map(r => `
        <div class="page-card">
            <div class="page-card-header">
                <a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(shortPath(r.url))}</a>
                <span style="color:#64748b;font-size:11px;flex-shrink:0;margin-left:12px">${r.components.length} component(s)</span>
            </div>
            <div class="page-card-body">${r.components.map(c => `<span class="comp-tag">${esc(c)}</span>`).join('')}</div>
        </div>`).join('') : "<p style='color:#94a3b8;font-size:12px'>None</p>";

    document.getElementById('notfound-body').innerHTML = notFound.length
        ? `<div class="empty-links">${notFound.map(r =>
            `<a href="${esc(r.url)}" class="empty-link" target="_blank" rel="noopener">${esc(shortPath(r.url))}</a>`).join('')}</div>`
        : "<p style='color:#94a3b8;font-size:12px'>None</p>";
}

// ── Screenshot modal ─────────────────────────────────────────────────────────
// Opens straight into a thumbnail grid of every screenshot for the component;
// clicking a thumbnail switches to a single-image viewer (prev/next, and a
// link to the live page it was captured from) starting at that image. Each
// component's screenshot list is only fetched the first time its modal is
// opened, then cached, so the report doesn't pay for data it never displays.
const modal       = document.getElementById('screenshot-modal');
const modalTitle  = modal.querySelector('.modal-title');
const modalGrid   = modal.querySelector('.modal-grid');
const modalViewer = modal.querySelector('.modal-viewer');
const modalImg    = modal.querySelector('.modal-img');
const modalCap    = modal.querySelector('.modal-caption');
const modalPageLink = modal.querySelector('.modal-page-link');
const modalCtr    = modal.querySelector('.modal-counter');
const modalPrev   = modal.querySelector('.modal-prev');
const modalNext   = modal.querySelector('.modal-next');

const _dataCache = {};
let _images = [], _idx = 0;

function loadComponentData(folder) {
    if (_dataCache[folder]) return Promise.resolve(_dataCache[folder]);
    return fetch(`${DIST_BASE}/${folder}/data.json`)
        .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(d => { _dataCache[folder] = d.shots; return d.shots; });
}

function openModal(folder) {
    modalTitle.textContent = folder.replace(/_/g, ' ');
    modalGrid.innerHTML = "<p style='color:#94a3b8;font-size:12px;padding:8px;'>Loading…</p>";
    modalGrid.hidden = false;
    modalViewer.hidden = true;
    modal.removeAttribute('hidden');

    loadComponentData(folder).then(shots => {
        _images = shots;
        _showGrid();
    }).catch(err => {
        modalGrid.innerHTML = `<p style="color:#b91c1c;font-size:12px;padding:8px;">Failed to load screenshots (${esc(err)})</p>`;
    });
}

function _showGrid() {
    modalGrid.innerHTML = _images.map((img, i) => `
        <button class="thumb-item" onclick="openViewer(${i})">
            <img src="${esc(img.thumb)}" alt="${esc(img.caption)}" loading="lazy">
            <span class="thumb-cap">${esc(img.caption)}</span>
        </button>`).join('');
    modalGrid.hidden = false;
    modalViewer.hidden = true;
}

function openViewer(i) {
    _idx = i;
    _renderViewer();
    modalGrid.hidden = true;
    modalViewer.hidden = false;
}

function _renderViewer() {
    const img = _images[_idx];
    modalImg.src = img.full;
    modalCap.textContent = img.caption;
    modalPageLink.href = img.url;
    modalPageLink.textContent = 'View page ↗ ' + shortPath(img.url);
    modalCtr.textContent = `${_idx + 1} / ${_images.length}`;
    modalPrev.disabled = _idx === 0;
    modalNext.disabled = _idx === _images.length - 1;
}

function modalNav(dir) {
    _idx = Math.max(0, Math.min(_images.length - 1, _idx + dir));
    _renderViewer();
}

function backToGrid() { _showGrid(); }

function closeModal() { modal.setAttribute('hidden', ''); }

modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });

document.addEventListener('keydown', e => {
    if (modal.hasAttribute('hidden')) return;
    if (e.key === 'Escape') {
        if (!modalViewer.hidden) backToGrid();
        else closeModal();
    }
    if (!modalViewer.hidden) {
        if (e.key === 'ArrowLeft')  modalNav(-1);
        if (e.key === 'ArrowRight') modalNav(1);
    }
});
"""


def _build_html(dist_dir: Path) -> str:
    """
    A thin static shell — no crawl data is embedded here. Everything shown
    is fetched client-side from dist_dir/summary.json (high-level breakdown)
    and dist_dir/{folder}/data.json (per-component screenshots, lazy-loaded).
    This means the template itself can be edited and reloaded in the browser
    without re-running a crawl, as long as a previous run's JSON is present.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Component Discovery</title>
<style>{REPORT_CSS}</style>
</head>
<body>

<div class="top-bar">
  <h1 id="report-title">Component Discovery</h1>
  <div class="meta" id="report-meta">Loading…</div>
</div>

<div class="content">
  <div class="summary-chips" id="summary-chips"></div>

  <div class="controls">
    <input id="search" type="text" placeholder="Search components…">
    <span class="visible-count" id="visible-count"></span>
  </div>

  <div class="table-wrap">
  <table id="comp-table">
    <thead>
      <tr>
        <th data-col="comp">Component Class</th>
        <th data-col="occ" style="text-align:right">Occurrences</th>
        <th data-col="pages" class="sorted-desc" style="text-align:right">Pages</th>
        <th class="no-sort">Coverage</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
  </div>

  <details class="accordion" open>
    <summary>Pages with components found <span class="summary-count" id="found-count"></span></summary>
    <div class="accordion-body" id="found-body"></div>
  </details>
  <details class="accordion">
    <summary>Pages with no components found <span class="summary-count" id="notfound-count"></span></summary>
    <div class="accordion-body" id="notfound-body"></div>
  </details>
</div>

<!-- Screenshot modal -->
<div id="screenshot-modal" class="modal-overlay" hidden>
  <div class="modal-box">
    <button class="modal-close" onclick="closeModal()">&#x2715;</button>
    <div class="modal-title"></div>

    <div class="modal-grid"></div>

    <div class="modal-viewer" hidden>
      <button class="modal-back" onclick="backToGrid()">&#8249; All screenshots</button>
      <div class="modal-img-wrap">
        <button class="modal-nav modal-prev" onclick="modalNav(-1)">&#8249;</button>
        <img class="modal-img" src="" alt="">
        <button class="modal-nav modal-next" onclick="modalNav(1)">&#8250;</button>
      </div>
      <div class="modal-footer">
        <div class="modal-caption"></div>
        <a class="modal-page-link" href="#" target="_blank" rel="noopener"></a>
        <div class="modal-counter"></div>
      </div>
    </div>
  </div>
</div>

<script>const DIST_BASE = {json.dumps(dist_dir.as_posix())};</script>
<script>{REPORT_JS}</script>
</body>
</html>
"""


INDEX_CSS = """
.index-card-pending { opacity: 0.6; }
.index-card-pending .page-card-header { cursor: default; }
.index-card-pending code {
    background: #f1f5f9; border: 1px solid #e2e8f0; border-radius: 4px;
    padding: 1px 5px; font-family: monospace; font-size: 11px;
}
"""

INDEX_JS = """
function esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

async function loadIndex() {
    const grid = document.getElementById('index-grid');
    let patterns;
    try {
        patterns = await (await fetch('patterns.json')).json();
    } catch (err) {
        grid.innerHTML = `<p style="color:#b91c1c">Failed to load patterns.json (${esc(err)}). ` +
            `Is this page being served over http:// rather than opened as a file?</p>`;
        return;
    }

    const keys = Object.keys(patterns).filter(k => k !== 'defaults');
    grid.innerHTML = keys.map(() => "<div class='page-card'><div class='page-card-header'>Loading…</div></div>").join('');

    const cards = await Promise.all(keys.map(async key => {
        const cfg = patterns[key];
        let summary = null;
        try {
            const r = await fetch(`dist/${key}/summary.json`);
            if (r.ok) summary = await r.json();
        } catch (err) { /* not generated yet */ }

        if (summary) {
            return `
                <div class="page-card">
                    <div class="page-card-header">
                        <a href="${esc(key)}.html">${esc(summary.label || cfg.label || key)}</a>
                    </div>
                    <div class="page-card-body" style="color:#64748b;font-size:11px;">
                        Generated ${esc(summary.generated)} · ${summary.total_pages} page(s) crawled · ${summary.components.length} component(s)
                    </div>
                </div>`;
        }
        return `
            <div class="page-card index-card-pending">
                <div class="page-card-header">${esc(cfg.label || key)}</div>
                <div class="page-card-body" style="color:#64748b;font-size:11px;">
                    Not generated yet — run: <code>python3 analyze.py --pattern ${esc(key)}</code>
                </div>
            </div>`;
    }));

    grid.innerHTML = cards.join('') || "<p style='color:#94a3b8;font-size:12px'>No patterns defined in patterns.json</p>";
}

loadIndex();
"""


def _build_index_html() -> str:
    """
    A static shell listing every pattern from patterns.json. Each one links
    to its generated report if dist/{pattern}/summary.json exists (checked
    live, client-side), or shows as not-yet-generated otherwise.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Component Discovery — Reports</title>
<style>{REPORT_CSS}{INDEX_CSS}</style>
</head>
<body>

<div class="top-bar">
  <h1>Component Discovery — Reports</h1>
  <div class="meta">Patterns from patterns.json</div>
</div>

<div class="content">
  <div class="index-grid" id="index-grid"></div>
</div>

<script>{INDEX_JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Discover components on a site's pages.",
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
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Pages to crawl in parallel (default: {DEFAULT_CONCURRENCY})")
    parser.add_argument("--out", default="",
                        help="Output HTML file (default: {pattern}.html)")
    parser.add_argument("--no-serve", action="store_true",
                        help="Don't start a local server / open the browser after crawling")
    parser.add_argument("--index", action="store_true",
                        help="Generate index.html linking to every pattern's report, and exit (skips crawling)")
    args = parser.parse_args()

    # ── --list-patterns ──────────────────────────────────────────────────────
    if args.list_patterns:
        patterns = load_patterns()
        print(f"Available patterns ({PATTERNS_FILE}):\n")
        for key, cfg in patterns.items():
            if key == "defaults":
                continue
            print(f"  {key:<20} {cfg.get('label', '')}")
            print(f"  {'':20} sitemap:        {cfg.get('sitemap', '')}")
            print(f"  {'':20} url_filter:     {cfg.get('url_filter', '')}")
            container = cfg.get('container', '')
            container_disp = " or ".join(container) if isinstance(container, list) else container
            print(f"  {'':20} container:      {container_disp}")
            print(f"  {'':20} component_root: {cfg.get('component_root', '')}\n")
        raise SystemExit(0)

    # ── --index ──────────────────────────────────────────────────────────────
    if args.index:
        index_path = Path("index.html")
        index_path.write_text(_build_index_html())
        print(f"Index → {index_path}")
        if not args.no_serve:
            _serve_report(index_path)
        raise SystemExit(0)

    if not args.pattern:
        parser.error("--pattern is required (use --list-patterns to see options)")

    pattern  = get_pattern(args.pattern)
    defaults = get_defaults()
    label       = pattern.get("label", args.pattern)
    sitemap_url = pattern["sitemap"]
    url_filter  = pattern["url_filter"]
    url_exclude = pattern.get("url_exclude", [])
    url_rewrite = pattern.get("url_rewrite", {})
    pre_click   = pattern.get("pre_click", [])

    if "sources" in pattern:
        sources = [_merge_source_defaults(s, defaults) for s in pattern["sources"]]
    else:
        sources = [_merge_source_defaults({
            "container":       pattern["container"],
            "component_root":  pattern["component_root"],
            "mode":            pattern.get("mode", "children"),
            "child_selectors": pattern.get("child_selectors", []),
            "identify_by":     pattern.get("identify_by", "class"),
            "identify_attr":   pattern.get("identify_attr", ""),
            "top_level_only":  pattern.get("top_level_only", False),
            "exclude_selectors": pattern.get("exclude_selectors", []),
            "nested_captures": pattern.get("nested_captures", {}),
        }, defaults)]

    out_path = Path(args.out) if args.out else Path(f"{args.pattern}.html")
    dist_dir = Path("dist") / args.pattern

    print(f"Pattern: {args.pattern}  ({label})")
    print(f"Preparing {dist_dir}/ …")
    clear_dist(dist_dir)

    if args.concurrency < 1:
        print(f"ERROR: --concurrency must be at least 1, got {args.concurrency}")
        raise SystemExit(1)

    if args.url:
        urls, results = asyncio.run(collect_and_crawl(
            dist_dir, sources, url_override=args.url, pre_click=pre_click,
            concurrency=args.concurrency,
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
            concurrency=args.concurrency,
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
            concurrency=args.concurrency,
        ))
        if not urls:
            print(f"No URLs matching '{url_filter}' found — exiting.")
            raise SystemExit(1)

    sorted_components, _component_occ, shots_by_folder = write_json_outputs(
        dist_dir, label, len(urls), results,
    )

    print(f"\n{'─'*60}")
    print(f"  {'COMPONENT':<50} {'PAGES':>6}")
    print(f"  {'─'*57}")
    for comp, pages in sorted_components:
        print(f"  {comp:<50} {len(pages):>6}")

    out_path.write_text(_build_html(dist_dir))
    print(f"\nReport       → {out_path}")
    print(f"Data         → {dist_dir}/summary.json, {dist_dir}/{{component}}/data.json")
    print(f"Screenshots  → {dist_dir.resolve()}/")
    total_shots = sum(len(v) for v in shots_by_folder.values())
    print(f"               {total_shots} screenshot(s) across {len(shots_by_folder)} component folder(s)")

    if not args.no_serve:
        _serve_report(out_path)


if __name__ == "__main__":
    main()
