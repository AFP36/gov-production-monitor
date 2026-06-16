# Government Production Contract Monitor — Setup Guide

## Quick Start (under 15 minutes)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get your SAM.gov API key (free)
1. Go to https://sam.gov/profile/details
2. Log in (create a free account if needed)
3. Click "Public API Key" — copy the key
4. Paste it into `production_config.yaml` under `api_keys.sam_gov`

### 3. Configure email alerts
In `production_config.yaml`, under `alerts.email`:
- Set `sender` to your Gmail address
- Create a Gmail App Password:
  - Google Account → Security → 2-Step Verification → App Passwords
  - Generate one for "Mail" and paste it as `password`
- Add your email to `recipients`

### 4. Test it (dry run — no alerts sent)
```bash
python production_monitor.py --test
```

### 5. Run it live
```bash
python production_monitor.py                  # fetch everything
python production_monitor.py --mode opps      # active bids only
python production_monitor.py --mode awards    # competitive intel only
```

---

## Schedule it to run automatically

### Mac/Linux (cron)
Open crontab:
```bash
crontab -e
```

Add this line to run every 4 hours:
```
0 */4 * * * cd /path/to/gov_production_monitor && python production_monitor.py >> cron.log 2>&1
```

### Windows (Task Scheduler)
1. Open Task Scheduler → Create Basic Task
2. Trigger: Daily, repeat every 4 hours
3. Action: Start a program
   - Program: `python`
   - Arguments: `production_monitor.py`
   - Start in: `C:\path\to\gov_production_monitor`

### Cloud (Railway.app — free tier)
1. Push this folder to a GitHub repo
2. Connect to Railway.app (free)
3. Add a cron service: `0 */4 * * *`
4. Set environment variables for API keys instead of hardcoding them

---

## What the output looks like

### Email subject:
```
🎬 Gov Production Monitor: 7 leads (4 bids, 2 high-priority)
```

### Email body (solicitations section):
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ACTIVE BIDS — RESPOND TO THESE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔴 HIGH PRIORITY [8/10] Training Video Production Services for Safety Programs
   Agency:     Department of Veterans Affairs
   NAICS:      512110 — Motion Picture and Video Production
   Set-Aside:  Small Business (SBA)
   Due:        2026-07-15T17:00:00
   Keywords:   training video, Section 508, instructional video
   Link:       https://sam.gov/opp/abc123/view
   Snippet:    The Department of Veterans Affairs requires production of 6 training
               videos for patient safety programs. Videos must be Section 508
               compliant and include closed captioning...
```

### CSV log (production_results.csv):
Builds a running history of every lead surfaced — useful for tracking which
agencies are buying, what types of work come up most, and timing patterns.

---

## Scoring guide

| Score | What it means |
|-------|---------------|
| 8–10  | High priority — top NAICS + keywords + Tier 1 agency + strong value |
| 5–7   | Solid lead — matches on NAICS or multiple keywords |
| 3–4   | Marginal — borderline match, worth reviewing manually |

Minimum score to trigger an alert is set to **4** in the config. Raise it to 5 or 6
if you're getting too many low-relevance results.

---

## Customizing for your specialty

**If you focus on training videos** — raise the score boost for `training_content` keywords
and `611430` NAICS in the config.

**If you do broadcast/PSA work** — bump up `broadcast_psa` keywords and add
`541810` (Advertising Agencies) to your high-priority NAICS list.

**If you do animation** — add `3D animation` and `motion graphics` to the
`animation_graphics` cluster, and weight them higher in scoring.

**To only see small business set-asides** — add a filter in `filters.set_asides`
to your set-aside type codes.

---

## Connecting to the trading strategy monitor

This script is designed to run alongside the contract trading monitor.
Both write to separate CSV logs. You can run them from the same machine
on the same schedule, or combine them into a single orchestrator script
that runs both sequentially.

The production monitor surfaces *opportunities you can bid on* —
a different signal from the trading monitor, which watches *awards to other companies*.
Both are worth running.
