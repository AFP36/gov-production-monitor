#!/usr/bin/env python3
"""
Government Production Contract Monitor
=======================================
Monitors SAM.gov and USASpending.gov for video production, training content,
broadcast, and multimedia contract opportunities and awards.

Designed to run on a schedule (cron, Task Scheduler, or cloud cron).
Works as a standalone module or alongside the trading strategy monitor.

Usage:
    python production_monitor.py                  # run once
    python production_monitor.py --mode awards    # awards only
    python production_monitor.py --mode opps      # solicitations only
    python production_monitor.py --test           # dry run, no alerts sent
"""

import requests
import json
import csv
import smtplib
import logging
import argparse
import time
import yaml
import os
import re
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path


# ─────────────────────────────────────────────
# Configuration loader
# ─────────────────────────────────────────────

def redact_api_key(text: str) -> str:
    """Strip api_key query-param values out of error messages before logging."""
    return re.sub(r"(api_key=)[^&\s]+", r"\1***REDACTED***", text)


def load_config(path: str = "production_config.yaml") -> dict:
    with open(path, "r") as f:
        config = yaml.safe_load(f)

    # Environment variables override YAML placeholders (Render cron sets these as secrets)
    if os.environ.get("SAM_GOV_API_KEY"):
        config["api_keys"]["sam_gov"] = os.environ["SAM_GOV_API_KEY"]
    if os.environ.get("EMAIL_SENDER"):
        config["alerts"]["email"]["sender"] = os.environ["EMAIL_SENDER"]
    if os.environ.get("EMAIL_PASSWORD"):
        config["alerts"]["email"]["password"] = os.environ["EMAIL_PASSWORD"]
    if os.environ.get("EMAIL_RECIPIENTS"):
        config["alerts"]["email"]["recipients"] = [
            addr.strip() for addr in os.environ["EMAIL_RECIPIENTS"].split(",") if addr.strip()
        ]

    return config


# ─────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────

@dataclass
class ProductionLead:
    """A government contract opportunity or award relevant to production work."""
    source: str                          # SAM_OPP, SAM_AWARD, USASPENDING
    lead_type: str                       # SOLICITATION, PRE_SOLICITATION, AWARD
    title: str
    agency: str
    sub_agency: str
    naics_code: str
    naics_label: str
    award_value: Optional[float]
    posted_date: str
    response_due: Optional[str]          # None for awards
    set_aside: Optional[str]
    matched_keywords: list[str] = field(default_factory=list)
    matched_naics: bool = False
    score: int = 0
    priority: str = "NORMAL"            # NORMAL, HIGH
    solicitation_number: str = ""
    sam_url: str = ""
    description_snippet: str = ""
    awardee_name: Optional[str] = None   # for awards/competitive intel


# ─────────────────────────────────────────────
# Scoring engine
# ─────────────────────────────────────────────

def score_lead(lead: ProductionLead, config: dict) -> ProductionLead:
    """Score a lead 1-10 based on relevance signals."""
    scoring = config["scoring"]
    boosts = scoring["boosts"]
    score = scoring["base_score"]

    high_naics = ["512110", "512191"]
    if lead.naics_code in high_naics:
        score += boosts["high_priority_naics"]

    high_kw_clusters = ["video_production", "training_content", "broadcast_psa"]
    kw_config = config["keywords"]
    high_priority_kws = []
    for cluster in high_kw_clusters:
        high_priority_kws.extend(kw_config.get(cluster, []))
    if any(kw.lower() in [m.lower() for m in lead.matched_keywords] for kw in high_priority_kws):
        score += boosts["high_priority_keywords"]

    tier1_agencies = [a["name"].lower() for a in config["target_agencies"]["tier_1"]]
    if any(t in lead.agency.lower() for t in tier1_agencies):
        score += boosts["tier_1_agency"]

    if lead.award_value:
        if lead.award_value >= 500_000:
            score += boosts["value_over_500k"]
        elif lead.award_value >= 100_000:
            score += boosts["value_over_100k"]

    set_aside_codes = ["SBA", "8A", "WOSB", "SDVOSB", "HZC"]
    if lead.set_aside and any(s in (lead.set_aside or "") for s in set_aside_codes):
        score += boosts["set_aside_eligible"]

    if "Section 508" in lead.description_snippet or "508" in lead.title:
        score += boosts["section_508_mentioned"]

    lead.score = min(score, 10)
    lead.priority = "HIGH" if lead.score >= scoring["thresholds"]["high_priority_alert"] else "NORMAL"
    return lead


