# Transatlantic Right-Wing Media Monitor

An automated research tool that tracks far-right and right-wing political movements across twelve categories spanning multiple countries and transnational networks. The system scrapes headlines from Google News RSS in both native languages and English, enriches them with article text and AI-generated one-sentence summaries (Gemini 2.5 Flash), and produces daily intelligence briefs via a multi-provider LLM fallback chain (Gemini Pro → Claude Sonnet → Gemini Flash).

## Architecture

The system is a three-stage pipeline, orchestrated by GitHub Actions:

```
┌─────────────────────────────────────────────────────────────────┐
│                        GitHub Actions                           │
│                                                                 │
│   Hourly            Every 2h (:30)       05:00 UTC              │
│   ┌──────────┐      ┌──────────────┐     ┌──────────────┐      │
│   │ Monitor  │─────▶│   Scraper    │────▶│  AI Reporter │      │
│   │ (RSS)    │      │ trafilatura  │     │  (multi-LLM) │      │
│   │          │      │ + Gemini     │     │              │      │
│   │          │      │   Flash      │     │              │      │
│   └────┬─────┘      └──────┬───────┘     └──────┬───────┘      │
│        │                   │                    │               │
│        ▼                   ▼                    ▼               │
│   monitor_state.json  enriched/            reports/             │
│   feeds/*.txt         enriched_YYYY-MM-DD  YYYY-MM-DD_HHMM      │
│   (+ AI summaries     .json                _report.md           │
│    written back)                           + _input.md          │
│                                            (or HTML email)      │
└─────────────────────────────────────────────────────────────────┘
```

### Stage 1: Monitor (`media-monitor.py`)

Scrapes Google News RSS feeds for twelve categories of right-wing political activity. Runs hourly. Stores all seen articles in `monitor_state.json` with deduplication per category. Generates human-readable `.txt` files in `feeds/` for browsing.

Before reading state, the script auto-pulls from `origin` to avoid race conditions when GitHub Actions and local runs modify state concurrently.

**Feed definitions live in `config.toml`.** Queries, `lang`, `country`, and `variants` are defined there rather than hardcoded in Python — edit the TOML to add or tweak a feed.

**Key design choice:** Uses only RSS metadata (titles, links, publication dates). Never visits article URLs. This keeps the monitor fast, lightweight, and immune to 403 blocks.

**Bilingual scraping:** Eight non-English categories define `variants` — each feed is scraped once in the native language (e.g. `no`/`NO`) and once in English (`en`/`US`). The English variant uses `country: "US"` rather than the target country, since the US Google News index is effectively the largest international English index and surfaces BBC, Reuters, Guardian, etc. alongside American outlets. The query terms themselves handle relevance filtering. Some feeds (Germany, Poland) use per-variant query overrides to drop ambiguous acronyms (e.g. "AfD", "PiS") from the English variant to avoid false positives.

The USA feed is split into two separate queries to bypass Google News' ~100-article RSS cap, effectively doubling coverage of the MAGA category.

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

Runs every two hours (at :30). Takes recent articles from `monitor_state.json`, decodes Google News redirect URLs (which are base64-encoded protobuf, not HTTP redirects) using `googlenewsdecoder`, then extracts article text via `trafilatura`. Truncates each extract to ~300 words. A per-request socket timeout prevents hangs on unresponsive hosts.

Like the monitor, it auto-pulls from `origin` before reading state to avoid racing with concurrent writes.

Output: `enriched/enriched_YYYY-MM-DD.json` — a self-contained dated file with titles, resolved URLs, article extracts, and extraction status per article. These accumulate as a running archive. Articles are routed by their **publication date**, not the scrape date — a single run may write to multiple date files.

After extraction, the scraper sends successful extracts to **Gemini 2.5 Flash** in batches and requests a single-sentence summary per article (≤25 words). Summaries are written back to both the enriched JSON and `monitor_state.json`, then the feed `.txt` files are rebuilt so each article displays an `[AI Summary: …]` line. The monitor no longer captures the RSS `description` field, since 96% of those were just title echoes.

**Expected success rate:** ~80% of articles yield clean text. Failures cluster around paywalled outlets (Reuters), aggregator shells (MSN), and social media links (Facebook). Known-bad domains are pre-filtered.

**Backfill utility:** `backfill_summaries.py` is a one-off script for generating summaries for the top N articles per feed that don't yet have one. It reuses extracts from existing enriched JSON files where possible and only scrapes the rest, so repeat runs are cheap.

### Stage 3: AI Reporter (`ai_reporter.py`)

Runs daily at 05:00 UTC. Loads enriched files covering the lookback window (e.g. today + yesterday for a 24-hour window, since it straddles midnight), then filters articles by publication date. If no enriched files exist, falls back to titles-only from `monitor_state.json`. Sends the compiled data to the LLM with structured prompt instructions (loaded from `config.toml`), then post-processes the output with a citation system that links reference numbers to source URLs.

