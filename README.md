# Transatlantic Right-Wing Media Monitor

An automated research tool that tracks far-right and right-wing political movements across twelve categories spanning multiple countries and transnational networks. The system scrapes headlines from Google News RSS in both native languages and English, optionally enriches them with article text, and generates AI-produced intelligence briefs via a multi-provider LLM fallback chain (Gemini Pro → Claude Sonnet → Gemini Flash).

## Architecture

The system is a three-stage pipeline, orchestrated by GitHub Actions:

```
┌─────────────────────────────────────────────────────────────────┐
│                        GitHub Actions                           │
│                                                                 │
│   Every 2h          05:00 UTC            06:00 UTC              │
│   ┌──────────┐      ┌──────────────┐     ┌──────────────┐      │
│   │ Monitor  │─────▶│   Scraper    │────▶│  AI Reporter │      │
│   │ (RSS)    │      │ (trafilatura)│     │  (multi-LLM) │      │
│   └────┬─────┘      └──────┬───────┘     └──────┬───────┘      │
│        │                   │                    │               │
│        ▼                   ▼                    ▼               │
│   monitor_state.json  enriched/            reports/             │
│   feeds/*.txt         enriched_YYYY-MM-DD  YYYY-MM-DD_HHMM     │
│                       .json                _report.md           │
│                                            + _input.md          │
│                                            (or HTML email)      │
└─────────────────────────────────────────────────────────────────┘
```

### Stage 1: Monitor (`media-monitor.py`)

Scrapes Google News RSS feeds for twelve categories of right-wing political activity. Runs every two hours. Stores all seen articles in `monitor_state.json` with deduplication per category. Generates human-readable `.txt` files in `feeds/` for browsing.

**Key design choice:** Uses only RSS metadata (titles, links, publication dates). Never visits article URLs. This keeps the monitor fast, lightweight, and immune to 403 blocks.

**Bilingual scraping:** Eight non-English categories define `variants` — each feed is scraped once in the native language (e.g. `no`/`NO`) and once in English (`en`/`US`). The English variant uses `country: "US"` rather than the target country, since the US Google News index is effectively the largest international English index and surfaces BBC, Reuters, Guardian, etc. alongside American outlets. The query terms themselves handle relevance filtering. Some feeds (Germany, Poland) use per-variant query overrides to drop ambiguous acronyms (e.g. "AfD", "PiS") from the English variant to avoid false positives.

**Categories tracked:**

| ID         | Label                                    | Languages |
|------------|------------------------------------------|-----------|
| `usa`      | 🇺🇸 MAGA / Trump                         | en        |
| `norway`   | 🇳🇴 Fremskrittspartiet                    | no + en   |
| `sweden`   | 🇸🇪 Sverigedemokraterna                   | sv + en   |
| `france`   | 🇫🇷 Rassemblement National                | fr + en   |
| `italy`    | 🇮🇹 Fratelli d'Italia / Lega              | it + en   |
| `germany`  | 🇩🇪 Alternative für Deutschland           | de + en   |
| `uk`       | 🇬🇧 Reform UK                             | en        |
| `hungary`  | 🇭🇺 Fidesz / Tisza                        | hu + en   |
| `poland`   | 🇵🇱 Prawo i Sprawiedliwość                | pl + en   |
| `spain`    | 🇪🇸 Vox                                   | es + en   |
| `general`  | 🌍 General Right-Wing News                | en        |
| `networks` | 🕸️ Transnational Networks                 | en        |

### Stage 2: Scraper (`article_scraper.py`)

Runs daily at 05:00 UTC. Takes the last 24 hours of articles from `monitor_state.json`, decodes Google News redirect URLs (which are base64-encoded protobuf, not HTTP redirects) using `googlenewsdecoder`, then extracts article text via `trafilatura`. Truncates each extract to ~300 words.

Output: `enriched/enriched_YYYY-MM-DD.json` — a self-contained dated file with titles, resolved URLs, article extracts, and extraction status per article. These accumulate as a running archive. Articles are routed by their **publication date**, not the scrape date — a single run may write to multiple date files.

**Expected success rate:** ~80% of articles yield clean text. Failures cluster around paywalled outlets (Reuters), aggregator shells (MSN), and social media links (Facebook). Known-bad domains are pre-filtered.

### Stage 3: AI Reporter (`ai_reporter.py`)

Runs daily at 06:00 UTC. Loads enriched files covering the lookback window (e.g. today + yesterday for a 24-hour window, since it straddles midnight), then filters articles by publication date. If no enriched files exist, falls back to titles-only from `monitor_state.json`. Sends the compiled data to the LLM with structured prompt instructions, then post-processes the output with a citation system that links reference numbers to source URLs.

