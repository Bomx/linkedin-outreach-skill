#!/usr/bin/env python3
"""
Local LinkedIn outreach CLI for the linkedin-outreach skill.

This intentionally uses a visible local browser profile and local JSON files.
It does not use proxies, VPS services, stealth patches, CAPTCHA solvers, or
credential-based auto-login.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote_plus, urlparse, urlunparse


LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_FEED_URL = "https://www.linkedin.com/feed/"
LINKEDIN_PEOPLE_SEARCH_URL = "https://www.linkedin.com/search/results/people/?keywords={keywords}&origin=GLOBAL_SEARCH_HEADER"
DEFAULT_DATA_DIR = "~/.linkedin-outreach"
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
NAV_TIMEOUT_MS = 45_000

INTENT_POSITIVE = [
    "hiring",
    "looking for",
    "need help",
    "recommend",
    "recommendation",
    "agency",
    "consultant",
    "freelancer",
    "seo audit",
    "migration",
    "traffic drop",
    "content strategy",
    "technical seo",
    "link building",
    "backlinks",
    "anyone know",
    "vendor",
    "tool for",
]

INTENT_NEGATIVE = [
    "recruiter",
    "job seeker",
    "open to work",
    "internship",
    "student",
]

OLD_DEFAULT_CONNECTION_NOTE = (
    "Hi {first_name}, saw your LinkedIn activity around {source_label}. "
    "Thought it would be good to connect."
)

DEFAULT_TEMPLATES = {
    "connection_note": "",
    "initial_dm": (
        "Hi {first_name}, thanks for connecting. I noticed {source_label} "
        "and thought it could be useful to compare notes."
    ),
}

CONNECTION_CACHED_SKIP_STATUSES = {"sent", "pending", "connected", "skipped"}
DEFAULT_SUPPRESSIONS = {"version": 1, "updated_at": None, "profiles": []}
DEFAULT_SAFETY_STATE = {"version": 1, "updated_at": None, "cooldown_until": None, "last_stop": None}
STOP_CONDITION_COOLDOWN_HOURS = 24
BROWSER_COMMANDS = {
    "scout-search",
    "scout-activity",
    "scout-comment-keyword",
    "scout-post",
    "signal-run",
    "connect",
    "sync-connections",
    "dm",
}


class UserFacingError(Exception):
    """An expected problem that should be shown cleanly to the operator."""


class LinkedInStopCondition(UserFacingError):
    """A LinkedIn checkpoint/security condition that should trigger cooldown."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def print_result(payload: Dict[str, Any]) -> None:
    print("RESULT: " + json.dumps(payload, ensure_ascii=True, sort_keys=True))


def require_playwright():
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeout
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise UserFacingError(
            "Playwright is not installed. Run: python3 -m pip install -r "
            "requirements.txt"
        ) from exc
    return sync_playwright, PlaywrightTimeout


def safe_read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UserFacingError(f"Invalid JSON in {path}: {exc}") from exc


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=True, sort_keys=True) + "\n")


def normalize_profile_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if not url:
        return None
    if url.startswith("/"):
        url = "https://www.linkedin.com" + url
    parsed = urlparse(url)
    if "linkedin.com" not in parsed.netloc.lower():
        return None
    match = re.search(r"/in/([^/?#]+)/?", parsed.path)
    if not match:
        return None
    handle = match.group(1).strip()
    if not handle or handle.lower() in {"me", "sales"}:
        return None
    if re.match(r"^ACo[A-Za-z0-9_-]+$", handle):
        return None
    clean = urlunparse(("https", "www.linkedin.com", f"/in/{handle}/", "", "", ""))
    return clean


def normalize_activity_url(url: str) -> str:
    url = url.strip()
    if url.startswith("/"):
        url = "https://www.linkedin.com" + url
    parsed = urlparse(url)
    if "linkedin.com" not in parsed.netloc.lower():
        raise UserFacingError("Activity target must be a LinkedIn URL.")
    path = parsed.path.rstrip("/")
    if "/recent-activity" in path or "/posts" in path:
        return urlunparse(("https", "www.linkedin.com", path + "/", "", parsed.query, ""))
    profile_match = re.search(r"/in/([^/?#]+)", path)
    if profile_match:
        return f"https://www.linkedin.com/in/{profile_match.group(1)}/recent-activity/all/"
    company_match = re.search(r"/company/([^/?#]+)", path)
    if company_match:
        return f"https://www.linkedin.com/company/{company_match.group(1)}/posts/"
    return urlunparse(("https", "www.linkedin.com", path + "/", "", parsed.query, ""))


def normalize_post_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip()
    if url.startswith("/"):
        url = "https://www.linkedin.com" + url
    parsed = urlparse(url)
    if "linkedin.com" not in parsed.netloc.lower():
        return None
    path = parsed.path
    if not any(part in path for part in ["/feed/update/", "/posts/", "/pulse/"]):
        return None
    return urlunparse(("https", "www.linkedin.com", path.rstrip("/") + "/", "", "", ""))


def profile_handle(profile_url: str) -> str:
    match = re.search(r"/in/([^/]+)/", profile_url)
    return match.group(1) if match else profile_url.rstrip("/").split("/")[-1]


def lead_id_for(profile_url: str) -> str:
    return "li_" + hashlib.sha256(profile_url.encode("utf-8")).hexdigest()[:12]


def first_name(full_name: Optional[str]) -> str:
    if not full_name:
        return "there"
    cleaned = re.sub(r"\s+", " ", full_name).strip()
    if not cleaned:
        return "there"
    return cleaned.split(" ")[0]


def is_valid_person_name(name: Optional[str]) -> bool:
    if not name:
        return False
    cleaned = re.sub(r"\s+", " ", name).strip()
    if len(cleaned) < 2 or len(cleaned) > 80:
        return False
    if re.search(r"^(premium|feed post|view profile|connect|follow|message)$", cleaned, re.I):
        return False
    if "visible to anyone" in cleaned.lower():
        return False
    return bool(re.search(r"[A-Za-z]", cleaned))


