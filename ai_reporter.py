#!/usr/bin/env python3
"""
AI Reporter — Generates intelligence briefs from the media monitor data.

Consumes enriched JSON data (falling back to titles-only from monitor_state.json
if necessary) to generate AI-produced intelligence briefs. Uses a resilient,
multi-provider LLM fallback chain (Gemini Pro → Claude Sonnet → Gemini Flash).

Enriched files are keyed by article publication date (enriched_YYYY-MM-DD.json).
The reporter loads enough daily files to cover the lookback window (e.g. 2 files
for --hours 24, since the window straddles midnight), then filters articles by
their publication timestamp to include only those within the window.

Usage Examples:
  python ai_reporter.py                          → HTML email mode (production default)
  python ai_reporter.py --markdown               → Writes local .md files (testing/review)
  python ai_reporter.py --markdown --email       → Writes local .md files and sends email
  python ai_reporter.py --markdown --model flash → Fast iteration using Gemini Flash
  python ai_reporter.py --hours 48               → Analyze a wider 48-hour window

Flags:
  --markdown          Write the analysis to a local Markdown file (.md) in reports/
  --email             Send the final report as an HTML email (default if --markdown isn't used)
  --model MODEL       Force a specific model (options: auto, pro, claude, flash).
                      'auto' uses the fallback chain (default: auto).
  --hours INT         Look-back window in hours for the analysis (default: 24)
  --no-enriched       Force titles-only analysis even if enriched article text exists
  --enriched-dir DIR  Override the directory to read enriched JSONs from (default: enriched/)
"""

import argparse
import glob
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
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )
except ImportError:
    print("Please install dependencies: pip install tenacity markdown")
    sys.exit(1)

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


STATE_FILE = "monitor_state.json"


# ── Provider Backends ──────────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=30),
    retry=retry_if_exception_type(Exception),
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
    "gemini-pro": {
        "fn": lambda prompt: _call_gemini(prompt, "gemini-2.5-pro"),
        "label": "Gemini 2.5 Pro",
        "env_key": "GEMINI_API_KEY",
    },
    "claude-sonnet": {
        "fn": lambda prompt: _call_anthropic(prompt, "claude-sonnet-4-6"),
        "label": "Claude Sonnet 4.6",
        "env_key": "ANTHROPIC_API_KEY",
    },
    "gemini-flash": {
        "fn": lambda prompt: _call_gemini(prompt, "gemini-2.5-flash"),
        "label": "Gemini 2.5 Flash",
        "env_key": "GEMINI_API_KEY",
    },
}

DEFAULT_CHAIN = ["gemini-pro", "claude-sonnet", "gemini-flash"]

