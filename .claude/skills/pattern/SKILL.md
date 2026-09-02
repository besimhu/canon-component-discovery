---
name: pattern
description: Interactively create a new crawl target in patterns.json for this component-discovery tool, asking the right follow-up questions based on the chosen mode (children/elements) and whether nested_captures/multi-source/shared-defaults are needed. Use when the user wants to add, define, or scaffold a new pattern, capture group, or crawl target for a new site/section — e.g. "add a pattern for X", "set up crawling for the docs section", "/pattern".
tools: Read, Edit, Bash, AskUserQuestion
---

# Add a pattern

Walks the user through defining one new entry in `patterns.json` for this project's crawler (`analyze.py`), asking only the questions relevant to the choices they've already made, then writes the result and offers to sanity-test it.

**Ground yourself first** — re-read the current field reference before asking anything, since it's the single source of truth and may have evolved:
- `README.md` → **Patterns (`patterns.json`)** section (flat format, multi-source format, shared defaults, full field table)
- `README.md` → **Modes** section (`children` vs `elements`)
- `README.md` → **Nested captures** section (`selector`/`limit`/`skip_self`/`name`, recursion rules)

Also read the current `patterns.json` so you know the existing pattern keys (for uniqueness) and what's already in the top-level `defaults` block (so you don't ask the user to redefine `exclude_selectors`/`nested_captures` that already apply globally).

Ask questions conversationally in plain chat for free-text values (URLs, selectors, labels) — `AskUserQuestion` doesn't fit those well. Reserve `AskUserQuestion` for genuine small-option decisions (mode, identify_by, yes/no branches on whether to add optional features). Don't ask about a field the user has already answered implicitly (e.g. if they say "identify by automation-testid", don't separately ask "class or attribute?").

## Flow

1. **Identity** — pattern key (must not already exist in `patterns.json`), and `label` (display name for the report title).

2. **Sitemap scope** — `sitemap` (full sitemap/sitemap-index URL) and `url_filter` (substring match). Ask if any URLs should be excluded (`url_exclude`) or rewritten (`url_rewrite`) — only mention `url_rewrite`'s one supported form (`trailing_slash`) if they describe a URL-without-trailing-slash-vs-actual-page mismatch; don't over-explain it otherwise.

3. **Capture groups** — ask whether this pattern needs **one** container/component_root pair or **multiple** (multi-source `sources` array — e.g. content that lives in different containers on different page types). For each capture group needed, gather:
   - `container` — one selector, or ask if a fallback list is needed (e.g. sites that don't always render `<main>`)
   - `component_root` — the selector for component wrapper(s)
   - `mode` — `children` or `elements`. Explain the one-line difference from the README if the user seems unsure (children = immediate div children of each `component_root` match; elements = each match *is* the component).

4. **Mode-specific follow-ups**, per capture group:
   - If `elements`:
     - `identify_by` — `class` (default) or `attribute`. If `attribute`, get `identify_attr` (e.g. `automation-testid`).
     - `top_level_only` — ask only if the user's markup nests one matched component inside another and they only want the outermost.
     - Ask whether any regions should be excluded (`exclude_selectors`) beyond what's already in `defaults` — skip this if `defaults` already covers it and the user confirms that's sufficient.
     - Ask whether any component here wraps other meaningful components needing more than one screenshot (→ `nested_captures`). If yes, for **each** rule gather: the wrapping component's identified name (the dict key), `selector` (relative to that component), `limit` (or none), whether to `skip_self` (pure structural wrapper vs. keep its own shot too), and a fallback `name` for children with no `identify_attr` value of their own. Remind them rules recurse automatically if a captured child's own name matches another rule's key — don't make them configure that explicitly.
   - If `children`: ask about `pre_click` (selectors to click before analysis, e.g. tabs/accordions gating content). Mention the hardcoded special-cases (`variable-spacing-wrapper`, `rte-textImage-cmp`, `aem-GridColumn*`) only if directly relevant — they're legacy literal-name matches, not something to configure.
   - `child_selectors` (elements mode only) — only ask if the user describes wanting to capture a specific sub-element inside each match as its own separate screenshot entry, distinct from `nested_captures`'s drill-down/recursion behavior.

5. **Shared defaults** — if the user's answers for `exclude_selectors`/`nested_captures` substantially duplicate what's already in `defaults`, point that out and suggest relying on inheritance instead of repeating it in the pattern (or ask if they'd rather promote a rule into `defaults` so future patterns get it too).

6. **Write it**:
   - Build the pattern object (flat format if one capture group, `sources` array if multiple).
   - Read the current `patterns.json`, insert the new key without disturbing existing entries or `defaults`, write it back.
   - Validate with `python3 -c "import json; json.load(open('patterns.json'))"`.
   - Show the user the new entry and a one-line summary of what it does.

7. **Offer a smoke test** — suggest `python3 analyze.py --pattern <key> --url <a-real-page-url> --no-serve` against one real URL they care about, and walk through the printed component list with them to confirm it matches expectations before they run a full crawl.
