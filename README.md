# IPO Bot — India IPO & GMP Telegram Notifier

Monitors newly announced Indian IPOs (Mainboard + SME) and Grey Market
Premium (GMP) changes, and sends instant Telegram alerts for:

1. A new IPO being announced
2. GMP changing significantly (≥ ₹5 **or** ≥ 5%, configurable)
3. Subscription opening
4. Subscription closing
5. Allotment day
6. Listing day

Plus optional morning (8 AM) and evening (8 PM) summaries of all active IPOs.

## Project structure

```
ipo_bot/
├── main.py            # Entry point — starts the scheduler, runs 24/7
├── scraper.py          # Scrapes Mainboard/SME IPO listings (Chittorgarh)
├── gmp.py               # Scrapes GMP data (InvestorGain, falls back to IPO Watch)
├── telegram.py         # Telegram Bot API notifier + message formatting
├── database.py         # SQLite layer: ipo / gmp_history / notifications_sent
├── config.py            # Loads & validates settings from .env
├── scheduler.py         # APScheduler jobs + change-detection business logic
├── utils.py              # Logging, retry decorator, HTML/date/number parsing
├── export_csv.py       # Bonus: export IPOs + GMP history to CSV
├── requirements.txt
├── .env.example
├── Dockerfile / docker-compose.yml / .dockerignore   # Bonus: Docker support
├── logs/                # app.log (rotating)
└── database/            # ipo_bot.db (SQLite)
```

## How it works

Every `POLL_INTERVAL_MINUTES` (default **10**), the bot:

1. Scrapes the latest Mainboard + SME IPO lists from Chittorgarh.
2. Scrapes the latest GMP table from InvestorGain (falls back to IPO Watch
   if that fails).
3. Upserts each IPO into the `ipo` table. A row that didn't exist before
   triggers a **🚀 NEW IPO DETECTED** alert.
4. Matches each IPO to its GMP quote (fuzzy name matching, since sources
   spell company names slightly differently) and compares it to the
   previously stored value. If it moved by at least `GMP_ABS_THRESHOLD`
   rupees **or** `GMP_PCT_THRESHOLD` percent, sends a **📈 GMP UPDATED**
   alert and logs the reading to `gmp_history`.
5. Checks every IPO's open/close/allotment/listing dates against today and
   sends the matching one-time milestone alert (🟢/🔴/🎯/📊).

The `notifications_sent` table records every alert that's gone out so nothing
is ever sent twice, even across restarts.

### Data sources & a note on reliability

IPO tracker sites (Chittorgarh, InvestorGain, IPO Watch, NSE, BSE) publish
their data as plain HTML tables but redesign their markup periodically, and
some sit behind anti-bot protection (Cloudflare etc.) that can outright
block naive scrapers. To keep this bot working as long as possible without
constant maintenance, `scraper.py` and `gmp.py` **do not** hardcode brittle
CSS selectors. Instead they:

- Parse every `<table>` on the page.
- Score each table against expected column-header keywords (e.g. "ipo",
  "gmp", "open", "close") and pick the best match.
- Map columns to internal fields by fuzzy keyword matching rather than
  fixed positions.

If a source changes so drastically that no table scores a match, the bot
logs a warning and skips that source for the cycle rather than crashing —
check `logs/app.log` for `"Could not identify the"` warnings, and update the
keyword lists near the top of `scraper.py` / `gmp.py` (or the URLs in
`.env`) if a source goes stale long-term. If a source starts returning
HTTP 403, it means that site is actively blocking automated requests; you
may need to source a different URL/mirror or add cookies/proxies yourself.

## Installation

Requires **Python 3.12+**.

```bash
git clone <this repo>
cd ipo_bot
python -m venv venv

# Windows
venv\Scripts\activate
# Linux / macOS
source venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # Windows: copy .env.example .env
```

Edit `.env` and fill in `BOT_TOKEN` and `CHAT_ID` (see below).

## Creating a Telegram bot (BOT_TOKEN)

