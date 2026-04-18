#!/usr/bin/env python3
"""
AI Reporter — generates intelligence briefs from the media monitor state file.

Modes:
  python ai_reporter.py              → HTML email (production)
  python ai_reporter.py --markdown   → writes .md files to reports/ (testing)
"""
import argparse
import json
import os
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
def generate_ai_content(client, prompt):
    return client.models.generate_content(model="gemini-2.5-flash", contents=prompt)


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
    """Normalize Unicode and strip non-breaking spaces."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\xa0", " ")
    return text


# ── Category Labels ────────────────────────────────────────────────────────

CATEGORY_LABELS = {
    "maga": "🇺🇸 MAGA / Trump",
    "frp": "🇳🇴 Fremskrittspartiet",
    "sd": "🇸🇪 Sverigedemokraterna",
    "rn": "🇫🇷 Rassemblement National",
    "fdi": "🇮🇹 Fratelli d'Italia / Lega",
    "reform": "🇬🇧 Reform UK",
    "general": "🌍 General Right-Wing",
    "nodes": "🕸️ Transnational Networks",
    "hungary": "🇭🇺 Hungary (Fidesz / Tisza)",
}


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


# ── Data Compilation ──────────────────────────────────────────────────────

def compile_recent_data(state: dict, cutoff: datetime) -> tuple[str, int, int]:
    """Extract recent articles from the state file.
    Returns (context_str, article_count, category_count)."""
    compiled_data = []
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
                compiled_data.append(f"- {item['title']} (Source: {item['source']})")
            compiled_data.append("")

    return "\n".join(compiled_data), article_count, category_count


def build_prompt(context: str) -> str:
    return (
        "You are an expert political analyst writing a daily intelligence brief. "
        "Analyze the last 24 hours of right-wing and far-right news from the data below.\n\n"
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
        f"DATA:\n{context}"
    )


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
    args = parser.parse_args()

    # In markdown mode, only the API key is required
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY not set.")
        sys.exit(1)

    if not args.markdown:
        sender_email = os.environ.get("SENDER_EMAIL")
        email_password = os.environ.get("EMAIL_PASSWORD")
        receiver_email = os.environ.get("RECEIVER_EMAIL")
        if not all([sender_email, email_password, receiver_email]):
            print("Error: Missing email secrets. Use --markdown for testing without email.")
            sys.exit(1)

    if not os.path.exists(STATE_FILE):
        print("No state file found.")
        sys.exit(1)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    prompt_context, article_count, category_count = compile_recent_data(state, cutoff)

    if not prompt_context.strip():
        print(f"No new articles in the last {args.hours} hours. Skipping report.")
        sys.exit(0)

    prompt = build_prompt(prompt_context)
    today_str = datetime.now().strftime("%B %d, %Y")
    date_slug = datetime.now().strftime("%Y-%m-%d")

    # ── Markdown mode: dump input + output to files ──────────────────────
    if args.markdown:
        os.makedirs(args.outdir, exist_ok=True)

        # 1. Write the raw input (what Gemini sees)
        input_path = os.path.join(args.outdir, f"{date_slug}_input.md")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write(f"# Gemini Input — {today_str}\n\n")
            f.write(f"**Look-back:** {args.hours} hours\n")
            f.write(f"**Articles:** {article_count} across {category_count} categories\n\n")
            f.write("---\n\n")
            f.write("## Prompt\n\n")
            f.write(f"```\n{prompt}\n```\n\n")
            f.write("---\n\n")
            f.write("## Raw Data Sent to Model\n\n")
            f.write(prompt_context)
        print(f"  ✓ Input saved → {input_path}")

        # 2. Call Gemini
        print("Generating AI Analysis (with automatic retries)...")
        client = genai.Client()
        try:
            response = generate_ai_content(client, prompt)
            analysis_text = sanitize(response.text)
        except Exception as e:
            print(f"Final failure after multiple retries: {e}")
            sys.exit(1)

        # 3. Write the analysis output
        output_path = os.path.join(args.outdir, f"{date_slug}_report.md")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(f"# Intelligence Brief — {today_str}\n\n")
            f.write(f"*{article_count} articles across {category_count} categories*\n\n")
            f.write("---\n\n")
            f.write(analysis_text)
        print(f"  ✓ Report saved → {output_path}")
        return

    # ── Email mode (production) ──────────────────────────────────────────
    print("Generating AI Analysis (with automatic retries)...")
    client = genai.Client()

    try:
        response = generate_ai_content(client, prompt)
        analysis_text = sanitize(response.text)
    except Exception as e:
        print(f"Final failure after multiple retries: {e}")
        sys.exit(1)

    # Convert Markdown → styled HTML
    analysis_html = md_lib.markdown(analysis_text, extensions=["extra", "smarty"])
    analysis_html = style_html(analysis_html)

    subject = f"Intelligence Brief: Transatlantic Right-Wing Media ({today_str})"
    full_html = build_html_email(analysis_html, today_str, article_count, category_count)

    # Build multipart message (plain text fallback + HTML)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = receiver_email
    msg.attach(MIMEText(analysis_text, "plain", "utf-8"))
    msg.attach(MIMEText(full_html, "html", "utf-8"))

    print("Sending email...")
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(sender_email, email_password)
        server.send_message(msg)
        server.quit()
        print("Report emailed successfully!")
    except Exception as e:
        print(f"Failed to send email: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