def truncate(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", str(value)).strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def score_lead(query: str, snippet: Optional[str], source_type: str) -> float:
    text = (snippet or "").lower()
    query_text = query.lower()
    score = 0.35
    if source_type == "post_comment":
        score += 0.08
    for phrase in INTENT_POSITIVE:
        if phrase in text:
            score += 0.06
    for phrase in INTENT_NEGATIVE:
        if phrase in text:
            score -= 0.08
    meaningful_terms = [term for term in re.findall(r"[a-z0-9]{4,}", query_text) if term not in {"looking", "need", "help", "with", "from"}]
    matched_terms = sum(1 for term in meaningful_terms if term in text)
    if meaningful_terms:
        score += min(0.12, matched_terms * 0.04)
    if "?" in text:
        score += 0.04
    intent = classify_intent(snippet, None)
    if intent["label"] == "buyer_intent":
        score += 0.18
    elif intent["label"] == "hiring_role":
        score += 0.03
    elif intent["label"] == "provider_promo":
        score -= 0.18
    elif intent["label"] == "discussion":
        score -= 0.06
    return max(0.05, min(0.99, round(score, 2)))


def classify_intent(snippet: Optional[str], headline: Optional[str]) -> Dict[str, Any]:
    text = f"{headline or ''} {snippet or ''}".lower()
    reasons: List[str] = []

    buyer_phrases = [
        "i'm looking for",
        "i am looking for",
        "looking for a good",
        "looking for an seo",
        "looking for a highly skilled",
        "need some help",
        "need help",
        "contract opportunity",
        "not in a position to fully own",
        "open to recommendations",
        "recommend an seo",
    ]
    hiring_phrases = [
        "looking to hire",
        "hiring",
        "permanent role",
        "full time",
        "join our team",
        "remote, uk based",
        "client-facing seo consultant",
    ]
    provider_phrases = [
        "view my services",
        "get your free",
        "book an appointment",
        "i help ",
        "ask us to",
        "our agency",
        "my agency",
        "seo specialist |",
        "seo ninja",
        "boosting revenue",
    ]

    buyer_hits = [phrase for phrase in buyer_phrases if phrase in text]
    hiring_hits = [phrase for phrase in hiring_phrases if phrase in text]
    provider_hits = [phrase for phrase in provider_phrases if phrase in text]

    if buyer_hits:
        reasons.extend(buyer_hits[:3])
        return {"label": "buyer_intent", "reasons": reasons}
    if hiring_hits:
        reasons.extend(hiring_hits[:3])
        return {"label": "hiring_role", "reasons": reasons}
    if provider_hits:
        reasons.extend(provider_hits[:3])
        return {"label": "provider_promo", "reasons": reasons}

    reasons.append("no_direct_buying_phrase")
    return {"label": "discussion", "reasons": reasons}


def split_statuses(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class DataStore:
    def __init__(self, data_dir: Optional[str] = None):
        raw = data_dir or os.getenv("LINKEDIN_OUTREACH_DATA_DIR") or DEFAULT_DATA_DIR
        self.base_dir = Path(raw).expanduser().resolve()
        self.session_dir = self.base_dir / "session"
        self.profile_dir = self.session_dir / "profile"
        self.storage_state_path = self.session_dir / "storage_state.json"
        self.session_meta_path = self.session_dir / "session_meta.json"
        self.db_dir = self.base_dir / "db"
        self.leads_path = self.db_dir / "leads.json"
        self.actions_path = self.db_dir / "actions.jsonl"
        self.runs_path = self.db_dir / "runs.jsonl"
        self.templates_path = self.db_dir / "templates.json"
        self.signals_path = self.db_dir / "signals.json"
        self.suppressions_path = self.db_dir / "suppressions.json"
        self.safety_path = self.db_dir / "safety_state.json"
        self.exports_dir = self.base_dir / "exports"
        self.screenshots_dir = self.base_dir / "screenshots"

    def ensure(self) -> None:
        for path in [
            self.session_dir,
            self.profile_dir,
            self.db_dir,
            self.exports_dir,
            self.screenshots_dir,
        ]:
            path.mkdir(parents=True, exist_ok=True)
        if not self.leads_path.exists():
            write_json_atomic(self.leads_path, {"version": 1, "updated_at": now_iso(), "leads": []})
        if not self.templates_path.exists():
            write_json_atomic(self.templates_path, DEFAULT_TEMPLATES)
        if not self.signals_path.exists():
            write_json_atomic(self.signals_path, {"version": 1, "updated_at": now_iso(), "signals": []})
        if not self.suppressions_path.exists():
            write_json_atomic(self.suppressions_path, {**DEFAULT_SUPPRESSIONS, "updated_at": now_iso()})
        if not self.safety_path.exists():
            write_json_atomic(self.safety_path, {**DEFAULT_SAFETY_STATE, "updated_at": now_iso()})

    def load_leads_doc(self) -> Dict[str, Any]:
        self.ensure()
        doc = safe_read_json(self.leads_path, {"version": 1, "updated_at": now_iso(), "leads": []})
        if not isinstance(doc, dict) or not isinstance(doc.get("leads"), list):
            raise UserFacingError(f"Unexpected leads database format: {self.leads_path}")
        return doc

    def save_leads_doc(self, doc: Dict[str, Any]) -> None:
        doc["updated_at"] = now_iso()
        write_json_atomic(self.leads_path, doc)

    def load_templates(self) -> Dict[str, str]:
        self.ensure()
        data = safe_read_json(self.templates_path, DEFAULT_TEMPLATES)
        changed = False
        if data.get("connection_note") == OLD_DEFAULT_CONNECTION_NOTE:
            data["connection_note"] = ""
            changed = True
        for key, value in DEFAULT_TEMPLATES.items():
            if key not in data:
                data[key] = value
                changed = True
        if changed:
            write_json_atomic(self.templates_path, data)
        return data

    def load_signals_doc(self) -> Dict[str, Any]:
        self.ensure()
        doc = safe_read_json(self.signals_path, {"version": 1, "updated_at": now_iso(), "signals": []})
        if not isinstance(doc, dict) or not isinstance(doc.get("signals"), list):
            raise UserFacingError(f"Unexpected signals database format: {self.signals_path}")
        return doc

    def save_signals_doc(self, doc: Dict[str, Any]) -> None:
        doc["updated_at"] = now_iso()
        write_json_atomic(self.signals_path, doc)

    def load_suppressions_doc(self) -> Dict[str, Any]:
        self.ensure()
        doc = safe_read_json(self.suppressions_path, {**DEFAULT_SUPPRESSIONS, "updated_at": now_iso()})
        if not isinstance(doc, dict) or not isinstance(doc.get("profiles"), list):
            raise UserFacingError(f"Unexpected suppressions database format: {self.suppressions_path}")
        return doc

    def suppressed_profile_map(self) -> Dict[str, Dict[str, Any]]:
        suppressed: Dict[str, Dict[str, Any]] = {}
        for row in self.load_suppressions_doc().get("profiles", []):
            profile_url = normalize_profile_url(row.get("profile_url"))
            if profile_url:
                suppressed[profile_url] = row
            handle = (row.get("profile_handle") or "").strip().lower()
            if handle:
                suppressed[f"handle:{handle}"] = row
        return suppressed

    def load_safety_state(self) -> Dict[str, Any]:
        self.ensure()
        doc = safe_read_json(self.safety_path, {**DEFAULT_SAFETY_STATE, "updated_at": now_iso()})
        if not isinstance(doc, dict):
            raise UserFacingError(f"Unexpected safety state format: {self.safety_path}")
        for key, value in DEFAULT_SAFETY_STATE.items():
            doc.setdefault(key, value)
        return doc

    def save_safety_state(self, doc: Dict[str, Any]) -> None:
        doc["updated_at"] = now_iso()
        write_json_atomic(self.safety_path, doc)

    def record_stop_condition(self, stop: str, screenshot: Optional[str] = None) -> Dict[str, Any]:
        cooldown_until = (datetime.now(timezone.utc) + timedelta(hours=STOP_CONDITION_COOLDOWN_HOURS)).replace(microsecond=0)
        doc = self.load_safety_state()
        doc["cooldown_until"] = cooldown_until.isoformat().replace("+00:00", "Z")
        doc["last_stop"] = {
            "condition": stop,
            "detected_at": now_iso(),
            "cooldown_hours": STOP_CONDITION_COOLDOWN_HOURS,
            "screenshot": screenshot,
        }
        self.save_safety_state(doc)
        self.log_action("linkedin_stop_condition", None, condition=stop, cooldown_until=doc["cooldown_until"], screenshot=screenshot)
        return doc

    def active_cooldown(self) -> Optional[Dict[str, Any]]:
        doc = self.load_safety_state()
        cooldown_until = parse_iso(doc.get("cooldown_until"))
        if cooldown_until and cooldown_until > datetime.now(timezone.utc):
            return doc
        return None

    def assert_no_active_cooldown(self, force: bool = False) -> None:
        active = self.active_cooldown()
        if not active or force:
            return
        last_stop = active.get("last_stop") or {}
        raise UserFacingError(
            "LinkedIn safety cooldown is active until "
            f"{active.get('cooldown_until')} after stop condition "
            f"{last_stop.get('condition') or 'unknown'}. "
            "Open LinkedIn manually, resolve any verification, and wait before resuming. "
            "Use --force-cooldown-override only if you have manually resolved the issue and accept the risk."
        )

    def log_action(self, action_type: str, lead: Optional[Dict[str, Any]] = None, **details: Any) -> None:
        row = {
            "timestamp": now_iso(),
            "type": action_type,
            "details": details,
        }
        if lead:
            row["lead_id"] = lead.get("id")
            row["profile_url"] = lead.get("profile_url")
        append_jsonl(self.actions_path, row)

    def log_run(self, command: str, result: Dict[str, Any]) -> None:
        append_jsonl(
            self.runs_path,
            {
                "timestamp": now_iso(),
                "command": command,
                "result_status": result.get("status"),
                "result": result,
            },
        )

    def upsert_leads(self, candidates: Iterable[Dict[str, Any]]) -> Tuple[int, int, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
        doc = self.load_leads_doc()
        leads = doc["leads"]
        by_url = {lead.get("profile_url"): lead for lead in leads}
        suppressed = self.suppressed_profile_map()
        inserted = 0
        updated = 0
        changed_rows: List[Dict[str, Any]] = []
        inserted_rows: List[Dict[str, Any]] = []
        updated_rows: List[Dict[str, Any]] = []

        for candidate in candidates:
            profile_url = normalize_profile_url(candidate.get("profile_url"))
            if not profile_url:
                continue
            candidate["profile_url"] = profile_url
            handle_key = f"handle:{profile_handle(profile_url).lower()}"
            if profile_url in suppressed or handle_key in suppressed:
                suppression = suppressed.get(profile_url) or suppressed.get(handle_key) or {}
                self.log_action(
                    "lead_suppressed",
                    None,
                    profile_url=profile_url,
                    full_name=candidate.get("full_name"),
                    reason=suppression.get("reason") or "suppressed_profile",
                )
                continue
            source = candidate.get("source") or {}
            timestamp = now_iso()
            existing = by_url.get(profile_url)
            if existing:
                for key in ["full_name", "headline", "location"]:
                    if candidate.get(key):
                        existing[key] = candidate.get(key)
                existing["first_name"] = first_name(existing.get("full_name"))
                existing["lead_score"] = max(float(existing.get("lead_score") or 0), float(candidate.get("lead_score") or 0))
                existing["source"] = source or existing.get("source")
                existing["intent"] = candidate.get("intent") or classify_intent(source.get("match_text"), existing.get("headline"))
                existing.setdefault("source_history", [])
                if source and not any(source_same_signal(source, old) for old in existing["source_history"]):
                    existing["source_history"].append(source)
                existing["last_seen_at"] = timestamp
                existing["updated_at"] = timestamp
                changed_rows.append(existing)
                updated_rows.append(existing)
                updated += 1
                self.log_action("lead_updated", existing, source=source)
                continue

            lead = {
                "id": lead_id_for(profile_url),
                "profile_url": profile_url,
                "profile_handle": profile_handle(profile_url),
                "full_name": truncate(candidate.get("full_name"), 160),
                "first_name": first_name(candidate.get("full_name")),
                "headline": truncate(candidate.get("headline"), 300),
                "location": truncate(candidate.get("location"), 160),
                "lead_score": float(candidate.get("lead_score") or 0.45),
                "status": "spotted",
                "connection_status": "not_sent",
                "messaging_status": "not_started",
                "tags": candidate.get("tags") or [],
                "notes": "",
                "source": source,
                "intent": candidate.get("intent") or classify_intent(candidate.get("source", {}).get("match_text"), candidate.get("headline")),
                "source_history": [source] if source else [],
                "last_connection_note": None,
                "last_dm": None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "last_seen_at": timestamp,
            }
            leads.append(lead)
            by_url[profile_url] = lead
            changed_rows.append(lead)
            inserted_rows.append(lead)
            inserted += 1
            self.log_action("lead_spotted", lead, source=source)

        if inserted or updated:
            self.save_leads_doc(doc)
        return inserted, updated, changed_rows, inserted_rows, updated_rows

    def update_leads(self, ids: Iterable[str], updates: Dict[str, Any], action_type: str) -> List[Dict[str, Any]]:
        wanted = set(ids)
        if not wanted:
            return []
        doc = self.load_leads_doc()
        changed: List[Dict[str, Any]] = []
        timestamp = now_iso()
        for lead in doc["leads"]:
            if lead.get("id") not in wanted:
                continue
            lead.update(updates)
            lead["updated_at"] = timestamp
            changed.append(lead)
            self.log_action(action_type, lead, updates=updates)
        if changed:
            self.save_leads_doc(doc)
        return changed

    def find_leads_by_ids(self, ids: Iterable[str]) -> List[Dict[str, Any]]:
        wanted = set(ids)
        return [lead for lead in self.load_leads_doc()["leads"] if lead.get("id") in wanted]

    def select_leads(
        self,
        statuses: Optional[List[str]] = None,
        intents: Optional[List[str]] = None,
        connection_statuses: Optional[List[str]] = None,
        messaging_statuses: Optional[List[str]] = None,
        min_score: float = 0.0,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        leads = list(self.load_leads_doc()["leads"])
        if statuses:
            leads = [lead for lead in leads if lead.get("status") in statuses]
        if intents:
            leads = [lead for lead in leads if (lead.get("intent") or {}).get("label") in intents]
        if connection_statuses:
            leads = [lead for lead in leads if lead.get("connection_status") in connection_statuses]
        if messaging_statuses:
            leads = [lead for lead in leads if lead.get("messaging_status") in messaging_statuses]
        leads = [lead for lead in leads if float(lead.get("lead_score") or 0) >= min_score]
        leads.sort(key=lambda item: (-float(item.get("lead_score") or 0), item.get("created_at") or ""))
        return leads[:limit]


def launch_context(pw, store: DataStore, headless: bool = False, channel: Optional[str] = None):
    selected_channel = channel or os.getenv("LINKEDIN_BROWSER_CHANNEL") or None
    kwargs: Dict[str, Any] = {
        "user_data_dir": str(store.profile_dir),
        "headless": headless,
        "viewport": DEFAULT_VIEWPORT,
        "locale": "en-US",
    }
    if selected_channel:
        kwargs["channel"] = selected_channel
    return pw.chromium.launch_persistent_context(**kwargs)


def first_page(context):
    return context.pages[0] if context.pages else context.new_page()


def is_logged_in(page) -> bool:
    try:
        url = page.url.lower()
        if "linkedin.com/feed" in url and "login" not in url:
            return True
        selectors = [
            'input[placeholder*="Search"]',
            ".global-nav__me",
            ".scaffold-layout",
            'a[href*="/mynetwork/"]',
            'a[href*="/messaging/"]',
        ]
        return any(page.locator(selector).count() > 0 for selector in selectors)
    except Exception:
        return False


def detect_stop_condition(page) -> Optional[str]:
    try:
        url = page.url.lower()
        if "checkpoint" in url or "challenge" in url:
            return "linkedin_checkpoint_or_challenge"
        text = (page.locator("body").text_content(timeout=2000) or "").lower()
        phrases = [
            "security verification",
            "quick security check",
            "verify your identity",
            "captcha",
            "unusual activity",
            "temporarily restricted",
            "account has been restricted",
        ]
        for phrase in phrases:
            if phrase in text:
                return phrase.replace(" ", "_")
    except Exception:
        return None
    return None


def stop_condition_screenshot(page, store: DataStore) -> Optional[str]:
    try:
        screenshot = store.screenshots_dir / f"stop_condition_{int(time.time())}.png"
        page.screenshot(path=str(screenshot))
        return str(screenshot)
    except Exception:
        return None


def raise_stop_condition(store: DataStore, stop: str, page=None) -> None:
    screenshot = stop_condition_screenshot(page, store) if page is not None else None
    safety = store.record_stop_condition(stop, screenshot=screenshot)
    message = f"LinkedIn stop condition detected: {stop}. Safety cooldown until {safety.get('cooldown_until')}"
    if screenshot:
        message += f". Screenshot: {screenshot}"
    raise LinkedInStopCondition(message)


def ensure_session(page, store: DataStore) -> None:
    log("Checking local LinkedIn session")
    page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    time.sleep(2)
    stop = detect_stop_condition(page)
    if stop:
        raise_stop_condition(store, stop, page)
    if not is_logged_in(page):
        raise UserFacingError("LinkedIn session is not logged in. Run the login command first.")


def extract_profile_name_once(page) -> Optional[str]:
    try:
        return page.evaluate(
            """() => {
                const cleanName = (raw) => {
                    if (!raw) return null;
                    const lines = raw.split('\\n').map(x => x.trim()).filter(Boolean);
                    for (const line of lines) {
                        if (line.length > 1 && line.length < 80 && !/view|profile|premium|try|followers|connections/i.test(line)) {
                            return line;
                        }
                    }
                    const compact = raw.trim().replace(/\\s+/g, ' ');
                    const stopWords = ['International Director', 'Director', 'Founder', 'CEO', 'CMO', 'Marketing', ' at ', 'Apex,', 'North Carolina'];
                    let best = compact;
                    for (const stop of stopWords) {
                        const idx = best.indexOf(stop);
                        if (idx > 2) best = best.slice(0, idx).trim();
                    }
                    return best && best.length < 80 && !/view|profile|premium|try/i.test(best) ? best : null;
                };
                const selectors = [
                    '.global-nav__me .t-14',
                    '.feed-identity-module__actor-meta a',
                    'a[href*="/in/"][aria-label*="View"]',
                    'a[href*="/in/"][aria-label*="profile" i]',
                    'a[href*="/in/"] span[aria-hidden="true"]'
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    const text = cleanName((el && (el.innerText || el.textContent)) || '');
                    if (text) return text;
                }
                const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'));
                for (const a of anchors) {
                    const text = cleanName(a.innerText || a.textContent || '');
                    const aria = (a.getAttribute('aria-label') || '').trim();
                    if (text) return text;
                    const match = aria.match(/View (.+?)'s profile/i);
                    if (match && match[1]) return match[1].trim();
                }
                return null;
            }"""
        )
    except Exception:
        return None


def extract_profile_name(page, timeout_seconds: int = 20) -> Optional[str]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        name = extract_profile_name_once(page)
        if name:
            return name
        try:
            page.mouse.wheel(0, 500)
        except Exception:
            pass
        time.sleep(2)
    return extract_profile_name_once(page)


def wait_for_login(page, timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    warned_stop_condition: Optional[str] = None
    while time.time() < deadline:
        if is_logged_in(page):
            return True
        stop = detect_stop_condition(page)
        if stop and stop != warned_stop_condition:
            warned_stop_condition = stop
            log(
                "LinkedIn is showing a manual verification/checkpoint. "
                "Complete it in the browser window; I will keep waiting."
            )
        time.sleep(2)
    return False


def human_delay(min_seconds: float, max_seconds: float) -> None:
    if max_seconds < min_seconds:
        max_seconds = min_seconds
    time.sleep(random.uniform(min_seconds, max_seconds))


def choose_navigation_mode(mode: str) -> str:
    mode = (mode or "direct").strip().lower()
    if mode == "random":
        return random.choice(["direct", "click"])
    if mode in {"direct", "click"}:
        return mode
    raise UserFacingError(f"Unknown navigation mode: {mode}")


def click_exact_profile_link(page, handle: str, profile_url: str, scroll_rounds: int = 3) -> bool:
    for _ in range(max(1, scroll_rounds)):
        clicked = page.evaluate(
            """(handle) => {
                const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'));
                const exact = anchors.find((a) => {
                    const href = (a.href || '').toLowerCase().split('?')[0].replace(/\\/$/, '');
                    return href.endsWith(`/in/${handle}`);
                });
                if (!exact) return false;
                exact.scrollIntoView({block: 'center', inline: 'center'});
                exact.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                exact.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                exact.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                exact.click();
                return true;
            }""",
            handle,
        )
        if clicked:
            human_delay(2.5, 4.2)
            return normalize_profile_url(page.url) == normalize_profile_url(profile_url) or f"/in/{handle}" in page.url.lower()
        page.mouse.wheel(0, random.randint(700, 1400))
        human_delay(1.0, 1.8)
    return False


def click_profile_from_source(page, lead: Dict[str, Any]) -> bool:
    profile_url = lead.get("profile_url") or ""
    handle = profile_handle(profile_url).lower()
    source = lead.get("source") or {}
    source_url = normalize_post_url(source.get("url"))
    if not handle or not source_url or "linkedin.com/feed/update/" not in source_url:
        return False

    log(f"Click-navigation: opening source post to click {lead.get('full_name') or handle}")
    page.goto(source_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    human_delay(2.5, 4.5)
    if source.get("target_role") in {"comment_engager", "comment_author"}:
        expand_comments(page, rounds=2)
    return click_exact_profile_link(page, handle, profile_url, scroll_rounds=5)


def click_profile_from_search(page, lead: Dict[str, Any]) -> bool:
    profile_url = lead.get("profile_url") or ""
    handle = profile_handle(profile_url).lower()
    raw_queries = [lead.get("full_name"), handle.replace("-", " ")]
    queries: List[str] = []
    for raw_query in raw_queries:
        query = (raw_query or "").strip()
        if query and query.lower() not in {existing.lower() for existing in queries}:
            queries.append(query)
    if not queries:
        return False

    for query in queries:
        log(f"Click-navigation: opening LinkedIn people search for {query}")
        page.goto(LINKEDIN_PEOPLE_SEARCH_URL.format(keywords=quote_plus(query)), wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        human_delay(2.8, 4.8)
        if click_exact_profile_link(page, handle, profile_url, scroll_rounds=3):
            return True
    return False


def open_profile_for_connection(page, lead: Dict[str, Any], navigation_mode: str) -> str:
    selected_mode = choose_navigation_mode(navigation_mode)
    profile_url = lead["profile_url"]
    if selected_mode == "click":
        try:
            if click_profile_from_source(page, lead):
                return "click"
            if click_profile_from_search(page, lead):
                return "click"
            log("Click-navigation did not find the exact profile; falling back to direct profile URL")
        except Exception as exc:
            log(f"Click-navigation failed ({exc}); falling back to direct profile URL")

    page.goto(profile_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    human_delay(2.0, 3.5)
    return "direct"


def incremental_scroll(page, rounds: int) -> None:
    for _ in range(max(0, rounds)):
        page.mouse.wheel(0, random.randint(900, 1800))
        human_delay(1.0, 2.2)


def extract_search_results(page, query: str) -> List[Dict[str, Optional[str]]]:
    return page.evaluate(
        """(query) => {
            const queryTerms = (query || '').toLowerCase().match(/[a-z0-9]{4,}/g) || [];
            const boringTerms = new Set(['looking', 'need', 'help', 'with', 'from']);
            const usefulTerms = queryTerms.filter(t => !boringTerms.has(t));
            const scoreText = (text) => {
                const lower = (text || '').toLowerCase();
                let score = Math.min(lower.length / 500, 2);
                for (const term of usefulTerms) {
                    if (lower.includes(term)) score += 2;
                }
                for (const phrase of ['looking for', 'need help', 'recommend', 'consultant', 'agency', 'hiring', 'vendor']) {
                    if (lower.includes(phrase)) score += 1;
                }
                return score;
            };
            const bestCardFor = (link) => {
                let node = link;
                let best = link;
                let bestScore = scoreText(link.innerText || link.textContent || '');
                for (let i = 0; i < 9 && node && node.parentElement; i++) {
                    node = node.parentElement;
                    const text = (node.innerText || node.textContent || '').trim();
                    if (!text || text.length > 5000) continue;
                    const score = scoreText(text);
                    if (score > bestScore) {
                        bestScore = score;
                        best = node;
                    }
                }
                return best;
            };
            const out = [];
            const seen = new Set();
            const anchors = Array.from(document.querySelectorAll('a[href*="/in/"]'));
            for (const link of anchors) {
                const href = (link.href || '').split('?')[0];
                if (!href.includes('/in/') || seen.has(href)) continue;
                const card = bestCardFor(link);
                const cardText = (card && card.innerText || '').trim().replace(/\\s+\\n/g, '\\n');
                const lines = cardText.split('\\n').map(x => x.trim()).filter(Boolean);
                const isGeneric = (x) => !x || /^(feed post|view .*profile|follow|connect|message|book an appointment)$/i.test(x) || /^[\u00b7\u2022\s]*(1st|2nd|3rd)\+?$/i.test(x) || /^\\d+[hdwmy]\\s*•?/i.test(x);
                let name = (link.innerText || '').trim();
                if (isGeneric(name) || name.length > 80) {
                    name = lines.find(x => !isGeneric(x) && x.length < 80) || null;
                }
                const nameIdx = name ? lines.findIndex(x => x === name) : -1;
                let headline = null;
                if (nameIdx >= 0) {
                    headline = lines.slice(nameIdx + 1).find(x => x && x !== name && x.length > 12 && !isGeneric(x) && !/commented|reposted|liked/i.test(x)) || null;
                }
                if (!headline) {
                    headline = lines.find(x => x && x !== name && x.length > 12 && !isGeneric(x) && !/commented|reposted|liked/i.test(x)) || null;
                }
                out.push({
                    profile_url: href,
                    full_name: name,
                    headline: headline,
                    location: null,
                    snippet: lines.slice(0, 8).join(' | '),
                    post_url: (Array.from(card.querySelectorAll('a[href*="/feed/update/"], a[href*="/posts/"], a[href*="/pulse/"]'))
                        .map(a => (a.href || '').split('?')[0])
                        .find(Boolean)) || null
                });
                seen.add(href);
            }
            return out;
        }""",
        query,
    )


def collect_post_urls(page, limit: int, include_pulse: bool = True) -> List[str]:
    urls = page.evaluate(
        """() => {
            const out = [];
            const seen = new Set();
            const selectors = [
                'a[href*="/feed/update/"]',
                'a[href*="/posts/"]',
                'a[href*="/pulse/"]',
                'a[href*="urn:li:activity"]'
            ];
            for (const selector of selectors) {
                for (const a of document.querySelectorAll(selector)) {
                    const href = (a.href || a.getAttribute('href') || '').split('?')[0];
                    if (!href || seen.has(href)) continue;
                    if (href.includes('/feed/update/') || href.includes('/posts/') || href.includes('/pulse/') || href.includes('urn:li:activity')) {
                        out.push(href);
                        seen.add(href);
                    }
                }
            }
            return out;
        }"""
    )
    cleaned: List[str] = []
    seen = set()
    for url in urls:
        normalized = normalize_post_url(url)
        if normalized and not include_pulse and "/pulse/" in normalized:
            continue
        if normalized and normalized not in seen:
            cleaned.append(normalized)
            seen.add(normalized)
            if len(cleaned) >= limit:
                break
    return cleaned


def collect_activity_post_urls(page, activity_url: str, max_posts: int, scrolls: int) -> List[str]:
    page.goto(activity_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    human_delay(2.0, 3.5)
    found: List[str] = []
    seen = set()
    for _ in range(max(1, scrolls)):
        for url in collect_post_urls(page, max_posts):
            if url not in seen:
                found.append(url)
                seen.add(url)
                if len(found) >= max_posts:
                    return found
        incremental_scroll(page, 1)
    return found


def expand_comments(page, rounds: int = 5) -> None:
    selectors = [
        'button:has-text("Load more comments")',
        'button:has-text("View more comments")',
        'button:has-text("Show previous comments")',
        'button:has-text("Load more replies")',
        'button:has-text("See more comments")',
    ]
    for _ in range(rounds):
        clicked = False
        for selector in selectors:
            try:
                locator = page.locator(selector)
                count = min(locator.count(), 5)
                for idx in range(count):
                    item = locator.nth(idx)
                    if item.is_visible(timeout=700):
                        item.click()
                        clicked = True
                        human_delay(0.8, 1.6)
            except Exception:
                continue
        incremental_scroll(page, 1)
        if not clicked:
            break


def extract_comment_authors(page) -> List[Dict[str, Optional[str]]]:
    return page.evaluate(
        """() => {
            const out = [];
            const seen = new Set();
            const isGeneric = (x) => !x || /^(feed post|view .*profile|follow|connect|message|like|reply)$/i.test(x) || /^[\u00b7\u2022\s]*(1st|2nd|3rd)\+?$/i.test(x) || /^\\d+[hdwmy]\\s*•?/i.test(x);
            const selectors = [
                'article.comments-comment-item a[href*="/in/"]',
                'a.comments-post-meta__actor-link[href*="/in/"]',
                'a.comments-post-meta__name-text[href*="/in/"]',
                'span.comments-post-meta__name-text a[href*="/in/"]',
                'a[href*="/in/"]'
            ];
            for (const selector of selectors) {
                for (const link of document.querySelectorAll(selector)) {
                    const href = (link.href || '').split('?')[0];
                    if (!href.includes('/in/') || seen.has(href)) continue;
                    const card = link.closest('article, div.comments-comment-item, li, div.feed-shared-update-v2') || link.parentElement;
                    const text = (card && card.innerText || '').trim();
                    const lines = text.split('\\n').map(x => x.trim()).filter(Boolean);
                    let name = (link.innerText || '').trim();
                    if (isGeneric(name) || name.length > 80) name = lines.find(x => !isGeneric(x) && x.length < 80) || null;
                    const nameIdx = name ? lines.findIndex(x => x === name) : -1;
                    const headline = nameIdx >= 0
                        ? (lines.slice(nameIdx + 1).find(x => x && x !== name && x.length > 12 && !isGeneric(x) && !/like|reply|comment/i.test(x)) || null)
                        : (lines.find(x => x && x !== name && x.length > 12 && !isGeneric(x) && !/like|reply|comment/i.test(x)) || null);
                    const commentText = lines.filter(x => x && x !== name && x !== headline && !isGeneric(x) && !/^(like|reply|see translation|edited)$/i.test(x)).slice(0, 8).join(' | ');
                    out.push({
                        profile_url: href,
                        full_name: name,
                        headline: headline,
                        location: null,
                        snippet: lines.slice(0, 8).join(' | '),
                        comment_text: commentText
                    });
                    seen.add(href);
                }
            }
            return out;
        }"""
    )


def build_source(source_type: str, label: str, url: str, match_text: Optional[str], **extra: Any) -> Dict[str, Any]:
    source = {
        "type": source_type,
        "label": truncate(label, 180),
        "url": url,
        "match_text": truncate(match_text, 500),
        "captured_at": now_iso(),
    }
    for key, value in extra.items():
        if value is not None:
            source[key] = value
    return source


def source_same_signal(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    keys = ["type", "label", "url", "match_text", "target_role", "signal_name"]
    return all((left.get(key) or "") == (right.get(key) or "") for key in keys)


def format_message(template: str, lead: Dict[str, Any]) -> str:
    source = lead.get("source") or {}
    values = {
        "first_name": lead.get("first_name") or first_name(lead.get("full_name")),
        "full_name": lead.get("full_name") or "",
        "headline": lead.get("headline") or "",
        "source_label": source.get("label") or "your recent LinkedIn activity",
        "profile_url": lead.get("profile_url") or "",
    }
    try:
        text = template.format(**values)
    except KeyError as exc:
        raise UserFacingError(f"Unknown template placeholder: {exc}") from exc
    return re.sub(r"\s+", " ", text).strip()


def validate_custom_connection_message(message: Optional[str], leads: List[Dict[str, Any]]) -> str:
    if not message:
        return ""
    note = re.sub(r"\s+", " ", message).strip()
    if not note:
        return ""
    if len(leads) != 1:
        raise UserFacingError("Connection notes must be custom per lead. Use --message with exactly one selected lead.")
    if "{" in note or "}" in note:
        raise UserFacingError("Connection notes must be final personalized text, not a placeholder template.")
    return note[:300]


def cached_connection_skip_outcome(lead: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    connection_status = (lead.get("connection_status") or "").strip().lower()
    if connection_status not in CONNECTION_CACHED_SKIP_STATUSES:
        return None
    return {
        "lead_id": lead["id"],
        "status": "skipped",
        "reason": "cached_connection_status",
        "cached_connection_status": connection_status,
        "navigation_mode": "not_opened",
    }


def text_matches_any(text: Optional[str], needles: Iterable[str]) -> bool:
    haystack = (text or "").lower()
    return any(needle.lower() in haystack for needle in needles if needle)


def print_lead_table(leads: List[Dict[str, Any]]) -> None:
    if not leads:
        print("No leads found.")
        return
    headers = ["id", "score", "intent", "status", "conn", "dm", "name", "source", "profile"]
    rows = []
    for lead in leads:
        source = lead.get("source") or {}
        intent = lead.get("intent") or {}
        rows.append(
            [
                lead.get("id", ""),
                f"{float(lead.get('lead_score') or 0):.2f}",
                intent.get("label", ""),
                lead.get("status", ""),
                lead.get("connection_status", ""),
                lead.get("messaging_status", ""),
                truncate(lead.get("full_name") or "", 24) or "",
                truncate(source.get("label") or "", 28) or "",
                lead.get("profile_url", ""),
            ]
        )
    widths = [len(h) for h in headers]
    for row in rows:
        for idx, value in enumerate(row):
            widths[idx] = min(max(widths[idx], len(str(value))), 60)
    fmt = "  ".join("{:<" + str(width) + "}" for width in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * width for width in widths]))
    for row in rows:
        print(fmt.format(*[str(value)[: widths[idx]] for idx, value in enumerate(row)]))


def is_first_degree(page) -> bool:
    try:
        degree = page.evaluate(
            """() => {
                const short = Array.from(document.querySelectorAll('span'))
                  .map(s => (s.textContent || '').trim())
                  .filter(t => t.length < 20);
                for (const t of short) {
                  if (/^[\\u00b7\\u2022\\s]*1st\\+?$/.test(t)) return '1st';
                  if (/^[\\u00b7\\u2022\\s]*2nd\\+?$/.test(t)) return '2nd';
                  if (/^[\\u00b7\\u2022\\s]*3rd\\+?$/.test(t)) return '3rd';
                }
                const body = (document.body.innerText || '').slice(0, 1200);
                if (body.includes('\\u00b7 1st') || body.includes(' 1st\\n')) return '1st';
                if (body.includes('\\u00b7 2nd') || body.includes(' 2nd\\n')) return '2nd';
                if (body.includes('\\u00b7 3rd') || body.includes(' 3rd\\n')) return '3rd';
                return null;
            }"""
        )
        return degree == "1st"
    except Exception:
        return False


def is_pending(page) -> bool:
    try:
        return page.locator('button:has-text("Pending"), a:has-text("Pending")').count() > 0
    except Exception:
        return False


def find_visible(locator, timeout: int = 1200):
    try:
        count = locator.count()
        for idx in range(min(count, 20)):
            item = locator.nth(idx)
            if item.is_visible(timeout=timeout):
                return item
    except Exception:
        return None
    return None


def find_connect_button(page):
    candidates = [
        page.get_by_role("button", name=re.compile(r"^Connect$", re.I)),
        page.get_by_role("link", name=re.compile(r"^Connect$", re.I)),
        page.locator('button[aria-label*="connect" i]'),
        page.locator('a[aria-label*="connect" i]'),
        page.locator('a[href*="/preload/custom-invite/"]'),
        page.locator('button:has-text("Connect")'),
        page.locator('a:has-text("Connect")'),
    ]
    for locator in candidates:
        item = find_visible(locator)
        if item:
            try:
                box = item.bounding_box()
                if not box or box.get("y", 9999) <= 700:
                    return item
            except Exception:
                return item

    return None


def find_send_button(page):
    candidates = [
        page.get_by_role("button", name=re.compile(r"^Send$", re.I)),
        page.get_by_role("button", name=re.compile(r"^Send now$", re.I)),
        page.locator('button:has-text("Send without a note")'),
        page.locator('button:has-text("Send invitation")'),
        page.locator('button:has-text("Send now")'),
        page.locator('button[aria-label*="Send" i]'),
    ]
    for locator in candidates:
        item = find_visible(locator)
        if item:
            return item
    return None


def wait_for_connection_dialog(page, timeout_ms: int = 6000) -> bool:
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        if find_send_button(page):
            return True
        try:
            dialog = find_visible(page.locator('[role="dialog"], .artdeco-modal'), timeout=500)
            if dialog:
                text = dialog.text_content(timeout=1000) or ""
                if re.search(r"add a note|send without a note|send invitation|send now|customize", text, re.I):
                    return True
        except Exception:
            pass
        time.sleep(0.35)
    return False


def click_connect_button(page, button) -> bool:
    if button is not None:
        try:
            button.scroll_into_view_if_needed(timeout=3000)
            human_delay(0.2, 0.5)
            button.click(force=True)
            human_delay(1.2, 2.0)
            if wait_for_connection_dialog(page):
                return True
        except Exception:
            pass

    try:
        point = page.evaluate(
            """() => {
                const buttons = Array.from(document.querySelectorAll('button, a[href*="/preload/custom-invite/"]'));
                const candidates = [];
                for (const b of buttons) {
                    const text = (b.textContent || '').trim().replace(/\\s+/g, ' ');
                    const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                    const href = (b.getAttribute('href') || '').toLowerCase();
                    const rect = b.getBoundingClientRect();
                    if (rect.top < 180 || rect.top > 760) continue;
                    if (rect.left < 0 || rect.left > Math.min(window.innerWidth * 0.72, 980)) continue;
                    if (/^Connect$/i.test(text) || (aria.includes('connect') && aria.includes('invite')) || href.includes('/preload/custom-invite/')) {
                        candidates.push({button: b, text, aria, href, top: rect.top, left: rect.left});
                    }
                }
                candidates.sort((a, b) => {
                    const aScore = ((a.aria.includes('invite') || a.href.includes('/preload/custom-invite/')) ? 0 : 10) + (a.text === 'Connect' ? 0 : 3) + a.left / 1000;
                    const bScore = ((b.aria.includes('invite') || b.href.includes('/preload/custom-invite/')) ? 0 : 10) + (b.text === 'Connect' ? 0 : 3) + b.left / 1000;
                    return aScore - bScore;
                });
                const found = candidates[0];
                if (!found) return null;
                found.button.scrollIntoView({block: 'center', inline: 'center'});
                const r = found.button.getBoundingClientRect();
                return {x: r.left + r.width / 2, y: r.top + r.height / 2, text: found.text, aria: found.aria};
            }"""
        )
        if point:
            page.mouse.move(point["x"], point["y"])
            human_delay(0.2, 0.5)
            page.mouse.click(point["x"], point["y"])
            human_delay(2.0, 3.2)
            if wait_for_connection_dialog(page):
                return True
    except Exception:
        pass

    try:
        opened = page.evaluate(
            """() => {
                const moreButtons = Array.from(document.querySelectorAll('button[aria-label="More"], button[aria-label*="More action"]'))
                  .map((button) => {
                    const rect = button.getBoundingClientRect();
                    return {button, rect};
                  })
                  .filter(({rect}) => rect.top > 150 && rect.top < 760 && rect.left >= 0 && rect.left < Math.min(window.innerWidth * 0.72, 980))
                  .sort((a, b) => a.rect.top - b.rect.top);
                for (const {button: b} of moreButtons) {
                    const rect = b.getBoundingClientRect();
                    b.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                    b.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                    b.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    b.click();
                    return {top: rect.top, left: rect.left, aria: b.getAttribute('aria-label')};
                }
                return null;
            }"""
        )
        if opened:
            human_delay(0.8, 1.4)
            clicked = page.evaluate(
                """() => {
                    const els = Array.from(document.querySelectorAll('[role="menuitem"], .artdeco-dropdown__item'));
                    for (const el of els) {
                        const text = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                        const r = el.getBoundingClientRect();
                        if (!/^Connect$/i.test(text)) continue;
                        if (r.top < 100 || r.top > 850 || r.left < 0 || r.left > Math.min(window.innerWidth * 0.72, 980)) continue;
                        el.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                        el.click();
                        return {top: r.top, left: r.left, text};
                    }
                    return null;
                }"""
            )
            if clicked:
                human_delay(2.0, 3.0)
                return wait_for_connection_dialog(page)
    except Exception:
        pass

    return False


def visible_text(locator) -> str:
    try:
        text = (locator.text_content(timeout=1000) or "").strip()
        if text:
            return re.sub(r"\s+", " ", text)
    except Exception:
        pass
    try:
        aria = locator.get_attribute("aria-label", timeout=1000) or ""
        return re.sub(r"\s+", " ", aria.strip())
    except Exception:
        return ""


def add_connection_note(page, note: str) -> Dict[str, Any]:
    if not note:
        return {"requested": False, "added": False, "reason": "no_note_requested"}
    note = note[:300]
    textarea = find_visible(page.locator('textarea[name="message"], textarea'), timeout=700)
    add_note = None if textarea else find_visible(page.get_by_role("button", name=re.compile(r"Add a note", re.I)))
    if add_note:
        add_note.click()
        human_delay(0.8, 1.3)
        textarea = find_visible(page.locator('textarea[name="message"], textarea'))
    if textarea:
        textarea.fill(note)
        return {"requested": True, "added": True, "reason": None}
    return {"requested": True, "added": False, "reason": "note_field_unavailable"}


def extract_messaging_url(page) -> Optional[str]:
    try:
        href = page.evaluate(
            """() => {
                const links = Array.from(document.querySelectorAll('a[href*="/messaging/compose/"]'));
                const found = links.map(a => a.getAttribute('href')).find(h => h && h.includes('profileUrn'));
                return found || null;
            }"""
        )
        if not href:
            return None
        if href.startswith("/"):
            href = "https://www.linkedin.com" + href
        parsed = urlparse(href)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", parsed.query, ""))
    except Exception:
        return None


def fill_and_send_dm(page, message: str) -> bool:
    textbox_selectors = [
        'div[role="textbox"][contenteditable="true"]',
        ".msg-form__contenteditable",
        'div[aria-label*="Write a message"]',
        'div[aria-label*="Message"]',
    ]
    textbox = None
    for selector in textbox_selectors:
        textbox = find_visible(page.locator(selector), timeout=2000)
        if textbox:
            break
    if not textbox:
        return False
    textbox.click()
    page.keyboard.insert_text(message)
    human_delay(0.6, 1.1)
    send = find_visible(page.get_by_role("button", name=re.compile(r"^Send$|Send message", re.I)), timeout=2000)
    if not send:
        send = find_visible(page.locator('button[aria-label*="Send" i]'), timeout=2000)
    if not send:
        return False
    send.click()
    return True


def cmd_doctor(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    playwright_ok = True
    playwright_error = None
    try:
        require_playwright()
    except UserFacingError as exc:
        playwright_ok = False
        playwright_error = str(exc)
    return {
        "status": "ok" if playwright_ok else "missing_dependency",
        "playwright_installed": playwright_ok,
        "playwright_error": playwright_error,
        "data_dir": str(store.base_dir),
        "profile_dir": str(store.profile_dir),
        "leads_path": str(store.leads_path),
        "signals_path": str(store.signals_path),
        "templates_path": str(store.templates_path),
        "next": "Run login --timeout 600" if playwright_ok else "Install requirements and run playwright install chromium",
    }


def cmd_login(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    sync_playwright, _ = require_playwright()
    with sync_playwright() as pw:
        context = launch_context(pw, store, headless=args.headless, channel=args.channel)
        page = first_page(context)
        try:
            page.set_default_timeout(10_000)
            log("Opening LinkedIn in the local browser profile")
            page.goto(LINKEDIN_FEED_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            time.sleep(2)
            if not is_logged_in(page):
                log("Not logged in yet. Please sign in manually in the opened browser window.")
                page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                if not wait_for_login(page, args.timeout):
                    screenshot = store.screenshots_dir / f"login_timeout_{int(time.time())}.png"
                    page.screenshot(path=str(screenshot))
                    return {
                        "status": "needs_login",
                        "error": "Timed out waiting for manual LinkedIn login",
                        "screenshot": str(screenshot),
                        "data_dir": str(store.base_dir),
                    }
            context.storage_state(path=str(store.storage_state_path))
            meta = {
                "status": "logged_in",
                "profile_name": extract_profile_name(page),
                "saved_at": now_iso(),
                "storage_state_path": str(store.storage_state_path),
                "profile_dir": str(store.profile_dir),
            }
            write_json_atomic(store.session_meta_path, meta)
            return {"status": "logged_in", **meta, "data_dir": str(store.base_dir)}
        finally:
            context.close()


def cmd_scout_search(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    queries = args.query or []
    if not queries:
        raise UserFacingError("Provide at least one --query.")
    sync_playwright, _ = require_playwright()
    candidates: List[Dict[str, Any]] = []
    with sync_playwright() as pw:
        context = launch_context(pw, store, headless=args.headless, channel=args.channel)
        page = first_page(context)
        try:
            page.set_default_timeout(12_000)
            ensure_session(page, store)
            for query in queries:
                if len(candidates) >= args.limit:
                    break
                search_url = f"https://www.linkedin.com/search/results/content/?keywords={quote_plus(query)}"
                log(f"Scouting LinkedIn content search: {query}")
                page.goto(search_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                human_delay(2.0, 3.5)
                incremental_scroll(page, args.scrolls)
                stop = detect_stop_condition(page)
                if stop:
                    raise_stop_condition(store, stop, page)
                rows = extract_search_results(page, query)
                for row in rows:
                    profile_url = normalize_profile_url(row.get("profile_url"))
                    if not profile_url:
                        continue
                    if not is_valid_person_name(row.get("full_name")):
                        continue
                    snippet = row.get("snippet")
                    intent = classify_intent(snippet, row.get("headline"))
                    if args.intent and intent.get("label") not in split_statuses(args.intent):
                        continue
                    candidates.append(
                        {
                            "profile_url": profile_url,
                            "full_name": row.get("full_name"),
                            "headline": row.get("headline"),
                            "location": row.get("location"),
                            "lead_score": score_lead(query, snippet, "search"),
                            "intent": intent,
                            "source": build_source(
                                "post_intent",
                                args.source_label or args.signal_name or query,
                                row.get("post_url") or search_url,
                                snippet,
                                signal_name=args.signal_name,
                                target_role="original_poster",
                                search_query=query,
                            ),
                        }
                    )
                    if len(candidates) >= args.limit:
                        break
        finally:
            context.close()

    inserted, updated, changed, inserted_rows, updated_rows = store.upsert_leads(candidates)
    connection_candidates = inserted_rows if args.connection_new_only else changed
    result = {
        "status": "ok",
        "source": "search",
        "queries": queries,
        "candidates_seen": len(candidates),
        "inserted": inserted,
        "updated": updated,
        "changed_lead_ids": [lead["id"] for lead in changed],
        "inserted_lead_ids": [lead["id"] for lead in inserted_rows],
        "updated_lead_ids": [lead["id"] for lead in updated_rows],
        "leads_path": str(store.leads_path),
    }
    result["connection_attempt"] = maybe_attempt_connections_after_scout(args, store, connection_candidates)
    return result


def cmd_scout_activity(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    activity_url = normalize_activity_url(args.target_url)
    label = args.source_label or args.signal_name or activity_url
    sync_playwright, _ = require_playwright()
    candidates: List[Dict[str, Any]] = []
    post_urls: List[str] = []
    with sync_playwright() as pw:
        context = launch_context(pw, store, headless=args.headless, channel=args.channel)
        page = first_page(context)
        try:
            page.set_default_timeout(12_000)
            ensure_session(page, store)
            log(f"Collecting recent posts from activity target: {activity_url}")
            post_urls = collect_activity_post_urls(page, activity_url, args.max_posts, args.scrolls)
            for post_url in post_urls:
                if len(candidates) >= args.limit:
                    break
                log(f"Collecting commenters from activity post: {post_url}")
                page.goto(post_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                human_delay(2.0, 3.5)
                expand_comments(page, rounds=args.expand_rounds)
                stop = detect_stop_condition(page)
                if stop:
                    raise_stop_condition(store, stop, page)
                for row in extract_comment_authors(page):
                    profile_url = normalize_profile_url(row.get("profile_url"))
                    if not profile_url:
                        continue
                    if not is_valid_person_name(row.get("full_name")):
                        continue
                    snippet = row.get("comment_text") or row.get("snippet")
                    intent = classify_intent(snippet, row.get("headline"))
                    candidates.append(
                        {
                            "profile_url": profile_url,
                            "full_name": row.get("full_name"),
                            "headline": row.get("headline"),
                            "location": row.get("location"),
                            "lead_score": score_lead(label, snippet, "post_comment"),
                            "intent": intent,
                            "source": build_source(
                                "activity_engagement",
                                label,
                                post_url,
                                snippet,
                                signal_name=args.signal_name,
                                target_role="comment_engager",
                                target_url=activity_url,
                            ),
                        }
                    )
                    if len(candidates) >= args.limit:
                        break
        finally:
            context.close()

    inserted, updated, changed, inserted_rows, updated_rows = store.upsert_leads(candidates)
    connection_candidates = inserted_rows if args.connection_new_only else changed
    result = {
        "status": "ok",
        "source": "activity_engagement",
        "activity_url": activity_url,
        "posts_seen": len(post_urls),
        "candidates_seen": len(candidates),
        "inserted": inserted,
        "updated": updated,
        "changed_lead_ids": [lead["id"] for lead in changed],
        "inserted_lead_ids": [lead["id"] for lead in inserted_rows],
        "updated_lead_ids": [lead["id"] for lead in updated_rows],
        "leads_path": str(store.leads_path),
    }
    result["connection_attempt"] = maybe_attempt_connections_after_scout(args, store, connection_candidates)
    return result


def cmd_scout_comment_keyword(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    keywords = args.keyword or []
    if not keywords:
        raise UserFacingError("Provide at least one --keyword.")
    query = args.query or " ".join(keywords)
    label = args.source_label or args.signal_name or f"comments mentioning: {', '.join(keywords)}"
    sync_playwright, _ = require_playwright()
    candidates: List[Dict[str, Any]] = []
    post_urls: List[str] = []
    with sync_playwright() as pw:
        context = launch_context(pw, store, headless=args.headless, channel=args.channel)
        page = first_page(context)
        try:
            page.set_default_timeout(12_000)
            ensure_session(page, store)
            search_url = f"https://www.linkedin.com/search/results/content/?keywords={quote_plus(query)}"
            log(f"Finding posts for comment keyword radar: {query}")
            page.goto(search_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            human_delay(2.0, 3.5)
            for _ in range(max(1, args.scrolls)):
                for post_url in collect_post_urls(page, args.max_posts, include_pulse=False):
                    if post_url not in post_urls:
                        post_urls.append(post_url)
                        if len(post_urls) >= args.max_posts:
                            break
                if len(post_urls) >= args.max_posts:
                    break
                incremental_scroll(page, 1)

            for post_url in post_urls:
                if len(candidates) >= args.limit:
                    break
                log(f"Scanning comments on post: {post_url}")
                page.goto(post_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                human_delay(2.0, 3.5)
                expand_comments(page, rounds=args.expand_rounds)
                stop = detect_stop_condition(page)
                if stop:
                    raise_stop_condition(store, stop, page)
                for row in extract_comment_authors(page):
                    snippet = row.get("comment_text") or row.get("snippet")
                    if not text_matches_any(snippet, keywords):
                        continue
                    profile_url = normalize_profile_url(row.get("profile_url"))
                    if not profile_url:
                        continue
                    if not is_valid_person_name(row.get("full_name")):
                        continue
                    intent = classify_intent(snippet, row.get("headline"))
                    candidates.append(
                        {
                            "profile_url": profile_url,
                            "full_name": row.get("full_name"),
                            "headline": row.get("headline"),
                            "location": row.get("location"),
                            "lead_score": score_lead(label, snippet, "comment_keyword"),
                            "intent": intent,
                            "source": build_source(
                                "comment_keyword",
                                label,
                                post_url,
                                snippet,
                                signal_name=args.signal_name,
                                target_role="comment_author",
                                keywords=keywords,
                                search_query=query,
                            ),
                        }
                    )
                    if len(candidates) >= args.limit:
                        break
        finally:
            context.close()

    inserted, updated, changed, inserted_rows, updated_rows = store.upsert_leads(candidates)
    connection_candidates = inserted_rows if args.connection_new_only else changed
    result = {
        "status": "ok",
        "source": "comment_keyword",
        "query": query,
        "keywords": keywords,
        "posts_seen": len(post_urls),
        "candidates_seen": len(candidates),
        "inserted": inserted,
        "updated": updated,
        "changed_lead_ids": [lead["id"] for lead in changed],
        "inserted_lead_ids": [lead["id"] for lead in inserted_rows],
        "updated_lead_ids": [lead["id"] for lead in updated_rows],
        "leads_path": str(store.leads_path),
    }
    result["connection_attempt"] = maybe_attempt_connections_after_scout(args, store, connection_candidates)
    return result


def cmd_scout_post(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    sync_playwright, _ = require_playwright()
    post_url = args.post_url.strip()
    label = args.source_label or post_url
    candidates: List[Dict[str, Any]] = []
    with sync_playwright() as pw:
        context = launch_context(pw, store, headless=args.headless, channel=args.channel)
        page = first_page(context)
        try:
            page.set_default_timeout(12_000)
            ensure_session(page, store)
            log(f"Scouting commenters from post: {post_url}")
            page.goto(post_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            human_delay(2.0, 3.5)
            expand_comments(page, rounds=args.expand_rounds)
            stop = detect_stop_condition(page)
            if stop:
                raise_stop_condition(store, stop, page)
            rows = extract_comment_authors(page)
            for row in rows:
                profile_url = normalize_profile_url(row.get("profile_url"))
                if not profile_url:
                    continue
                if not is_valid_person_name(row.get("full_name")):
                    continue
                snippet = row.get("snippet")
                intent = classify_intent(snippet, row.get("headline"))
                candidates.append(
                    {
                        "profile_url": profile_url,
                        "full_name": row.get("full_name"),
                        "headline": row.get("headline"),
                        "location": row.get("location"),
                        "lead_score": score_lead(label, snippet, "post_comment"),
                        "intent": intent,
                        "source": build_source("post_comment", label, post_url, snippet),
                    }
                )
                if len(candidates) >= args.limit:
                    break
        finally:
            context.close()

    inserted, updated, changed, inserted_rows, updated_rows = store.upsert_leads(candidates)
    connection_candidates = inserted_rows if args.connection_new_only else changed
    result = {
        "status": "ok",
        "source": "post_comment",
        "post_url": post_url,
        "candidates_seen": len(candidates),
        "inserted": inserted,
        "updated": updated,
        "changed_lead_ids": [lead["id"] for lead in changed],
        "inserted_lead_ids": [lead["id"] for lead in inserted_rows],
        "updated_lead_ids": [lead["id"] for lead in updated_rows],
        "leads_path": str(store.leads_path),
    }
    result["connection_attempt"] = maybe_attempt_connections_after_scout(args, store, connection_candidates)
    return result


def selectable_for_connection(lead: Dict[str, Any], min_score: float, intents: Optional[List[str]]) -> bool:
    if lead.get("status") in {"rejected", "contacted"}:
        return False
    if lead.get("connection_status") not in {"not_sent", "queued"}:
        return False
    if float(lead.get("lead_score") or 0) < min_score:
        return False
    intent_label = (lead.get("intent") or {}).get("label")
    if intents and intent_label not in intents:
        return False
    return True


def maybe_attempt_connections_after_scout(args, store: DataStore, changed: List[Dict[str, Any]]) -> Dict[str, Any]:
    if getattr(args, "no_connect", False):
        return {"status": "skipped", "reason": "no_connect"}

    limit = max(0, int(getattr(args, "connection_limit", 20) or 0))
    if limit <= 0:
        return {"status": "skipped", "reason": "connection_limit_zero"}

    min_score = float(getattr(args, "connection_min_score", 0.0) or 0.0)
    intents = split_statuses(getattr(args, "connection_intent", "")) if getattr(args, "connection_intent", None) else None
    execute_connections = bool(getattr(args, "execute_connections", False))
    seen = set()
    selected_ids: List[str] = []
    for lead in changed:
        lead_id = lead.get("id")
        if not lead_id or lead_id in seen:
            continue
        seen.add(lead_id)
        if selectable_for_connection(lead, min_score, intents):
            selected_ids.append(lead_id)
        if not execute_connections and len(selected_ids) >= limit:
            break

    if not selected_ids:
        return {"status": "ok", "selected": 0, "message": "No newly scouted leads matched connection filters."}

    queued = store.update_leads(selected_ids, {"status": "queued", "connection_status": "queued"}, "lead_queued")
    connect_args = argparse.Namespace(
        data_dir=getattr(args, "data_dir", None),
        headless=getattr(args, "headless", False),
        channel=getattr(args, "channel", None),
        lead_id=[lead["id"] for lead in queued],
        queued=False,
        limit=limit,
        min_score=min_score,
        message=getattr(args, "connection_message", None),
        no_note=getattr(args, "connection_no_note", False),
        execute=execute_connections,
        min_delay=getattr(args, "connection_min_delay", 45.0),
        max_delay=getattr(args, "connection_max_delay", 90.0),
        navigation_mode=getattr(args, "connection_navigation_mode", "direct"),
    )
    connection_result = cmd_connect(connect_args)
    return {
        "status": connection_result.get("status"),
        "selected": len(queued),
        "target_sends": limit,
        "queued_ids": [lead["id"] for lead in queued],
        "execute": execute_connections,
        "result": connection_result,
    }


def cmd_review(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    queued: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    marked_connected: List[Dict[str, Any]] = []

    if args.queue:
        queued = store.update_leads(args.queue, {"status": "queued", "connection_status": "queued"}, "lead_queued")
    if args.reject:
        rejected = store.update_leads(args.reject, {"status": "rejected", "connection_status": "skipped"}, "lead_rejected")
    if args.mark_connected:
        marked_connected = store.update_leads(args.mark_connected, {"connection_status": "connected"}, "lead_marked_connected")
    if args.queue_top:
        intents = split_statuses(args.intent) if args.intent else None
        top = store.select_leads(statuses=["spotted"], intents=intents, min_score=args.min_score, limit=args.queue_top)
        queued.extend(store.update_leads([lead["id"] for lead in top], {"status": "queued", "connection_status": "queued"}, "lead_queued"))

    statuses = split_statuses(args.status)
    intents = split_statuses(args.intent) if args.intent else None
    listed = store.select_leads(statuses=statuses if statuses != ["all"] else None, intents=intents, min_score=args.min_score, limit=args.limit)
    if args.list or not (args.queue or args.reject or args.queue_top or args.mark_connected):
        print_lead_table(listed)

    return {
        "status": "ok",
        "queued": [lead["id"] for lead in queued],
        "rejected": [lead["id"] for lead in rejected],
        "marked_connected": [lead["id"] for lead in marked_connected],
        "listed_count": len(listed),
        "leads_path": str(store.leads_path),
    }


def cmd_signal_add(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    doc = store.load_signals_doc()
    signals = doc["signals"]
    existing = next((signal for signal in signals if signal.get("name") == args.name), None)
    signal = {
        "name": args.name,
        "type": args.type,
        "enabled": not args.disabled,
        "source_label": args.source_label,
        "target_url": args.target_url,
        "query": args.query,
        "keywords": args.keyword or [],
        "intent": args.intent,
        "limit": args.limit,
        "max_posts": args.max_posts,
        "scrolls": args.scrolls,
        "expand_rounds": args.expand_rounds,
        "created_at": existing.get("created_at") if existing else now_iso(),
        "updated_at": now_iso(),
    }
    if existing:
        existing.update(signal)
        action = "updated"
    else:
        signals.append(signal)
        action = "created"
    store.save_signals_doc(doc)
    return {"status": "ok", "action": action, "signal": signal, "signals_path": str(store.signals_path)}


def cmd_signal_list(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    signals = store.load_signals_doc()["signals"]
    if args.enabled:
        signals = [signal for signal in signals if signal.get("enabled", True)]
    if signals:
        headers = ["name", "type", "enabled", "label", "target/query/keywords"]
        rows = []
        for signal in signals:
            target = signal.get("target_url") or signal.get("query") or ", ".join(signal.get("keywords") or [])
            rows.append([
                signal.get("name", ""),
                signal.get("type", ""),
                str(signal.get("enabled", True)),
                truncate(signal.get("source_label") or "", 28) or "",
                truncate(target or "", 72) or "",
            ])
        widths = [len(h) for h in headers]
        for row in rows:
            for idx, value in enumerate(row):
                widths[idx] = min(max(widths[idx], len(str(value))), 72)
        fmt = "  ".join("{:<" + str(width) + "}" for width in widths)
        print(fmt.format(*headers))
        print(fmt.format(*["-" * width for width in widths]))
        for row in rows:
            print(fmt.format(*[str(value)[: widths[idx]] for idx, value in enumerate(row)]))
    else:
        print("No signals found.")
    return {"status": "ok", "count": len(signals), "signals": signals, "signals_path": str(store.signals_path)}


def cmd_signal_run(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    signals = store.load_signals_doc()["signals"]
    if args.name:
        wanted = set(args.name)
        signals = [signal for signal in signals if signal.get("name") in wanted]
    else:
        signals = [signal for signal in signals if signal.get("enabled", True)]
    if not signals:
        return {"status": "ok", "message": "No matching signals to run.", "results": []}

    results = []
    for signal in signals:
        signal_type = signal.get("type")
        common = {
            "data_dir": args.data_dir,
            "headless": args.headless,
            "channel": args.channel,
            "signal_name": signal.get("name"),
            "source_label": signal.get("source_label"),
            "limit": args.limit or signal.get("limit") or 25,
            "scrolls": signal.get("scrolls") or 4,
            "connection_limit": args.connection_limit,
            "connection_min_score": args.connection_min_score,
            "connection_intent": args.connection_intent,
            "connection_message": args.connection_message,
            "connection_no_note": args.connection_no_note,
            "execute_connections": args.execute_connections,
            "no_connect": args.no_connect,
            "connection_min_delay": args.connection_min_delay,
            "connection_max_delay": args.connection_max_delay,
            "connection_navigation_mode": args.connection_navigation_mode,
            "connection_new_only": args.connection_new_only,
        }
        log(f"Running saved signal: {signal.get('name')} ({signal_type})")
        if signal_type == "activity":
            run_args = argparse.Namespace(
                **common,
                target_url=signal.get("target_url"),
                max_posts=signal.get("max_posts") or 5,
                expand_rounds=signal.get("expand_rounds") or 4,
            )
            if not run_args.target_url:
                raise UserFacingError(f"Signal {signal.get('name')} is missing target_url.")
            result = cmd_scout_activity(run_args)
        elif signal_type == "comment_keyword":
            run_args = argparse.Namespace(
                **common,
                keyword=signal.get("keywords") or [],
                query=signal.get("query"),
                max_posts=signal.get("max_posts") or 8,
                expand_rounds=signal.get("expand_rounds") or 5,
            )
            if not run_args.keyword:
                raise UserFacingError(f"Signal {signal.get('name')} is missing keywords.")
            result = cmd_scout_comment_keyword(run_args)
        elif signal_type == "post_intent":
            query = signal.get("query")
            if not query:
                raise UserFacingError(f"Signal {signal.get('name')} is missing query.")
            run_args = argparse.Namespace(
                **common,
                query=[query],
                intent=signal.get("intent"),
            )
            result = cmd_scout_search(run_args)
        else:
            raise UserFacingError(f"Unsupported signal type for {signal.get('name')}: {signal_type}")
        results.append({"signal": signal.get("name"), "type": signal_type, "result": result})

    return {"status": "ok", "ran": len(results), "results": results}


def cmd_connect(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    store.load_templates()
    target_sends = max(0, int(args.limit or 0))
    if target_sends <= 0:
        return {"status": "ok", "message": "Connection send limit is zero.", "selected": 0, "attempted": 0, "sent_count": 0}

    if args.lead_id:
        leads = store.find_leads_by_ids(args.lead_id)
    elif args.queued:
        selection_limit = target_sends if not args.execute else max(target_sends * 3, target_sends)
        leads = store.select_leads(statuses=["queued"], connection_statuses=["queued", "not_sent"], min_score=args.min_score, limit=selection_limit)
    else:
        raise UserFacingError("Use --lead-id ... or --queued.")
    if not args.execute:
        leads = leads[:target_sends]
    if not leads:
        return {"status": "ok", "message": "No leads selected for connection requests.", "selected": 0}

    skipped_cached = []
    attemptable_leads = []
    for lead in leads:
        cached_skip = cached_connection_skip_outcome(lead)
        if cached_skip:
            skipped_cached.append(cached_skip)
        else:
            attemptable_leads.append(lead)

    custom_note = "" if args.no_note else validate_custom_connection_message(args.message, attemptable_leads)
    dry_runs = []
    if not args.execute:
        for lead in attemptable_leads:
            note = custom_note if custom_note else ""
            dry_runs.append(
                {
                    "lead_id": lead["id"],
                    "profile_url": lead["profile_url"],
                    "note_preview": note,
                    "note_mode": "custom" if note else "none",
                }
            )
            store.log_action("connection_dry_run", lead, note_preview=note)
        print_lead_table(leads)
        return {
            "status": "dry_run",
            "selected": len(leads),
            "target_sends": target_sends,
            "attemptable": len(attemptable_leads),
            "would_send": dry_runs,
            "would_skip": skipped_cached,
            "next": "Re-run with --execute to send.",
        }

    if not attemptable_leads:
        return {
            "status": "ok",
            "selected": len(leads),
            "target_sends": target_sends,
            "attempted": 0,
            "sent_count": 0,
            "outcomes": skipped_cached,
            "message": "All selected leads already have cached connection statuses, so no profiles were opened.",
        }

    sync_playwright, _ = require_playwright()
    outcomes = list(skipped_cached)
    sent_count = 0
    attempted = 0
    with sync_playwright() as pw:
        context = launch_context(pw, store, headless=args.headless, channel=args.channel)
        page = first_page(context)
        try:
            page.set_default_timeout(12_000)
            ensure_session(page, store)
            for index, lead in enumerate(attemptable_leads):
                if sent_count >= target_sends:
                    break
                note = custom_note if custom_note else ""
                outcome = send_connection(page, store, lead, note, navigation_mode=args.navigation_mode)
                outcomes.append(outcome)
                attempted += 1
                if outcome.get("status") == "sent":
                    sent_count += 1
                if sent_count < target_sends and index < len(attemptable_leads) - 1:
                    human_delay(args.min_delay, args.max_delay)
        finally:
            context.close()
    return {
        "status": "ok",
        "selected": len(leads),
        "target_sends": target_sends,
        "sent_count": sent_count,
        "attempted": attempted,
        "exhausted_before_target": sent_count < target_sends,
        "outcomes": outcomes,
    }


def send_connection(page, store: DataStore, lead: Dict[str, Any], note: str, navigation_mode: str = "direct") -> Dict[str, Any]:
    profile_url = lead["profile_url"]
    log(f"Opening profile for connection request: {profile_url}")
    used_navigation = open_profile_for_connection(page, lead, navigation_mode)
    stop = detect_stop_condition(page)
    if stop:
        raise_stop_condition(store, stop, page)

    if is_first_degree(page):
        store.update_leads([lead["id"]], {"connection_status": "connected"}, "connection_already_connected")
        return {"lead_id": lead["id"], "status": "connected", "navigation_mode": used_navigation}
    if is_pending(page):
        store.update_leads([lead["id"]], {"connection_status": "pending", "status": "contacted"}, "connection_already_pending")
        return {"lead_id": lead["id"], "status": "pending", "navigation_mode": used_navigation}

    button = find_connect_button(page)
    modal_opened = click_connect_button(page, button)
    if not modal_opened:
        screenshot = store.screenshots_dir / f"connect_modal_missing_{lead['id']}_{int(time.time())}.png"
        page.screenshot(path=str(screenshot))
        store.update_leads([lead["id"]], {"connection_status": "failed"}, "connection_failed")
        store.log_action("connection_failed", lead, reason="connect_modal_missing", screenshot=str(screenshot))
        return {
            "lead_id": lead["id"],
            "status": "failed",
            "reason": "connect_modal_missing",
            "screenshot": str(screenshot),
            "navigation_mode": used_navigation,
        }

    note_result = add_connection_note(page, note)
    send = find_send_button(page)
    if not send:
        screenshot = store.screenshots_dir / f"send_button_missing_{lead['id']}_{int(time.time())}.png"
        page.screenshot(path=str(screenshot))
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        store.update_leads([lead["id"]], {"connection_status": "failed"}, "connection_failed")
        store.log_action("connection_failed", lead, reason="send_button_missing", screenshot=str(screenshot))
        return {
            "lead_id": lead["id"],
            "status": "failed",
            "reason": "send_button_missing",
            "screenshot": str(screenshot),
            "navigation_mode": used_navigation,
        }

    send_label = visible_text(send)
    try:
        if not send.is_enabled(timeout=1500):
            screenshot = store.screenshots_dir / f"send_button_disabled_{lead['id']}_{int(time.time())}.png"
            page.screenshot(path=str(screenshot))
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            store.update_leads([lead["id"]], {"connection_status": "failed"}, "connection_failed")
            store.log_action(
                "connection_failed",
                lead,
                reason="send_button_disabled",
                send_button=send_label,
                screenshot=str(screenshot),
            )
            return {
                "lead_id": lead["id"],
                "status": "failed",
                "reason": "send_button_disabled",
                "send_button": send_label,
                "screenshot": str(screenshot),
                "navigation_mode": used_navigation,
            }
        send.click()
    except Exception as exc:
        screenshot = store.screenshots_dir / f"send_click_failed_{lead['id']}_{int(time.time())}.png"
        page.screenshot(path=str(screenshot))
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        store.update_leads([lead["id"]], {"connection_status": "failed"}, "connection_failed")
        store.log_action(
            "connection_failed",
            lead,
            reason="send_click_failed",
            error=str(exc),
            send_button=send_label,
            screenshot=str(screenshot),
        )
        return {
            "lead_id": lead["id"],
            "status": "failed",
            "reason": "send_click_failed",
            "error": str(exc),
            "send_button": send_label,
            "screenshot": str(screenshot),
            "navigation_mode": used_navigation,
        }
    human_delay(1.2, 2.0)
    sent_note = note if note_result.get("added") else ""
    store.update_leads(
        [lead["id"]],
        {
            "status": "contacted",
            "connection_status": "sent",
            "last_connection_note": sent_note,
            "connection_requested_at": now_iso(),
        },
        "connection_sent",
    )
    store.log_action(
        "connection_sent",
        lead,
        note_preview=sent_note,
        note_requested=bool(note_result.get("requested")),
        note_added=bool(note_result.get("added")),
        note_reason=note_result.get("reason"),
        send_button=send_label,
        navigation_mode=used_navigation,
    )
    return {
        "lead_id": lead["id"],
        "status": "sent",
        "navigation_mode": used_navigation,
        "note_preview": sent_note,
        "note_requested": bool(note_result.get("requested")),
        "note_added": bool(note_result.get("added")),
        "note_reason": note_result.get("reason"),
        "send_button": send_label,
    }


def sync_connection_status(page, store: DataStore, lead: Dict[str, Any]) -> Dict[str, Any]:
    profile_url = lead["profile_url"]
    previous_status = lead.get("connection_status")
    log(f"Checking connection status: {profile_url}")
    page.goto(profile_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    human_delay(2.0, 3.5)
    stop = detect_stop_condition(page)
    if stop:
        raise_stop_condition(store, stop, page)

    if is_first_degree(page):
        updates: Dict[str, Any] = {
            "status": "contacted",
            "connection_status": "connected",
            "connected_at": lead.get("connected_at") or now_iso(),
        }
        store.update_leads([lead["id"]], updates, "connection_status_synced")
        store.log_action("connection_status_synced", lead, previous_status=previous_status, connection_status="connected")
        return {"lead_id": lead["id"], "status": "connected", "previous_status": previous_status}
    if is_pending(page):
        updates = {"status": "contacted", "connection_status": "pending"}
        store.update_leads([lead["id"]], updates, "connection_status_synced")
        store.log_action("connection_status_synced", lead, previous_status=previous_status, connection_status="pending")
        return {"lead_id": lead["id"], "status": "pending", "previous_status": previous_status}

    store.log_action("connection_status_synced", lead, previous_status=previous_status, connection_status=previous_status or "unknown")
    return {"lead_id": lead["id"], "status": previous_status or "unknown", "previous_status": previous_status}


def cmd_sync_connections(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    if args.lead_id:
        leads = store.find_leads_by_ids(args.lead_id)
    elif args.contacted:
        leads = store.select_leads(
            statuses=["contacted"],
            connection_statuses=["sent", "pending", "connected"],
            min_score=args.min_score,
            limit=args.limit,
        )
    else:
        leads = store.select_leads(
            connection_statuses=["sent", "pending"],
            min_score=args.min_score,
            limit=args.limit,
        )
    if not leads:
        return {"status": "ok", "message": "No leads selected for connection sync.", "selected": 0, "outcomes": []}

    sync_playwright, _ = require_playwright()
    outcomes = []
    with sync_playwright() as pw:
        context = launch_context(pw, store, headless=args.headless, channel=args.channel)
        page = first_page(context)
        try:
            page.set_default_timeout(12_000)
            ensure_session(page, store)
            for index, lead in enumerate(leads):
                outcomes.append(sync_connection_status(page, store, lead))
                if index < len(leads) - 1:
                    human_delay(args.min_delay, args.max_delay)
        finally:
            context.close()
    return {"status": "ok", "selected": len(leads), "outcomes": outcomes}


def cmd_dm(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    templates = store.load_templates()
    if args.lead_id:
        leads = store.find_leads_by_ids(args.lead_id)
    elif args.connected:
        leads = store.select_leads(connection_statuses=["connected"], messaging_statuses=["not_started"], min_score=args.min_score, limit=args.limit)
    else:
        raise UserFacingError("Use --lead-id ... or --connected.")
    leads = leads[: args.limit]
    if not leads:
        return {"status": "ok", "message": "No leads selected for DMs.", "selected": 0}

    template = args.message or templates.get("initial_dm") or ""
    if not template:
        raise UserFacingError("No DM template found. Add --message or edit db/templates.json.")

    if not args.execute:
        previews = []
        for lead in leads:
            message = format_message(template, lead)
            previews.append({"lead_id": lead["id"], "profile_url": lead["profile_url"], "message_preview": message})
            store.log_action("dm_dry_run", lead, message_preview=message)
        print_lead_table(leads)
        return {"status": "dry_run", "selected": len(leads), "would_send": previews, "next": "Re-run with --execute to send."}

    sync_playwright, _ = require_playwright()
    outcomes = []
    with sync_playwright() as pw:
        context = launch_context(pw, store, headless=args.headless, channel=args.channel)
        page = first_page(context)
        try:
            page.set_default_timeout(12_000)
            ensure_session(page, store)
            for index, lead in enumerate(leads):
                message = format_message(template, lead)
                outcome = send_dm(page, store, lead, message)
                outcomes.append(outcome)
                if index < len(leads) - 1:
                    human_delay(args.min_delay, args.max_delay)
        finally:
            context.close()
    return {"status": "ok", "selected": len(leads), "outcomes": outcomes}


def send_dm(page, store: DataStore, lead: Dict[str, Any], message: str) -> Dict[str, Any]:
    profile_url = lead["profile_url"]
    log(f"Opening profile for DM: {profile_url}")
    page.goto(profile_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    human_delay(2.0, 3.5)
    stop = detect_stop_condition(page)
    if stop:
        raise_stop_condition(store, stop, page)
    if not is_first_degree(page):
        store.log_action("dm_failed", lead, reason="not_first_degree")
        return {"lead_id": lead["id"], "status": "skipped", "reason": "not_first_degree"}
    if lead.get("connection_status") != "connected":
        store.update_leads(
            [lead["id"]],
            {
                "status": "contacted",
                "connection_status": "connected",
                "connected_at": lead.get("connected_at") or now_iso(),
            },
            "connection_status_synced",
        )

    messaging_url = extract_messaging_url(page)
    if messaging_url:
        page.goto(messaging_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    else:
        button = find_visible(page.get_by_role("button", name=re.compile(r"^Message$|Message", re.I)))
        if not button:
            button = find_visible(page.locator('a[href*="/messaging/compose/"], button[aria-label*="Message" i]'))
        if not button:
            screenshot = store.screenshots_dir / f"message_button_missing_{lead['id']}_{int(time.time())}.png"
            page.screenshot(path=str(screenshot))
            store.log_action("dm_failed", lead, reason="message_button_missing", screenshot=str(screenshot))
            return {"lead_id": lead["id"], "status": "failed", "reason": "message_button_missing", "screenshot": str(screenshot)}
        button.click()

    human_delay(1.5, 2.8)
    if not fill_and_send_dm(page, message):
        screenshot = store.screenshots_dir / f"dm_send_failed_{lead['id']}_{int(time.time())}.png"
        page.screenshot(path=str(screenshot))
        store.update_leads([lead["id"]], {"messaging_status": "failed"}, "dm_failed")
        store.log_action("dm_failed", lead, reason="composer_or_send_missing", screenshot=str(screenshot))
        return {"lead_id": lead["id"], "status": "failed", "reason": "composer_or_send_missing", "screenshot": str(screenshot)}

    store.update_leads(
        [lead["id"]],
        {
            "status": "contacted",
            "messaging_status": "initial_sent",
            "last_dm": message,
            "last_dm_sent_at": now_iso(),
        },
        "dm_sent",
    )
    store.log_action("dm_sent", lead, message_preview=message)
    return {"lead_id": lead["id"], "status": "sent", "message_preview": message}


def cmd_status(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    leads = store.load_leads_doc()["leads"]
    safety_state = store.load_safety_state()
    active_cooldown = store.active_cooldown()
    by_status: Dict[str, int] = {}
    by_connection: Dict[str, int] = {}
    by_message: Dict[str, int] = {}
    for lead in leads:
        by_status[lead.get("status", "unknown")] = by_status.get(lead.get("status", "unknown"), 0) + 1
        by_connection[lead.get("connection_status", "unknown")] = by_connection.get(lead.get("connection_status", "unknown"), 0) + 1
        by_message[lead.get("messaging_status", "unknown")] = by_message.get(lead.get("messaging_status", "unknown"), 0) + 1
    return {
        "status": "ok",
        "total_leads": len(leads),
        "by_status": by_status,
        "by_connection_status": by_connection,
        "by_messaging_status": by_message,
        "data_dir": str(store.base_dir),
        "leads_path": str(store.leads_path),
        "safety": {
            "cooldown_active": bool(active_cooldown),
            "cooldown_until": safety_state.get("cooldown_until"),
            "last_stop": safety_state.get("last_stop"),
            "safety_path": str(store.safety_path),
        },
    }


def cmd_export(args) -> Dict[str, Any]:
    store = DataStore(args.data_dir)
    store.ensure()
    leads = store.load_leads_doc()["leads"]
    statuses = split_statuses(args.status)
    if statuses != ["all"]:
        leads = [lead for lead in leads if lead.get("status") in statuses]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path(args.output).expanduser().resolve() if args.output else store.exports_dir / f"linkedin_leads_{timestamp}.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "id",
        "profile_url",
        "full_name",
        "headline",
        "lead_score",
        "intent",
        "status",
        "connection_status",
        "messaging_status",
        "source_type",
        "source_label",
        "source_signal_name",
        "source_target_role",
        "source_url",
        "source_match_text",
        "source_history_count",
        "created_at",
        "updated_at",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for lead in leads:
            source = lead.get("source") or {}
            intent = lead.get("intent") or {}
            writer.writerow(
                {
                    "id": lead.get("id"),
                    "profile_url": lead.get("profile_url"),
                    "full_name": lead.get("full_name"),
                    "headline": lead.get("headline"),
                    "lead_score": lead.get("lead_score"),
                    "intent": intent.get("label"),
                    "status": lead.get("status"),
                    "connection_status": lead.get("connection_status"),
                    "messaging_status": lead.get("messaging_status"),
                    "source_type": source.get("type"),
                    "source_label": source.get("label"),
                    "source_signal_name": source.get("signal_name"),
                    "source_target_role": source.get("target_role"),
                    "source_url": source.get("url"),
                    "source_match_text": source.get("match_text"),
                    "source_history_count": len(lead.get("source_history") or []),
                    "created_at": lead.get("created_at"),
                    "updated_at": lead.get("updated_at"),
                }
            )
    return {"status": "ok", "exported": len(leads), "output": str(out_path)}


def add_common_browser_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-dir", default=None, help="Override local data directory.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless. Not recommended for login.")
    parser.add_argument("--channel", default=None, help='Optional Playwright browser channel, e.g. "chrome".')
    parser.add_argument(
        "--force-cooldown-override",
        action="store_true",
        help="Bypass the local safety cooldown after manually resolving LinkedIn verification. Use sparingly.",
    )


def add_connection_after_scout_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--connection-limit", type=int, default=5, help="Target successful connection sends after scouting.")
    parser.add_argument("--connection-min-score", type=float, default=0.0, help="Minimum score for auto connection attempts.")
    parser.add_argument("--connection-intent", default=None, help="Optional intent labels eligible for auto connection, comma-separated.")
    parser.add_argument("--connection-message", default=None, help="Final custom connection note text. Only valid for one selected lead.")
    parser.add_argument("--connection-no-note", action="store_true", help="Send connection requests without a note. This is the default.")
    parser.add_argument("--execute-connections", action="store_true", help="Actually send connection requests after scouting. Default is dry-run.")
    parser.add_argument("--no-connect", action="store_true", help="Skip the post-scout connection attempt phase.")
    parser.add_argument("--connection-new-only", action="store_true", help="Only auto-connect leads inserted during this scout run.")
    parser.add_argument("--connection-min-delay", type=float, default=45.0, help="Minimum delay between live connection sends.")
    parser.add_argument("--connection-max-delay", type=float, default=90.0, help="Maximum delay between live connection sends.")
    parser.add_argument(
        "--connection-navigation-mode",
        choices=["direct", "click", "random"],
        default="direct",
        help="How to open profiles before connecting. random mixes direct URL loads with LinkedIn search/click navigation.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local LinkedIn outreach skill CLI")
    parser.add_argument("--data-dir", dest="global_data_dir", default=None, help="Override local data directory.")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="Check dependencies and state paths.")
    doctor.add_argument("--data-dir", default=None, help="Override local data directory.")
    doctor.set_defaults(func=cmd_doctor)

    login = sub.add_parser("login", help="Open local browser and save LinkedIn session.")
    add_common_browser_args(login)
    login.add_argument("--timeout", type=int, default=600, help="Seconds to wait for manual login.")
    login.set_defaults(func=cmd_login)

    scout_search = sub.add_parser("scout-search", help="Scout leads from LinkedIn content search.")
    add_common_browser_args(scout_search)
    add_connection_after_scout_args(scout_search)
    scout_search.add_argument("--query", action="append", required=True, help="High-intent search query. Repeatable.")
    scout_search.add_argument("--intent", default=None, help="Only store matching intent labels, e.g. buyer_intent.")
    scout_search.add_argument("--source-label", default=None, help="Human label for this source.")
    scout_search.add_argument("--signal-name", default=None, help="Optional stable signal name stored in source history.")
    scout_search.add_argument("--limit", type=int, default=25, help="Max candidates to store.")
    scout_search.add_argument("--scrolls", type=int, default=4, help="Search result scroll rounds.")
    scout_search.set_defaults(func=cmd_scout_search)

    scout_activity = sub.add_parser("scout-activity", help="Scout commenters engaging with a person/company activity feed.")
    add_common_browser_args(scout_activity)
    add_connection_after_scout_args(scout_activity)
    scout_activity.add_argument("--target-url", required=True, help="LinkedIn profile/company/activity URL to monitor.")
    scout_activity.add_argument("--source-label", default=None, help="Human label for this source.")
    scout_activity.add_argument("--signal-name", default=None, help="Optional stable signal name stored in source history.")
    scout_activity.add_argument("--limit", type=int, default=30, help="Max commenter leads to store.")
    scout_activity.add_argument("--max-posts", type=int, default=5, help="Max recent posts to inspect.")
    scout_activity.add_argument("--scrolls", type=int, default=6, help="Activity feed scroll rounds.")
    scout_activity.add_argument("--expand-rounds", type=int, default=4, help="Comment expansion rounds per post.")
    scout_activity.set_defaults(func=cmd_scout_activity)

    scout_comment_keyword = sub.add_parser("scout-comment-keyword", help="Scout commenters who mention keywords under searched posts.")
    add_common_browser_args(scout_comment_keyword)
    add_connection_after_scout_args(scout_comment_keyword)
    scout_comment_keyword.add_argument("--keyword", action="append", required=True, help="Comment keyword/phrase to match. Repeatable.")
    scout_comment_keyword.add_argument("--query", default=None, help="LinkedIn content search query. Defaults to keywords.")
    scout_comment_keyword.add_argument("--source-label", default=None, help="Human label for this source.")
    scout_comment_keyword.add_argument("--signal-name", default=None, help="Optional stable signal name stored in source history.")
    scout_comment_keyword.add_argument("--limit", type=int, default=30, help="Max commenter leads to store.")
    scout_comment_keyword.add_argument("--max-posts", type=int, default=8, help="Max searched posts to inspect.")
    scout_comment_keyword.add_argument("--scrolls", type=int, default=5, help="Search scroll rounds.")
    scout_comment_keyword.add_argument("--expand-rounds", type=int, default=5, help="Comment expansion rounds per post.")
    scout_comment_keyword.set_defaults(func=cmd_scout_comment_keyword)

    scout_post = sub.add_parser("scout-post", help="Scout leads from LinkedIn post commenters.")
    add_common_browser_args(scout_post)
    add_connection_after_scout_args(scout_post)
    scout_post.add_argument("--post-url", required=True, help="LinkedIn post URL.")
    scout_post.add_argument("--source-label", default=None, help="Human label for this source.")
    scout_post.add_argument("--limit", type=int, default=30, help="Max candidates to store.")
    scout_post.add_argument("--expand-rounds", type=int, default=6, help="Comment expansion rounds.")
    scout_post.set_defaults(func=cmd_scout_post)

    review = sub.add_parser("review", help="List, queue, reject, or mark leads.")
    review.add_argument("--data-dir", default=None, help="Override local data directory.")
    review.add_argument("--list", action="store_true", help="Print a lead table.")
    review.add_argument("--status", default="spotted,queued", help='Statuses to list, comma-separated, or "all".')
    review.add_argument("--intent", default=None, help="Intent labels to list/queue, comma-separated.")
    review.add_argument("--limit", type=int, default=30, help="Max leads to list.")
    review.add_argument("--min-score", type=float, default=0.0, help="Minimum lead score.")
    review.add_argument("--queue", nargs="+", help="Lead IDs to queue.")
    review.add_argument("--reject", nargs="+", help="Lead IDs to reject.")
    review.add_argument("--mark-connected", nargs="+", help="Lead IDs to mark as already connected.")
    review.add_argument("--queue-top", type=int, default=0, help="Queue the top N spotted leads.")
    review.set_defaults(func=cmd_review)

    signal_add = sub.add_parser("signal-add", help="Create or update a reusable radar signal in db/signals.json.")
    signal_add.add_argument("--data-dir", default=None, help="Override local data directory.")
    signal_add.add_argument("--name", required=True, help="Stable signal name.")
    signal_add.add_argument("--type", required=True, choices=["activity", "comment_keyword", "post_intent"], help="Signal type.")
    signal_add.add_argument("--source-label", default=None, help="Human label stored on leads.")
    signal_add.add_argument("--target-url", default=None, help="LinkedIn profile/company URL for activity signals.")
    signal_add.add_argument("--query", default=None, help="Search query for post_intent/comment_keyword signals.")
    signal_add.add_argument("--keyword", action="append", help="Keyword for comment_keyword signals. Repeatable.")
    signal_add.add_argument("--intent", default=None, help="Intent label filter for post_intent signals, e.g. buyer_intent.")
    signal_add.add_argument("--limit", type=int, default=25, help="Default lead cap when running this signal.")
    signal_add.add_argument("--max-posts", type=int, default=5, help="Default max posts for activity/comment_keyword signals.")
    signal_add.add_argument("--scrolls", type=int, default=4, help="Default scroll rounds.")
    signal_add.add_argument("--expand-rounds", type=int, default=4, help="Default comment expansion rounds.")
    signal_add.add_argument("--disabled", action="store_true", help="Store signal as disabled.")
    signal_add.set_defaults(func=cmd_signal_add)

    signal_list = sub.add_parser("signal-list", help="List reusable radar signals.")
    signal_list.add_argument("--data-dir", default=None, help="Override local data directory.")
    signal_list.add_argument("--enabled", action="store_true", help="Only list enabled signals.")
    signal_list.set_defaults(func=cmd_signal_list)

    signal_run = sub.add_parser("signal-run", help="Run saved radar signals.")
    add_common_browser_args(signal_run)
    add_connection_after_scout_args(signal_run)
    signal_run.add_argument("--name", action="append", help="Specific signal name to run. Repeatable. Defaults to enabled signals.")
    signal_run.add_argument("--limit", type=int, default=None, help="Override per-signal lead cap.")
    signal_run.set_defaults(func=cmd_signal_run)

    connect = sub.add_parser("connect", help="Dry-run or send connection requests.")
    add_common_browser_args(connect)
    connect.add_argument("--lead-id", action="append", help="Specific lead ID. Repeatable.")
    connect.add_argument("--queued", action="store_true", help="Use queued leads.")
    connect.add_argument("--limit", type=int, default=3, help="Max requests in this run.")
    connect.add_argument("--min-score", type=float, default=0.0, help="Minimum lead score.")
    connect.add_argument("--message", default=None, help="Final custom connection note text. Only valid for one selected lead.")
    connect.add_argument("--no-note", action="store_true", help="Send without a connection note. This is the default.")
    connect.add_argument("--execute", action="store_true", help="Actually click Send. Default is dry-run.")
    connect.add_argument("--min-delay", type=float, default=45.0, help="Minimum delay between sends.")
    connect.add_argument("--max-delay", type=float, default=90.0, help="Maximum delay between sends.")
    connect.add_argument(
        "--navigation-mode",
        choices=["direct", "click", "random"],
        default="direct",
        help="How to open profiles before connecting. random mixes direct URL loads with LinkedIn search/click navigation.",
    )
    connect.set_defaults(func=cmd_connect)

    sync_connections = sub.add_parser("sync-connections", help="Check contacted profiles and mark accepted connections.")
    add_common_browser_args(sync_connections)
    sync_connections.add_argument("--lead-id", action="append", help="Specific lead ID. Repeatable.")
    sync_connections.add_argument("--contacted", action="store_true", help="Sync contacted leads with sent/pending/connected connection status.")
    sync_connections.add_argument("--limit", type=int, default=12, help="Max profiles to check.")
    sync_connections.add_argument("--min-score", type=float, default=0.0, help="Minimum lead score.")
    sync_connections.add_argument("--min-delay", type=float, default=8.0, help="Minimum delay between profile checks.")
    sync_connections.add_argument("--max-delay", type=float, default=18.0, help="Maximum delay between profile checks.")
    sync_connections.set_defaults(func=cmd_sync_connections)

    dm = sub.add_parser("dm", help="Dry-run or send DMs to first-degree leads.")
    add_common_browser_args(dm)
    dm.add_argument("--lead-id", action="append", help="Specific lead ID. Repeatable.")
    dm.add_argument("--connected", action="store_true", help="Use connected leads with no DM started.")
    dm.add_argument("--limit", type=int, default=3, help="Max DMs in this run.")
    dm.add_argument("--min-score", type=float, default=0.0, help="Minimum lead score.")
    dm.add_argument("--message", default=None, help="Custom DM template.")
    dm.add_argument("--execute", action="store_true", help="Actually click Send. Default is dry-run.")
    dm.add_argument("--min-delay", type=float, default=45.0, help="Minimum delay between sends.")
    dm.add_argument("--max-delay", type=float, default=90.0, help="Maximum delay between sends.")
    dm.set_defaults(func=cmd_dm)

    status = sub.add_parser("status", help="Summarize local JSON database.")
    status.add_argument("--data-dir", default=None, help="Override local data directory.")
    status.set_defaults(func=cmd_status)

    export = sub.add_parser("export", help="Export leads to CSV.")
    export.add_argument("--data-dir", default=None, help="Override local data directory.")
    export.add_argument("--status", default="all", help='Statuses to export, comma-separated, or "all".')
    export.add_argument("--output", default=None, help="CSV output path.")
    export.set_defaults(func=cmd_export)

    return parser


def command_needs_browser(args) -> bool:
    command = getattr(args, "command", "")
    if command not in BROWSER_COMMANDS:
        return False
    if command in {"connect", "dm"}:
        return bool(getattr(args, "execute", False))
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "data_dir", None) is None and getattr(args, "global_data_dir", None):
        args.data_dir = args.global_data_dir
    try:
        if command_needs_browser(args):
            DataStore(getattr(args, "data_dir", None)).assert_no_active_cooldown(
                force=bool(getattr(args, "force_cooldown_override", False))
            )
        result = args.func(args)
        command = args.command
        try:
            store = DataStore(getattr(args, "data_dir", None))
            store.ensure()
            store.log_run(command, result)
        except Exception as exc:
            result.setdefault("warnings", []).append(f"Could not write run log: {exc}")
        print_result(result)
        return 0 if result.get("status") not in {"error", "missing_dependency", "needs_login"} else 1
    except UserFacingError as exc:
        print_result({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        print_result({"status": "interrupted"})
        return 130
    except Exception as exc:
        print_result({"status": "error", "error": f"Unexpected error: {exc}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
