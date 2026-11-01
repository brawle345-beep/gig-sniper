# language: Python 3.11, file: gig-sniper.py
# poll job boards, filter by language and score, cap spam, push to Telegram

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable

import feedparser
import requests

TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
KEYWORDS = [k.strip().lower() for k in os.environ.get("KEYWORDS", "python").split(",") if k.strip()]
NEGATIVE = [k.strip().lower() for k in os.environ.get("NEGATIVE", "unpaid,equity only,commission only,intern").split(",") if k.strip()]
MIN_SCORE = int(os.environ.get("MIN_SCORE", "3"))  # поднял порог с 2 до 3
MAX_AGE_HOURS = int(os.environ.get("MAX_AGE_HOURS", "24"))
MAX_SEND_PER_RUN = 3  # максимум 3 сообщения за запуск
RU_ONLY = True  # только с кириллицей
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
STATE = Path("seen.json")
UA = "gig_sniper/1.0"

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
            if age_h < 6:
                score += 2
            elif age_h < 24:
                score += 1
        return score

    def fresh(self) -> bool:
        if self.posted is None:
            return True
        return datetime.now(timezone.utc) - self.posted < timedelta(hours=MAX_AGE_HOURS)

    def to_message(self) -> str:
        meta = [esc(self.company), esc(self.source)]
        if self.salary_min or self.salary_max:
            lo = f"{self.salary_min:,}" if self.salary_min else "?"
            hi = f"{self.salary_max:,}" if self.salary_max else "?"
            meta.append(f"{lo} – {hi}")
        if self.posted:
            meta.append(self.posted.strftime("%d.%m %H:%M UTC"))
        body = esc(truncate(strip_tags(self.description), 280))
        return "\n".join([f"<b>{esc(self.title)}</b>", " · ".join(x for x in meta if x), "", body, self.url])

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip()

def truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"

def _int(x) -> int | None:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None

def _parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"): s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except ValueError:
        return None

def _parse_unix(x) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(x), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None

# --- адаптеры ---
def fetch_hh(query: str) -> Iterable[Lead]:
    r = requests.get("https://api.hh.ru/vacancies", params={"text": query, "per_page": 20, "order_by": "publication_time"}, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    for item in r.json().get("items", []):
        salary = item.get("salary") or {}
        snippet = item.get("snippet") or {}
        desc = f"{snippet.get('requirement', '')} {snippet.get('responsibility', '')}"
        yield Lead("hh.ru", item.get("name", ""), item.get("alternate_url", ""), (item.get("employer") or {}).get("name", ""), desc, _parse_iso(item.get("published_at")), _int(salary.get("from")), _int(salary.get("to")))

def fetch_remoteok() -> Iterable[Lead]:
    r = requests.get("https://remoteok.com/api", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    for item in r.json():
        if not isinstance(item, dict) or "position" not in item: continue
        yield Lead("remoteok", item.get("position", ""), item.get("url", ""), item.get("company", ""), item.get("description", ""), _parse_iso(item.get("date")), _int(item.get("salary_min")), _int(item.get("salary_max")), item.get("tags") or [])

def fetch_remotive() -> Iterable[Lead]:
    r = requests.get("https://remotive.com/api/remote-jobs", params={"limit": 50}, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    for item in r.json().get("jobs", []):
        yield Lead("remotive", item.get("title", ""), item.get("url", ""), item.get("company_name", ""), item.get("description", ""), _parse_iso(item.get("publication_date")), tags=[item["category"]] if item.get("category") else [])

def fetch_wwr() -> Iterable[Lead]:
    r = requests.get("https://weworkremotely.com/remote-jobs.rss", headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    for e in feedparser.parse(r.content).entries:
        posted = None
        if e.get("published"):
            try: posted = parsedate_to_datetime(e.published)
            except Exception: pass
        yield Lead("weworkremotely", e.get("title", ""), e.get("link", ""), e.get("author", ""), e.get("summary", ""), posted)

def fetch_freelancer(query: str) -> Iterable[Lead]:
    r = requests.get("https://www.freelancer.com/api/projects/0.1/projects/active/", params={"query": query, "limit": 30, "job_details": "true"}, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    for item in r.json().get("result", {}).get("projects", []):
        budget = item.get("budget") or {}
        yield Lead("freelancer", item.get("title", ""), f"https://www.freelancer.com/projects/{item.get('seo_url', '')}", (item.get("owner") or {}).get("username", ""), item.get("preview_description", ""), _parse_unix(item.get("time_submitted")), _int(budget.get("minimum")), _int(budget.get("maximum")), [j.get("name", "") for j in (item.get("jobs") or [])])

# --- состояние ---
def load_state() -> dict[str, int]:
    if STATE.exists():
        try: return json.loads(STATE.read_text())
        except json.JSONDecodeError: return {}
    return {}

def save_state(seen: dict[str, int]) -> None:
    cutoff = int(time.time()) - 30 * 86400
    pruned = {k: v for k, v in seen.items() if v > cutoff}
    STATE.write_text(json.dumps(pruned, indent=0))

def lead_key(lead: Lead) -> str:
    return hashlib.sha256((lead.source + "|" + lead.url).encode()).hexdigest()

def has_cyrillic(text: str) -> bool:
    return bool(re.search(r'[а-яА-ЯёЁ]', text))

# --- телеграм ---
def send(message: str) -> bool:
    if DRY_RUN:
        print("---- DRY RUN ----\n" + message)
        return True
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", json={"chat_id": TG_CHAT, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}, timeout=30)
    if not r.ok:
        print(f"telegram error: {r.status_code} {r.text}", file=sys.stderr)
        return False
    return True

SOURCES = [fetch_remoteok, fetch_remotive, fetch_wwr]

def main() -> int:
    if not (TG_TOKEN and TG_CHAT) and not DRY_RUN:
        print("TG_TOKEN / TG_CHAT missing", file=sys.stderr)
        return 1

    seen = load_state()
    shipped = 0

    def handle(lead: Lead) -> None:
        nonlocal shipped
        if shipped >= MAX_SEND_PER_RUN: return # лимит спама
        key = lead_key(lead)
        
        text_to_check = f"{lead.title} {lead.description}"
        if RU_ONLY and not has_cyrillic(text_to_check):
            seen.setdefault(key, int(time.time()))
            return # если нет русских букв — скипаем
            
        if key in seen or not lead.fresh() or lead.score() < MIN_SCORE:
            seen.setdefault(key, int(time.time()))
            return
        if send(lead.to_message()):
            seen[key] = int(time.time())
            shipped += 1
            time.sleep(0.5)

    # hh.ru
    for kw in KEYWORDS[:5]:
        try:
            for lead in fetch_hh(kw): handle(lead)
        except Exception as e: print(f"hh[{kw}] failed: {e}", file=sys.stderr)

    # международные (с фильтром на кириллицу)
    for source in SOURCES:
        try:
            for lead in source(): handle(lead)
        except Exception as e: print(f"{source.__name__} failed: {e}", file=sys.stderr)

    # фриланс
    for kw in KEYWORDS[:3]:
        try:
            for lead in fetch_freelancer(kw): handle(lead)
        except Exception as e: print(f"freelancer[{kw}] failed: {e}", file=sys.stderr)

    save_state(seen)
    print(f"shipped={shipped} tracked={len(seen)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