1. Open Telegram and message **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot` and follow the prompts (choose a name, then a username
   ending in `bot`).
3. BotFather replies with your bot token, e.g.
   `123456789:AAExampleTokenXXXXXXXXXXXXXXXXXXXXX`.
4. Paste it into `.env` as `BOT_TOKEN=...`.
5. Send your new bot **any message** (e.g. `/start`) so it's allowed to
   message you back — Telegram bots can't message users who haven't
   started a conversation with them first.

## Getting your Chat ID (CHAT_ID)

Easiest option:

1. Message **[@userinfobot](https://t.me/userinfobot)** on Telegram — it
   replies with your numeric user ID. Use that as `CHAT_ID`.

Alternative (works for groups/channels too):

1. Add your bot to the target group/channel (or just message it directly).
2. Send any message there.
3. Visit `https://api.telegram.org/bot<BOT_TOKEN>/getUpdates` in a browser.
4. Find `"chat":{"id": ...}` in the JSON response — that number is your
   `CHAT_ID` (group/channel IDs are usually negative, that's expected).

## Running

```bash
python main.py
```

On startup it validates your `.env`, initializes the SQLite database at
`database/ipo_bot.db`, runs an immediate poll cycle, and then keeps polling
every `POLL_INTERVAL_MINUTES`. Press `Ctrl+C` to stop (it shuts down the
scheduler cleanly).

Logs are written to `logs/app.log` (rotating, 5 MB × 5 backups) and to the
console.

### Running continuously

**Free 24/7 hosting: Oracle Cloud Always Free VM** (no PC required to stay on):

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (a card is required for
   identity verification but the "Always Free" shapes are never charged).
2. Create an instance: *Compute → Instances → Create Instance*.
   - Image: **Canonical Ubuntu 22.04** (or 24.04).
   - Shape: pick one under **Always Free eligible** (e.g. `VM.Standard.E2.1.Micro`,
     or an Ampere `VM.Standard.A1.Flex` with 1 OCPU / 6 GB RAM if available in
     your region — both are free forever).
   - Under *Add SSH keys*, either upload your own public key or let Oracle
     generate one and download the private key — you'll need it to log in.
3. Once it's running, note its **public IP**, then open the firewall for
   outbound HTTPS (it's open by default; you don't need to open any inbound
   port since this bot only makes outbound calls to Telegram/IPO sites).
4. Connect and set up:

   ```bash
   ssh -i /path/to/your/private_key.pem ubuntu@<PUBLIC_IP>
   sudo apt update && sudo apt install -y python3 python3-venv python3-pip
   ```

5. Get your code onto the VM (pick one):

   ```bash
   # Option A: if you push this project to a git repo
   git clone <your-repo-url> ipo_bot && cd ipo_bot

   # Option B: copy directly from your PC before shutting it down
   # (run this from your PC, not the VM):
   scp -i /path/to/your/private_key.pem -r "c:/BOT IPO/ipo_bot" ubuntu@<PUBLIC_IP>:~/ipo_bot
   ```

6. On the VM:

   ```bash
   cd ipo_bot
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   # your .env (with BOT_TOKEN/CHAT_ID) should already be inside ipo_bot/ from the copy above
   ```

7. Follow the **systemd** steps below so it survives reboots and restarts
   automatically on failure — that's what makes it truly 24/7.

**Linux (systemd)** — create `/etc/systemd/system/ipo-bot.service`:

```ini
[Unit]
Description=IPO Bot
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/path/to/ipo_bot
ExecStart=/path/to/ipo_bot/venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ipo-bot
journalctl -u ipo-bot -f   # view logs
```

**Linux/macOS (simple, no systemd)** — using `nohup`:

```bash
nohup venv/bin/python main.py >> logs/nohup.out 2>&1 &
```

Or under `tmux`/`screen` so it survives SSH disconnects.

**Windows** — Task Scheduler:

1. Open Task Scheduler → *Create Task*.
2. General tab: check "Run whether user is logged on or not".
3. Triggers tab: "At startup" (or "At log on").
4. Actions tab: Action = "Start a program",
   Program = `C:\path\to\ipo_bot\venv\Scripts\python.exe`,
   Arguments = `main.py`,
   Start in = `C:\path\to\ipo_bot`.
5. Settings tab: enable "If the task fails, restart every" for resilience.

Or simply keep a terminal window open running `python main.py` — for a
quick always-on setup, `pythonw main.py` avoids a visible console window.

**Raspberry Pi (systemd, same as Linux above)**:

```bash
sudo apt update && sudo apt install -y python3 python3-venv
git clone <this repo> && cd ipo_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
```

Then follow the systemd steps above (a Pi 3/4/5 running Raspberry Pi OS is
plenty for this workload).

**Docker (any platform)**:

```bash
cp .env.example .env   # fill in BOT_TOKEN / CHAT_ID
docker compose up -d --build
docker compose logs -f
```

**Free 24/7 hosting with no credit card: GitHub Actions**

Every option above needs a machine that's always on. If you don't have one
and don't want to pay for a VPS or verify a card with a cloud provider, a
GitHub Actions scheduled workflow can run this bot for free, forever, using
only a GitHub account (email signup, no card, no personal details beyond
that). This repo already includes everything needed:

- [run_once.py](run_once.py) — runs exactly one poll cycle and exits
  (instead of `main.py`'s persistent 24/7 loop), because GitHub Actions
  spins up a fresh disposable machine per run rather than keeping one alive.
- [.github/workflows/ipo-bot.yml](.github/workflows/ipo-bot.yml) — triggers
  that script every 10 minutes and commits the updated
  `database/ipo_bot.db` back into the repo, since that's the only way state
  survives between runs on a disposable runner.

Setup:

1. Create a **public** GitHub repository (public = unlimited free Actions
   minutes; private repos only get ~2,000 free minutes/month, which a
   10-minute cadence will exceed). There's nothing sensitive in the code —
   your token/chat id are never committed, only stored as encrypted secrets
   (step 3).
2. Push this project to it:

   ```bash
   cd ipo_bot
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

3. On GitHub: *Settings → Secrets and variables → Actions → New repository
   secret*. Add two secrets: `BOT_TOKEN` and `CHAT_ID` (same values as your
   local `.env` — never commit `.env` itself, it's already gitignored).
4. Go to the *Actions* tab and enable workflows if prompted. The workflow
   runs automatically on its schedule, or trigger it immediately via
   *Actions → IPO Bot Poll Cycle → Run workflow*.

Trade-offs vs. a persistent host (`main.py`): GitHub's cron schedule can
jitter by a few minutes under load, so alerts may occasionally arrive a
little later than `POLL_INTERVAL_MINUTES` implies; and because
`database/ipo_bot.db` gets committed every run, the repo's git history
grows slowly over time (harmless for a hobby project; if it matters to you
later, periodically squash history or graduate to a VPS/Pi).

## Configuration reference (`.env`)

| Variable | Default | Description |
|---|---|---|
| `BOT_TOKEN` | *(required)* | Telegram bot token from BotFather |
| `CHAT_ID` | *(required)* | Telegram chat/user/group id to notify |
| `POLL_INTERVAL_MINUTES` | `10` | How often to re-check IPOs & GMP |
| `RUN_IMMEDIATELY_ON_START` | `true` | Run one cycle immediately at startup |
| `MORNING_SUMMARY_TIME` | `08:00` | 24h HH:MM for the morning summary |
| `EVENING_SUMMARY_TIME` | `20:00` | 24h HH:MM for the evening summary |
| `ENABLE_MORNING_SUMMARY` / `ENABLE_EVENING_SUMMARY` | `true` | Toggle summaries |
| `TIMEZONE` | `Asia/Kolkata` | Timezone used for scheduling |
| `GMP_ABS_THRESHOLD` | `5` | Minimum ₹ change to trigger a GMP alert |
| `GMP_PCT_THRESHOLD` | `5` | Minimum % change to trigger a GMP alert |
| `REQUEST_TIMEOUT` | `20` | HTTP timeout (seconds) per request |
| `MAX_RETRIES` | `3` | Retry attempts for failed HTTP requests |
| `RETRY_BACKOFF_SECONDS` | `3` | Base delay between retries (grows linearly) |
| `DB_PATH` / `LOG_DIR` / `LOG_FILE` / `LOG_LEVEL` / `CSV_EXPORT_DIR` | see `.env.example` | Paths & log verbosity |
| `CHITTORGARH_MAINBOARD_URL` / `CHITTORGARH_SME_URL` / `INVESTORGAIN_GMP_URL` / `IPOWATCH_GMP_URL` | see `.env.example` | Override if a source moves |

## Database schema

- **`ipo`** — one row per IPO with its latest known snapshot (name, type,
  price band, lot size, issue size, all four dates, registrar, exchange,
  current GMP, GMP change, kostak, subject-to-sauda, source URL, last
  updated timestamp).
- **`gmp_history`** — append-only log of every GMP reading (`ipo_id`, `gmp`,
  `kostak`, `subject_to_sauda`, `recorded_at`) — powers CSV export / your
  own charting.
- **`notifications_sent`** — append-only log of every alert sent (`ipo_id`,
  `notification_type`, `details`, `sent_at`) — the de-duplication guard.

## Bonus: CSV export & GMP history

```bash
python export_csv.py                          # exports/ipos.csv + exports/gmp_history.csv
python export_csv.py --ipo-name "ABC Limited"  # only that IPO's GMP history
```

Open `gmp_history.csv` in Excel/pandas/matplotlib to chart an IPO's GMP over
time — every reading the bot has ever seen is in there.

## Error handling

- All HTTP requests go through a shared retry wrapper (`utils.retry`) with
  linear backoff, covering timeouts, connection errors, and transient
  5xx/429 responses. Telegram sends specifically respect `Retry-After` on
  429s.
- A scraping failure on one source (timeout, block, markup change) is
  logged and skipped — it never crashes the poll cycle or takes down the
  other source.
- Any unexpected exception while processing a single IPO is caught and
  logged per-IPO, so one bad row can't stop the rest of the cycle.

## Extending further

The architecture already supports layering on: a Flask web dashboard (read
from `Database.as_dict_list()` / `get_gmp_history()`), email/Discord/WhatsApp
alert channels (mirror `TelegramNotifier`'s `notify_*` methods), a REST API
(wrap `Database` in a thin FastAPI/Flask app), and a GitHub Actions
scheduled workflow (run `python main.py` as a one-shot cycle via a script
entry point) — these weren't built in to keep the delivered bot's
dependency footprint and moving parts minimal, but the modular design
(scraper / gmp / database / telegram all decoupled) makes each a
self-contained addition.