MODEL_ALIASES = {
    "flash": "gemini-flash",
    "pro": "gemini-pro",
    "claude": "claude-sonnet",
    "auto": None,  # uses the full fallback chain
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
    now = datetime.now()
    days_needed = (
        hours + 23
    ) // 24 + 1  # +1 because the window always straddles midnight
    all_articles: list[dict] = []
    seen_titles: set[str] = set()
    files_loaded = 0

    for offset in range(days_needed):
        date_slug = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
        pattern = os.path.join(enriched_dir, f"enriched_{date_slug}*.json")
        for path in sorted(glob.glob(pattern)):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for a in data.get("articles", []):
                    key = a.get("title", "")
                    if key and key not in seen_titles:
                        seen_titles.add(key)
                        all_articles.append(a)
                files_loaded += 1
            except Exception as e:
                print(f"  ⚠ Failed to load enriched file {path}: {e}")

    if files_loaded:
        print(f"  Loaded {files_loaded} enriched file(s) spanning {days_needed} day(s)")
    return all_articles if all_articles else None


def compile_from_enriched(
    articles: list[dict], cutoff: datetime
) -> tuple[str, dict, int, int]:
    """Build prompt context from enriched data (titles + extracts).
    Only includes articles within the look-back window.
    Returns (context_str, reference_map, article_count, category_count).
    """
    by_cat = {}
    for a in articles:
        if get_sort_time(a) >= cutoff:
            by_cat.setdefault(a.get("category", "unknown"), []).append(a)

    compiled_data = []
    reference_map = {}
    ref_num = 0
    article_count = 0
    category_count = 0

    for cat_id, items in by_cat.items():
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

            line = f"- [{ref_num}] {item['title']} (Source: {item['source']})"
            extract = item.get("extract")
            if extract and item.get("extract_status") == "ok":
                line += f"\n  EXTRACT: {extract}"
            compiled_data.append(line)

        compiled_data.append("")

    return "\n".join(compiled_data), reference_map, article_count, category_count


def compile_from_state(state: dict, cutoff: datetime) -> tuple[str, dict, int, int]:
    """Build prompt context from monitor_state.json (titles only).
    Returns (context_str, reference_map, article_count, category_count).
    """
    compiled_data = []
    reference_map = {}
    ref_num = 0
    article_count = 0
    category_count = 0

    for category, items in state.items():
        recent_items = [item for item in items if get_sort_time(item) >= cutoff]
        if recent_items:
            category_count += 1
            article_count += len(recent_items)
            label = CATEGORY_LABELS.get(category, category.upper())
            compiled_data.append(f"### CATEGORY: {label} ###")
            for item in recent_items:
                ref_num += 1
                reference_map[ref_num] = {
                    "title": item.get("title", ""),
                    "source": item.get("source", ""),
                    "url": item.get("url", ""),
                }
                compiled_data.append(
                    f"- [{ref_num}] {item['title']} (Source: {item['source']})"
                )
            compiled_data.append("")

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
    lines = ["\n---\n", "## Sources\n"]
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


# ── Prompt ─────────────────────────────────────────────────────────────────


def build_prompt(context: str, enriched: bool = False, hours: int = 24) -> str:
    prompt_cfg = CONFIG["prompt"]
    data_description = (
        prompt_cfg["enriched_data_description"]
        if enriched
        else prompt_cfg["titles_only_data_description"]
    )
    return prompt_cfg["instructions"].format(
        hours=hours,
        data_description=data_description,
        context=context,
    )


# ── HTML Template ──────────────────────────────────────────────────────────


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
<body style="margin:0; padding:0; background-color:#f0f0f0; font-family:Georgia, 'Times New Roman', serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f0f0f0;">
<tr><td align="center" style="padding:24px 16px;">

<!-- Container -->
<table role="presentation" width="640" cellpadding="0" cellspacing="0"
       style="background-color:#ffffff; border-radius:4px; max-width:640px; width:100%;">

  <!-- Header -->
  <tr>
    <td style="background-color:#1a1a2e; padding:32px 40px; border-radius:4px 4px 0 0;">
      <p style="margin:0 0 4px 0; font-size:11px; letter-spacing:2px; text-transform:uppercase;
                color:#8888aa; font-family:Helvetica,Arial,sans-serif;">
        Intelligence Brief
      </p>
      <h1 style="margin:0; font-size:22px; color:#ffffff; font-weight:normal; line-height:1.3;">
        Transatlantic Right-Wing Media Monitor
      </h1>
      <p style="margin:8px 0 0 0; font-size:13px; color:#8888aa;
                font-family:Helvetica,Arial,sans-serif;">
        {today_str}&ensp;·&ensp;{article_count} articles across {category_count} categories
      </p>
    </td>
  </tr>

  <!-- Body -->
  <tr>
    <td style="padding:32px 40px; font-size:15px; line-height:1.7; color:#2a2a2a;">
      {analysis_html}
    </td>
  </tr>

  <!-- Footer -->
  <tr>
    <td style="padding:24px 40px; border-top:1px solid #e0e0e0; font-size:11px;
               color:#999999; font-family:Helvetica,Arial,sans-serif;">
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
    replacements = [
        (
            "<h1>",
            '<h1 style="font-size:20px; color:#1a1a2e; margin:28px 0 12px 0; '
            'border-bottom:2px solid #1a1a2e; padding-bottom:6px;">',
        ),
        ("<h2>", '<h2 style="font-size:17px; color:#1a1a2e; margin:24px 0 10px 0;">'),
        (
            "<h3>",
            '<h3 style="font-size:15px; color:#333; margin:20px 0 8px 0; '
            'font-family:Helvetica,Arial,sans-serif;">',
        ),
        ("<p>", '<p style="margin:0 0 14px 0;">'),
        ("<ul>", '<ul style="margin:0 0 16px 0; padding-left:20px;">'),
        ("<ol>", '<ol style="margin:0 0 16px 0; padding-left:20px;">'),
        ("<li>", '<li style="margin:0 0 6px 0;">'),
        ("<strong>", '<strong style="color:#1a1a2e;">'),
        (
            "<blockquote>",
            '<blockquote style="margin:16px 0; padding:12px 20px; '
            "border-left:3px solid #1a1a2e; background:#f8f8fa; "
            'font-style:italic; color:#555;">',
        ),
        ("<hr>", '<hr style="border:none; border-top:1px solid #ddd; margin:24px 0;">'),
        (
            "<hr/>",
            '<hr style="border:none; border-top:1px solid #ddd; margin:24px 0;">',
        ),
        (
            "<hr />",
            '<hr style="border:none; border-top:1px solid #ddd; margin:24px 0;">',
        ),
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
        choices=["auto", "flash", "pro", "claude"],
        help="Model selection: 'auto' (Pro→Claude→Flash fallback chain, default), "
        "'pro' (Gemini Pro only), 'claude' (Claude only), 'flash' (Flash only)",
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
        k for k in ["GEMINI_API_KEY", "ANTHROPIC_API_KEY"] if os.environ.get(k)
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
    now = datetime.now()
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

    prompt = build_prompt(prompt_context, enriched=using_enriched, hours=args.hours)

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
                f"```\n{build_prompt('(article data follows below)', enriched=using_enriched, hours=args.hours)}\n```\n\n"
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
