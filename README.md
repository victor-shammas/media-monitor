# Transatlantic Right-Wing Media Monitor

An automated research tool that tracks far-right and right-wing political movements across nine countries and transnational networks. The system scrapes headlines from Google News RSS, optionally enriches them with article text, and generates AI-produced intelligence briefs via the Gemini API.

## Architecture

The system is a three-stage pipeline, orchestrated by GitHub Actions:

```
┌─────────────────────────────────────────────────────────────────┐
│                        GitHub Actions                           │
│                                                                 │
│   Every 2h          05:00 UTC            06:00 UTC              │
│   ┌──────────┐      ┌──────────────┐     ┌──────────────┐      │
│   │ Monitor  │─────▶│   Scraper    │────▶│  AI Reporter │      │
│   │ (RSS)    │      │ (trafilatura)│     │  (Gemini)    │      │
│   └────┬─────┘      └──────┬───────┘     └──────┬───────┘      │
│        │                   │                    │               │
│        ▼                   ▼                    ▼               │
│   monitor_state.json  enriched/            reports/             │
│   feeds/*.txt         enriched_YYYY-MM-DD  YYYY-MM-DD_report   │
│                       .json                .md + _input.md      │
│                                            (or HTML email)      │
└─────────────────────────────────────────────────────────────────┘
```

### Stage 1: Monitor (`media-monitor.py`)

Scrapes Google News RSS feeds for nine categories of right-wing political activity. Runs every two hours. Stores all seen articles in `monitor_state.json` with deduplication per category. Generates human-readable `.txt` files in `feeds/` for browsing.

**Key design choice:** Uses only RSS metadata (titles, links, publication dates). Never visits article URLs. This keeps the monitor fast, lightweight, and immune to 403 blocks.

**Categories tracked:**

| ID        | Label                               | Language |
|-----------|-------------------------------------|----------|
| `maga`    | 🇺🇸 MAGA / Trump                    | en       |
| `frp`     | 🇳🇴 Fremskrittspartiet               | no       |
| `sd`      | 🇸🇪 Sverigedemokraterna              | sv       |
| `rn`      | 🇫🇷 Rassemblement National           | fr       |
| `fdi`     | 🇮🇹 Fratelli d'Italia / Lega         | it       |
| `reform`  | 🇬🇧 Reform UK                        | en       |
| `general` | 🌍 General Right-Wing                | en       |
| `nodes`   | 🕸️ Transnational Network Infrastructure | en    |
| `hungary` | 🇭🇺 Hungary (Fidesz / Tisza)         | en       |

### Stage 2: Scraper (`article_scraper.py`)

Runs daily at 05:00 UTC. Takes the last 24 hours of articles from `monitor_state.json`, decodes Google News redirect URLs (which are base64-encoded protobuf, not HTTP redirects) using `googlenewsdecoder`, then extracts article text via `trafilatura`. Truncates each extract to ~300 words.

Output: `enriched/enriched_YYYY-MM-DD.json` — a self-contained dated file with titles, resolved URLs, article extracts, and extraction status per article. These accumulate as a running archive.

**Expected success rate:** ~80% of articles yield clean text. Failures cluster around paywalled outlets (Reuters), aggregator shells (MSN), and social media links (Facebook). Known-bad domains are pre-filtered.

### Stage 3: AI Reporter (`ai_reporter.py`)

Runs daily at 06:00 UTC. Checks for today's enriched file; if found, builds the prompt with titles + article extracts. If not, falls back to titles-only from `monitor_state.json`. Sends the compiled data to Google Gemini with structured prompt instructions, then post-processes the output with a citation system that links reference numbers to source URLs.

**Output modes:**
- `--markdown` — writes `reports/YYYY-MM-DD_report.md` and `reports/YYYY-MM-DD_input.md` (for prompt inspection)
- Default — sends a styled HTML email via Gmail SMTP

## File Structure

```
media-monitor/
├── .github/workflows/
│   ├── monitor.yml          # RSS scraper, every 2h
│   ├── scraper.yml          # Article enrichment, daily 05:00
│   └── daily_report.yml     # AI report, daily 06:00
├── media-monitor.py         # Stage 1: RSS monitor
├── article_scraper.py       # Stage 2: Article text extraction
├── ai_reporter.py           # Stage 3: Gemini analysis + email
├── test_scraper.py          # Feasibility testing for scraper
├── monitor_state.json       # Persistent article database (auto-generated)
├── feeds/                   # Human-readable .txt files per category
├── enriched/                # Dated enriched JSON files (auto-generated)
├── reports/                 # Markdown reports (when using --markdown)
└── index.html               # Single-page web viewer for feeds/
```