# ─────────────────────────────────────────────
# Keyword matcher
# ─────────────────────────────────────────────

def extract_keywords(text: str, config: dict) -> list[str]:
    """Find which monitored keywords appear in a block of text."""
    text_lower = text.lower()
    matched = []
    for cluster_kws in config["keywords"].values():
        for kw in cluster_kws:
            if kw.lower() in text_lower and kw not in matched:
                matched.append(kw)
    return matched


def is_relevant(title: str, description: str, naics: str, config: dict) -> tuple[bool, list[str]]:
    """Return (relevant, matched_keywords). True if NAICS matches OR keywords match."""
    monitored_naics = [n["code"] for n in config["naics_codes"]]
    naics_hit = naics in monitored_naics

    combined_text = f"{title} {description}"
    kw_hits = extract_keywords(combined_text, config)

    return (naics_hit or len(kw_hits) > 0), kw_hits


# ─────────────────────────────────────────────
# SAM.gov — Contract Opportunities
# ─────────────────────────────────────────────

def fetch_sam_opportunities(config: dict, lookback_days: int = 2) -> list[ProductionLead]:
    """Fetch open solicitations and pre-solicitations from SAM.gov API."""
    api_key = config["api_keys"]["sam_gov"]
    if not api_key or api_key == "YOUR_SAM_GOV_API_KEY":
        logging.warning("SAM.gov API key not configured — skipping opportunity fetch.")
        return []

    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%m/%d/%Y")
    today = datetime.now().strftime("%m/%d/%Y")
    base_url = "https://api.sam.gov/prod/opportunities/v2/search"

    # Build NAICS filter
    naics_list = [n["code"] for n in config["naics_codes"]]

    params = {
        "api_key": api_key,
        "postedFrom": since,
        "postedTo": today,             # SAM.gov requires both postedFrom and postedTo
        "limit": 250,
        "ptype": "o,p,k",              # o=solicitation, p=pre-solicitation, k=combined synopsis
        "ncode": ",".join(naics_list),
    }

    leads = []
    try:
        resp = requests.get(base_url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        opportunities = data.get("opportunitiesData", [])
        logging.info(f"SAM Opportunities: {len(opportunities)} results returned")

        for opp in opportunities:
            title = opp.get("title", "")
            description = opp.get("description", "") or ""
            naics = opp.get("naicsCode", "") or ""
            agency = opp.get("departmentName", "") or opp.get("subtierName", "")
            sub_agency = opp.get("subtierName", "")
            set_aside = opp.get("typeOfSetAsideDescription", "")
            sol_number = opp.get("solicitationNumber", "")
            posted = opp.get("postedDate", "")
            response_due = opp.get("responseDeadLine", "")
            notice_id = opp.get("noticeId", "")
            ptype = opp.get("type", "o")

            relevant, kw_hits = is_relevant(title, description, naics, config)
            if not relevant:
                continue

            lead_type = "PRE_SOLICITATION" if ptype == "p" else "SOLICITATION"
            naics_label = next((n["label"] for n in config["naics_codes"] if n["code"] == naics), naics)

            lead = ProductionLead(
                source="SAM_OPP",
                lead_type=lead_type,
                title=title,
                agency=agency,
                sub_agency=sub_agency,
                naics_code=naics,
                naics_label=naics_label,
                award_value=None,
                posted_date=posted,
                response_due=response_due,
                set_aside=set_aside,
                matched_keywords=kw_hits,
                matched_naics=(naics in [n["code"] for n in config["naics_codes"]]),
                solicitation_number=sol_number,
                sam_url=f"https://sam.gov/opp/{notice_id}/view",
                description_snippet=description[:400],
            )
            lead = score_lead(lead, config)

            min_score = config["scoring"]["thresholds"]["alert_minimum"]
            if lead.score >= min_score:
                leads.append(lead)

    except requests.RequestException as e:
        logging.error(f"SAM opportunities fetch failed: {redact_api_key(str(e))}")

    return leads


# ─────────────────────────────────────────────
# SAM.gov — Contract Awards
# ─────────────────────────────────────────────

def fetch_sam_awards(config: dict, lookback_days: int = 2) -> list[ProductionLead]:
    """Fetch recent contract awards from SAM.gov API (competitive intel)."""
    api_key = config["api_keys"]["sam_gov"]
    if not api_key or api_key == "YOUR_SAM_GOV_API_KEY":
        logging.warning("SAM.gov API key not configured — skipping awards fetch.")
        return []

    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%m/%d/%Y")
    today = datetime.now().strftime("%m/%d/%Y")
    base_url = "https://api.sam.gov/prod/opportunities/v2/search"

    naics_list = [n["code"] for n in config["naics_codes"]]

    params = {
        "api_key": api_key,
        "postedFrom": since,
        "postedTo": today,             # SAM.gov requires both postedFrom and postedTo
        "limit": 250,
        "ptype": "a",                  # a = award notices
        "ncode": ",".join(naics_list),
    }

    leads = []
    try:
        resp = requests.get(base_url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        awards = data.get("opportunitiesData", [])
        logging.info(f"SAM Awards: {len(awards)} results returned")

        for award in awards:
            title = award.get("title", "")
            description = award.get("description", "") or ""
            naics = award.get("naicsCode", "") or ""
            agency = award.get("departmentName", "") or ""
            sub_agency = award.get("subtierName", "") or ""
            notice_id = award.get("noticeId", "")
            posted = award.get("postedDate", "")

            # Award-specific fields
            award_amount_raw = award.get("award", {}) or {}
            award_value = None
            awardee = None
            if isinstance(award_amount_raw, dict):
                try:
                    award_value = float(award_amount_raw.get("amount", 0) or 0)
                except (ValueError, TypeError):
                    pass
                awardee = award_amount_raw.get("awardee", {}) or {}
                awardee = awardee.get("name", "") if isinstance(awardee, dict) else str(awardee)

            relevant, kw_hits = is_relevant(title, description, naics, config)
            if not relevant:
                continue

            min_val = config["filters"]["min_award_value"]
            if award_value and award_value < min_val:
                continue

            naics_label = next((n["label"] for n in config["naics_codes"] if n["code"] == naics), naics)

            lead = ProductionLead(
                source="SAM_AWARD",
                lead_type="AWARD",
                title=title,
                agency=agency,
                sub_agency=sub_agency,
                naics_code=naics,
                naics_label=naics_label,
                award_value=award_value,
                posted_date=posted,
                response_due=None,
                set_aside=None,
                matched_keywords=kw_hits,
                matched_naics=(naics in [n["code"] for n in config["naics_codes"]]),
                solicitation_number=award.get("solicitationNumber", ""),
                sam_url=f"https://sam.gov/opp/{notice_id}/view",
                description_snippet=description[:400],
                awardee_name=awardee,
            )
            lead = score_lead(lead, config)

            min_score = config["scoring"]["thresholds"]["alert_minimum"]
            if lead.score >= min_score:
                leads.append(lead)

    except requests.RequestException as e:
        logging.error(f"SAM awards fetch failed: {redact_api_key(str(e))}")

    return leads


# ─────────────────────────────────────────────
# USASpending — Keyword search
# ─────────────────────────────────────────────

def fetch_usaspending_awards(config: dict, lookback_days: int = 2) -> list[ProductionLead]:
    """Search USASpending for recent awards matching production keywords."""
    since = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

    naics_list = [n["code"] for n in config["naics_codes"]]

    # Build keyword list from all clusters
    all_keywords = []
    for cluster_kws in config["keywords"].values():
        all_keywords.extend(cluster_kws)
    # Limit to most distinctive terms to avoid too-broad results
    core_keywords = [k for k in all_keywords if len(k) > 8][:20]

    payload = {
        "filters": {
            "time_period": [{"start_date": since, "end_date": datetime.now().strftime("%Y-%m-%d")}],
            "award_type_codes": ["A", "B", "C", "D"],  # Contracts only
            "naics_codes": naics_list,
        },
        "fields": [
            "Award ID", "Recipient Name", "Award Amount", "Total Outlays",
            "Description", "Contract Award Type", "NAICS Code", "NAICS Description",
            "Awarding Agency", "Awarding Sub Agency", "Start Date", "End Date",
            "generated_internal_id"
        ],
        "sort": "Award Amount",
        "order": "desc",
        "limit": 100,
        "page": 1,
    }

    leads = []
    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        logging.info(f"USASpending: {len(results)} results returned")

        min_val = config["filters"]["min_award_value"]

        for award in results:
            title = award.get("Description", "") or ""
            naics = str(award.get("NAICS Code", "") or "")
            agency = award.get("Awarding Agency", "") or ""
            sub_agency = award.get("Awarding Sub Agency", "") or ""
            try:
                award_value = float(award.get("Award Amount", 0) or 0)
            except (ValueError, TypeError):
                award_value = 0.0
            awardee = award.get("Recipient Name", "")
            start_date = award.get("Start Date", "")
            award_id = award.get("generated_internal_id", "")

            if award_value < min_val:
                continue

            relevant, kw_hits = is_relevant(title, title, naics, config)
            if not relevant:
                continue

            naics_label = award.get("NAICS Description", naics)
            lead = ProductionLead(
                source="USASPENDING",
                lead_type="AWARD",
                title=title or f"Award to {awardee}",
                agency=agency,
                sub_agency=sub_agency,
                naics_code=naics,
                naics_label=naics_label,
                award_value=award_value,
                posted_date=start_date,
                response_due=None,
                set_aside=None,
                matched_keywords=kw_hits,
                matched_naics=(naics in [n["code"] for n in config["naics_codes"]]),
                sam_url=f"https://www.usaspending.gov/award/{award_id}",
                awardee_name=awardee,
            )
            lead = score_lead(lead, config)

            min_score = config["scoring"]["thresholds"]["alert_minimum"]
            if lead.score >= min_score:
                leads.append(lead)

    except requests.RequestException as e:
        logging.error(f"USASpending fetch failed: {e}")

    return leads


# ─────────────────────────────────────────────
# Alert formatting
# ─────────────────────────────────────────────

def format_value(v: Optional[float]) -> str:
    if v is None:
        return "TBD"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"


def build_email_body(leads: list[ProductionLead]) -> str:
    solicitations = [l for l in leads if l.lead_type in ("SOLICITATION", "PRE_SOLICITATION")]
    awards = [l for l in leads if l.lead_type == "AWARD"]
    high_priority = [l for l in leads if l.priority == "HIGH"]

    lines = [
        "=" * 60,
        "GOVERNMENT PRODUCTION CONTRACT MONITOR",
        f"Report generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "=" * 60,
        "",
        f"SUMMARY: {len(leads)} leads found",
        f"  • {len(solicitations)} active bids / solicitations",
        f"  • {len(awards)} awards (competitive intel)",
        f"  • {len(high_priority)} high-priority leads",
        "",
    ]

    if solicitations:
        lines += ["━" * 60, "ACTIVE BIDS — RESPOND TO THESE", "━" * 60, ""]
        for lead in sorted(solicitations, key=lambda x: x.score, reverse=True):
            priority_flag = "🔴 HIGH PRIORITY" if lead.priority == "HIGH" else "🟡"
            lines += [
                f"{priority_flag} [{lead.score}/10] {lead.title}",
                f"   Agency:     {lead.agency}",
                f"   NAICS:      {lead.naics_code} — {lead.naics_label}",
                f"   Set-Aside:  {lead.set_aside or 'Full & Open'}",
                f"   Due:        {lead.response_due or 'See SAM.gov'}",
                f"   Keywords:   {', '.join(lead.matched_keywords[:5]) or '(NAICS match)'}",
                f"   Link:       {lead.sam_url}",
                f"   Snippet:    {lead.description_snippet[:200]}",
                "",
            ]

    if awards:
        lines += ["━" * 60, "AWARDS — COMPETITIVE INTEL", "━" * 60, ""]
        for lead in sorted(awards, key=lambda x: x.award_value or 0, reverse=True):
            lines += [
                f"[{lead.score}/10] {lead.title}",
                f"   Winner:   {lead.awardee_name or 'Not listed'}",
                f"   Value:    {format_value(lead.award_value)}",
                f"   Agency:   {lead.agency}",
                f"   NAICS:    {lead.naics_code} — {lead.naics_label}",
                f"   Source:   {lead.source}",
                f"   Link:     {lead.sam_url}",
                "",
            ]

    lines += [
        "─" * 60,
        "Direct links: sam.gov | usaspending.gov",
        "─" * 60,
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
# Alert delivery
# ─────────────────────────────────────────────

def send_email_alert(leads: list[ProductionLead], config: dict, dry_run: bool = False):
    alert_cfg = config["alerts"]["email"]
    if not alert_cfg.get("enabled"):
        return

    high_count = sum(1 for l in leads if l.priority == "HIGH")
    opp_count = sum(1 for l in leads if l.lead_type in ("SOLICITATION", "PRE_SOLICITATION"))
    subject = f"🎬 Gov Production Monitor: {len(leads)} leads ({opp_count} bids, {high_count} high-priority)"

    body = build_email_body(leads)

    if dry_run:
        print("\n[DRY RUN — EMAIL NOT SENT]\n")
        print(f"Subject: {subject}\n")
        print(body)
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = alert_cfg["sender"]
        msg["To"] = ", ".join(alert_cfg["recipients"])
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(alert_cfg["smtp_host"], alert_cfg["smtp_port"]) as server:
            server.starttls()
            server.login(alert_cfg["sender"], alert_cfg["password"])
            server.send_message(msg)

        logging.info(f"Email alert sent: {subject}")
    except Exception as e:
        logging.error(f"Email send failed: {e}")


def send_slack_alert(leads: list[ProductionLead], config: dict, dry_run: bool = False):
    alert_cfg = config["alerts"].get("slack", {})
    if not alert_cfg.get("enabled"):
        return

    high = [l for l in leads if l.priority == "HIGH"]
    opps = [l for l in leads if l.lead_type in ("SOLICITATION", "PRE_SOLICITATION")]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "🎬 Gov Production Contract Monitor"}},
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*{len(leads)} leads found* — {len(opps)} active bids, {len(high)} high-priority\n_{datetime.now().strftime('%Y-%m-%d %H:%M')}_"}},
        {"type": "divider"},
    ]

    for lead in sorted(leads, key=lambda x: x.score, reverse=True)[:8]:
        emoji = "🔴" if lead.priority == "HIGH" else "🟡"
        lead_type = "BID" if lead.lead_type != "AWARD" else "INTEL"
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                "text": (
                    f"{emoji} *[{lead_type}]* {lead.title[:80]}\n"
                    f"*Score:* {lead.score}/10 | *Agency:* {lead.agency}\n"
                    f"*Value:* {format_value(lead.award_value)} | *NAICS:* {lead.naics_code}\n"
                    f"<{lead.sam_url}|View on SAM.gov>"
                )
            }
        })

    payload = {"blocks": blocks}

    if dry_run:
        print("\n[DRY RUN — SLACK NOT SENT]")
        print(json.dumps(payload, indent=2))
        return

    try:
        resp = requests.post(alert_cfg["webhook_url"], json=payload, timeout=10)
        resp.raise_for_status()
        logging.info("Slack alert sent.")
    except requests.RequestException as e:
        logging.error(f"Slack send failed: {e}")


