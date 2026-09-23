#!/usr/bin/env python3
"""
AI Reporter — Generates intelligence briefs from the media monitor data.

Consumes enriched JSON data (falling back to titles-only from monitor_state.json
if necessary) to generate AI-produced intelligence briefs. Uses a resilient,
multi-provider LLM fallback chain (Gemini 3.6 Flash → Mistral Medium → Mistral Small → Gemini 2.5 Flash).

Enriched files are keyed by article publication date (enriched_YYYY-MM-DD.json).
The reporter loads enough daily files to cover the lookback window (e.g. 2 files
for --hours 24, since the window straddles midnight), then filters articles by
their publication timestamp to include only those within the window.

For continuity, condensed versions of the last few briefs in reports/ (see
`previous_issues` in config.toml) are included in the prompt as background.

Usage Examples:
  python ai_reporter.py                          → HTML email mode (production default)
  python ai_reporter.py --markdown               → Writes local .md files (testing/review)
  python ai_reporter.py --markdown --email       → Writes local .md files and sends email
  python ai_reporter.py --markdown --model flash  → Fast iteration using Gemini Flash
  python ai_reporter.py --model mistral-medium    → Run Mistral Medium only
  python ai_reporter.py --hours 48               → Analyze a wider 48-hour window

Flags:
  --markdown          Write the analysis to a local Markdown file (.md) in reports/
  --email             Send the final report as an HTML email (default if --markdown isn't used)
  --model MODEL       Force a specific model (options: auto, mistral, flash, flash-25, claude).
                      'auto' uses the fallback chain (default: auto).
  --hours INT         Look-back window in hours for the analysis (default: 24)
  --no-enriched       Force titles-only analysis even if enriched article text exists
  --enriched-dir DIR  Override the directory to read enriched JSONs from (default: enriched/)
"""

import argparse
import json
import os
import re
import smtplib
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

try:
    import markdown as md_lib
    from tenacity import (
        retry,
        retry_if_exception,
        stop_after_attempt,
        wait_exponential,
    )
except ImportError:
    print("Please install dependencies: pip install tenacity markdown")
    sys.exit(1)

from monitor_utils import normalize_title_for_dedup
from llm_rate_limit import is_retryable_error

import tomllib

with open(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.toml"), "rb"
) as _f:
    CONFIG = tomllib.load(_f)
CATEGORY_LABELS = CONFIG["categories"]

# Optional providers — imported on demand
genai = None
anthropic = None


def _ensure_gemini():
    global genai
    if genai is None:
        from google import genai as _genai

        genai = _genai


def _ensure_anthropic():
    global anthropic
    if anthropic is None:
        import anthropic as _anthropic

        anthropic = _anthropic


STATE_FILE = "data/monitor_state.json"
# Budget for the article context only (not the surrounding instruction template).
# ~700K chars ≈ 175-210K tokens depending on language. mistral-large-latest
# (Mistral Large 3) has a 256K-token window shared between prompt and output.
# Our feeds are all Latin-script European, but several (Polish, Hungarian, German)
# tokenize denser than English (~3.3-3.7 chars/token), so 700K keeps an ~11-16%
# margin under the window even on a dense-language day, after reserving the 16384
# output tokens and the instruction template. Don't push much past this without
# switching to real token-counting (mistral-common) instead of this char heuristic.
MAX_PROMPT_CHARS = 700_000


# ── Provider Backends ──────────────────────────────────────────────────────

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=30),
    retry=retry_if_exception(is_retryable_error),
    before_sleep=lambda rs: print(
        f"    ⚠ Retrying in {rs.next_action.sleep:.0f}s... "
        f"(attempt {rs.attempt_number})"
    ),
)
def _call_mistral(prompt: str, model: str = "mistral-large-latest") -> str:
    import urllib.request

    api_key = os.environ["MISTRAL_API_KEY"]
    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16384,
        }
    ).encode()
    req = urllib.request.Request(
        f"{MISTRAL_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    return re.sub(r"<think>.*?</think>\s*", "", text, flags=re.DOTALL)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=30),
    retry=retry_if_exception(is_retryable_error),
    before_sleep=lambda rs: print(
        f"    ⚠ Retrying in {rs.next_action.sleep:.0f}s... "
        f"(attempt {rs.attempt_number})"
    ),
)
def _call_gemini(prompt: str, model: str) -> str:
    _ensure_gemini()
    client = genai.Client()
    response = client.models.generate_content(model=model, contents=prompt)
    return response.text