**Multi-provider fallback:** The default mode (`--model auto`) tries providers in sequence until one succeeds: Gemini 2.5 Pro → Claude Sonnet 4.6 → Gemini 2.5 Flash. Each provider gets 3 retry attempts with exponential backoff before falling back. If a provider's API key isn't set, it's silently skipped.

**Output modes:**
- `--markdown` — writes `reports/YYYY-MM-DD_HHMM_report.md` and `reports/YYYY-MM-DD_HHMM_input.md` (for prompt inspection). Timestamped filenames allow multiple runs per day without overwriting.
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
├── ai_reporter.py           # Stage 3: Multi-LLM analysis + email
├── config.toml              # Shared config: category labels + AI prompt
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

The reporter assigns sequential reference numbers to every article (`[1]`, `[2]`, etc.) before sending to the LLM. The prompt instructs the model to cite sources inline using these numbers. After the model responds, the script replaces each `[N]` with a clickable markdown link to the actual article URL. A Sources appendix lists only the articles actually cited.

This avoids LLM hallucination of URLs — the model only handles numbers (which it's reliable at), while the script handles the URL mapping deterministically.

## GitHub Secrets

The following secrets must be configured in the repository settings:

| Secret              | Used by          | Description                                |
|---------------------|------------------|--------------------------------------------|
| `GEMINI_API_KEY`    | Reporter         | Google Gemini API key                      |
| `ANTHROPIC_API_KEY` | Reporter         | Anthropic API key (for Claude fallback)    |
| `SENDER_EMAIL`      | Reporter         | Gmail address for sending                  |
| `EMAIL_PASSWORD`    | Reporter         | Gmail app password (**no spaces**)         |
| `RECEIVER_EMAIL`    | Reporter         | Destination email address                  |

**Note on Gmail app passwords:** Google displays them as `xxxx xxxx xxxx xxxx` but the actual password is the 16 characters concatenated with no spaces.

## Local Development

### Prerequisites

```bash
pip install google-genai anthropic tenacity markdown trafilatura googlenewsdecoder
```

### Set environment variables

```bash
export GEMINI_API_KEY="your-key-here"
export ANTHROPIC_API_KEY="your-key-here"               # optional, for Claude fallback
export SENDER_EMAIL="your-email@gmail.com"              # only for email mode
export EMAIL_PASSWORD="yourapppasswordnospaces"         # only for email mode
export RECEIVER_EMAIL="destination@email.com"           # only for email mode
```

**macOS users:** Disable smart quotes in System Settings → Keyboard → Text Input, or in Terminal → Edit → Substitutions. macOS will silently convert straight quotes to curly quotes in `export` commands, breaking authentication.

### Run the pipeline locally

```bash
# 1. Scrape RSS feeds
python media-monitor.py -d feeds

# 2. Enrich with article text
python article_scraper.py --hours 48

# 3. Generate report (markdown, for testing)
python ai_reporter.py --markdown --hours 48
```

### Useful flags

| Script               | Flag                | Effect                                     |
|----------------------|---------------------|--------------------------------------------|
| `ai_reporter.py`    | `--markdown`        | Write to `reports/` instead of emailing    |
| `ai_reporter.py`    | `--model auto`      | Full fallback chain (default)              |
| `ai_reporter.py`    | `--model pro`       | Gemini Pro only                            |
| `ai_reporter.py`    | `--model claude`    | Claude Sonnet only                         |
| `ai_reporter.py`    | `--model flash`     | Gemini Flash only                          |
| `ai_reporter.py`    | `--hours N`         | Look-back window in hours                  |
| `ai_reporter.py`    | `--no-enriched`     | Force titles-only even if enriched exists  |
| `article_scraper.py`| `--category norway` | Scrape only one category                   |
| `article_scraper.py`| `--hours N`         | Look-back window                           |
| `media-monitor.py`  | `--feeds usa norway` | Run only specific feeds                   |

## Constraints and Design Decisions

**Lightweight by design.** The monitor never visits article URLs — it works entirely from RSS metadata. This avoids 403 blocks, rate limiting, and high processing costs. The scraper is a separate, optional enrichment step.

**Graceful degradation.** If the scraper fails or hasn't run, the reporter falls back to titles-only analysis. If all LLM providers fail, the retry and fallback logic exhausts every option before exiting.

**Multi-provider resilience.** The reporter chains Gemini Pro → Claude Sonnet → Gemini Flash. Each gets retries with exponential backoff. Providers whose API keys aren't configured are silently skipped.

**No external databases.** All state is stored in flat JSON files committed to the repository. This keeps the system portable and inspectable.

**Sanitization.** LLM output is normalized to strip non-breaking spaces, curly quotes, em dashes, and other Unicode characters that cause email encoding failures.