# ─────────────────────────────────────────────
# CSV logging
# ─────────────────────────────────────────────

def log_to_csv(leads: list[ProductionLead], config: dict):
    csv_path = config["logging"].get("results_csv", "production_results.csv")
    file_exists = Path(csv_path).exists()

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "run_date", "source", "lead_type", "priority", "score",
            "title", "agency", "naics_code", "award_value",
            "response_due", "set_aside", "awardee_name",
            "matched_keywords", "sam_url"
        ])
        if not file_exists:
            writer.writeheader()
        for lead in leads:
            writer.writerow({
                "run_date": datetime.now().isoformat(),
                "source": lead.source,
                "lead_type": lead.lead_type,
                "priority": lead.priority,
                "score": lead.score,
                "title": lead.title[:120],
                "agency": lead.agency,
                "naics_code": lead.naics_code,
                "award_value": lead.award_value,
                "response_due": lead.response_due,
                "set_aside": lead.set_aside,
                "awardee_name": lead.awardee_name,
                "matched_keywords": "; ".join(lead.matched_keywords),
                "sam_url": lead.sam_url,
            })
    logging.info(f"Logged {len(leads)} leads to {csv_path}")


# ─────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────

def run(config_path: str = "production_config.yaml", mode: str = "all", dry_run: bool = False):
    config = load_config(config_path)

    # Logging setup
    log_cfg = config.get("logging", {})
    logging.basicConfig(
        level=getattr(logging, log_cfg.get("log_level", "INFO")),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_cfg.get("log_file", "production_monitor.log")),
            logging.StreamHandler(),
        ]
    )

    lookback = config["schedule"]["lookback_days"]
    opp_cfg = config.get("opportunity_monitoring", {})
    leads = []

    # Solicitations & pre-solicitations
    if mode in ("all", "opps") and opp_cfg.get("solicitations", True):
        logging.info("Fetching SAM.gov opportunities...")
        leads += fetch_sam_opportunities(config, lookback)

    # Awards (SAM)
    if mode in ("all", "awards") and opp_cfg.get("awards", True):
        logging.info("Fetching SAM.gov awards...")
        leads += fetch_sam_awards(config, lookback)

    # Awards (USASpending)
    if mode in ("all", "awards") and opp_cfg.get("awards", True):
        logging.info("Fetching USASpending awards...")
        leads += fetch_usaspending_awards(config, lookback)

    # Deduplicate by SAM URL
    seen = set()
    unique_leads = []
    for lead in leads:
        key = lead.sam_url or lead.title
        if key not in seen:
            seen.add(key)
            unique_leads.append(lead)
    leads = unique_leads

    logging.info(f"Total leads after dedup: {len(leads)}")

    if not leads:
        logging.info("No matching leads found this run.")
        return

    # Log to CSV
    log_to_csv(leads, config)

    # Send alerts
    send_email_alert(leads, config, dry_run=dry_run)
    send_slack_alert(leads, config, dry_run=dry_run)

    logging.info("Run complete.")
    return leads


# ─────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Government Production Contract Monitor")
    parser.add_argument("--config", default="production_config.yaml", help="Path to config file")
    parser.add_argument("--mode", choices=["all", "opps", "awards"], default="all",
                        help="What to fetch: all, opps (solicitations only), awards only")
    parser.add_argument("--test", action="store_true", help="Dry run — fetch but don't send alerts")
    args = parser.parse_args()

    run(config_path=args.config, mode=args.mode, dry_run=args.test)
