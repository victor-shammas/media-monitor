# Transatlantic Right-Wing Media Monitor

An automated research tool that tracks far-right and right-wing political movements across twelve categories spanning multiple countries and transnational networks. The system scrapes headlines from Google News RSS in both native languages and English, enriches them with article text and AI-generated one-sentence summaries, and produces daily intelligence briefs.

## Architecture

The system is a unified pipeline with an optional daily reporting step, orchestrated by GitHub Actions:

```
┌─────────────────────────────────────────────────────────────────┐
│                        GitHub Actions                           │
│                                                                 │
│   Every 2h (pipeline.yml)              05:00 UTC                │
│   ┌──────────────────────────────┐     ┌──────────────────┐    │
│   │ media-monitor.py --enrich    │     │  ai_reporter.py  │    │
│   │                              │     │                  │    │
│   │ 1. Fetch RSS                 │     │  Multi-LLM       │    │
│   │ 2. Resolve URLs              │     │  intelligence    │    │
│   │ 3. Extract article text      │     │  brief           │    │
│   │ 4. Generate AI summaries     │     │                  │    │
│   └──────────┬───────────────────┘     └────────┬─────────┘    │
│              │                                  │              │
│              ▼                                  ▼              │
│   monitor_state.json                       reports/            │
│   feeds/*.txt                              YYYY-MM-DD_HHMM     │
│   data-private/enriched_YYYY-MM-DD.json    _report.md          │
└─────────────────────────────────────────────────────────────────┘
```

### The Pipeline (`media-monitor.py`)

A single script that handles both RSS fetching and article enrichment:

**Fetch phase** (always runs): Scrapes Google News RSS feeds for twelve categories of right-wing political activity. Stores all seen articles in `monitor_state.json` with deduplication per category. Generates human-readable `.txt` files in `feeds/` for browsing.

**Enrichment phase** (with `--enrich`): Takes recent articles from state, decodes Google News redirect URLs using `googlenewsdecoder`, extracts article text via `trafilatura` (truncated to ~300 words), and generates one-sentence AI summaries in batches. Summaries are written back to both `monitor_state.json` and dated enriched JSON files in `data-private/`.

Without `--enrich`, the script only fetches RSS metadata — fast, lightweight, and immune to 403 blocks.

**Feed definitions live in `config.toml`.** Queries, `lang`, `country`, and `variants` are defined there rather than hardcoded in Python.

**Bilingual scraping:** Eight non-English categories define `variants` — each feed is scraped once in the native language and once in English. The English variant uses `country: "US"` since the US Google News index surfaces the broadest international English coverage. Some feeds use per-variant query overrides to drop ambiguous acronyms from the English variant.

**Categories tracked:**

| ID         | Label                                    | Languages |
|------------|------------------------------------------|-----------|
| `usa`      | MAGA / Trump                             | en        |
| `norway`   | Fremskrittspartiet                       | no + en   |
| `sweden`   | Sverigedemokraterna                      | sv + en   |
| `france`   | Rassemblement National                   | fr + en   |
| `italy`    | Fratelli d'Italia / Lega                 | it + en   |
| `germany`  | Alternative fur Deutschland              | de + en   |
| `uk`       | Reform UK                                | en        |
| `hungary`  | Fidesz / Tisza                           | hu + en   |
| `poland`   | Prawo i Sprawiedliwosc                   | pl + en   |
| `spain`    | Vox                                      | es + en   |
| `general`  | General Right-Wing News                  | en        |
| `networks` | Transnational Networks                   | en        |

### AI Reporter (`ai_reporter.py`)

Runs daily at 05:00 UTC. Loads enriched files covering the lookback window, filters articles by publication date, and sends compiled data to the LLM with structured prompt instructions from `config.toml`. If no enriched files exist, falls back to titles-only from `monitor_state.json`.

The reporter assigns sequential reference numbers to articles before sending to the LLM. The prompt instructs the model to cite sources inline, and the script replaces each `[N]` with a clickable link post-hoc. This avoids LLM hallucination of URLs.

**Output modes:**
- Default: styled HTML email via Gmail SMTP
- `--markdown`: writes to `reports/`

### Backfill Utilities

- `backfill_summaries.py` — generates summaries for the top N articles per feed that don't yet have one, reusing existing extracts where possible.
- `backfill_enriched_summaries.py` — generates summaries for all unsummarized articles in a specific enriched file.

## File Structure