**Multi-provider fallback:** The default mode (`--model auto`) tries providers in sequence until one succeeds: Gemini 2.5 Pro → Claude Sonnet 4.6 → Gemini 2.5 Flash. Each provider gets 3 retry attempts with exponential backoff before falling back. If a provider's API key isn't set, it's silently skipped.

**Output modes:**
- Default — sends a styled HTML email via Gmail SMTP. A `reports/YYYY-MM-DD_HHMM_input.md` copy of the LLM prompt is still written for inspection.
- `--markdown` — also writes `reports/YYYY-MM-DD_HHMM_report.md`. Timestamped filenames allow multiple runs per day without overwriting.

## File Structure

```
media-monitor/
├── .github/workflows/
│   ├── monitor.yml          # RSS scraper, hourly
│   ├── scraper.yml          # Article enrichment + AI summaries, every 2h (:30)
│   ├── daily_report.yml     # AI report, daily 05:00 UTC
│   └── pages.yml            # GitHub Pages deploy for index.html
├── media-monitor.py         # Stage 1: RSS monitor
├── article_scraper.py       # Stage 2: Text extraction + Gemini Flash summaries
├── ai_reporter.py           # Stage 3: Multi-LLM analysis + email
├── backfill_summaries.py    # One-off: fill in summaries for existing articles
├── config.toml              # Category labels, feed definitions, AI prompt
├── blocklist.json           # Domains to skip during scraping
├── monitor_state.json       # Persistent article database (auto-generated)
├── feeds/                   # Human-readable .txt files per category
├── enriched/                # Dated enriched JSON files (auto-generated)
├── reports/                 # Markdown reports and prompt inputs
└── index.html               # Single-page web viewer, served via GitHub Pages
```

### Web viewer (`index.html`)

A single-page viewer for the `feeds/` output, deployed to GitHub Pages by `pages.yml` on every push to `main`. Features:

- Category tabs ordered as US, UK, France, Italy, Germany, Sweden, Norway, Hungary, Poland, Spain, Networks, General, using SVG flag icons instead of native emoji.
- An in-place **Translate (EN)** button next to the header for non-English feeds.
- Each article row shows its AI summary underneath the headline when available.

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

| Secret              | Used by              | Description                                |
|---------------------|----------------------|--------------------------------------------|
| `GEMINI_API_KEY`    | Scraper, Reporter    | Google Gemini API key (summaries + report) |
| `ANTHROPIC_API_KEY` | Reporter             | Anthropic API key (for Claude fallback)    |
| `SENDER_EMAIL`      | Reporter             | Gmail address for sending                  |
| `EMAIL_PASSWORD`    | Reporter             | Gmail app password (**no spaces**)         |
| `RECEIVER_EMAIL`    | Reporter             | Destination email address                  |

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

# 2. Enrich with article text + generate Gemini Flash summaries
python article_scraper.py --hours 48

# 3. Generate report (markdown, for testing)
python ai_reporter.py --markdown --hours 48

# Optional: backfill summaries for top N articles per feed
python backfill_summaries.py --per-feed 20
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
| `media-monitor.py`  | `--rebuild`         | Rebuild feed `.txt` files from state only  |
| `backfill_summaries.py` | `--per-feed N`  | Articles per feed (default: 20)            |
| `backfill_summaries.py` | `--category X`  | Backfill a single category                 |

## Constraints and Design Decisions

**Lightweight by design.** The monitor never visits article URLs — it works entirely from RSS metadata. This avoids 403 blocks, rate limiting, and high processing costs. The scraper is a separate, optional enrichment step.

**Graceful degradation.** If the scraper fails or hasn't run, the reporter falls back to titles-only analysis. If all LLM providers fail, the retry and fallback logic exhausts every option before exiting.

**Multi-provider resilience.** The reporter chains Gemini Pro → Claude Sonnet → Gemini Flash. Each gets retries with exponential backoff. Providers whose API keys aren't configured are silently skipped.

**No external databases.** All state is stored in flat JSON files committed to the repository. This keeps the system portable and inspectable.

**Concurrency-safe state.** Both the monitor and the scraper auto-pull from `origin` before reading `monitor_state.json`, so a local run won't clobber writes made in parallel by GitHub Actions.

**UTC everywhere.** All timestamps across the pipeline (monitor, scraper, reporter) are in UTC to keep date-slug routing of enriched files and the reporter's lookback window consistent regardless of where the job runs.

**Sanitization.** LLM output is normalized to strip non-breaking spaces, curly quotes, em dashes, and other Unicode characters that cause email encoding failures.
