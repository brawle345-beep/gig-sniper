# language: Python 3.11, file: gig-sniper.py
import os
import re
import sys
import time
import json
import hashlib
from pathlib import Path
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Iterable

import requests
import feedparser
from bs4 import BeautifulSoup

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
KEYWORDS = [k.strip().lower() for k in os.environ.get("KEYWORDS", "python,automation,scraping,api,backend").split(",") if k.strip()]
NEGATIVE = [k.strip().lower() for k in os.environ.get("NEGATIVE", "unpaid,equity only,commission only,intern").split(",") if k.strip()]
MIN_SCORE = int(os.environ.get("MIN_SCORE", "2"))
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "24"))
MAX_SEND_PER_RUN = 3
RU_ONLY = False
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
STATE = Path("seen.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# --- СПИСОК РЕАЛЬНЫХ TELEGRAM-КАНАЛОВ ---
TG_CHANNELS = [
    "it_vakansii_jobs", "vakansii_it", "zrabota", "huntmejob", "newdirections",
    "job_python", "python_jobs", "remote_it_jobs", "Remoteit", "remote_jobs_relocate",
    "devops_jobs", "habr_career", "jobGeeks", "it_vac", "geekjobs", "myjobit",
    "jobs_it", "Getitrussia", "Relocats", "normrabota", "zapwork", "digitaltender",
    "distantsiya", "itjobs_ru", "remotejob"
]

@dataclass
class Lead:
    source: str
    title: str
    url: str
    company: str
    description: str
    posted: datetime | None
    salary_min: int | None = None
    salary_max: int | None = None
    tags: list[str] = field(default_factory=list)

    def score(self) -> int:
        text = f"{self.title} {self.description} {' '.join(self.tags)}".lower()
        if any(n in text for n in NEGATIVE):
            return -1
        score = sum(1 for k in KEYWORDS if k in text)
        if self.salary_min or self.salary_max:
            score += 2
        if self.posted:
            age_h = (datetime.now(timezone.utc) - self.posted).total_seconds() / 3600
            if age_h < 6: score += 2
            elif age_h < 24: score += 1
        return score

    def fresh(self) -> bool:
        if self.posted is None: return True
        return datetime.now(timezone.utc) - self.posted < timedelta(hours=MAX_AGE_HOURS)

    def to_message(self) -> str:
        salary = ""
        if self.salary_min or self.salary_max:
            lo = f"${self.salary_min:,}" if self.salary_min else "?"
            hi = f"${self.salary_max:,}" if self.salary_max else "?"
            salary = f"💰 <b>{lo} – {hi}</b>\n"
        body = esc(truncate(strip_tags(self.description), 200))
        return (
            f"🔥 <b>{esc(self.title)}</b>\n"
            f"🏢 {esc(self.company) if self.company else 'Не указана'}\n"
            f"🌍 {esc(self.source)}\n"
            f"{salary}\n"
            f"📝 <i>{body}</i>\n\n"
            f"👉 <a href='{self.url}'>Откликнуться</a>"
        )

def esc(s: str) -> str: return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
def strip_tags(s: str) -> str: return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()
def truncate(s: str, n: int) -> str: return s if len(s) <= n else s[: n - 1].rstrip() + "…"

def _int(x) -> int | None:
    try: return int(float(x))
    except (TypeError, ValueError): return None

def _parse_iso(s) -> datetime | None:
    if not s: return None
    try:
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError: return None

def _parse_unix(x) -> datetime | None:
    try: return datetime.fromtimestamp(int(x), tz=timezone.utc)
    except (TypeError, ValueError, OSError): return None

# --- ПАРСЕР TELEGRAM ---
def fetch_telegram_channel(channel_name: str) -> Iterable[Lead]:
    try:
        url = f"https://t.me/s/{channel_name}"
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        if r.status_code != 200: return
        soup = BeautifulSoup(r.text, "lxml")
        messages = soup.find_all("div", class_="tgme_widget_message_wrap")
        
        for msg in messages:
            text_div = msg.find("div", class_="tgme_widget_message_text")
            if not text_div: continue
            text = text_div.get_text(separator="\n", strip=True)
            link_div = msg.find("a", class_="tgme_widget_message_date")
            post_url = link_div.get("href", "") if link_div else ""
            time_tag = msg.find("time")
            posted = _parse_iso(time_tag.get("datetime")) if time_tag and time_tag.get("datetime") else None
                
            if not text or not post_url: continue
            lines = [line for line in text.split("\n") if line.strip()]
            title = lines[0][:100] if lines else f"Вакансия в @{channel_name}"
            
            yield Lead(source=f"TG: @{channel_name}", title=title, url=post_url, company="Telegram", description=text, posted=posted)
    except Exception as e:
        print(f"tg_channel[{channel_name}] failed: {e}", file=sys.stderr)

# --- БИРЖИ ---
def fetch_remoteok() -> Iterable[Lead]:
    r = requests.get("https://remoteok.com/api", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    for item in r.json():
        if not isinstance(item, dict) or "position" not in item: continue
        yield Lead("remoteok", item.get("position", ""), item.get("url", ""), item.get("company", ""), item.get("description", ""), _parse_iso(item.get("date")), _int(item.get("salary_min")), _int(item.get("salary_max")), item.get("tags") or [])

def fetch_freelancer(query: str) -> Iterable[Lead]:
    r = requests.get("https://www.freelancer.com/api/projects/0.1/projects/active/", params={"query": query, "limit": 30, "job_details": "true"}, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    for item in r.json().get("result", {}).get("projects", []):
        budget = item.get("budget") or {}
        yield Lead("freelancer", item.get("title", ""), f"https://www.freelancer.com/projects/{item.get('seo_url', '')}", (item.get("owner") or {}).get("username", ""), item.get("preview_description", ""), _parse_unix(item.get("time_submitted")), _int(budget.get("minimum")), _int(budget.get("maximum")), [j.get("name", "") for j in (item.get("jobs") or [])])

# --- СТАТУС И ОТПРАВКА ---
def load_state() -> dict[str, int]:
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except json.JSONDecodeError: return {}
    return {}

def save_state(seen: dict[str, int]) -> None:
    cutoff = int(time.time()) - 30 * 86400
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    STATE.write_text(json.dumps(pruned, indent=0))

def lead_key(lead: Lead) -> str: return hashlib.sha256((lead.source + "|" + lead.url).encode()).hexdigest()

def send(message: str) -> bool:
    if DRY_RUN:
        print("---- DRY RUN ----\n" + message)
        return True
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30)
    return r.ok

# --- ОСНОВНОЙ ЦИКЛ ---
def main() -> int:
    if not (TG_TOKEN and TG_CHAT) and not DRY_RUN:
        print("TG_TOKEN / TG_CHAT missing", file=sys.stderr)
        return 1

    seen = load_state()
    shipped = 0
    feed_items = []

    def handle(lead: Lead) -> None:
        nonlocal shipped
        key = lead_key(lead)
        if key in seen or not lead.fresh() or lead.score() < MIN_SCORE:
            return
            
        if len(feed_items) < 30:
            feed_items.append({
                "title": lead.title, "company": lead.company, "source": lead.source,
                "url": lead.url, "salary_min": lead.salary_min, "salary_max": lead.salary_max
            })
        
        if shipped < MAX_SEND_PER_RUN:
            if send(lead.to_message()):
                seen[key] = int(time.time())
                shipped += 1
                time.sleep(0.5)

    # 1. Парсим Telegram-каналы
    for channel in TG_CHANNELS:
        for lead in fetch_telegram_channel(channel): handle(lead)

    # 2. Парсим Freelancer по ключам
    for kw in KEYWORDS[:3]:
        try:
            for lead in fetch_freelancer(kw): handle(lead)
        except Exception as e: print(f"freelancer[{kw}] failed: {e}", file=sys.stderr)

    # 3. Парсим RemoteOK
    try:
        for lead in fetch_remoteok(): handle(lead)
    except Exception as e: print(f"remoteok failed: {e}", file=sys.stderr)

    save_state(seen)
    
    if feed_items:
        Path("feed.json").write_text(json.dumps(feed_items, ensure_ascii=False, indent=1))
        print(f"feed.json saved with {len(feed_items)} items")
        
    print(f"shipped={shipped} tracked={len(seen)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