def _is_retryable_anthropic(exception):
    """Only retry on server errors and rate limits, not client errors."""
    _ensure_anthropic()
    # Never retry bad requests, auth errors, etc.
    if isinstance(
        exception,
        (
            anthropic.BadRequestError,
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
        ),
    ):
        return False
    # Retry on overload, rate limits, server errors
    if isinstance(
        exception,
        (
            anthropic.RateLimitError,
            anthropic.InternalServerError,
            anthropic.APIStatusError,
        ),
    ):
        return True
    return False


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=30),
    retry=retry_if_exception(_is_retryable_anthropic),
    before_sleep=lambda rs: print(
        f"    ⚠ Retrying in {rs.next_action.sleep:.0f}s... "
        f"(attempt {rs.attempt_number})"
    ),
)
def _call_anthropic(prompt: str, model: str) -> str:
    _ensure_anthropic()
    client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
    response = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ── Fallback Chain ─────────────────────────────────────────────────────────

PROVIDERS = {
    "mistral-large": {
        "fn": lambda prompt: _call_mistral(prompt, "mistral-large-latest"),
        "label": "Mistral Large",
        "env_key": "MISTRAL_API_KEY",
    },
    "mistral-medium": {
        "fn": lambda prompt: _call_mistral(prompt, "mistral-medium-latest"),
        "label": "Mistral Medium",
        "env_key": "MISTRAL_API_KEY",
    },
    "mistral-small": {
        "fn": lambda prompt: _call_mistral(prompt, "mistral-small-latest"),
        "label": "Mistral Small",
        "env_key": "MISTRAL_API_KEY",
    },
    "gemini-flash": {
        "fn": lambda prompt: _call_gemini(prompt, "gemini-3.6-flash"),
        "label": "Gemini 3.6 Flash",
        "env_key": "GEMINI_API_KEY",
    },
    "gemini-flash-25": {
        "fn": lambda prompt: _call_gemini(prompt, "gemini-2.5-flash"),
        "label": "Gemini 2.5 Flash",
        "env_key": "GEMINI_API_KEY",
    },
    "claude-sonnet": {
        "fn": lambda prompt: _call_anthropic(prompt, "claude-sonnet-4-6"),
        "label": "Claude Sonnet 4.6",
        "env_key": "ANTHROPIC_API_KEY",
    },
}

# NOTE: claude-sonnet is intentionally OMITTED from the default chain to avoid
# Anthropic costs. Re-add "claude-sonnet" to this list to reactivate it; the
# PROVIDERS entry above is left in place. The --model claude flag still works
# for one-off explicit runs.
#
# Ordering: gemini-flash (3.6) leads because it is the strongest model that
# actually succeeds in CI, and because the Mistral tiers ahead of it were
# costing every run retry backoff before failing anyway.
# gemini-flash-25 (2.5) stays last as a genuine last resort -- landing on it
# means everything above it failed, which is worth being able to see.
#
# mistral-large is intentionally OMITTED: this account's key cannot reach it.
# GET /v1/models lists no mistral-large entry of any kind, and calls to
# mistral-large-latest return a persistent 403 (not the 429 that medium and
# small return, which is what authenticated-but-throttled looks like). It is
# therefore a guaranteed-dead round trip on every run, not a transient
# failure. The PROVIDERS entry is left in place -- re-add the name here if
# the plan ever grants access.
DEFAULT_CHAIN = [
    "gemini-flash",
    "mistral-medium", "mistral-small",
    "gemini-flash-25",
]

MODEL_ALIASES = {
    "mistral": "mistral-large",
    "mistral-large": "mistral-large",
    "mistral-medium": "mistral-medium",
    "mistral-small": "mistral-small",
    "flash": "gemini-flash",
    "flash-25": "gemini-flash-25",
    "claude": "claude-sonnet",
    "auto": None,
}


