#!/usr/bin/env python3
import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

try:
    from google import genai
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )
except ImportError:
    print("Please install dependencies: pip install google-genai tenacity")
    sys.exit(1)

STATE_FILE = "monitor_state.json"


# --- RETRY LOGIC ---
# This tells the script to try up to 5 times if it hits a ServerError (503)
# It waits 4s, 8s, 16s, 32s between attempts.
@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    retry=retry_if_exception_type(Exception),  # Catches the 503 ServerError
    before_sleep=lambda retry_state: print(
        f"  ⚠ Model busy (503). Retrying in {retry_state.next_action.sleep}s... (Attempt {retry_state.attempt_number})"
    ),
)
def generate_ai_content(client, prompt):
    return client.models.generate_content(model="gemini-2.5-flash", contents=prompt)


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


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    sender_email = os.environ.get("SENDER_EMAIL")
    email_password = os.environ.get("EMAIL_PASSWORD")
    receiver_email = os.environ.get("RECEIVER_EMAIL")

    if not all([api_key, sender_email, email_password, receiver_email]):
        print("Error: Missing required environment variables/secrets.")
        sys.exit(1)

    if not os.path.exists(STATE_FILE):
        print("No state file found.")
        sys.exit(1)

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        state = json.load(f)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    compiled_data = []

    for category, items in state.items():
        recent_items = [item for item in items if get_sort_time(item) >= cutoff]
        if recent_items:
            compiled_data.append(f"### CATEGORY: {category.upper()} ###")
            for item in recent_items:
                compiled_data.append(f"- {item['title']} (Source: {item['source']})")
            compiled_data.append("\n")

    if not compiled_data:
        print("No new articles in the last 24 hours. Skipping report.")
        sys.exit(0)

    prompt_context = "\n".join(compiled_data)
    prompt = (
        "You are an expert political analyst monitoring international media. "
        "Analyze the last 24 hours of right-wing news from around the world contained in the data below. "
        "Create a high-level summary of the far right's activities, strategies, and priorities. "
        "Format your response cleanly with readable paragraphs and bullet points.\n\n"
        f"DATA:\n{prompt_context}"
    )

    print("Generating AI Analysis (with automatic retries)...")
    client = genai.Client()

    try:
        response = generate_ai_content(client, prompt)
        analysis_text = response.text
    except Exception as e:
        print(f"Final failure after multiple retries: {e}")
        sys.exit(1)

    print("Sending email...")
    msg = EmailMessage()
    msg.set_content(analysis_text, charset="utf-8")

    today_str = datetime.now().strftime("%B %d, %Y")
    msg["Subject"] = f"Intelligence Brief: Transatlantic Right-Wing Media ({today_str})"
    msg["From"] = sender_email
    msg["To"] = receiver_email

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
