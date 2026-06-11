<div align="center">

<img src="https://readme-typing-svg.demolab.com?font=Orbitron&weight=900&size=48&duration=3000&pause=1000&color=00BCD4&center=true&vCenter=true&width=700&height=80&lines=SANDI+RIDWAN" alt="Sandi Ridwan" />

<img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mono&weight=700&size=16&duration=2500&pause=800&color=00E5FF&center=true&vCenter=true&multiline=true&width=860&height=50&lines=Data+Aggregation+Agent+%7C+7+Sources+%E2%86%92+Groq+AI+%E2%86%92+PostgreSQL+%E2%86%92+Airtable+%E2%86%92+Email" alt="Tagline" />

<br/>

![Python](https://img.shields.io/badge/Python-3.12-00BCD4?style=for-the-badge&logo=python&logoColor=white)
![Groq](https://img.shields.io/badge/Groq_AI-LLaMA_3.3_70B-00E5FF?style=for-the-badge&logo=groq&logoColor=black)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Railway-336791?style=for-the-badge&logo=postgresql&logoColor=white)
![Airtable](https://img.shields.io/badge/Airtable-3_Table_Routing-18BFFF?style=for-the-badge&logo=airtable&logoColor=white)
![SendGrid](https://img.shields.io/badge/SendGrid-Email_Digest-1A82E2?style=for-the-badge&logo=sendgrid&logoColor=white)
![Railway](https://img.shields.io/badge/Railway-24%2F7_Live-0B0D0E?style=for-the-badge&logo=railway&logoColor=white)
![Sentry](https://img.shields.io/badge/Sentry-Monitored-362D59?style=for-the-badge&logo=sentry&logoColor=white)
![Tests](https://img.shields.io/badge/Tests-302_Passed-00C853?style=for-the-badge&logo=pytest&logoColor=white)

</div>

---

```
╔══════════════════════════════════════════════════════════════════════════╗
║                                                                          ║
║    ██████╗  █████╗ ████████╗ █████╗      █████╗  ██████╗  ███████╗      ║
║    ██╔══██╗██╔══██╗╚══██╔══╝██╔══██╗    ██╔══██╗██╔════╝ ██╔════╝      ║
║    ██║  ██║███████║   ██║   ███████║    ███████║██║  ███╗ ███████╗      ║
║    ██║  ██║██╔══██║   ██║   ██╔══██║    ██╔══██║██║   ██║ ╚════██║      ║
║    ██████╔╝██║  ██║   ██║   ██║  ██║    ██║  ██║╚██████╔╝ ███████║      ║
║    ╚═════╝ ╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝    ╚═╝  ╚═╝ ╚═════╝  ╚══════╝      ║
║                                                                          ║
║      A G G R E G A T I O N   A G E N T  ·  AI-Powered  ·  24/7         ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

## 🎬 Demo

<div align="center">

<a href="https://youtu.be/-jBGvlEubUU">
  <img src="Thumbnail.png" width="860" alt="Demo Video" />
</a>

<br/><br/>

<!-- Upload GIF via GitHub Issue trick → paste link here -->
<div align="center">
  <video src="https://github.com/user-attachments/assets/330888c1-bb9a-4492-80c5-1de0200a4dc3" 
         width="860" 
         controls 
         autoplay 
         loop 
         muted>
  </video>
</div>

---

## 🧠 Overview

**Data Aggregation Agent** is a production-grade, AI-powered data pipeline running 24/7 on Railway. It autonomously scrapes **7 heterogeneous source types**, scores every record using **Groq LLaMA 3.3 70B**, routes results into a 3-tier Airtable system, persists to **PostgreSQL** with full deduplication, and delivers a formatted HTML email digest via **SendGrid** — every day at 08:00 UTC, zero human intervention.

<div align="center">

| Metric | Value |
|-------:|:------|
| 🔌 Sources | 7 scraper types (RSS · REST API · Authenticated · Playwright JS · Sitemap · PDF · Static HTML) |
| 🤖 AI Model | Groq LLaMA 3.3 70B via `llama-3.3-70b-versatile` |
| 📊 Records/Run | ~61 records · ~33,500 tokens/day (Groq free tier: 100K/day) |
| 🗄️ Storage | PostgreSQL on Railway · SQLAlchemy ORM · UPSERT dedup |
| 📋 Routing | Airtable 3-tier: High Fit (≥7.0) · Medium Fit (4–6.9) · Low Fit (<4.0) |
| 📧 Digest | HTML + plaintext via SendGrid API (port 443 — Railway-compatible) |
| ⏰ Schedule | Daily @ 08:00 UTC · APScheduler CronTrigger |
| 🧹 Auto-clean | Ghost row cleaner runs after every sync |
| 🔍 Monitoring | Sentry SDK + Railway logs |
| ✅ Test Suite | **302 passed · 2 skipped · 0 failed** |

</div>

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                     RAILWAY WORKER PROCESS                           │
│                   APScheduler · Daily 08:00 UTC                      │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 1 — SCRAPING  (8 scrapers, isolated error handling)            │
│                                                                      │
│  StaticHTML   Authenticated   PlaywrightJS   RestAPI (HTTP/2+ETag)   │
│  RssFeed      Sitemap         PdfSource      AdvancedAuth            │
│                                                                      │
│                        ▼ ~61 records / run                           │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 2 — GROQ AI SCORING                                            │
│  score_batch() · retry on 429 · fallback LOW (never drop records)    │
│  Output: total_score (0–10) + fit_tier + reasoning                   │
└──────────────────────┬───────────────────────────────────────────────┘
                       │
          ┌────────────┴─────────────┐
          ▼                          ▼
┌─────────────────┐       ┌──────────────────────────────────────────┐
│  STEP 3: DB     │       │  STEP 4: AIRTABLE ROUTING                │
│  PostgreSQL     │       │  score ≥ 7.0 → 🟢 High Fit              │
│  SQLAlchemy ORM │       │  score 4–6.9 → 🟡 Medium Fit            │
│  UPSERT dedup   │       │  score < 4.0 → 🔴 Low Fit               │
└─────────────────┘       │  Upsert by Source ID · 0 duplicates ever │
                          └──────────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 4b — AIRTABLE GHOST ROW CLEANER                                │
│  Removes empty rows after every sync · runs automatically            │
└──────────────────────────────┬───────────────────────────────────────┘
                               │
                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STEP 5 — SENDGRID EMAIL DIGEST                                      │
│  HTML + plaintext · Tier color-coding · HTTP 202 confirmed           │
│  🟢 High Fit #16a34a · 🟡 Medium #d97706 · 🔴 Low #dc2626           │
└──────────────────────────────────────────────────────────────────────┘
```

---

## ⚡ Technical Challenges Solved

### Challenge 1 — 7-Type Scraper Architecture (One Codebase, Zero Cross-Contamination)

**Problem:** Client needed 7 fundamentally different scraper types in a single pipeline. A failure in one scraper must not crash the others.

**Solution:** `BaseScraper` abstract class with `_fetch_records()` and `field_map()` as abstract methods. Returns `ScrapeResult` object (not raw list). Pipeline runner uses `hasattr(result, "records")` check and catches exceptions per-scraper.

```python
class BaseScraper(ABC):
    @abstractmethod
    def _fetch_records(self) -> list[dict]: ...

    def scrape(self) -> ScrapeResult:
        try:
            raw = self._fetch_records()
            return ScrapeResult(records=self._normalize(raw))
        except Exception as exc:
            self.logger.error("❌ %s failed: %s", self.__class__.__name__, exc)
            return ScrapeResult(records=[], error=str(exc))

# Pipeline runner — safe extraction regardless of return type
result = scraper.scrape()
records = result.records if hasattr(result, "records") else list(result)
```

---

### Challenge 2 — Railway SMTP Blocked (All Ports)

**Problem:** Railway free tier blocks ALL outbound SMTP ports (587 AND 465) at the network level. Changing ports doesn't help.

**Solution:** Replace SMTP with SendGrid HTTP API (port 443 — never blocked). Priority: SendGrid if `SENDGRID_API_KEY` set, SMTP fallback for local dev only.

```python
def send_digest(payload, ...):
    if sendgrid_key := os.getenv("SENDGRID_API_KEY", ""):
        # SendGrid HTTP API — port 443, Railway-compatible
        message = Mail(from_email=from_email, to_emails=recipients, subject=subject)
        message.add_content(Content("text/html", build_html(payload)))
        response = SendGridAPIClient(api_key=sendgrid_key).send(message)
        # HTTP 202 = queued for delivery ✅
    else:
        # SMTP fallback — local dev only
        _send_via_smtp(payload, recipients, subject, smtp_config)
```

---

### Challenge 3 — Airtable Ghost Row Cleaner

**Problem:** Airtable creates 3 empty default rows on every new table. Previous failed-scoring runs also left rows with no Source ID. These accumulate silently — 203 ghost rows discovered on first automated cleanup.

**Solution:** `clean_empty_rows()` runs after every sync. Identifies empty rows via `Source ID` field check, deletes one by one (Airtable API has no bulk delete).

```python
def clean_empty_rows(client, table_names) -> dict[str, int]:
    for table_name in table_names:
        all_records = client.list_records(table_name)
        empty = [r for r in all_records
                 if not str(r.get("fields", {}).get("Source ID", "")).strip()]
        for rec in empty:
            client.delete_record(table_name, rec["id"])
    # Result: 203 rows removed on first run, 0 on every subsequent run
```

---

### Challenge 4 — Groq Rate Limit (TPD-Aware Graceful Fallback)

**Problem:** Groq free tier: 100,000 tokens/day. Scoring 61 records ≈ 33,500 tokens/run. Sequential requests trigger per-minute 429 errors frequently.

**Solution:** Groq SDK handles retry automatically. Failed records (after 3 retries) fall back to `fit_tier=LOW, score=0.0` — never dropped from pipeline. PipelineStats tracks every metric per run.

```python
def score_batch(records):
    for rec in records:
        try:
            scored = score_record(rec)   # Groq SDK auto-retries 429
        except Exception:
            rec["fit_tier"] = "LOW"      # Graceful fallback
            rec["total_score"] = 0.0     # Never drop a record
            scored = rec
        results.append(scored)
    return results
```

---

### Challenge 5 — SQLAlchemy Dual-Mode DB (Railway + Local)

**Problem:** Tests need SQLite in-memory (no PostgreSQL required). Production needs PostgreSQL on Railway. Both must use the same ORM code.

**Solution:** `DATABASE_URL` env var auto-switches mode. SQLite in-memory for tests with transactional fixtures. `os.makedirs()` before `logging.basicConfig()` — Railway container has no `logs/` directory.

```python
# storage.py — dual-mode auto-detection
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///:memory:")
if "railway" in DATABASE_URL:
    logger.info("Using Railway DATABASE_URL")
engine = create_engine(DATABASE_URL)

# main.py — order matters in cloud deploy
os.makedirs("logs", exist_ok=True)          # BEFORE logging.basicConfig()
logging.basicConfig(handlers=[FileHandler("logs/pipeline.log")])
```

---

## 📁 File Structure

```
data-aggregation-agent/
├── main.py                              # Pipeline runner + APScheduler cron
├── Procfile                             # worker: python main.py
├── railway.json                         # startCommand + restart policy
├── requirements.txt                     # All dependencies pinned
├── env.example                          # Environment variable reference
│
├── src/
│   ├── scoring.py                       # Groq scoring — score_batch() + score_record()
│   ├── storage.py                       # SQLAlchemy ORM — init_db() + upsert_records()
│   ├── airtable_sync.py                 # RoutingEngine + AirtableClient + clean_empty_rows()
│   ├── digest.py                        # SendGrid (primary) + SMTP fallback
│   ├── pdf_extractor.py                 # pdfplumber + PyMuPDF two-pass
│   └── scrapers/
│       ├── base.py                      # BaseScraper abstract class
│       ├── source_static_example.py     # StaticHtmlSourceScraper
│       ├── source_authenticated.py      # SessionManager + AuthenticatedSourceScraper
│       ├── source_02_playwright_js.py   # PlaywrightJsScraper
│       ├── source_03_rest_api.py        # RestApiScraper (HTTP/2 + ETag + cursor pagination)
│       ├── source_04_rss_feed.py        # RssFeedScraper (multi-feed + cross-feed dedup)
│       ├── source_05_sitemap.py         # SitemapScraper
│       ├── source_06_pdf_source.py      # PdfSourceScraper
│       └── source_07_authenticated_advanced.py  # AdvancedAuthScraper
│
└── tests/
    ├── conftest.py                      # SQLite in-memory fixtures
    ├── test_scoring.py
    ├── test_scrapers.py
    ├── test_sources.py
    ├── test_airtable.py
    ├── test_digest.py
    ├── test_storage.py
    └── test_main.py
```

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/SandiRidwan/data-aggregation-agent.git
cd data-aggregation-agent
python -m venv venv && source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp env.example .env
```

```env
GROQ_API_KEY=gsk_xxxxxxxxxxxx
AIRTABLE_TOKEN=patXXXXXXXXXXXXXX
AIRTABLE_BASE_ID=appXXXXXXXXXXXXXX
AIRTABLE_TABLE_A=High Fit
AIRTABLE_TABLE_B=Medium Fit
AIRTABLE_TABLE_C=Low Fit
DATABASE_URL=postgresql://user:pass@localhost:5432/aggregator
SENDGRID_API_KEY=SG.xxxxxxxxxxxxxxxx
SENDGRID_FROM=your_verified@email.com
DIGEST_TO=recipient@email.com
DRY_RUN=false
RUN_ONCE=false
CRON_HOUR=8
CRON_MINUTE=0
SENTRY_DSN=https://xxx@sentry.io/xxx
```

### 3. Run

```bash
# Safe test mode (no real API calls)
DRY_RUN=true GROQ_API_KEY=dummy DATABASE_URL="sqlite:///:memory:" python main.py

# Run once (real APIs)
RUN_ONCE=true python main.py

# Run test suite
python -m pytest tests/ -v
```

### 4. Deploy to Railway

```bash
git push origin main
# Railway → New Project → Deploy from GitHub
# → Add PostgreSQL service
# → Worker Variables → + Add Variable Reference → DATABASE_URL
# → Add all env vars including SENDGRID_API_KEY
# → RUN_ONCE=false for scheduled mode
```

---

## 📊 Live Pipeline Results

```
Pipeline run — 10 Jun 2026  (run_20260610_080000)

✅ RssFeedScraper     →  61 records  (BBC Tech · TechCrunch · Hacker News)
──────────────────────────────────────────────────
   Scraped total: 61 records from 8 sources

✅ Scored  61/61  |  HIGH: 60  MEDIUM: 1  LOW: 0
✅ DB upsert        61 new, 0 updated
✅ Airtable sync    45 created, 16 updated, 0 errors
✅ Airtable clean   all tables already clean (0 ghost rows)
✅ Digest sent      SendGrid → thevoster36@gmail.com — HTTP 202
──────────────────────────────────────────────────
   Pipeline done in 251.5s  ·  errors=0
   Next run: 2026-06-11 08:00:00 UTC
```

---

## 🧪 Test Suite

```
$ python -m pytest tests/ -v

302 passed · 2 skipped · 0 failed ✅
(2 skipped = golden examples requiring real Groq API key)
```

---

## 🛠️ Tech Stack

<div align="center">

| Layer | Technology |
|-------|------------|
| **Language** | Python 3.12 |
| **AI / Scoring** | Groq API · LLaMA 3.3 70B |
| **Scraping** | requests · httpx[http2] · BeautifulSoup4 · feedparser · playwright · curl_cffi |
| **PDF** | pdfplumber (primary) · PyMuPDF/fitz (fallback) |
| **Database** | PostgreSQL · SQLAlchemy ORM · psycopg2-binary |
| **Airtable** | pyairtable · score-based routing · upsert + ghost row cleaner |
| **Email** | SendGrid API (primary) · smtplib SMTP (fallback) |
| **Scheduling** | APScheduler · BlockingScheduler · CronTrigger |
| **Deployment** | Railway.app · Nixpacks · GitHub auto-deploy |
| **Monitoring** | Sentry SDK · Railway logs |
| **Testing** | pytest · pytest-mock · pytest-cov · SQLite in-memory |

</div>

---

## 👤 Author

<div align="center">

<img src="https://readme-typing-svg.demolab.com?font=Orbitron&weight=700&size=20&duration=3000&pause=1000&color=00BCD4&center=true&vCenter=true&width=400&lines=Sandi+Ridwan" />

**Data Automation Engineer · Web Scraping Specialist · AI Automation Builder**

📍 Palu, Central Sulawesi, Indonesia

[![Upwork](https://img.shields.io/badge/Upwork-Hire_Me-00BCD4?style=for-the-badge&logo=upwork&logoColor=white)](https://www.upwork.com/freelancers/sandiridwan)
[![LinkedIn](https://img.shields.io/badge/LinkedIn-Connect-0077B5?style=for-the-badge&logo=linkedin&logoColor=white)](https://linkedin.com/in/sandi-ridwan)
[![GitHub](https://img.shields.io/badge/GitHub-Follow-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/SandiRidwan)

</div>

---

<div align="center">
<img src="https://readme-typing-svg.demolab.com?font=JetBrains+Mono&size=12&duration=4000&pause=1000&color=00BCD4&center=true&vCenter=true&width=700&lines=302+tests+%E2%9C%85+%7C+7+scrapers+%7C+Groq+AI+%7C+SendGrid+%7C+Railway+24%2F7+%7C+errors%3D0" />
</div>