def generate_with_fallback(
    prompt: str, chain: list[str] | None = None
) -> tuple[str, str]:
    """Try each provider in the chain until one succeeds.
    Returns (response_text, provider_label).
    """
    if chain is None:
        chain = DEFAULT_CHAIN

    # Filter to providers whose API key is actually set
    available = []
    skipped = []
    for name in chain:
        prov = PROVIDERS[name]
        if os.environ.get(prov["env_key"]):
            available.append(name)
        else:
            skipped.append(f"{prov['label']} (no {prov['env_key']})")

    if skipped:
        print(f"  ℹ Skipping: {', '.join(skipped)}")

    if not available:
        print(
            "Error: No API keys set. Need at least one of: GEMINI_API_KEY, ANTHROPIC_API_KEY"
        )
        sys.exit(1)

    last_error = None
    for name in available:
        prov = PROVIDERS[name]
        print(f"  → Trying {prov['label']}...")
        try:
            text = prov["fn"](prompt)
            print(f"  ✓ Success with {prov['label']}")
            return text, prov["label"]
        except Exception as e:
            # Unwrap tenacity RetryError to show the real cause
            actual = e
            if hasattr(e, "last_attempt") and e.last_attempt.failed:
                actual = e.last_attempt.exception()
            last_error = actual
            print(f"  ✗ {prov['label']} failed: {actual}")
            if name != available[-1]:
                print(f"    Falling back to next provider...")

    print(f"All providers failed. Last error: {last_error}")
    sys.exit(1)


# ── Helpers ────────────────────────────────────────────────────────────────