```
media-monitor/
├── .github/workflows/
│   ├── pipeline.yml            # Unified RSS + enrichment pipeline, every 2h
│   ├── daily_report.yml        # AI intelligence brief, daily 05:00 UTC
│   ├── backfill.yml            # Manual backfill of AI summaries
│   └── pages.yml               # GitHub Pages deploy for index.html
├── media-monitor.py            # Unified pipeline: RSS fetch + optional enrichment
├── article_scraper.py          # Enrichment library (URL resolution, extraction, summaries)
├── monitor_utils.py            # Shared utilities (state, blocklist, git sync)
├── ai_reporter.py              # Multi-LLM analysis + email
├── backfill_summaries.py       # One-off: fill in summaries for existing articles
├── backfill_enriched_summaries.py  # One-off: summarize a specific enriched file
├── config.toml                 # Category labels, feed definitions, AI prompt
├── blocklist.json              # Blocked URLs, sources, and title patterns
├── monitor_state.json          # Persistent article database (auto-generated)
├── feeds/                      # Human-readable .txt files per category
├── data-private/               # Dated enriched JSON files (separate private repo)
├── reports/                    # Markdown reports and prompt inputs
└── index.html                  # Single-page web viewer, served via GitHub Pages
```

### Web viewer (`index.html`)

A single-page viewer for the `feeds/` output, deployed to GitHub Pages on every push to `main`. Features category tabs with flag icons, an in-place translate button for non-English feeds, and AI summary display.

## Data Flow

```
Google News RSS
    │
    ▼
monitor_state.json ──────────────────────┐
    │                                    │
    ▼                                    ▼
data-private/enriched_YYYY-MM-DD.json  [fallback if no
    │                                   enriched file]
    ▼                                    │
ai_reporter.py ◀─────────────────────────┘
    │
    ├──▶ reports/*.md           (--markdown mode)
    └──▶ HTML email via Gmail   (production mode)
```

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

### Run the pipeline locally

```bash
# Full pipeline: fetch RSS + enrich + summarize
python media-monitor.py --enrich

# Or use the convenience script (pulls, runs, commits, pushes)
./run.sh

# RSS fetch only (no enrichment)
python media-monitor.py

# Rebuild feed .txt files from state
python media-monitor.py --rebuild

# Run enrichment standalone
python article_scraper.py --hours 48

# Generate report (markdown, for testing)
python ai_reporter.py --markdown --hours 48

# Backfill summaries
python backfill_summaries.py --per-feed 20
python backfill_enriched_summaries.py data-private/enriched_2026-04-17.json
```

### Useful flags

| Script               | Flag                  | Effect                                     |
|----------------------|-----------------------|--------------------------------------------|
| `media-monitor.py`   | `--enrich`            | Run full pipeline (fetch + enrich)         |
| `media-monitor.py`   | `--fetch-only`        | RSS fetch only (default behavior)          |
| `media-monitor.py`   | `--enrich-hours N`    | Look-back window for enrichment            |
| `media-monitor.py`   | `--feeds usa norway`  | Run only specific feeds                    |
| `media-monitor.py`   | `--rebuild`           | Rebuild feed `.txt` files from state       |
| `article_scraper.py` | `--category norway`   | Scrape only one category                   |
| `article_scraper.py` | `--hours N`           | Look-back window                           |
| `ai_reporter.py`     | `--markdown`          | Write to `reports/` instead of emailing    |
| `ai_reporter.py`     | `--model auto`        | Full fallback chain (default)              |
| `ai_reporter.py`     | `--hours N`           | Look-back window in hours                  |
| `ai_reporter.py`     | `--no-enriched`       | Force titles-only even if enriched exists  |

## Design Decisions

**Unified pipeline.** RSS fetching and enrichment run as a single script (`media-monitor.py --enrich`) to eliminate race conditions between separate workflows writing to `monitor_state.json`.

**Lightweight by default.** Without `--enrich`, the monitor only reads RSS metadata. This keeps the fast path free of external dependencies like `trafilatura`.

**Graceful degradation.** If enrichment fails or hasn't run, the reporter falls back to titles-only analysis. If all LLM providers fail, retries and fallback logic exhaust every option before exiting.

**No external databases.** All state is stored in flat JSON files committed to the repository.

**Language-grouped batching.** Articles are sorted by language before being sent to the LLM for summarization, preventing cross-language contamination in batches.

**UTC everywhere.** All timestamps are UTC to keep date-slug routing and lookback windows consistent regardless of where the job runs.