## Data Flow

```
Google News RSS
    │
    ▼
monitor_state.json ──────────────────────┐
    │                                    │
    ▼                                    ▼
enriched/enriched_YYYY-MM-DD.json    [fallback if no
    │                                 enriched file]
    ▼                                    │
ai_reporter.py ◀─────────────────────────┘
    │
    ├──▶ reports/*.md           (--markdown mode)
    └──▶ HTML email via Gmail   (production mode)
```

## Citation System

The reporter assigns sequential reference numbers to every article (`[1]`, `[2]`, etc.) before sending to Gemini. The prompt instructs Gemini to cite sources inline using these numbers. After Gemini responds, the script replaces each `[N]` with a clickable markdown link to the actual article URL. A Sources appendix lists only the articles Gemini actually cited.

This avoids LLM hallucination of URLs — Gemini only handles numbers (which it's reliable at), while the script handles the URL mapping deterministically.

## GitHub Secrets

The following secrets must be configured in the repository settings:

| Secret           | Used by          | Description                        |
|------------------|------------------|------------------------------------|
| `GEMINI_API_KEY` | Reporter         | Google Gemini API key (free tier)  |
| `SENDER_EMAIL`   | Reporter         | Gmail address for sending          |
| `EMAIL_PASSWORD` | Reporter         | Gmail app password (**no spaces**) |
| `RECEIVER_EMAIL` | Reporter         | Destination email address          |

**Note on Gmail app passwords:** Google displays them as `xxxx xxxx xxxx xxxx` but the actual password is the 16 characters concatenated with no spaces.

## Local Development

### Prerequisites

```bash
pip install google-genai tenacity markdown trafilatura googlenewsdecoder
```

### Set environment variables

```bash
export GEMINI_API_KEY="your-key-here"
export SENDER_EMAIL="your-email@gmail.com"        # only for email mode
export EMAIL_PASSWORD="yourapppasswordnospaces"    # only for email mode
export RECEIVER_EMAIL="destination@email.com"      # only for email mode
```

**macOS users:** Disable smart quotes in System Settings → Keyboard → Text Input, or in Terminal → Edit → Substitutions. macOS will silently convert straight quotes to curly quotes in `export` commands, breaking authentication.

### Run the pipeline locally

```bash
# 1. Scrape RSS feeds
python media-monitor.py -d feeds

# 2. Enrich with article text
python article_scraper.py --hours 48

# 3. Generate report (markdown, for testing)
python ai_reporter.py --markdown --hours 48 --model flash
```

### Useful flags

| Script             | Flag              | Effect                                     |
|--------------------|-------------------|--------------------------------------------|
| `ai_reporter.py`   | `--markdown`      | Write to `reports/` instead of emailing    |
| `ai_reporter.py`   | `--model flash`   | Use fast Gemini model (default: `pro`)     |
| `ai_reporter.py`   | `--hours N`       | Look-back window in hours                  |
| `ai_reporter.py`   | `--no-enriched`   | Force titles-only even if enriched exists  |
| `article_scraper.py` | `--category frp`  | Scrape only one category                 |
| `article_scraper.py` | `--hours N`       | Look-back window                          |
| `media-monitor.py` | `--feeds maga frp` | Run only specific feeds                  |
| `test_scraper.py`  | `--sample 30`     | Test N random URLs for scrapeability       |
| `test_scraper.py`  | `--output r.json` | Save detailed results                     |

## Constraints and Design Decisions

**Lightweight by design.** The monitor never visits article URLs — it works entirely from RSS metadata. This avoids 403 blocks, rate limiting, and high processing costs. The scraper is a separate, optional enrichment step.

**Graceful degradation.** If the scraper fails or hasn't run, the reporter falls back to titles-only analysis. If Gemini is overloaded (503), the retry logic waits and retries up to 5 times with exponential backoff.

**No external databases.** All state is stored in flat JSON files committed to the repository. This keeps the system portable and inspectable.

**Sanitization.** Gemini output is normalized to strip non-breaking spaces, curly quotes, em dashes, and other Unicode characters that cause email encoding failures.