def get_sort_time(item: dict) -> datetime:
    date_str = item.get("date", "")
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except Exception:
        added_str = item.get("added_at", "")
        try:
            return datetime.strptime(added_str, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


def sanitize(text: str) -> str:
    """Normalize Unicode and replace characters that cause email encoding issues."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\xa0", " ")
    text = text.replace("\u201c", '"')
    text = text.replace("\u201d", '"')
    text = text.replace("\u2018", "'")
    text = text.replace("\u2019", "'")
    text = text.replace("\u2013", "-")
    text = text.replace("\u2014", "--")
    text = text.replace("\u2026", "...")
    text = text.replace("\u00ab", '"')
    text = text.replace("\u00bb", '"')
    return text


# ── Data Sources ──────────────────────────────────────────────────────────


def load_enriched(enriched_dir: str, hours: int = 24) -> list[dict] | None:
    """Load enriched files covering the look-back window.

    For hours <= 24 only today's file is needed; for larger windows
    we also load previous days (e.g. 48 h → today + yesterday).
    Returns a merged, deduplicated list of articles or None.
    """
    now = datetime.now(timezone.utc)
    days_needed = (
        hours + 23
    ) // 24 + 1  # +1 because the window always straddles midnight
    all_articles: list[dict] = []
    seen_titles: set[str] = set()
    files_loaded = 0

    for offset in range(days_needed):
        date_slug = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        path = os.path.join(enriched_dir, f"enriched_{date_slug}.json")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for a in data.get("articles", []):
                key = normalize_title_for_dedup(a.get("title", ""))
                if key and key not in seen_titles:
                    seen_titles.add(key)
                    all_articles.append(a)
            files_loaded += 1
        except Exception as e:
            print(f"  ⚠ Failed to load enriched file {path}: {e}")

    if files_loaded:
        print(f"  Loaded {files_loaded} enriched file(s) spanning {days_needed} day(s)")
    return all_articles if all_articles else None


def _format_enriched_article(item: dict, ref_num: int) -> str:
    line = f"- [{ref_num}] {item['title']} (Source: {item['source']})"
    extract = item.get("extract")
    if extract and item.get("extract_status") == "ok":
        line += f"\n  EXTRACT: {extract}"
    return line


def compile_from_enriched(
    articles: list[dict], cutoff: datetime
) -> tuple[str, dict, int, int]:
    """Build prompt context from enriched data (titles + extracts).
    Only includes articles within the look-back window.
    Returns (context_str, reference_map, article_count, category_count).
    """
    by_cat: dict[str, list[dict]] = {}
    for a in articles:
        if get_sort_time(a) >= cutoff:
            by_cat.setdefault(a.get("category", "unknown"), []).append(a)

    for items in by_cat.values():
        items.sort(key=get_sort_time, reverse=True)

    num_cats = len(by_cat)
    if num_cats == 0:
        return "", {}, 0, 0

    per_cat_budget = MAX_PROMPT_CHARS // num_cats

    capped: dict[str, list[dict]] = {}
    for cat_id, items in by_cat.items():
        chars_used = 0
        kept: list[dict] = []
        for item in items:
            article_text = _format_enriched_article(item, 0)
            if kept and chars_used + len(article_text) > per_cat_budget:
                break
            kept.append(item)
            chars_used += len(article_text)
        capped[cat_id] = kept

    total_before = sum(len(v) for v in by_cat.values())
    total_after = sum(len(v) for v in capped.values())
    if total_after < total_before:
        print(
            f"  ⚠ Capped articles evenly: {total_before} → {total_after} "
            f"(~{total_after // num_cats} per category) to fit context window"
        )

    compiled_data = []
    reference_map = {}
    ref_num = 0
    article_count = 0
    category_count = 0

    for cat_id, items in capped.items():
        category_count += 1
        article_count += len(items)
        label = CATEGORY_LABELS.get(cat_id, cat_id.upper())
        compiled_data.append(f"### CATEGORY: {label} ###")

        for item in items:
            ref_num += 1
            url = item.get("resolved_url") or item.get("google_url", "")
            reference_map[ref_num] = {
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "url": url,
            }
            compiled_data.append(_format_enriched_article(item, ref_num))

        compiled_data.append("")

    return "\n".join(compiled_data), reference_map, article_count, category_count


def compile_from_state(state: dict, cutoff: datetime) -> tuple[str, dict, int, int]:
    """Build prompt context from monitor_state.json (titles only).
    Returns (context_str, reference_map, article_count, category_count).
    """
    by_cat: dict[str, list[dict]] = {}
    for category, items in state.items():
        recent = [item for item in items if get_sort_time(item) >= cutoff]
        if recent:
            recent.sort(key=get_sort_time, reverse=True)
            by_cat[category] = recent

    num_cats = len(by_cat)
    if num_cats == 0:
        return "", {}, 0, 0

    per_cat_budget = MAX_PROMPT_CHARS // num_cats

    compiled_data = []
    reference_map = {}
    ref_num = 0
    article_count = 0
    category_count = 0

    for category, items in by_cat.items():
        category_count += 1
        label = CATEGORY_LABELS.get(category, category.upper())
        compiled_data.append(f"### CATEGORY: {label} ###")
        chars_used = 0
        cat_article_count = 0

        for item in items:
            line = f"- [{ref_num + 1}] {item['title']} (Source: {item['source']})"
            if cat_article_count and chars_used + len(line) > per_cat_budget:
                break
            ref_num += 1
            cat_article_count += 1
            reference_map[ref_num] = {
                "title": item.get("title", ""),
                "source": item.get("source", ""),
                "url": item.get("url", ""),
            }
            compiled_data.append(line)
            chars_used += len(line)

        article_count += cat_article_count
        compiled_data.append("")

    total_before = sum(len(v) for v in by_cat.values())
    if article_count < total_before:
        print(
            f"  ⚠ Capped articles evenly: {total_before} → {article_count} "
            f"(~{article_count // num_cats} per category) to fit context window"
        )

    return "\n".join(compiled_data), reference_map, article_count, category_count


# ── Citation System ───────────────────────────────────────────────────────


def inject_links_markdown(text: str, ref_map: dict) -> tuple[str, set]:
    """Replace citation patterns with markdown links. Returns (text, cited_nums).

    Handles both single [N] and comma-separated [N, N, N] citation groups.
    """
    cited = set()

    def link_single(num: int) -> str:
        ref = ref_map.get(num)
        if ref and ref.get("url"):
            cited.add(num)
            return f"[[{num}]]({ref['url']})"
        return f"[{num}]"

    def replace_group(m):
        inner = m.group(1)
        nums = [int(n.strip()) for n in inner.split(",") if n.strip().isdigit()]
        return " ".join(link_single(n) for n in nums)

    def replace_single(m):
        return link_single(int(m.group(1)))

    result = re.sub(
        r"\[(\d+(?:\s*,\s*\d+)+)\]",
        replace_group,
        text,
    )
    result = re.sub(
        r"(?<!\[)\[(\d+)\](?!\(|\])",
        replace_single,
        result,
    )

    return result, cited


def build_sources_appendix_md(ref_map: dict, cited: set) -> str:
    """Build a markdown Sources section listing only cited references."""
    if not cited:
        return ""
    lines = ["\n\n---\n", "## Sources\n"]
    for num in sorted(cited):
        ref = ref_map[num]
        title = ref["title"]
        source = ref["source"]
        url = ref["url"]
        if url:
            lines.append(f"{num}. [{title}]({url}) — *{source}*")
        else:
            lines.append(f"{num}. {title} — *{source}*")
    return "\n".join(lines) + "\n"


# ── Continuity (previous issues) ─────────────────────────────────────────

REPORT_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{4}_report\.md$")
# One or more citations, linked ([[N]](url)) or bare ([N] / [N, M]), including
# any commas or spaces between them.
CITATION_RE = re.compile(
    r"(?:\s*,?\s*(?:\[\[\d+\]\]\([^)]*\)|\[\d+(?:\s*,\s*\d+)*\]))+"
)
SUMMARY_LABEL_RE = re.compile(r"^executive summary\s*:?\s*", re.I)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"“*])")
PREVIOUS_SECTION_SENTENCES = 2
PREVIOUS_MAX_LINE_CHARS = 400


def _clip(text: str, limit: int = PREVIOUS_MAX_LINE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def condense_report(md: str) -> str:
    """Reduce a saved brief to its skeleton for use as continuity context.

    Keeps the executive summary, each section heading with its opening
    sentences, and the Watchlist items. Drops citations, the Sources
    appendix and the report's header/disclaimer.
    """
    # Body starts after the first horizontal rule (below the header/disclaimer
    # block) and ends at the Sources appendix.
    parts = re.split(r"^---\s*$", md, maxsplit=1, flags=re.M)
    body = parts[1] if len(parts) == 2 else md
    body = re.split(r"^#{1,3}\s*Sources\s*$", body, maxsplit=1, flags=re.M)[0]
    body = CITATION_RE.sub("", body).replace("**", "")

    # Split into (heading, paragraphs) pairs; text before any heading is
    # treated as the executive summary.
    sections: list[tuple[str, list[str]]] = [("Executive Summary", [])]
    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block or re.fullmatch(r"-{3,}", block):
            continue
        m = re.match(r"^(#{1,6})\s*(.+?)\s*$", block.splitlines()[0])
        if m:
            level, title = len(m.group(1)), m.group(2)
            rest = "\n".join(block.splitlines()[1:]).strip()
            # A top-level title like "Daily Intelligence Brief: ..." is not a section.
            if level > 1:
                if title.lower().startswith("executive summary"):
                    title = "Executive Summary"
                sections.append((title, []))
            if rest:
                sections[-1][1].append(rest)
        else:
            sections[-1][1].append(block)

    lines = []
    for title, paras in sections:
        if not paras and title != "Executive Summary":
            lines.append(f"- {title}")
            continue
        if not paras:
            continue
        if title == "Executive Summary":
            # Some briefs put the label inline ("Executive Summary: ...") or on
            # its own line above the paragraph.
            text = " ".join(SUMMARY_LABEL_RE.sub("", p) for p in paras).strip()
            if text:
                lines.append(f"Summary: {_clip(text, 800)}")
        elif title.lower().startswith("watchlist"):
            lines.append("Watchlist:")
            for para in paras:
                for item in re.split(r"\n(?=\s*(?:\d+\.|[-*])\s)", para):
                    item = re.sub(r"^\s*(?:\d+\.|[-*])\s*", "", item).strip()
                    if item:
                        lines.append(f"  * {_clip(item)}")
        else:
            opening = " ".join(SENTENCE_SPLIT_RE.split(" ".join(paras[0].split()))[
                :PREVIOUS_SECTION_SENTENCES
            ])
            lines.append(f"- {title}: {_clip(opening)}")
    return "\n".join(lines)


def load_previous_issues(reports_dir: str, count: int, today: str) -> str:
    """Condensed text of the last `count` briefs, one per day, newest first.

    Reports dated `today` are skipped so a same-day re-run does not treat an
    earlier run over the same window as a previous issue.
    """
    if count <= 0 or not os.path.isdir(reports_dir):
        return ""
    latest_by_date: dict[str, str] = {}
    for name in sorted(os.listdir(reports_dir)):
        m = REPORT_NAME_RE.match(name)
        if m and m.group(1) < today:
            latest_by_date[m.group(1)] = name  # sorted, so the last run of a day wins
    issues = []
    for date in sorted(latest_by_date, reverse=True)[:count]:
        path = os.path.join(reports_dir, latest_by_date[date])
        try:
            with open(path, "r", encoding="utf-8") as f:
                condensed = condense_report(f.read())
        except OSError as e:
            print(f"  ⚠ Could not read previous issue {path}: {e}")
            continue
        label = datetime.strptime(date, "%Y-%m-%d").strftime("%B %d, %Y")
        issues.append(f"=== Issue of {label} ===\n{condensed}")
    return "\n\n".join(issues)


# ── Prompt ─────────────────────────────────────────────────────────────────


def build_prompt(
    context: str, enriched: bool = False, hours: int = 24, previous: str = ""
) -> str:
    prompt_cfg = CONFIG["prompt"]
    data_description = (
        prompt_cfg["enriched_data_description"]
        if enriched
        else prompt_cfg["titles_only_data_description"]
    )
    return prompt_cfg["instructions"].format(
        hours=hours,
        data_description=data_description,
        previous=previous or "(none available)",
        context=context,
    )


# ── HTML Template ──────────────────────────────────────────────────────────

# System sans-serif stack: renders natively on Apple Mail, Gmail, Outlook.
FONT_STACK = (
    "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)

# Warm-neutral palette, shared with the web dashboard (index.html).
INK = "#1c1917"
BODY_TEXT = "#292524"
MUTE = "#78716c"
LINE = "#e7e5e4"
ACCENT = "#b45309"


def build_html_email(
    analysis_html: str,
    today_str: str,
    article_count: int,
    category_count: int,
    provider_label: str = "Gemini",
) -> str:
    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="margin:0; padding:0; background-color:#f5f5f4; font-family:{FONT_STACK};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f5f5f4;">
<tr><td align="center" style="padding:24px 16px;">

<!-- Container -->
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
       style="background-color:#ffffff; border:1px solid {LINE}; border-radius:8px; max-width:640px; width:100%;">

  <!-- Header -->
  <tr>
    <td style="background-color:#fafaf9; padding:36px 40px 28px 40px; border-top:3px solid {ACCENT};
               border-bottom:1px solid {LINE}; border-radius:8px 8px 0 0; font-family:{FONT_STACK};">
      <p style="margin:0 0 10px 0; font-size:11px; font-weight:600; letter-spacing:1.8px;
                text-transform:uppercase; color:{ACCENT};">
        Intelligence Brief
      </p>
      <h1 style="margin:0; font-size:24px; color:{INK}; font-weight:600; line-height:1.25;
                 letter-spacing:-0.3px;">
        Transatlantic Right-Wing Media Monitor
      </h1>
      <p style="margin:10px 0 0 0; font-size:13px; color:{MUTE};">
        {today_str}&ensp;·&ensp;{article_count} articles across {category_count} categories
      </p>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:32px 40px; font-size:15px; line-height:1.65; color:{BODY_TEXT};
               font-family:{FONT_STACK};">
      {analysis_html}
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="padding:24px 40px; border-top:1px solid {LINE}; font-size:11px;
               color:#a8a29e; font-family:{FONT_STACK};">
      Generated automatically by the Transatlantic Right-Wing Media Monitor.
      Analysis by {provider_label}&ensp;·&ensp;Data from Google News RSS.
    </td>
  </tr>

</table>
<!-- /Container -->

</td></tr>
</table>
</body>
</html>"""


# ── Inline Styles for Markdown → HTML ──────────────────────────────────────


def style_html(raw_html: str) -> str:
    """Inject inline styles into the converted Markdown HTML for email clients."""
    hr = f'<hr style="border:none; border-top:1px solid {LINE}; margin:28px 0;">'
    replacements = [
        (
            "<h1>",
            f'<h1 style="font-size:20px; font-weight:600; color:{INK}; margin:28px 0 12px 0; '
            f'border-bottom:1px solid {LINE}; padding-bottom:8px; font-family:{FONT_STACK};">',
        ),
        (
            "<h2>",
            f'<h2 style="font-size:17px; font-weight:600; color:{INK}; margin:28px 0 10px 0; '
            f'letter-spacing:-0.2px; font-family:{FONT_STACK};">',
        ),
        (
            "<h3>",
            f'<h3 style="font-size:12px; font-weight:600; color:{MUTE}; margin:20px 0 8px 0; '
            f'letter-spacing:1.2px; text-transform:uppercase; font-family:{FONT_STACK};">',
        ),
        ("<p>", '<p style="margin:0 0 14px 0;">'),
        ("<ul>", '<ul style="margin:0 0 16px 0; padding-left:20px;">'),
        ("<ol>", '<ol style="margin:0 0 16px 0; padding-left:20px;">'),
        ("<li>", '<li style="margin:0 0 6px 0;">'),
        ("<strong>", f'<strong style="color:{INK}; font-weight:600;">'),
        (
            "<blockquote>",
            '<blockquote style="margin:16px 0; padding:12px 20px; '
            f'border-left:3px solid {ACCENT}; background:#fafaf9; color:#57534e;">',
        ),
        ("<a href=", f'<a style="color:{ACCENT}; text-decoration:none;" href='),
        ("<hr>", hr),
        ("<hr/>", hr),
        ("<hr />", hr),
    ]
    for old, new in replacements:
        raw_html = raw_html.replace(old, new)
    return raw_html


# ── Main ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="AI Intelligence Reporter")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Write markdown reports to reports/ instead of sending email (testing mode)",
    )
    parser.add_argument(
        "--hours", type=int, default=24, help="Look-back window in hours (default: 24)"
    )
    parser.add_argument(
        "--outdir",
        default="reports",
        help="Output directory for markdown reports (default: reports/)",
    )
    parser.add_argument(
        "--model",
        default="auto",
        choices=["auto", "mistral", "mistral-large", "mistral-medium",
                 "mistral-small", "flash", "flash-25", "claude"],
        help="Model selection: 'auto' (full fallback chain), 'mistral' (Large), "
        "'mistral-medium', 'mistral-small', 'flash', 'flash-25', 'claude'",
    )
    parser.add_argument(
        "--enriched-dir",
        default="data-private",
        help="Directory containing enriched JSON files (default: enriched/)",
    )
    parser.add_argument(
        "--no-enriched",
        action="store_true",
        help="Force titles-only mode even if enriched data exists",
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="Send email report (default when --markdown is not set; "
        "combine with --markdown to do both)",
    )
    args = parser.parse_args()

    # ── Determine output modes ───────────────────────────────────────────
    # No flags          → email only  (backward compatible)
    # --markdown        → markdown only (backward compatible)
    # --email           → email only
    # --markdown --email→ both
    do_markdown = args.markdown
    do_email = args.email or (not args.markdown)

    # Build the provider chain based on --model
    model_key = MODEL_ALIASES[args.model]
    if model_key is None:
        # "auto" — use the full fallback chain
        chain = DEFAULT_CHAIN
    else:
        # Single provider requested
        chain = [model_key]

    # Check at least one API key exists
    available_keys = {
        k for k in ["MISTRAL_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"] if os.environ.get(k)
    }
    needed_keys = {PROVIDERS[name]["env_key"] for name in chain}
    if not available_keys & needed_keys:
        print(f"Error: No API keys set for the requested providers.")
        print(f"  Need at least one of: {', '.join(sorted(needed_keys))}")
        sys.exit(1)

    if do_email:
        sender_email = os.environ.get("SENDER_EMAIL")
        email_password = os.environ.get("EMAIL_PASSWORD")
        receiver_email = os.environ.get("RECEIVER_EMAIL")
        if not all([sender_email, email_password, receiver_email]):
            print(
                "Error: Missing email secrets. Use --markdown for testing without email."
            )
            sys.exit(1)

    if not os.path.exists(STATE_FILE):
        print("No state file found.")
        sys.exit(1)

    # ── Choose data source ────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    report_slug = now.strftime("%Y-%m-%d_%H%M")  # for report filenames (no collisions)
    today_str = now.strftime("%B %d, %Y")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    using_enriched = False

    if not args.no_enriched:
        enriched_articles = load_enriched(args.enriched_dir, args.hours)
        if enriched_articles:
            enriched_with_text = sum(
                1 for a in enriched_articles if a.get("extract_status") == "ok"
            )
            print(
                f"Using enriched data: {len(enriched_articles)} articles "
                f"({enriched_with_text} with text extracts)"
            )
            prompt_context, ref_map, article_count, category_count = (
                compile_from_enriched(enriched_articles, cutoff)
            )
            using_enriched = True
        else:
            print("No enriched file found for today. Falling back to titles-only.")

    if not using_enriched:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        prompt_context, ref_map, article_count, category_count = compile_from_state(
            state, cutoff
        )

    if not prompt_context.strip():
        print(f"No articles found. Skipping report.")
        sys.exit(0)

    previous_issues = load_previous_issues(
        args.outdir, CONFIG["prompt"].get("previous_issues", 0), now.strftime("%Y-%m-%d")
    )
    if previous_issues:
        n_prev = previous_issues.count("=== Issue of ")
        print(f"Continuity: {n_prev} previous issue(s), {len(previous_issues):,} chars.")
    else:
        print("Continuity: no previous issues found.")

    prompt = build_prompt(
        prompt_context,
        enriched=using_enriched,
        hours=args.hours,
        previous=previous_issues,
    )

    print(
        f"Compiled {article_count} articles across {category_count} categories "
        f"({len(ref_map)} references indexed)."
    )

    # ── Generate AI analysis (once) ─────────────────────────────────────
    mode_label = []
    if do_markdown:
        mode_label.append("markdown")
    if do_email:
        mode_label.append("email")
    print(f"Output mode: {' + '.join(mode_label)}")

    chain_labels = [
        PROVIDERS[n]["label"] for n in chain if os.environ.get(PROVIDERS[n]["env_key"])
    ]
    print(f"Provider chain: {' → '.join(chain_labels)}")

    print(f"Generating AI Analysis...")

    try:
        analysis_raw, provider_used = generate_with_fallback(prompt, chain)
        analysis_text = sanitize(analysis_raw)
    except SystemExit:
        raise
    except Exception as e:
        print(f"Final failure: {e}")
        sys.exit(1)

    # Inject markdown links and build sources appendix
    analysis_linked, cited = inject_links_markdown(analysis_text, ref_map)
    sources_appendix = build_sources_appendix_md(ref_map, cited)

    print(f"  ✓ {provider_used} cited {len(cited)} of {len(ref_map)} references.")

    # ── Markdown output ──────────────────────────────────────────────────
    if do_markdown:
        os.makedirs(args.outdir, exist_ok=True)

        # 1. Write the input debug file
        input_path = os.path.join("data-private/reports", f"{report_slug}_input.md")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write(f"# AI Input — {today_str}\n\n")
            f.write(f"**Provider:** {provider_used}\n")
            f.write(f"**Look-back:** {args.hours} hours\n")
            f.write(
                f"**Data source:** {'enriched' if using_enriched else 'titles-only'}\n"
            )
            f.write(
                f"**Articles:** {article_count} across {category_count} categories\n"
            )
            f.write(f"**References indexed:** {len(ref_map)}\n\n")
            f.write("---\n\n")
            f.write("## Prompt Instructions\n\n")
            f.write(
                f"```\n{build_prompt('(article data follows below)', enriched=using_enriched, hours=args.hours, previous=previous_issues)}\n```\n\n"
            )
            f.write("---\n\n")
            f.write("## Article Data\n\n")
            f.write(prompt_context)
        print(f"  ✓ Input saved → {input_path}")

        # 2. Write the report
        output_path = os.path.join(args.outdir, f"{report_slug}_report.md")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# Intelligence Brief — {today_str}\n\n")
            f.write(
                "> **AI-GENERATED CONTENT — NOT VERIFIED.** "
                "This report was produced automatically by a large language model. "
                "Claims, interpretations, and citations may contain errors.\n\n"
            )
            f.write(f"*{article_count} articles across {category_count} categories")
            if using_enriched:
                f.write(f" (enriched)")
            f.write(f" · Analysis by {provider_used}*\n\n")
            f.write("---\n\n")
            f.write(analysis_linked)
            f.write(sources_appendix)
        print(f"  ✓ Report saved → {output_path}")

    # ── Email output ─────────────────────────────────────────────────────
    if do_email:
        full_markdown = analysis_linked + sources_appendix

        # Convert Markdown → styled HTML
        analysis_html = md_lib.markdown(full_markdown, extensions=["extra"])
        analysis_html = style_html(analysis_html)

        subject = f"Intelligence Brief: Transatlantic Right-Wing Media ({today_str})"
        full_html = build_html_email(
            analysis_html, today_str, article_count, category_count, provider_used
        )

        # Plain-text fallback
        plain_sources = ""
        if cited:
            plain_sources = "\n---\nSources:\n"
            for num in sorted(cited):
                ref = ref_map[num]
                plain_sources += (
                    f"  [{num}] {sanitize(ref['title'])} — {sanitize(ref['source'])}\n"
                )
                if ref["url"]:
                    plain_sources += f"        {ref['url']}\n"
        plain_text = sanitize(analysis_text + plain_sources)

        # Build multipart message
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = receiver_email
        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(sanitize(full_html), "html", "utf-8"))

        print("Sending email...")
        try:
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(sender_email, email_password)
            server.send_message(msg)
            server.quit()
            print("  ✓ Report emailed successfully!")
        except Exception as e:
            print(f"Failed to send email: {e}")
            sys.exit(1)


if __name__ == "__main__":
    main()
