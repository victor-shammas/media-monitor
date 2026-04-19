#!/usr/bin/env python3
"""
AI Reporter — generates intelligence briefs from the media monitor.

Data sources (checked in order):
  1. enriched/enriched_YYYY-MM-DD.json  (titles + article extracts)
  2. monitor_state.json                  (titles only, fallback)

Modes:
  python ai_reporter.py --markdown              → writes .md files (testing)
  python ai_reporter.py                          → HTML email (production)
  python ai_reporter.py --markdown --email      → both markdown + email
  python ai_reporter.py --markdown --model flash → fast iteration
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
    from google import genai
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )
except ImportError:
    print("Please install dependencies: pip install google-genai tenacity markdown")
    sys.exit(1)

STATE_FILE = "monitor_state.json"


# ── Retry Logic ────────────────────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception_type(Exception),
    before_sleep=lambda retry_state: print(
        f"  ⚠ Model busy (503). Retrying in {retry_state.next_action.sleep}s... "
        f"(Attempt {retry_state.attempt_number})"
    ),
)
def generate_ai_content(client, prompt, model="gemini-2.5-flash"):
    return client.models.generate_content(model=model, contents=prompt)


MODEL_ALIASES = {
    "flash": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",
}


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


# ── Category Labels ────────────────────────────────────────────────────────

CATEGORY_LABELS = {
    "maga": "🇺🇸 MAGA / Trump",
    "frp": "🇳🇴 Fremskrittspartiet",
    "sd": "🇸🇪 Sverigedemokraterna",
    "rn": "🇫🇷 Rassemblement National",
    "fdi": "🇮🇹 Fratelli d'Italia / Lega",
    "reform": "🇬🇧 Reform UK",
    "afd": "🇩🇪 Alternative für Deutschland",
    "general": "🌍 General Right-Wing",
    "nodes": "🕸️ Transnational Networks",
    "hungary": "🇭🇺 Hungary (Fidesz / Tisza)",
}


# ── Data Sources ──────────────────────────────────────────────────────────

def load_enriched(enriched_dir: str, date_slug: str) -> list[dict] | None:
    """Try to load today's enriched file. Returns list of articles or None."""
    path = os.path.join(enriched_dir, f"enriched_{date_slug}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        articles = data.get("articles", [])
        if not articles:
            return None
        return articles
    except Exception as e:
        print(f"  ⚠ Failed to load enriched file: {e}")
        return None


def compile_from_enriched(articles: list[dict]) -> tuple[str, dict, int, int]:
    """Build prompt context from enriched data (titles + extracts).
    Returns (context_str, reference_map, article_count, category_count).
    """
    by_cat = {}
    for a in articles:
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
        r'\[(\d+(?:\s*,\s*\d+)+)\]',
        replace_group,
        text,
    )
    result = re.sub(
        r'(?<!\[)\[(\d+)\](?!\(|\])',
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

def build_prompt(context: str, enriched: bool = False) -> str:
    data_description = (
        "Each article includes a title and, where available, an EXTRACT "
        "with the opening paragraphs of the article. Use the extracts to "
        "ground your analysis in the actual framing and argumentation of "
        "the source material."
        if enriched else
        "Each article is represented by its headline and source outlet."
    )

    return (
        "You are an expert political analyst writing a daily intelligence brief. "
        "Analyze the last 24 hours of right-wing and far-right news from the data below.\n\n"
        f"DATA DESCRIPTION: {data_description}\n\n"
        "FORMAT INSTRUCTIONS:\n"
        "- Write in Markdown.\n"
        "- Begin with a short executive summary paragraph (2-3 sentences) of the day's "
        "most significant developments.\n"
        "- Then organize your analysis by thematic cluster, NOT by country. Use ## headings "
        "for each cluster (e.g., '## Immigration and Border Politics', "
        "'## Transnational Conservative Networking').\n"
        "- Within each section, note cross-national patterns and connections where they exist.\n"
        "- Use **bold** for party names and key actors on first mention.\n"
        "- End with a short 'Watchlist' section flagging 2-3 emerging stories to track.\n"
        "- Keep the tone analytical and concise — this is a professional briefing, "
        "not a news summary.\n\n"
        "CITATION INSTRUCTIONS:\n"
        "- Each article in the data has a reference number in square brackets, e.g. [1], [2].\n"
        "- When you make a claim that draws on a specific article, cite it inline using its "
        "number: e.g. 'Vance faced bipartisan criticism [14] amid growing internal dissent [27].'\n"
        "- Cite only the single most relevant source per claim. Never stack multiple citations.\n"
        "- Do NOT invent reference numbers that are not in the data.\n"
        "- Do NOT create a bibliography or references section — just use inline citations.\n\n"
        f"DATA:\n{context}"
    )


# ── HTML Template ──────────────────────────────────────────────────────────

def build_html_email(analysis_html: str, today_str: str, article_count: int,
                     category_count: int) -> str:
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
      Analysis by Gemini&ensp;·&ensp;Data from Google News RSS.
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
        ("<h1>", '<h1 style="font-size:20px; color:#1a1a2e; margin:28px 0 12px 0; '
                 'border-bottom:2px solid #1a1a2e; padding-bottom:6px;">'),
        ("<h2>", '<h2 style="font-size:17px; color:#1a1a2e; margin:24px 0 10px 0;">'),
        ("<h3>", '<h3 style="font-size:15px; color:#333; margin:20px 0 8px 0; '
                 'font-family:Helvetica,Arial,sans-serif;">'),
        ("<p>", '<p style="margin:0 0 14px 0;">'),
        ("<ul>", '<ul style="margin:0 0 16px 0; padding-left:20px;">'),
        ("<ol>", '<ol style="margin:0 0 16px 0; padding-left:20px;">'),
        ("<li>", '<li style="margin:0 0 6px 0;">'),
        ("<strong>", '<strong style="color:#1a1a2e;">'),
        ("<blockquote>", '<blockquote style="margin:16px 0; padding:12px 20px; '
                         'border-left:3px solid #1a1a2e; background:#f8f8fa; '
                         'font-style:italic; color:#555;">'),
        ("<hr>", '<hr style="border:none; border-top:1px solid #ddd; margin:24px 0;">'),
        ("<hr/>", '<hr style="border:none; border-top:1px solid #ddd; margin:24px 0;">'),
        ("<hr />", '<hr style="border:none; border-top:1px solid #ddd; margin:24px 0;">'),
    ]
    for old, new in replacements:
        raw_html = raw_html.replace(old, new)
    return raw_html


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AI Intelligence Reporter")
    parser.add_argument(
        "--markdown", action="store_true",
        help="Write markdown reports to reports/ instead of sending email (testing mode)"
    )
    parser.add_argument(
        "--hours", type=int, default=24,
        help="Look-back window in hours (default: 24)"
    )
    parser.add_argument(
        "--outdir", default="reports",
        help="Output directory for markdown reports (default: reports/)"
    )
    parser.add_argument(
        "--model", default="pro", choices=["flash", "pro"],
        help="Gemini model: 'flash' (fast, good for testing) or 'pro' (best, default)"
    )
    parser.add_argument(
        "--enriched-dir", default="enriched",
        help="Directory containing enriched JSON files (default: enriched/)"
    )
    parser.add_argument(
        "--no-enriched", action="store_true",
        help="Force titles-only mode even if enriched data exists"
    )
    parser.add_argument(
        "--email", action="store_true",
        help="Send email report (default when --markdown is not set; "
             "combine with --markdown to do both)"
    )
    args = parser.parse_args()

    # ── Determine output modes ───────────────────────────────────────────
    # No flags          → email only  (backward compatible)
    # --markdown        → markdown only (backward compatible)
    # --email           → email only
    # --markdown --email→ both
    do_markdown = args.markdown
    do_email = args.email or (not args.markdown)

    model_name = MODEL_ALIASES[args.model]

    # In markdown-only mode, only the API key is required
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set.")
        sys.exit(1)

    if do_email:
        sender_email = os.environ.get("SENDER_EMAIL")
        email_password = os.environ.get("EMAIL_PASSWORD")
        receiver_email = os.environ.get("RECEIVER_EMAIL")
        if not all([sender_email, email_password, receiver_email]):
            print("Error: Missing email secrets. Use --markdown for testing without email.")
            sys.exit(1)

    if not os.path.exists(STATE_FILE):
        print("No state file found.")
        sys.exit(1)

    # ── Choose data source ────────────────────────────────────────────────
    date_slug = datetime.now().strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%B %d, %Y")
    using_enriched = False

    if not args.no_enriched:
        enriched_articles = load_enriched(args.enriched_dir, date_slug)
        if enriched_articles:
            enriched_with_text = sum(
                1 for a in enriched_articles if a.get("extract_status") == "ok"
            )
            print(
                f"Using enriched data: {len(enriched_articles)} articles "
                f"({enriched_with_text} with text extracts)"
            )
            prompt_context, ref_map, article_count, category_count = \
                compile_from_enriched(enriched_articles)
            using_enriched = True
        else:
            print("No enriched file found for today. Falling back to titles-only.")

    if not using_enriched:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
        prompt_context, ref_map, article_count, category_count = \
            compile_from_state(state, cutoff)

    if not prompt_context.strip():
        print(f"No articles found. Skipping report.")
        sys.exit(0)

    prompt = build_prompt(prompt_context, enriched=using_enriched)

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

    print(f"Generating AI Analysis via {model_name} (with automatic retries)...")
    client = genai.Client()

    try:
        response = generate_ai_content(client, prompt, model=model_name)
        analysis_text = sanitize(response.text)
    except Exception as e:
        print(f"Final failure after multiple retries: {e}")
        sys.exit(1)

    # Inject markdown links and build sources appendix
    analysis_linked, cited = inject_links_markdown(analysis_text, ref_map)
    sources_appendix = build_sources_appendix_md(ref_map, cited)

    print(f"  ✓ Gemini cited {len(cited)} of {len(ref_map)} references.")

    # ── Markdown output ──────────────────────────────────────────────────
    if do_markdown:
        os.makedirs(args.outdir, exist_ok=True)

        # 1. Write the input debug file
        input_path = os.path.join(args.outdir, f"{date_slug}_input.md")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write(f"# Gemini Input — {today_str}\n\n")
            f.write(f"**Look-back:** {args.hours} hours\n")
            f.write(f"**Data source:** {'enriched' if using_enriched else 'titles-only'}\n")
            f.write(f"**Articles:** {article_count} across {category_count} categories\n")
            f.write(f"**References indexed:** {len(ref_map)}\n\n")
            f.write("---\n\n")
            f.write("## Prompt Instructions\n\n")
            f.write(f"```\n{build_prompt('(article data follows below)', enriched=using_enriched)}\n```\n\n")
            f.write("---\n\n")
            f.write("## Article Data\n\n")
            f.write(prompt_context)
        print(f"  ✓ Input saved → {input_path}")

        # 2. Write the report
        output_path = os.path.join(args.outdir, f"{date_slug}_report.md")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# Intelligence Brief — {today_str}\n\n")
            f.write(f"*{article_count} articles across {category_count} categories")
            if using_enriched:
                f.write(f" (enriched)")
            f.write("*\n\n")
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
        full_html = build_html_email(analysis_html, today_str, article_count, category_count)

        # Plain-text fallback
        plain_sources = ""
        if cited:
            plain_sources = "\n---\nSources:\n"
            for num in sorted(cited):
                ref = ref_map[num]
                plain_sources += f"  [{num}] {sanitize(ref['title'])} — {sanitize(ref['source'])}\n"
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
