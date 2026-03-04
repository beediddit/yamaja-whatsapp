#!/usr/bin/env python3
"""
yamaja_cold_intake.py — v4.0.0
Yamaja WhatsApp Chatbot Webhook Server (Render)

Sits between customers (via Interakt/WhatsApp) and the Yamaja Engines sales team.
Detects temperature (cold/warm/hot), parses prefilled website messages, tracks
conversation state in SQLite, and forwards confirmed leads via WhatsApp to the
sales line.

Endpoints:
  POST /webhook              — Interakt incoming messages (main)
  GET  /webhook              — Health check / webhook verification
  POST /webhook/website      — Website contact form submissions
  GET  /health               — Server health with version, DB path
  GET  /leads                — All leads (auth required)
  GET  /leads/<phone>        — Single conversation (auth required)
  GET  /debug/webhooks       — Last 50 raw webhook payloads (auth required)
  POST /reset/<phone>        — Reset a specific conversation (auth required)
  POST /reset-all            — Reset all conversations (auth required)

Deployment: Render Starter plan, gunicorn --workers 1
"""

import os
import re
import json
import time
import sqlite3
import threading
import logging
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify
import requests

# ─── App & Logging ────────────────────────────────────────────────────────────

app = Flask(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

VERSION = "4.0.0"

# ─── Configuration ────────────────────────────────────────────────────────────

INTERAKT_API_KEY = os.environ.get('INTERAKT_API_KEY', '')
MAKE_WEBHOOK_URL = os.environ.get('MAKE_WEBHOOK_URL', '')      # kept for future use
ADMIN_SECRET     = os.environ.get('ADMIN_SECRET', '')

CLAUDIA_PHONE  = '18765642888'   # The chatbot number
SALES_PHONE    = '18763712888'   # General sales line for lead forwarding
MANAGER_PHONE  = '18769951632'   # Escalation target

# ─── SQLite Database ──────────────────────────────────────────────────────────

# Render Starter plan: /opt/render/project/src/data/ is persistent across deploys.
# Falls back to ./data/ for local development.
_RENDER_DATA_DIR = '/opt/render/project/src/data'
_LOCAL_DATA_DIR  = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')

if os.path.isdir(_RENDER_DATA_DIR):
    DB_DIR  = _RENDER_DATA_DIR
else:
    DB_DIR  = _LOCAL_DATA_DIR
    os.makedirs(DB_DIR, exist_ok=True)

DB_PATH = os.path.join(DB_DIR, 'yamaja_conversations.db')
logger.info(f"Database path: {DB_PATH}")


def _get_db():
    """Open a SQLite connection with WAL mode for concurrency safety."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create database tables if they don't exist."""
    with _get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                phone       TEXT PRIMARY KEY,
                data        TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS webhook_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at TEXT NOT NULL,
                payload     TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dedup_keys (
                key         TEXT PRIMARY KEY,
                created_at  REAL NOT NULL
            )
        """)
        conn.commit()
    logger.info("Database initialised")


# Initialise tables on startup
init_db()

# ─── Phone-level Locking ─────────────────────────────────────────────────────

_phone_locks: dict = {}
_phone_locks_mutex = threading.Lock()


def _get_phone_lock(phone: str) -> threading.Lock:
    """Return (or create) a per-phone threading.Lock."""
    with _phone_locks_mutex:
        if phone not in _phone_locks:
            _phone_locks[phone] = threading.Lock()
        return _phone_locks[phone]


# ─── SQLite Conversation Helpers ─────────────────────────────────────────────

def load_conversation(phone: str) -> dict | None:
    """Load conversation JSON blob from SQLite. Returns None if not found."""
    with _get_db() as conn:
        row = conn.execute(
            "SELECT data FROM conversations WHERE phone = ?", (phone,)
        ).fetchone()
    if row:
        try:
            return json.loads(row["data"])
        except json.JSONDecodeError:
            logger.error(f"Corrupt conversation data for {phone}")
            return None
    return None


def save_conversation(convo: dict):
    """Persist conversation JSON blob to SQLite."""
    phone = convo["phone"]
    now   = datetime.now(timezone.utc).isoformat()
    data  = json.dumps(convo, default=str)
    with _get_db() as conn:
        conn.execute(
            """INSERT INTO conversations (phone, data, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(phone) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at""",
            (phone, data, now)
        )
        conn.commit()


def delete_conversation(phone: str):
    """Remove a conversation from SQLite."""
    with _get_db() as conn:
        conn.execute("DELETE FROM conversations WHERE phone = ?", (phone,))
        conn.commit()


def delete_all_conversations():
    """Remove all conversations from SQLite."""
    with _get_db() as conn:
        conn.execute("DELETE FROM conversations")
        conn.commit()


def list_all_conversations() -> list[dict]:
    """Return all conversation blobs as a list."""
    with _get_db() as conn:
        rows = conn.execute("SELECT data FROM conversations").fetchall()
    result = []
    for row in rows:
        try:
            result.append(json.loads(row["data"]))
        except json.JSONDecodeError:
            pass
    return result


# ─── Webhook Log Helpers ──────────────────────────────────────────────────────

def log_webhook(payload: dict):
    """Append raw webhook payload to the SQLite log (keep last 50)."""
    now = datetime.now(timezone.utc).isoformat()
    with _get_db() as conn:
        conn.execute(
            "INSERT INTO webhook_log (received_at, payload) VALUES (?, ?)",
            (now, json.dumps(payload, default=str))
        )
        # Prune to last 50
        conn.execute("""
            DELETE FROM webhook_log
            WHERE id NOT IN (
                SELECT id FROM webhook_log ORDER BY id DESC LIMIT 50
            )
        """)
        conn.commit()


def get_webhook_log() -> list[dict]:
    """Return the last 50 webhook payloads."""
    with _get_db() as conn:
        rows = conn.execute(
            "SELECT received_at, payload FROM webhook_log ORDER BY id DESC LIMIT 50"
        ).fetchall()
    result = []
    for row in rows:
        try:
            result.append({
                "received_at": row["received_at"],
                "payload": json.loads(row["payload"])
            })
        except json.JSONDecodeError:
            pass
    return result


# ─── Deduplication Helpers ────────────────────────────────────────────────────

DEDUP_TTL_SECONDS = 300   # 5 minutes — duplicate webhooks arrive within seconds


def is_duplicate(key: str) -> bool:
    """Return True if this dedup key was seen within DEDUP_TTL_SECONDS."""
    cutoff = time.time() - DEDUP_TTL_SECONDS
    with _get_db() as conn:
        row = conn.execute(
            "SELECT created_at FROM dedup_keys WHERE key = ?", (key,)
        ).fetchone()
    if row and row["created_at"] > cutoff:
        return True
    return False


def mark_seen(key: str):
    """Record a dedup key as processed."""
    now = time.time()
    with _get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO dedup_keys (key, created_at) VALUES (?, ?)",
            (key, now)
        )
        conn.commit()


def cleanup_dedup_every_nth(n: int = 100):
    """Prune expired dedup entries; called every nth webhook to keep table small."""
    cutoff = time.time() - DEDUP_TTL_SECONDS
    with _get_db() as conn:
        conn.execute("DELETE FROM dedup_keys WHERE created_at < ?", (cutoff,))
        conn.commit()


# Counter for dedup cleanup scheduling
_webhook_counter = 0


# ─── Auth Helper ─────────────────────────────────────────────────────────────

def _check_admin_auth() -> bool:
    """Check request for ADMIN_SECRET via ?secret= param or X-Admin-Secret header."""
    if not ADMIN_SECRET:
        # If secret not configured, block all access for safety
        return False
    secret = (
        request.args.get('secret', '') or
        request.headers.get('X-Admin-Secret', '')
    )
    return secret == ADMIN_SECRET


# ─── Phone Normalisation ─────────────────────────────────────────────────────

def normalize_phone(phone: str) -> str:
    """
    Normalise a phone number to 11-digit string (1XXXXXXXXXX for Jamaica/US).
    Strips all non-digit characters, adds leading '1' if 10 digits.
    """
    phone = re.sub(r'[^\d]', '', phone)
    if len(phone) == 11 and phone.startswith('1'):
        return phone
    if len(phone) == 10:
        return '1' + phone
    return phone   # Return as-is for non-standard numbers


def _format_phone_for_interakt(phone: str) -> tuple[str, str]:
    """
    Split a normalised phone into (countryCode, phoneNumber) for Interakt API.
    For Jamaica (+1): countryCode='+1', phoneNumber='876XXXXXXX' (10 digits).
    """
    phone = re.sub(r'[^\d]', '', phone)
    if phone.startswith('1') and len(phone) == 11:
        return '+1', phone[1:]
    if len(phone) == 10:
        return '+1', phone
    return '+1', phone


# ─── Temperature Detection Patterns ──────────────────────────────────────────

WEBSITE_PATTERNS = [
    {
        # Pattern 1: Engine detail — bottom CTA button
        "name": "cta_button",
        "regex": re.compile(
            r"interested in the (.+?)\.\s*Can you give me more details",
            re.IGNORECASE
        ),
        "extracts": {"engine_model": 1},
        "branch": "engine_sales",
        "temperature": "warm"
    },
    {
        # Pattern 2: Engine detail — floating button with full profile (logged in)
        # Must come before float_basic (more specific)
        "name": "float_enriched",
        "regex": re.compile(
            r"looking at the (.+?) specs.*My name is (.+?)\."
            r"\s*I am a (.+?) customer\."
            r"\s*My current engine is (.+?)\."
            r"(?:\s*Serial:\s*(.+?)\.)?",
            re.IGNORECASE | re.DOTALL
        ),
        "extracts": {
            "engine_model": 1,
            "customer_name": 2,
            "customer_type": 3,
            "current_engine": 4,
            "serial_number": 5
        },
        "branch": "engine_sales",
        "temperature": "hot"
    },
    {
        # Pattern 3: Engine detail — floating button (not logged in)
        "name": "float_basic",
        "regex": re.compile(
            r"looking at the (.+?) specs on the website",
            re.IGNORECASE
        ),
        "extracts": {"engine_model": 1},
        "branch": "engine_sales",
        "temperature": "warm"
    },
    {
        # Pattern 4: Homepage "Learn More" CTA — cold, no branch
        "name": "homepage_cta",
        "regex": re.compile(
            r"learn more about what you offer",
            re.IGNORECASE
        ),
        "extracts": {},
        "branch": "",
        "temperature": "cold"
    },
    {
        # Pattern 5: Engines page WhatsApp button
        "name": "engines_page",
        "regex": re.compile(
            r"interested in Yamaha outboard engines",
            re.IGNORECASE
        ),
        "extracts": {},
        "branch": "engine_sales",
        "temperature": "warm"
    },
    {
        # Pattern 6: Boats page — general CTA
        "name": "boats_page",
        "regex": re.compile(
            r"interested in buying a boat",
            re.IGNORECASE
        ),
        "extracts": {},
        "branch": "boat_sales",
        "temperature": "warm"
    },
    {
        # Pattern 7: Boats page — specific boat model (no "specs" keyword)
        "name": "boats_specific",
        "regex": re.compile(
            r"interested in the (.+?)(?:\s*\[|$)(?!.*\bspecs\b)",
            re.IGNORECASE
        ),
        "extracts": {"boat_model": 1},
        "branch": "boat_sales",
        "temperature": "warm"
    },
    {
        # Pattern 8: Service page CTA
        "name": "service_page",
        "regex": re.compile(
            r"need help with service",
            re.IGNORECASE
        ),
        "extracts": {},
        "branch": "service",
        "temperature": "warm"
    },
    {
        # Pattern 9: Parts page CTA
        "name": "parts_page",
        "regex": re.compile(
            r"help finding a specific Yamaha part",
            re.IGNORECASE
        ),
        "extracts": {},
        "branch": "parts_sales",
        "temperature": "warm"
    },
    {
        # Pattern 10: Accessories page CTA
        "name": "accessories_page",
        "regex": re.compile(
            r"looking for marine accessories",
            re.IGNORECASE
        ),
        "extracts": {},
        "branch": "general_accessories",
        "temperature": "warm"
    },
    {
        # Pattern 11: ATV page CTA
        "name": "atv_page",
        "regex": re.compile(
            r"interested in Yamaha ATVs?(?:\s*/\s*UTVs?)?",
            re.IGNORECASE
        ),
        "extracts": {},
        "branch": "atv_utv",
        "temperature": "warm"
    },
    {
        # Pattern 12: Accessories → Trailers prefill
        "name": "accessories_trailers",
        "regex": re.compile(r"interested in boat trailers", re.IGNORECASE),
        "extracts": {},
        "branch": "trailers",
        "temperature": "warm"
    },
    {
        # Pattern 13: Accessories → Electronics prefill
        "name": "accessories_electronics",
        "regex": re.compile(r"interested in (?:Garmin )?marine electronics", re.IGNORECASE),
        "extracts": {},
        "branch": "electronics",
        "temperature": "warm"
    },
    {
        # Pattern 14: Accessories → Fishing Gear prefill
        "name": "accessories_fishing",
        "regex": re.compile(r"interested in fishing gear", re.IGNORECASE),
        "extracts": {},
        "branch": "fishing_gear",
        "temperature": "warm"
    },
    {
        # Pattern 15: Accessories → General prefill
        "name": "accessories_general",
        "regex": re.compile(r"looking for general (?:boat )?accessories", re.IGNORECASE),
        "extracts": {},
        "branch": "general_accessories",
        "temperature": "warm"
    },
]

# Wave runner / PWC detection — banned from import in Jamaica
WAVE_RUNNER_PATTERN = re.compile(
    r"\b(wave\s*runner|jet\s*ski|pwc|personal\s*water\s*craft)\b",
    re.IGNORECASE
)

# Machine-readable YAMJA tag appended to website WhatsApp messages
# Format: [YAMJA:page=engine-detail,model=F115LB,family=F115B]
YAMJA_TAG_PATTERN = re.compile(r"\[yamja:([^\]]+)\]", re.IGNORECASE)

# Engine model extraction for contact form messages
ENGINE_MODEL_PATTERN = re.compile(
    r"(?:the\s+)?((?:F|LF|VF|FL)\d{2,3}[A-Z]{0,6})\b",
    re.IGNORECASE
)
ENGINE_FAMILY_PATTERN = re.compile(
    r"\((\w+(?:\s+\w+)?)\s*(?:family|series)?\)",
    re.IGNORECASE
)

# Contact form inquiry type → internal branch mapping
INQUIRY_TO_BRANCH = {
    "Engines":       "engine_sales",
    "Boats":         "boat_sales",
    "Parts":         "parts_sales",
    "Service":       "service",
    "Trailers":      "trailers",
    "Electronics":   "electronics",
    "ATVs":          "atv_utv",
    "Fishing":       "fishing_gear",
    "Accessories":   "general_accessories",
    "Other":         "general_inquiry"
}

# Human-readable branch display names for lead messages
BRANCH_DISPLAY = {
    "engine_sales":       "Engine Sales",
    "parts_sales":        "Parts & Accessories",
    "service":            "Service / Repair",
    "boat_sales":         "Boat Sales",
    "trailers":           "Trailers",
    "electronics":        "Marine Electronics",
    "atv_utv":            "ATV / UTV",
    "fishing_gear":       "Fishing Gear",
    "general_accessories":"General Accessories",
    "general_inquiry":    "General Inquiry",
    "wave_runner_banned": "Wave Runner (Banned Item)"
}

# Post-confirm website links by branch
BRANCH_LINKS = {
    "engine_sales":       "Browse engines: yamja.com/engines.html",
    "parts_sales":        "Look up parts: yamja.com/parts-lookup.html (login required)",
    "boat_sales":         "Browse boats: yamja.com/boats.html",
    "service":            "Service info: yamja.com/service.html",
}

# States where free-text input should be buffered (3-second window)
BUFFERED_STATES = {
    "branch_parts_b2a", "branch_parts_b2b", "branch_parts_b2c",
    "branch_engine_a2a", "branch_engine_a2b",
    "branch_boat_d2",
    "branch_trailer_e1",
    "branch_electronics_f2",
    "branch_atv_g2",
    "branch_fishing_h2",
    "branch_gen_acc_i1",
    "branch_general_j1",
    "branch_service_c2a", "branch_service_c2b", "branch_service_c2c",
    "branch_service_c3",
}

BUFFER_WINDOW_SECONDS = 3

# Background timers for buffer flushing (one per phone)
_buffer_timers: dict = {}  # phone -> threading.Timer
_buffer_timers_mutex = threading.Lock()


# ─── Intent Detection ─────────────────────────────────────────────────────────

INTENT_KEYWORDS = [
    # Order matters: more specific first
    (re.compile(r"\b(part|parts)\b", re.IGNORECASE),              "parts_sales"),
    (re.compile(r"\b(engine|motor|outboard)\b", re.IGNORECASE),   "engine_sales"),
    (re.compile(r"\b(service|repair|maintenance|fix)\b", re.IGNORECASE), "service"),
    (re.compile(r"\bboat\b", re.IGNORECASE),                       "boat_sales"),
    (re.compile(r"\btrailer\b", re.IGNORECASE),                    "trailers"),
    (re.compile(r"\b(atv|utv|quad)\b", re.IGNORECASE),            "atv_utv"),
    (re.compile(r"\b(fishing|lure|reel|rod)\b", re.IGNORECASE),   "fishing_gear"),
    (re.compile(r"accessor", re.IGNORECASE),                       "general_accessories"),
]


def detect_intent(text: str) -> str | None:
    """
    Keyword-based intent detection for the first cold message.
    Returns a branch string or None if no intent is found.
    """
    for pattern, branch in INTENT_KEYWORDS:
        if pattern.search(text):
            logger.info(f"Intent detected: '{branch}' from text: '{text[:80]}'")
            return branch
    return None


# ─── Temperature Detection ────────────────────────────────────────────────────

def detect_temperature(message_text: str) -> tuple[str, str, dict, str]:
    """
    Analyse the first message to determine temperature and extract fields.
    Returns (temperature, branch, extracted_fields, pattern_name).
    """
    # Parse YAMJA tag first — overrides extracted fields later
    tag_match = YAMJA_TAG_PATTERN.search(message_text)
    tag_data: dict = {}
    if tag_match:
        for pair in tag_match.group(1).split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                tag_data[k.strip().lower()] = v.strip()
        logger.info(f"YAMJA tag parsed: {tag_data}")

    # Check website patterns
    for pattern in WEBSITE_PATTERNS:
        match = pattern["regex"].search(message_text)
        if match:
            extracted: dict = {}
            for field_name, group_idx in pattern["extracts"].items():
                try:
                    val = match.group(group_idx)
                    if val:
                        extracted[field_name] = val.strip()
                except IndexError:
                    pass

            # YAMJA tag overrides
            if tag_data:
                if "model" in tag_data and tag_data["model"]:
                    extracted["engine_model"] = tag_data["model"]
                if "family" in tag_data and tag_data["family"]:
                    extracted["engine_family"] = tag_data["family"]
                if "page" in tag_data:
                    extracted["source_page"] = tag_data["page"]

            branch = pattern["branch"]
            # If tag has page=boats, override branch accordingly
            if tag_data.get("page") == "boats" and not branch:
                branch = "boat_sales"

            return pattern["temperature"], branch, extracted, pattern["name"]

    # No website pattern matched — cold
    return "cold", "", {}, "none"


def check_wave_runner(message_text: str) -> bool:
    """Return True if the message mentions wave runners / jet skis / PWC."""
    return bool(WAVE_RUNNER_PATTERN.search(message_text))


# ─── Conversation Object Factory ──────────────────────────────────────────────

def new_conversation(phone: str, country_code: str = "+1") -> dict:
    """Create a fresh conversation object for a phone number."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "phone": phone,
        "country_code": country_code,
        "temperature": "cold",
        "source_page": "direct",

        # Identity
        "customer_name": "",
        "email": "",
        "customer_type": "",
        "fisherman_id": "",

        # Routing
        "branch": "",
        "sub_branch": "",
        "confirmed": False,
        "pending_intent": "",      # Detected from first message
        "pending_details": "",     # First message text — pre-fill details
        "first_message": "",       # Customer's very first message

        # Engine fields
        "engine_model": "",
        "engine_family": "",
        "accessories_needed": "",
        "condition_preference": "",
        "use_case": "",
        "boat_size": "",

        # Parts fields
        "unit_info": "",
        "parts_list": "",
        "parts_description": "",

        # Service fields
        "service_type": "",
        "service_engine_model": "",
        "service_serial": "",
        "issue_description": "",
        "desired_engine": "",
        "boat_info": "",
        "service_location": "",
        "urgency": "",
        "last_service": "",

        # Boat fields
        "boat_condition": "",
        "boat_model": "",
        "boat_use": "",

        # Trailer fields
        "trailer_boat_info": "",

        # Electronics fields
        "electronics_brand": "",
        "electronics_details": "",

        # ATV/UTV fields
        "atv_type": "",
        "atv_model": "",
        "atv_use_case": "",
        "atv_passengers": "",
        "atv_details": "",

        # Fishing fields
        "fishing_brand": "",
        "fishing_details": "",

        # General Accessories
        "accessories_details": "",

        # General Inquiry
        "general_inquiry": "",
        "other_details": "",

        # Profile / website context
        "current_engine": "",
        "serial_number": "",
        "website_referral": False,
        "referred_model": "",
        "referred_family": "",
        "website_message": "",

        # Returning customer tracking
        "lead_forwarded_at": "",   # ISO timestamp when lead was sent to sales
        "escalated": False,

        # Message buffering (3-second window)
        "_buffer": [],
        "_buffer_started_at": 0.0,

        # Tracking
        "all_messages": [],
        "message_count": 0,
        "state": "init",
        "created_at": now,
        "last_message_at": now,
        "source": "whatsapp_cold_intake"
    }


# ─── Conversation Access (SQLite-backed) ─────────────────────────────────────

def get_or_create_conversation(phone: str) -> dict:
    """
    Load conversation from SQLite or create a new one.
    Does NOT auto-expire completed conversations — returning customer logic
    handles that explicitly.
    """
    convo = load_conversation(phone)
    if convo is None:
        convo = new_conversation(phone)
        logger.info(f"New conversation created for {phone}")
    else:
        logger.info(f"Loaded existing conversation for {phone}, state={convo.get('state')}")
    return convo


# ─── Interakt API Helpers ─────────────────────────────────────────────────────

def send_whatsapp_message(phone: str, message: str) -> bool:
    """Send a plain text WhatsApp message via Interakt Send API."""
    if not INTERAKT_API_KEY:
        logger.warning("INTERAKT_API_KEY not set — skipping WhatsApp text send")
        return False

    country_code, clean_phone = _format_phone_for_interakt(phone)
    url = "https://api.interakt.ai/v1/public/message/"
    headers = {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "countryCode": country_code,
        "phoneNumber": clean_phone,
        "callbackData": "yamaja_chatbot",
        "type": "Text",
        "data": {
            "message": message
        }
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"[WA TEXT] → {phone}: HTTP {resp.status_code} | {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"[WA TEXT] send failed for {phone}: {e}")
        return False


def send_whatsapp_buttons(phone: str, message: str, buttons: list[str]) -> bool:
    """
    Send a WhatsApp interactive button message via Interakt Send API.
    Maximum 3 buttons; each button title truncated to 20 characters.
    """
    if not INTERAKT_API_KEY:
        logger.warning("INTERAKT_API_KEY not set — skipping button send")
        return False

    country_code, clean_phone = _format_phone_for_interakt(phone)
    url = "https://api.interakt.ai/v1/public/message/"
    headers = {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json"
    }

    button_list = []
    for i, btn_text in enumerate(buttons[:3]):   # WhatsApp max 3 buttons
        button_list.append({
            "type": "reply",
            "reply": {
                "id": f"btn_{i}",
                "title": btn_text[:20]
            }
        })

    payload = {
        "countryCode": country_code,
        "phoneNumber": clean_phone,
        "callbackData": "yamaja_chatbot",
        "type": "Interactive",
        "data": {
            "interactive": {
                "type": "button",
                "body": {"text": message},
                "action": {"buttons": button_list}
            }
        }
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"[WA BTN] → {phone}: HTTP {resp.status_code} | btns={buttons}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"[WA BTN] send failed for {phone}: {e}")
        return False


def send_whatsapp_list(phone: str, message: str, list_title: str,
                       items: list[str]) -> bool:
    """
    Send a WhatsApp list message via Interakt Send API.
    Row titles truncated to 24 characters.
    """
    if not INTERAKT_API_KEY:
        logger.warning("INTERAKT_API_KEY not set — skipping list send")
        return False

    country_code, clean_phone = _format_phone_for_interakt(phone)
    url = "https://api.interakt.ai/v1/public/message/"
    headers = {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json"
    }

    rows = [
        {"id": f"list_{i}", "title": item[:24]}
        for i, item in enumerate(items)
    ]

    payload = {
        "countryCode": country_code,
        "phoneNumber": clean_phone,
        "callbackData": "yamaja_chatbot",
        "type": "Interactive",
        "data": {
            "interactive": {
                "type": "list",
                "body": {"text": message},
                "action": {
                    "button": list_title[:20],
                    "sections": [{"title": "Options", "rows": rows}]
                }
            }
        }
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"[WA LIST] → {phone}: HTTP {resp.status_code} | items={items}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"[WA LIST] send failed for {phone}: {e}")
        return False


# ─── Lead Forwarding ──────────────────────────────────────────────────────────

def forward_lead_to_whatsapp(convo: dict) -> bool:
    """
    Forward a confirmed lead to SALES_PHONE as a formatted WhatsApp text message
    via the Interakt Send API.

    Replaces forward_to_make() as the primary lead forwarding method.
    """
    phone        = convo.get("phone", "")
    name         = convo.get("customer_name", "Unknown")
    branch       = convo.get("branch", "")
    temperature  = convo.get("temperature", "cold")
    fisherman_id = convo.get("fisherman_id", "") or "N/A"
    first_msg    = convo.get("first_message", "")
    timestamp    = convo.get("last_message_at", datetime.now(timezone.utc).isoformat())

    branch_display = BRANCH_DISPLAY.get(branch, branch.replace("_", " ").title())

    # Build branch-specific details block
    details_lines = []

    if branch == "engine_sales":
        if convo.get("engine_model"):
            details_lines.append(f"Model: {convo['engine_model']}")
        if convo.get("engine_family"):
            details_lines.append(f"Family: {convo['engine_family']}")
        if convo.get("condition_preference"):
            details_lines.append(f"Condition: {convo['condition_preference']}")
        if convo.get("use_case"):
            details_lines.append(f"Use: {convo['use_case']}")
        if convo.get("boat_size"):
            details_lines.append(f"Boat Size: {convo['boat_size']}")
        if convo.get("accessories_needed"):
            details_lines.append(f"Accessories: {convo['accessories_needed']}")
        if convo.get("current_engine"):
            details_lines.append(f"Current Engine: {convo['current_engine']}")
        if convo.get("serial_number"):
            details_lines.append(f"Serial: {convo['serial_number']}")

    elif branch == "parts_sales":
        if convo.get("unit_info"):
            details_lines.append(f"Engine Info: {convo['unit_info']}")
        if convo.get("parts_list"):
            details_lines.append(f"Part Numbers: {convo['parts_list']}")
        if convo.get("parts_description"):
            details_lines.append(f"Description: {convo['parts_description']}")
        if convo.get("sub_branch"):
            details_lines.append(f"Request Type: {convo['sub_branch']}")

    elif branch == "service":
        if convo.get("service_type"):
            details_lines.append(f"Service Type: {convo['service_type']}")
        if convo.get("service_engine_model"):
            details_lines.append(f"Engine: {convo['service_engine_model']}")
        if convo.get("service_serial"):
            details_lines.append(f"Serial: {convo['service_serial']}")
        if convo.get("issue_description"):
            details_lines.append(f"Issue: {convo['issue_description']}")
        if convo.get("desired_engine"):
            details_lines.append(f"Desired Engine: {convo['desired_engine']}")
        if convo.get("service_location"):
            details_lines.append(f"Location: {convo['service_location']}")
        if convo.get("urgency"):
            details_lines.append(f"Urgency: {convo['urgency']}")

    elif branch == "boat_sales":
        if convo.get("boat_model"):
            details_lines.append(f"Model: {convo['boat_model']}")
        if convo.get("boat_condition"):
            details_lines.append(f"Condition Pref: {convo['boat_condition']}")
        if convo.get("boat_use"):
            details_lines.append(f"Use: {convo['boat_use']}")

    elif branch == "trailers":
        if convo.get("trailer_boat_info"):
            details_lines.append(f"Boat Info: {convo['trailer_boat_info']}")

    elif branch == "electronics":
        if convo.get("electronics_brand"):
            details_lines.append(f"Brand: {convo['electronics_brand']}")
        if convo.get("electronics_details"):
            details_lines.append(f"Details: {convo['electronics_details']}")

    elif branch == "atv_utv":
        if convo.get("atv_type"):
            details_lines.append(f"Type: {convo['atv_type']}")
        if convo.get("atv_use_case"):
            details_lines.append(f"Use: {convo['atv_use_case']}")
        if convo.get("atv_model"):
            details_lines.append(f"Model: {convo['atv_model']}")
        if convo.get("condition_preference"):
            details_lines.append(f"Condition: {convo['condition_preference']}")

    elif branch == "fishing_gear":
        if convo.get("fishing_brand"):
            details_lines.append(f"Brand/Category: {convo['fishing_brand']}")
        if convo.get("fishing_details"):
            details_lines.append(f"Details: {convo['fishing_details']}")

    elif branch == "general_accessories":
        if convo.get("accessories_details"):
            details_lines.append(f"Details: {convo['accessories_details']}")

    elif branch in ("general_inquiry", "wave_runner_banned"):
        if convo.get("general_inquiry"):
            details_lines.append(f"Request: {convo['general_inquiry']}")

    if convo.get("website_message"):
        details_lines.append(f"Website Message: {convo['website_message'][:200]}")

    details_block = "\n".join(details_lines) if details_lines else "No additional details"

    message = (
        "📨 NEW LEAD FROM CLAUDIA\n\n"
        f"👤 Name: {name}\n"
        f"📞 Phone: {phone}\n"
        f"🎯 Category: {branch_display}\n"
        f"🌡️ Temperature: {temperature}\n"
        f"🔹 Fisherman ID: {fisherman_id}\n\n"
        f"💬 First Message: {first_msg}\n\n"
        f"📋 Details:\n{details_block}\n\n"
        f"📱 Reply to customer: wa.me/{phone}\n"
        f"⏰ Received: {timestamp}"
    )

    logger.info(f"Forwarding lead for {phone} to SALES_PHONE {SALES_PHONE}")
    return send_whatsapp_message(SALES_PHONE, message)


# ─── Make.com Forwarding (DISABLED — kept for future use) ────────────────────

# def forward_to_make(convo: dict) -> bool:
#     """Forward a confirmed lead to Make.com webhook (disabled in v4)."""
#     if not MAKE_WEBHOOK_URL:
#         logger.warning("MAKE_WEBHOOK_URL not set — skipping Make.com forward")
#         return False
#
#     payload = {
#         "lead_type":       "whatsapp",
#         "temperature":     convo.get("temperature"),
#         "source_page":     convo.get("source_page"),
#         "customer_name":   convo.get("customer_name"),
#         "email":           convo.get("email"),
#         "phone":           convo.get("phone"),
#         "country_code":    convo.get("country_code"),
#         "customer_type":   convo.get("customer_type"),
#         "fisherman_id":    convo.get("fisherman_id"),
#         "branch":          convo.get("branch"),
#         "sub_branch":      convo.get("sub_branch"),
#         "engine_model":    convo.get("engine_model"),
#         "engine_family":   convo.get("engine_family"),
#         "current_engine":  convo.get("current_engine"),
#         "serial_number":   convo.get("serial_number"),
#         "condition_preference": convo.get("condition_preference"),
#         "use_case":        convo.get("use_case"),
#         "boat_size":       convo.get("boat_size"),
#         "accessories_needed": convo.get("accessories_needed"),
#         "unit_info":       convo.get("unit_info"),
#         "parts_list":      convo.get("parts_list"),
#         "parts_description": convo.get("parts_description"),
#         "service_type":    convo.get("service_type"),
#         "service_engine_model": convo.get("service_engine_model"),
#         "service_serial":  convo.get("service_serial"),
#         "issue_description": convo.get("issue_description"),
#         "desired_engine":  convo.get("desired_engine"),
#         "boat_info":       convo.get("boat_info"),
#         "service_location": convo.get("service_location"),
#         "urgency":         convo.get("urgency"),
#         "last_service":    convo.get("last_service"),
#         "boat_condition":  convo.get("boat_condition"),
#         "boat_use":        convo.get("boat_use"),
#         "trailer_boat_info": convo.get("trailer_boat_info"),
#         "electronics_brand": convo.get("electronics_brand"),
#         "electronics_details": convo.get("electronics_details"),
#         "atv_type":        convo.get("atv_type"),
#         "atv_model":       convo.get("atv_model"),
#         "atv_use_case":    convo.get("atv_use_case"),
#         "atv_passengers":  convo.get("atv_passengers"),
#         "atv_details":     convo.get("atv_details"),
#         "fishing_brand":   convo.get("fishing_brand"),
#         "fishing_details": convo.get("fishing_details"),
#         "accessories_details": convo.get("accessories_details"),
#         "general_inquiry": convo.get("general_inquiry"),
#         "other_details":   convo.get("other_details"),
#         "website_message": convo.get("website_message"),
#         "first_message":   convo.get("first_message"),
#         "all_messages":    " | ".join(convo.get("all_messages", [])[-20:]),
#         "message_count":   convo.get("message_count"),
#         "confirmed":       convo.get("confirmed"),
#         "source":          convo.get("source"),
#         "received_at":     convo.get("created_at")
#     }
#
#     try:
#         resp = requests.post(MAKE_WEBHOOK_URL, json=payload, timeout=15)
#         logger.info(f"Make.com forward for {convo['phone']}: {resp.status_code}")
#         return resp.status_code == 200
#     except Exception as e:
#         logger.error(f"Make.com forward failed: {e}")
#         return False


# ─── Manager Escalation ───────────────────────────────────────────────────────

def escalate_to_manager(convo: dict) -> bool:
    """
    Send an escalation alert to MANAGER_PHONE when a customer reports
    no follow-up after >= 4 hours.
    """
    name      = convo.get("customer_name", "Unknown")
    phone     = convo.get("phone", "")
    branch    = BRANCH_DISPLAY.get(convo.get("branch", ""), convo.get("branch", ""))
    first_msg = convo.get("first_message", "")
    forwarded = convo.get("lead_forwarded_at", "")
    now       = datetime.now(timezone.utc)

    # Calculate how long since lead was forwarded
    wait_str = "Unknown time"
    if forwarded:
        try:
            forwarded_dt = datetime.fromisoformat(forwarded)
            delta = now - forwarded_dt
            hours   = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            wait_str = f"{hours}h {minutes}m"
        except Exception:
            pass

    message = (
        f"⚠️ ESCALATION — Customer waiting {wait_str}\n\n"
        f"👤 {name} ({phone})\n"
        f"🎯 {branch}\n"
        f"💬 Original: {first_msg}\n"
        f"📱 wa.me/{phone}\n\n"
        f"Lead was forwarded at {forwarded}. Customer reports no follow-up."
    )

    logger.info(f"Escalating {phone} to MANAGER_PHONE {MANAGER_PHONE}")
    return send_whatsapp_message(MANAGER_PHONE, message)


# ─── Summary Builder ──────────────────────────────────────────────────────────

def build_summary(convo: dict) -> str:
    """Build a human-readable summary of the conversation data for confirmation."""
    branch = convo.get("branch", "")
    lines  = []

    if branch == "engine_sales":
        lines.append("Here's your engine inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Model: {convo['engine_model'] or 'Help me choose'}")
        lines.append(f"🔹 Condition: {convo['condition_preference'] or 'Not specified'}")
        if convo.get("use_case"):
            lines.append(f"🔹 Use: {convo['use_case']}")
        if convo.get("boat_size"):
            lines.append(f"🔹 Boat Size: {convo['boat_size']}")
        if convo.get("accessories_needed"):
            lines.append(f"🔹 Accessories: {convo['accessories_needed']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "parts_sales":
        lines.append("Here's your parts inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Request Type: {convo.get('sub_branch', 'Other')}")
        if convo.get("unit_info"):
            lines.append(f"🔹 Engine Info: {convo['unit_info']}")
        if convo.get("parts_list"):
            lines.append(f"🔹 Part Numbers: {convo['parts_list']}")
        if convo.get("parts_description"):
            lines.append(f"🔹 Description: {convo['parts_description']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "service":
        lines.append("Your service request:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Service Type: {convo.get('service_type', 'Not specified')}")
        if convo.get("service_engine_model"):
            lines.append(f"🔹 Engine: {convo['service_engine_model']}")
        if convo.get("service_serial"):
            lines.append(f"🔹 Serial: {convo['service_serial']}")
        if convo.get("issue_description"):
            lines.append(f"🔹 Issue: {convo['issue_description']}")
        if convo.get("desired_engine"):
            lines.append(f"🔹 Desired Engine: {convo['desired_engine']}")
        if convo.get("service_location"):
            lines.append(f"🔹 Location: {convo['service_location']}")
        if convo.get("urgency"):
            lines.append(f"🔹 Urgency: {convo['urgency']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "boat_sales":
        lines.append("Your boat inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        if convo.get("boat_model"):
            lines.append(f"🔹 Model: {convo['boat_model']}")
        lines.append(f"🔹 Condition Pref: {convo.get('boat_condition', 'Not specified')}")
        lines.append(f"🔹 Usage: {convo.get('boat_use', 'Not specified')}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "trailers":
        lines.append("Your trailer inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Boat Info: {convo.get('trailer_boat_info', 'Not specified')}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "electronics":
        lines.append("Your electronics inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Brand: {convo.get('electronics_brand', 'Not specified')}")
        lines.append(f"🔹 Details: {convo.get('electronics_details', 'Not specified')}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "atv_utv":
        lines.append("Your ATV/UTV inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Type: {convo.get('atv_type', 'Not specified')}")
        lines.append(f"🔹 Use: {convo.get('atv_use_case', 'Not specified')}")
        if convo.get("atv_model"):
            lines.append(f"🔹 Model: {convo['atv_model']}")
        lines.append(f"🔹 Condition: {convo.get('condition_preference', 'Not specified')}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "fishing_gear":
        lines.append("Your fishing gear inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Category: {convo.get('fishing_brand', 'Not specified')}")
        lines.append(f"🔹 Details: {convo.get('fishing_details', 'Not specified')}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "general_accessories":
        lines.append("Your accessories inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Details: {convo.get('accessories_details', 'Not specified')}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "general_inquiry":
        lines.append("Here's what I have:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Request: {convo.get('general_inquiry', 'Not specified')}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    else:
        lines.append("Your inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        if convo.get("general_inquiry"):
            lines.append(f"🔹 Details: {convo['general_inquiry']}")

    return "\n".join(lines)


# ─── Post-Confirm Flow ────────────────────────────────────────────────────────

def handle_post_confirm(convo: dict):
    """
    Steps after the customer confirms their lead (Yes, Send It):
    1. Forward lead via WhatsApp to SALES_PHONE.
    2. Send confirmation message with timeframe.
    3. Send category-specific website link.
    4. Mark state as 'completed'.
    """
    phone  = convo["phone"]
    name   = convo.get("customer_name", "there")
    branch = convo.get("branch", "")

    # 1. Forward lead to sales
    forward_lead_to_whatsapp(convo)
    convo["lead_forwarded_at"] = datetime.now(timezone.utc).isoformat()
    convo["confirmed"] = True
    convo["state"] = "completed"

    # 2. Confirmation message
    send_whatsapp_message(
        phone,
        f"Your inquiry has been forwarded to a sales representative who will be "
        f"in touch shortly. If you don't hear from us within a few hours, come "
        f"back here and let us know — we'll escalate right away."
    )

    # 3. Category-specific website link
    link_line = BRANCH_LINKS.get(branch, "Visit us: yamja.com")
    send_whatsapp_message(
        phone,
        f"In the meantime, feel free to explore more on our website at yamja.com\n\n"
        f"{link_line}"
    )

    logger.info(f"Lead confirmed and forwarded for {phone}, branch={branch}")


# ─── Returning Customer Flow ──────────────────────────────────────────────────

def handle_returning_customer(convo: dict, message_text: str) -> bool:
    """
    Handle messages from customers whose state == 'completed'.
    Returns True if handled (caller should not do further processing).
    """
    phone       = convo["phone"]
    name        = convo.get("customer_name", "there")
    branch      = convo.get("branch", "")
    branch_disp = BRANCH_DISPLAY.get(branch, branch.replace("_", " ").title())
    text_lower  = message_text.strip().lower()

    lead_forwarded_at = convo.get("lead_forwarded_at", "")
    now = datetime.now(timezone.utc)

    # Calculate time since forwarded
    hours_elapsed = 999  # default to > 4h if unknown
    forwarded_dt  = None
    if lead_forwarded_at:
        try:
            forwarded_dt  = datetime.fromisoformat(lead_forwarded_at)
            delta         = now - forwarded_dt
            hours_elapsed = delta.total_seconds() / 3600
        except Exception:
            pass

    state = convo.get("state")

    # ── Sub-state: awaiting response to "Did our team reach out?"
    if state == "completed_awaiting_follow_up":
        if any(w in text_lower for w in ["yes", "good", "great", "got back", "they called",
                                          "yeah", "yep", "all good"]):
            send_whatsapp_buttons(
                phone,
                f"Wonderful! Glad our team was able to help you, {name}. "
                "Is there anything else I can assist you with?",
                ["Start New Inquiry", "That's All, Thanks"]
            )
            convo["state"] = "completed_new_or_done"
            save_conversation(convo)
            return True

        elif any(w in text_lower for w in ["no", "nope", "nah", "still waiting", "haven't heard",
                                            "not yet", "nobody", "no response", "no contact"]):
            # Escalate to manager
            if not convo.get("escalated"):
                escalate_to_manager(convo)
                convo["escalated"] = True

            send_whatsapp_message(
                phone,
                f"I'm sorry to hear that, {name}. I've escalated this to a manager "
                f"who will reach out to you directly via WhatsApp or phone. "
                f"Thank you for your patience!"
            )
            convo["state"] = "completed"
            save_conversation(convo)
            return True

        # They typed something else — treat as ambiguous, ask again
        send_whatsapp_buttons(
            phone,
            f"Did our team get back to you about your {branch_disp} inquiry?",
            ["Yes, All Good", "No, Still Waiting"]
        )
        save_conversation(convo)
        return True

    # ── Sub-state: new inquiry or done
    if state == "completed_new_or_done":
        if any(w in text_lower for w in ["new", "start", "another", "inquiry",
                                          "yes", "yeah", "more"]):
            _start_new_inquiry(convo)
            return True
        else:
            send_whatsapp_message(
                phone,
                f"Thanks for reaching out to Yamaja Engines, {name}! "
                "Have a great day. 🙌"
            )
            save_conversation(convo)
            return True

    # ── Main returning customer entry point
    # Check if customer is complaining about no response
    no_response_keywords = ["no response", "still waiting", "haven't heard",
                            "no call", "nobody", "not reached", "waiting", "hours"]
    if any(kw in text_lower for kw in no_response_keywords):
        if forwarded_dt:
            expires_at = forwarded_dt + timedelta(hours=4)
            expires_str = expires_at.strftime("%I:%M %p")
            minutes_left = max(0, int((expires_at - now).total_seconds() / 60))

            if hours_elapsed < 4:
                elapsed_min = int(hours_elapsed * 60)
                elapsed_str = f"{elapsed_min} minute{'s' if elapsed_min != 1 else ''}" \
                               if elapsed_min < 60 else \
                               f"{int(hours_elapsed)}h {int((hours_elapsed % 1) * 60)}m"
                send_whatsapp_message(
                    phone,
                    f"Your inquiry was forwarded {elapsed_str} ago. "
                    f"Your 4-hour window expires at {expires_str}. "
                    f"If you haven't heard by then, come back and we'll escalate "
                    f"this to a manager right away."
                )
                send_whatsapp_buttons(
                    phone,
                    "What would you like to do in the meantime?",
                    ["Start New Inquiry", "That's All, Thanks"]
                )
                convo["state"] = "completed_new_or_done"
                save_conversation(convo)
                return True

    # ── Less than 4 hours since forwarded
    if hours_elapsed < 4 and forwarded_dt:
        elapsed_min = int(hours_elapsed * 60)
        elapsed_str = f"{elapsed_min} minute{'s' if elapsed_min != 1 else ''}" \
                       if elapsed_min < 60 else \
                       f"{int(hours_elapsed)}h {int((hours_elapsed % 1) * 60)}m"

        send_whatsapp_message(
            phone,
            f"Welcome back, {name}! Your inquiry was forwarded {elapsed_str} ago. "
            "Our team should be reaching out soon."
        )
        send_whatsapp_buttons(
            phone,
            "What would you like to do?",
            ["Start New Inquiry", "That's All, Thanks"]
        )
        convo["state"] = "completed_new_or_done"
        save_conversation(convo)
        return True

    # ── 4+ hours since forwarded — ask if team followed up
    send_whatsapp_message(
        phone,
        f"Welcome back, {name}! Did our team get back to you about your "
        f"{branch_disp} inquiry?"
    )
    send_whatsapp_buttons(
        phone,
        "Please let us know:",
        ["Yes, All Good", "No, Still Waiting"]
    )
    convo["state"] = "completed_awaiting_follow_up"
    save_conversation(convo)
    return True


def _start_new_inquiry(convo: dict):
    """Reset conversation for a new inquiry, preserving name and fisherman ID."""
    phone     = convo["phone"]
    name      = convo.get("customer_name", "")
    fid       = convo.get("fisherman_id", "")
    temp      = convo.get("temperature", "cold")

    fresh = new_conversation(phone)
    fresh["customer_name"] = name
    fresh["fisherman_id"]  = fid
    fresh["temperature"]   = temp
    fresh["state"]         = "awaiting_menu_1"
    # Copy conversation to dict (saves by reference when passed out)
    convo.clear()
    convo.update(fresh)

    send_whatsapp_list(
        phone,
        f"Of course, {name}! What are you looking for today?",
        "Choose One",
        ["Engines", "Parts", "Service", "Something Else"]
    )
    save_conversation(convo)


# ─── Message Batching Helpers ─────────────────────────────────────────────────

# Note: Interactive detection is handled via the is_interactive flag returned
# by parse_interakt_message() and passed through the processing pipeline.


def _flush_buffer(convo: dict) -> str | None:
    """
    If there is a pending buffer older than BUFFER_WINDOW_SECONDS, flush and
    return the concatenated text. Otherwise return None.
    """
    buf = convo.get("_buffer", [])
    buf_started = convo.get("_buffer_started_at", 0.0)

    if not buf:
        return None

    if (time.time() - buf_started) >= BUFFER_WINDOW_SECONDS:
        combined = " ".join(buf)
        convo["_buffer"] = []
        convo["_buffer_started_at"] = 0.0
        logger.info(f"Buffer flushed for {convo['phone']}: '{combined[:100]}'")
        return combined

    return None  # Buffer not ready yet


def _add_to_buffer(convo: dict, text: str):
    """Append text to the conversation's pending buffer."""
    buf = convo.setdefault("_buffer", [])
    if not buf:
        convo["_buffer_started_at"] = time.time()
    buf.append(text.strip())
    logger.info(f"Buffered message for {convo['phone']}: '{text[:60]}' "
                f"(buffer size: {len(buf)})")


def _schedule_buffer_flush(phone: str):
    """
    Schedule a background timer to flush the message buffer after BUFFER_WINDOW_SECONDS.
    If a timer already exists for this phone, cancel it and create a new one
    (debounce pattern — resets the window on each new message).
    """
    with _buffer_timers_mutex:
        existing = _buffer_timers.get(phone)
        if existing:
            existing.cancel()

        def _do_flush():
            logger.info(f"Timer-triggered buffer flush for {phone}")
            phone_lock = _get_phone_lock(phone)
            if not phone_lock.acquire(timeout=5):
                logger.warning(f"Timer flush: could not acquire lock for {phone}")
                return
            try:
                convo = load_conversation(phone)
                if not convo:
                    return
                flushed = _flush_buffer(convo)
                if flushed:
                    logger.info(f"Timer flush processing for {phone}: '{flushed[:80]}'")
                    _route_message(convo, flushed)
                    save_conversation(convo)
            except Exception as e:
                logger.error(f"Timer flush error for {phone}: {e}")
            finally:
                phone_lock.release()
                with _buffer_timers_mutex:
                    _buffer_timers.pop(phone, None)

        timer = threading.Timer(BUFFER_WINDOW_SECONDS + 0.5, _do_flush)
        timer.daemon = True
        _buffer_timers[phone] = timer
        timer.start()


# ─── Interactive Message Parsing ─────────────────────────────────────────────

def parse_interakt_message(message_obj: dict) -> tuple[str, bool]:
    """
    Extract the actual message text from an Interakt message object.

    Interakt sends button/list replies as JSON strings in various fields.
    Returns (text, is_interactive) where is_interactive=True means it was
    a button/list tap, not free text.
    """
    message_text = ""
    is_interactive = False

    # 1. Check dedicated 'interactive' sub-object
    if "interactive" in message_obj:
        interactive = message_obj["interactive"]
        if "button_reply" in interactive:
            message_text = interactive["button_reply"].get("title", "")
            is_interactive = True
        elif "list_reply" in interactive:
            message_text = interactive["list_reply"].get("title", "")
            is_interactive = True
        if message_text:
            logger.info(f"Interactive reply extracted (interactive obj): '{message_text}'")
            return message_text, is_interactive

    # 2. Check 'message' field — Interakt sometimes puts JSON here for interactive replies
    raw_message = message_obj.get("message", "")
    if isinstance(raw_message, str) and raw_message.strip().startswith("{"):
        try:
            parsed = json.loads(raw_message)
            # Button reply format
            if parsed.get("type") == "button_reply":
                message_text = parsed.get("title", "") or \
                               (parsed.get("button_reply") or {}).get("title", "")
                is_interactive = True
            elif parsed.get("type") == "list_reply":
                message_text = parsed.get("title", "") or \
                               (parsed.get("list_reply") or {}).get("title", "")
                is_interactive = True
            # Interakt nested format: {"type": "interactive", "interactive": {...}}
            elif parsed.get("type") == "interactive":
                inner = parsed.get("interactive", {})
                if "button_reply" in inner:
                    message_text = inner["button_reply"].get("title", "")
                    is_interactive = True
                elif "list_reply" in inner:
                    message_text = inner["list_reply"].get("title", "")
                    is_interactive = True
            if message_text:
                logger.info(f"Interactive reply extracted (message JSON): '{message_text}'")
                return message_text, is_interactive
        except (json.JSONDecodeError, AttributeError):
            pass

    # 3. Check standard text field
    if "text" in message_obj:
        val = message_obj["text"]
        if isinstance(val, str) and val.strip():
            return val.strip(), False
        # Sometimes Interakt wraps text: {"text": {"body": "..."}}
        if isinstance(val, dict) and val.get("body"):
            return val["body"].strip(), False

    # 4. Check "Initial Message" (Interakt's own field name)
    if "Initial Message" in message_obj:
        val = message_obj["Initial Message"]
        if isinstance(val, str) and val.strip():
            return val.strip(), False

    # 5. Check plain 'message' field (string, non-JSON)
    if raw_message and isinstance(raw_message, str) and not raw_message.startswith("{"):
        return raw_message.strip(), False

    # 6. Last-resort scan of all string values
    skip_fields = {
        "id", "country_code", "campaign_name", "raw_template",
        "channel_failure_reason", "message_status", "chat_message_type",
        "_internal_lead_source", "source_url", "campaign_id", "type",
        "timestamp", "from"
    }
    for key, val in message_obj.items():
        if key in skip_fields:
            continue
        if isinstance(val, str) and 0 < len(val.strip()) < 500:
            logger.info(f"Message extracted via last-resort scan, field='{key}': '{val[:80]}'")
            return val.strip(), False

    return "", False


# ─── Cold Conversation Flow ───────────────────────────────────────────────────

def process_cold_message(convo: dict, message_text: str):
    """Handle messages for a COLD conversation (full intake tree)."""
    state     = convo["state"]
    phone     = convo["phone"]
    text_lower = message_text.strip().lower()

    # ── init: Welcome, check traits.name or ask for name ──────────────────────
    if state == "init":
        # Check if Interakt has a name in traits (set during create_conversation)
        traits_name = convo.get("_traits_name", "")
        if traits_name:
            first_name = traits_name.split()[0]
            convo["state"] = "awaiting_name_confirm"
            convo["_proposed_name"] = traits_name
            send_whatsapp_buttons(
                phone,
                f"Hi *{traits_name}*! Can I call you {first_name}, or would you "
                "prefer a different name?",
                [f"Yes, {first_name}"[:20], "Different Name"]
            )
        else:
            convo["state"] = "awaiting_name"
            send_whatsapp_message(
                phone,
                "Welcome to Yamaja Engines Ltd! 🛥️ I'm Claudia, your AI assistant. "
                "Could you please provide your full name?"
            )
        return

    # ── awaiting_name_confirm: Traits name presented, waiting for confirmation ─
    elif state == "awaiting_name_confirm":
        proposed = convo.get("_proposed_name", "")
        if ("different" in text_lower or "no" in text_lower or
                "wrong" in text_lower or "change" in text_lower):
            convo["state"] = "awaiting_name"
            send_whatsapp_message(phone, "No problem! What would you like me to call you?")
        else:
            # Accept the proposed name (or typed confirmation)
            convo["customer_name"] = proposed
            _proceed_after_name(convo)
        return

    # ── awaiting_name: User types their name ──────────────────────────────────
    elif state == "awaiting_name":
        entered_name = message_text.strip()
        if not entered_name or len(entered_name) < 2:
            send_whatsapp_message(phone, "Could you please share your name?")
            return
        convo["_proposed_name"] = entered_name
        first_word = entered_name.split()[0]
        convo["state"] = "awaiting_name_verify"
        send_whatsapp_buttons(
            phone,
            f"Just to confirm — shall I call you *{first_word}*?",
            [f"Yes, {first_word}"[:20], "That's Wrong"]
        )
        return

    # ── awaiting_name_verify: Confirm the typed name ──────────────────────────
    elif state == "awaiting_name_verify":
        if ("wrong" in text_lower or "no" in text_lower or
                "different" in text_lower or "change" in text_lower):
            convo["state"] = "awaiting_name"
            send_whatsapp_message(phone, "Sorry about that! What should I call you?")
        else:
            convo["customer_name"] = convo.get("_proposed_name", message_text.strip())
            _proceed_after_name(convo)
        return

    # ── awaiting_fisherman_id: Ask if they have a fisherman ID ────────────────
    elif state == "awaiting_fisherman_id":
        # Normalise typed yes/no to button values
        if text_lower in ("yes", "yeah", "yep", "yup", "ye", "y", "i have one",
                          "yes i have one", "yes, i have one"):
            convo["state"] = "awaiting_fisherman_id_value"
            send_whatsapp_message(phone, "Please type your Fisherman ID number:")
        elif text_lower in ("no", "nah", "nope", "none", "don't have", "i don't have",
                            "i don't have one", "no fisherman id", "no id"):
            convo["fisherman_id"] = ""
            _proceed_after_fisherman_id(convo)
        elif any(c.isdigit() for c in text_lower):
            # Looks like they typed an ID number directly
            convo["fisherman_id"] = message_text.strip()
            logger.info(f"Fisherman ID accepted directly: {convo['fisherman_id']}")
            _proceed_after_fisherman_id(convo)
        else:
            # Re-show the button prompt
            send_whatsapp_buttons(
                phone,
                "Do you have a Fisherman ID? (It helps us apply tax incentives for "
                "registered fishermen.)",
                ["Yes, I Have One", "No"]
            )
        return

    # ── awaiting_fisherman_id_value: User is typing their ID ─────────────────
    elif state == "awaiting_fisherman_id_value":
        convo["fisherman_id"] = message_text.strip()
        logger.info(f"Fisherman ID collected: {convo['fisherman_id']}")
        _proceed_after_fisherman_id(convo)
        return

    # ── awaiting_menu_1: Engines / Parts / Service / Something Else ───────────
    elif state == "awaiting_menu_1":
        if "engine" in text_lower:
            _go_engine_sales(convo)
        elif "part" in text_lower:
            _go_parts_sales(convo)
        elif "service" in text_lower or "repair" in text_lower or "maintenance" in text_lower:
            _go_service(convo)
        else:
            # Something Else → Menu 2
            convo["state"] = "awaiting_menu_2"
            send_whatsapp_list(
                phone,
                "No problem! What about these?",
                "Choose One",
                ["Boats", "Trailers", "Electronics", "Something Else"]
            )
        return

    # ── awaiting_menu_2: Boats / Trailers / Electronics / Something Else ──────
    elif state == "awaiting_menu_2":
        if "boat" in text_lower:
            _go_boat_sales(convo)
        elif "trailer" in text_lower:
            _go_trailers(convo)
        elif "electronic" in text_lower or "garmin" in text_lower or "audio" in text_lower:
            _go_electronics(convo)
        else:
            convo["state"] = "awaiting_menu_3"
            send_whatsapp_list(
                phone,
                "We've got more! Looking for any of these?",
                "Choose One",
                ["ATVs & UTVs", "Fishing Gear", "General Accessories", "Something Else"]
            )
        return

    # ── awaiting_menu_3: ATVs / Fishing / Accessories / Something Else ────────
    elif state == "awaiting_menu_3":
        if "atv" in text_lower or "utv" in text_lower:
            _go_atv_utv(convo)
        elif "fish" in text_lower or "lure" in text_lower:
            _go_fishing_gear(convo)
        elif "accessor" in text_lower:
            _go_gen_accessories(convo)
        else:
            _go_general_inquiry(convo)
        return

    # ── Branch states ─────────────────────────────────────────────────────────
    # ENGINE SALES (Branch A)
    elif state == "branch_engine_a1":
        if any(w in text_lower for w in ["know", "want", "specific", "model"]):
            convo["state"] = "branch_engine_a2a"
            send_whatsapp_message(
                phone,
                "What model are you looking for? Also, will you need any additional "
                "accessories with the engine? I can forward details to a sales "
                "representative as soon as we have everything."
            )
        else:
            convo["state"] = "branch_engine_a2b"
            send_whatsapp_message(
                phone,
                "No problem! What will the engine be used for — commercial fishing, "
                "pleasure boating, or something else? And what size boat will it be "
                "going on (approximate length in feet)?"
            )
        return

    elif state == "branch_engine_a2a":
        convo["engine_model"] = message_text.strip()
        convo["state"] = "branch_engine_a3"
        send_whatsapp_buttons(
            phone,
            "Are you looking for brand new, pre-owned, or open to either?",
            ["New", "Pre-Owned", "Either"]
        )
        return

    elif state == "branch_engine_a2b":
        convo["use_case"] = message_text.strip()
        convo["state"] = "branch_engine_a3"
        send_whatsapp_buttons(
            phone,
            "Are you looking for brand new, pre-owned, or open to either?",
            ["New", "Pre-Owned", "Either"]
        )
        return

    elif state == "branch_engine_a3":
        convo["condition_preference"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            phone,
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # PARTS SALES (Branch B)
    elif state == "branch_parts_b1":
        if "engine info" in text_lower or "have engine" in text_lower or \
                "engine model" in text_lower:
            convo["sub_branch"] = "engine_info"
            convo["state"] = "branch_parts_b2a"
            send_whatsapp_message(
                phone,
                "Please send me your engine model, year, and serial number, along "
                "with what parts you're looking for. We'll check pricing and "
                "availability right away."
            )
        elif "part number" in text_lower or "part no" in text_lower:
            convo["sub_branch"] = "part_numbers"
            convo["state"] = "branch_parts_b2b"
            send_whatsapp_message(
                phone,
                "Great — please list the Yamaha part numbers you need quoted."
            )
        else:
            convo["sub_branch"] = "other"
            convo["state"] = "branch_parts_b2c"
            send_whatsapp_message(
                phone,
                "No problem — describe what you need and we'll track it down."
            )
        return

    elif state == "branch_parts_b2a":
        convo["unit_info"] = message_text.strip()
        _show_confirm(convo)
        return

    elif state == "branch_parts_b2b":
        convo["parts_list"] = message_text.strip()
        _show_confirm(convo)
        return

    elif state == "branch_parts_b2c":
        convo["parts_description"] = message_text.strip()
        _show_confirm(convo)
        return

    # SERVICE (Branch C)
    elif state == "branch_service_c1":
        if "routine" in text_lower or "maintenance" in text_lower:
            convo["service_type"] = "routine_maintenance"
            convo["state"] = "branch_service_c2a"
            send_whatsapp_message(
                phone,
                "What engine model and serial number do you have? And when was the "
                "last service, if you remember?"
            )
        elif "repair" in text_lower or "diagnostic" in text_lower:
            convo["service_type"] = "repair_diagnostic"
            convo["state"] = "branch_service_c2b"
            send_whatsapp_message(
                phone,
                "What's going on with the engine? Please describe the issue and "
                "share your engine model and serial number."
            )
        else:
            convo["service_type"] = "repowering_rigging"
            convo["state"] = "branch_service_c2c"
            send_whatsapp_message(
                phone,
                "Looking to repower or get a new engine installed? Tell me about "
                "your current setup — what boat and engine do you have now, and do "
                "you have a new engine in mind? Note: we offer installation and "
                "rigging on new Yamaha engines purchased from us."
            )
        return

    elif state == "branch_service_c2a":
        convo["service_engine_model"] = message_text.strip()
        convo["state"] = "branch_service_c3"
        send_whatsapp_message(phone, "Where is the boat/engine located?")
        return

    elif state == "branch_service_c2b":
        convo["issue_description"] = message_text.strip()
        convo["state"] = "branch_service_c3"
        send_whatsapp_message(phone, "Where is the boat/engine located?")
        return

    elif state == "branch_service_c2c":
        convo["desired_engine"] = message_text.strip()
        convo["state"] = "branch_service_c3"
        send_whatsapp_message(phone, "Where is the boat/engine located?")
        return

    elif state == "branch_service_c3":
        convo["service_location"] = message_text.strip()
        convo["state"] = "branch_service_c4"
        send_whatsapp_buttons(
            phone,
            "How urgent is this?",
            ["Urgent — Need ASAP", "Can Schedule", "Just Getting a Quote"]
        )
        return

    elif state == "branch_service_c4":
        convo["urgency"] = message_text.strip()
        _show_confirm(convo)
        return

    # BOAT SALES (Branch D)
    elif state == "branch_boat_d1":
        convo["boat_condition"] = message_text.strip()
        convo["state"] = "branch_boat_d2"
        send_whatsapp_message(
            phone,
            "What type of boating — commercial fishing, pleasure, or other? "
            "And do you have a size range in mind (length in feet)?"
        )
        return

    elif state == "branch_boat_d2":
        convo["boat_use"] = message_text.strip()
        _show_confirm(convo)
        return

    # TRAILERS (Branch E)
    elif state == "branch_trailer_e1":
        convo["trailer_boat_info"] = message_text.strip()
        _show_confirm(convo)
        return

    # ELECTRONICS (Branch F)
    elif state == "branch_electronics_f1":
        if "garmin" in text_lower:
            convo["electronics_brand"] = "Garmin"
            convo["sub_branch"] = "garmin"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                phone,
                "Fishfinder, GPS chartplotter, combo unit, or something else from "
                "Garmin? Let us know what you're looking for and we'll check availability."
            )
        elif "jl" in text_lower or "jl audio" in text_lower:
            convo["electronics_brand"] = "JL Audio"
            convo["sub_branch"] = "jl_audio"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                phone,
                "What JL Audio marine product are you looking for — speakers, "
                "amplifiers, subwoofers, or a full setup? Tell us about your boat "
                "and what you have in mind."
            )
        elif "fusion" in text_lower:
            convo["electronics_brand"] = "Fusion Audio"
            convo["sub_branch"] = "fusion_audio"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                phone,
                "What Fusion marine audio product are you interested in — stereo "
                "head units, speakers, amplifiers, or accessories? Let us know "
                "your setup."
            )
        elif "interstate" in text_lower or "batter" in text_lower:
            convo["electronics_brand"] = "Interstate Batteries"
            convo["sub_branch"] = "interstate_batteries"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                phone,
                "What type of Interstate battery do you need — starting, deep cycle, "
                "dual purpose? If you know your boat/engine setup, that helps us "
                "recommend the right one."
            )
        else:
            convo["electronics_brand"] = "Other"
            convo["sub_branch"] = "other_brand"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                phone,
                "No problem — tell us what electronics or audio equipment you're "
                "looking for, including the brand if you have one in mind."
            )
        return

    elif state == "branch_electronics_f2":
        convo["electronics_details"] = message_text.strip()
        _show_confirm(convo)
        return

    # ATV/UTV (Branch G)
    elif state == "branch_atv_g1":
        if "atv" in text_lower or "quad" in text_lower:
            convo["atv_type"] = "ATV"
            convo["state"] = "branch_atv_g2"
            send_whatsapp_message(
                phone,
                "What will the ATV be used for — farming/agriculture, recreation, "
                "or commercial/utility? And do you have a size or model in mind?"
            )
        elif "utv" in text_lower or "side" in text_lower:
            convo["atv_type"] = "UTV"
            convo["state"] = "branch_atv_g2"
            send_whatsapp_message(
                phone,
                "What will the UTV be used for — farming, property maintenance, "
                "or recreation? How many passengers do you need to seat?"
            )
        else:
            convo["atv_type"] = "Need Help"
            convo["state"] = "branch_atv_g2"
            send_whatsapp_message(
                phone,
                "Tell me about what you'll be using it for and the terrain "
                "(farm, hills, flat land, etc.), and I'll recommend the right model."
            )
        return

    elif state == "branch_atv_g2":
        convo["atv_use_case"] = message_text.strip()
        convo["state"] = "branch_atv_g3"
        send_whatsapp_buttons(
            phone,
            "Are you looking for brand new or open to pre-owned?",
            ["New", "Pre-Owned", "Either"]
        )
        return

    elif state == "branch_atv_g3":
        convo["condition_preference"] = message_text.strip()
        _show_confirm(convo)
        return

    # FISHING GEAR (Branch H)
    elif state == "branch_fishing_h1":
        if "iland" in text_lower:
            convo["fishing_brand"] = "Iland Lures"
            convo["sub_branch"] = "iland_lures"
            convo["state"] = "branch_fishing_h2"
            send_whatsapp_message(
                phone,
                "What Iland lures are you interested in? Let us know the type of "
                "fishing (offshore trolling, inshore, etc.) and any specific "
                "products or sizes you're after."
            )
        elif "daiwa" in text_lower:
            convo["fishing_brand"] = "Daiwa"
            convo["sub_branch"] = "daiwa"
            convo["state"] = "branch_fishing_h2"
            send_whatsapp_message(
                phone,
                "What Daiwa product are you looking for — reels, rods, line, "
                "or accessories? Let us know what type of fishing you do and "
                "we can recommend."
            )
        elif "general" in text_lower:
            convo["fishing_brand"] = "General"
            convo["sub_branch"] = "general"
            convo["state"] = "branch_fishing_h2"
            send_whatsapp_message(
                phone,
                "What fishing gear do you need — rods, reels, tackle, lures, "
                "line, or accessories? Let us know and we'll check what we "
                "have in stock."
            )
        else:
            convo["fishing_brand"] = "Specific"
            convo["sub_branch"] = "specific"
            convo["state"] = "branch_fishing_h2"
            send_whatsapp_message(
                phone,
                "Tell us exactly what you're after — brand, product, size, "
                "quantity — and we'll track it down."
            )
        return

    elif state == "branch_fishing_h2":
        convo["fishing_details"] = message_text.strip()
        _show_confirm(convo)
        return

    # GENERAL ACCESSORIES (Branch I)
    elif state == "branch_gen_acc_i1":
        convo["accessories_details"] = message_text.strip()
        _show_confirm(convo)
        return

    # GENERAL INQUIRY (Branch J)
    elif state == "branch_general_j1":
        convo["general_inquiry"] = message_text.strip()
        _show_confirm(convo)
        return

    # ── Confirmation Handler ───────────────────────────────────────────────────
    elif state == "awaiting_confirm":
        if any(w in text_lower for w in ["yes", "send", "✅", "look", "correct",
                                          "confirm", "right", "good", "ok", "okay"]):
            handle_post_confirm(convo)
        else:
            # Let them fix
            convo["state"] = "awaiting_fix"
            send_whatsapp_message(
                phone,
                "No problem! What would you like to update? Just type the "
                "correction and I'll update the inquiry."
            )
        return

    elif state == "awaiting_fix":
        # Accept any free-text correction
        convo["general_inquiry"] = (
            convo.get("general_inquiry", "") +
            " [Correction: " + message_text.strip() + "]"
        )
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            phone,
            summary + f"\n\nNote added: {message_text.strip()}\n\nDoes this look right now?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # ── Completed / Post-complete ─────────────────────────────────────────────
    elif state in ("completed", "post_complete", "completed_new_or_done",
                   "completed_awaiting_follow_up"):
        handle_returning_customer(convo, message_text)
        return

    # ── Unknown state fallback ────────────────────────────────────────────────
    else:
        logger.warning(f"Unknown state '{state}' for {phone} — resetting to name step")
        send_whatsapp_message(
            phone,
            "Sorry, I got a bit confused! Let me start fresh. "
            "Could you please provide your full name?"
        )
        convo["state"] = "awaiting_name"


# ─── Cold Flow Helper: Route by Branch After Fisherman ID ────────────────────

def _proceed_after_name(convo: dict):
    """After name is confirmed, ask for Fisherman ID (with button UI)."""
    phone = convo["phone"]
    name  = convo["customer_name"]
    logger.info(f"Name confirmed for {phone}: {name}")
    convo["state"] = "awaiting_fisherman_id"
    send_whatsapp_buttons(
        phone,
        f"Thanks, {name}! Do you have a Fisherman ID? "
        "(It helps us apply tax incentives for registered fishermen.)",
        ["Yes, I Have One", "No"]
    )


def _proceed_after_fisherman_id(convo: dict):
    """After fisherman ID step, either route via pending_intent or show menu."""
    phone  = convo["phone"]
    name   = convo["customer_name"]
    intent = convo.get("pending_intent", "")

    if intent:
        logger.info(f"Routing {phone} via pending_intent='{intent}'")
        # Pre-fill details from first message
        pending_details = convo.get("pending_details", "")

        if intent == "engine_sales":
            _go_engine_sales(convo)
            # Pre-fill engine model if we can find one in the first message
            if pending_details:
                model_match = ENGINE_MODEL_PATTERN.search(pending_details)
                if model_match:
                    convo["engine_model"] = model_match.group(1)
                    logger.info(f"Pre-filled engine_model from first msg: {convo['engine_model']}")

        elif intent == "parts_sales":
            _go_parts_sales(convo)

        elif intent == "service":
            _go_service(convo)

        elif intent == "boat_sales":
            _go_boat_sales(convo)

        elif intent == "trailers":
            _go_trailers(convo)

        elif intent == "atv_utv":
            _go_atv_utv(convo)

        elif intent == "fishing_gear":
            _go_fishing_gear(convo)

        elif intent == "general_accessories":
            _go_gen_accessories(convo)

        else:
            _show_menu_1(convo)
    else:
        _show_menu_1(convo)


def _show_menu_1(convo: dict):
    """Show the main menu (Menu 1)."""
    name = convo.get("customer_name", "there")
    convo["state"] = "awaiting_menu_1"
    send_whatsapp_list(
        convo["phone"],
        f"Great, thank you {name}! What are you looking for today?",
        "Choose One",
        ["Engines", "Parts", "Service", "Something Else"]
    )


# ─── Branch Jump Helpers ──────────────────────────────────────────────────────

def _go_engine_sales(convo: dict):
    convo["branch"] = "engine_sales"
    convo["state"]  = "branch_engine_a1"
    send_whatsapp_buttons(
        convo["phone"],
        "Outstanding! We stock a range of new (and sometimes pre-owned) Yamaha "
        "outboard motors. Do you know which model you're looking for, or would "
        "you like help narrowing down?",
        ["I Know What I Want", "Help Me Choose"]
    )


def _go_parts_sales(convo: dict):
    convo["branch"] = "parts_sales"
    convo["state"]  = "branch_parts_b1"
    send_whatsapp_buttons(
        convo["phone"],
        "You need parts for your Yamaha engine? Do you have a parts list already, "
        "or do you need help identifying what you need?",
        ["I Have Engine Info", "I Have Part Numbers", "Other Parts Request"]
    )


def _go_service(convo: dict):
    convo["branch"] = "service"
    convo["state"]  = "branch_service_c1"
    send_whatsapp_buttons(
        convo["phone"],
        "We offer full Yamaha-certified service and maintenance. What do you need?",
        ["Routine Maintenance", "Repair / Diagnostic", "Repowering / Rigging"]
    )


def _go_boat_sales(convo: dict):
    convo["branch"] = "boat_sales"
    convo["state"]  = "branch_boat_d1"
    send_whatsapp_buttons(
        convo["phone"],
        "We carry new and pre-owned boats from top brands. What are you looking for?",
        ["New", "Used", "Either"]
    )


def _go_trailers(convo: dict):
    convo["branch"] = "trailers"
    convo["state"]  = "branch_trailer_e1"
    send_whatsapp_message(
        convo["phone"],
        "We carry single and tandem axle boat trailers for all sizes. To recommend "
        "the right trailer, can you tell me your boat size, make, and/or model — "
        "whatever you have?"
    )


def _go_electronics(convo: dict):
    convo["branch"] = "electronics"
    convo["state"]  = "branch_electronics_f1"
    send_whatsapp_list(
        convo["phone"],
        "We carry marine electronics and audio from several leading brands. "
        "Which brand are you interested in?",
        "Choose Brand",
        ["Garmin", "JL Audio", "Fusion Audio", "Interstate Batteries", "Other Brand"]
    )


def _go_atv_utv(convo: dict):
    convo["branch"] = "atv_utv"
    convo["state"]  = "branch_atv_g1"
    send_whatsapp_buttons(
        convo["phone"],
        "We carry Yamaha ATVs and side-by-side UTVs for farming, utility, and "
        "recreation. What are you looking for?",
        ["ATV (Quad)", "UTV / Side-by-Side", "Not Sure — Help Me"]
    )


def _go_fishing_gear(convo: dict):
    convo["branch"] = "fishing_gear"
    convo["state"]  = "branch_fishing_h1"
    send_whatsapp_list(
        convo["phone"],
        "We carry fishing gear from top brands. What are you looking for?",
        "Choose Category",
        ["Iland Lures", "Daiwa Fishing", "General Fishing Gear", "Something Specific"]
    )


def _go_gen_accessories(convo: dict):
    convo["branch"] = "general_accessories"
    convo["state"]  = "branch_gen_acc_i1"
    send_whatsapp_message(
        convo["phone"],
        "We stock a wide range of marine accessories — engine parts, safety "
        "equipment, anchoring gear, boat covers, propellers, lubricants, and more. "
        "What are you looking for?"
    )


def _go_general_inquiry(convo: dict):
    convo["branch"] = "general_inquiry"
    convo["state"]  = "branch_general_j1"
    send_whatsapp_message(
        convo["phone"],
        "No problem at all! Tell me what you're looking for or how we can help, "
        "and I'll make sure the right person follows up with you."
    )


def _show_confirm(convo: dict):
    """Present the confirmation summary with Yes / Fix buttons."""
    convo["state"] = "awaiting_confirm"
    summary = build_summary(convo)
    send_whatsapp_buttons(
        convo["phone"],
        summary + "\n\nDoes this look right?",
        ["✅ Yes, Send It", "✏️ Let Me Fix"]
    )


# ─── Warm Conversation Flow ───────────────────────────────────────────────────

def process_warm_message(convo: dict, message_text: str):
    """Handle WARM flow — model or category detected from website, express intake."""
    state     = convo["state"]
    phone     = convo["phone"]
    text_lower = message_text.strip().lower()

    if state == "init":
        model  = convo.get("engine_model") or convo.get("boat_model") or "the product you selected"
        branch = convo.get("branch", "")

        # Check Interakt traits for name
        traits_name = convo.get("_traits_name", "")
        if traits_name:
            convo["customer_name"] = traits_name
            convo["state"]         = "warm_awaiting_fisherman_id"
            first_name = traits_name.split()[0]
            send_whatsapp_buttons(
                phone,
                f"Hey {first_name}! 👋 I see you're looking at *{model}* on our website. "
                "Do you have a Fisherman ID?",
                ["Yes, I Have One", "No"]
            )
        else:
            convo["state"] = "warm_awaiting_name"
            send_whatsapp_message(
                phone,
                f"Hey there! 👋 I see you're looking at *{model}* on our website. "
                "Great choice!\n\nBefore I get you a quote, could I grab your full name?"
            )
        return

    elif state == "warm_awaiting_name":
        entered = message_text.strip()
        convo["_proposed_name"] = entered
        first_word = entered.split()[0]
        convo["state"] = "warm_awaiting_name_verify"
        send_whatsapp_buttons(
            phone,
            f"Just to confirm — shall I call you *{first_word}*?",
            [f"Yes, {first_word}"[:20], "That's Wrong"]
        )
        return

    elif state == "warm_awaiting_name_verify":
        if any(w in text_lower for w in ["wrong", "no", "different", "change"]):
            convo["state"] = "warm_awaiting_name"
            send_whatsapp_message(phone, "No problem! What should I call you?")
        else:
            convo["customer_name"] = convo.get("_proposed_name", message_text.strip())
            convo["state"] = "warm_awaiting_fisherman_id"
            send_whatsapp_buttons(
                phone,
                f"Thanks, {convo['customer_name']}! Quick question — do you have a "
                "Fisherman ID? It helps us apply tax incentives.",
                ["Yes, I Have One", "No"]
            )
        return

    elif state == "warm_awaiting_fisherman_id":
        if text_lower in ("yes", "yeah", "yep", "yup", "i have one", "yes, i have one"):
            convo["state"] = "warm_awaiting_fisherman_id_value"
            send_whatsapp_message(phone, "Please type your Fisherman ID number:")
        elif text_lower in ("no", "nah", "nope", "none", "don't have"):
            convo["fisherman_id"] = ""
            _warm_route_by_branch(convo)
        elif any(c.isdigit() for c in text_lower):
            convo["fisherman_id"] = message_text.strip()
            _warm_route_by_branch(convo)
        else:
            send_whatsapp_buttons(
                phone,
                "Do you have a Fisherman ID?",
                ["Yes, I Have One", "No"]
            )
        return

    elif state == "warm_awaiting_fisherman_id_value":
        convo["fisherman_id"] = message_text.strip()
        _warm_route_by_branch(convo)
        return

    elif state == "warm_engine_condition":
        convo["condition_preference"] = message_text.strip()
        convo["state"] = "warm_engine_accessories"
        send_whatsapp_message(
            phone,
            "Got it! Will you need any additional accessories with the engine, "
            "or just the standard included accessories?"
        )
        return

    elif state == "warm_engine_accessories":
        convo["accessories_needed"] = message_text.strip()
        _show_confirm(convo)
        return

    elif state in ("completed", "post_complete", "completed_new_or_done",
                   "completed_awaiting_follow_up"):
        handle_returning_customer(convo, message_text)
        return

    # For all branch states, fall through to cold handler (shared states)
    process_cold_message(convo, message_text)


def _warm_route_by_branch(convo: dict):
    """After fisherman ID in warm flow, route to appropriate branch step."""
    branch = convo.get("branch", "")
    phone  = convo["phone"]

    if branch == "engine_sales":
        model = convo.get("engine_model") or "the engine you selected"
        convo["state"] = "warm_engine_condition"
        send_whatsapp_buttons(
            phone,
            f"Perfect! So you're interested in the *{model}*. "
            "Are you looking for brand new, pre-owned, or open to either?",
            ["New", "Pre-Owned", "Either"]
        )
    elif branch == "boat_sales":
        model = convo.get("boat_model", "")
        convo["state"] = "branch_boat_d1"
        prompt = "We carry new and pre-owned boats from top brands. "
        if model:
            prompt = f"Great — I see you're interested in the *{model}*. "
        send_whatsapp_buttons(
            phone,
            prompt + "Are you looking for new or pre-owned?",
            ["New", "Used", "Either"]
        )
    elif branch == "service":
        _go_service(convo)
    elif branch == "parts_sales":
        _go_parts_sales(convo)
    elif branch == "trailers":
        _go_trailers(convo)
    elif branch == "electronics":
        _go_electronics(convo)
    elif branch == "fishing_gear":
        _go_fishing_gear(convo)
    elif branch == "general_accessories":
        _go_gen_accessories(convo)
    elif branch == "atv_utv":
        _go_atv_utv(convo)
    else:
        _show_menu_1(convo)


# ─── Hot Conversation Flow ────────────────────────────────────────────────────

def process_hot_message(convo: dict, message_text: str):
    """Handle HOT flow — most data known from enriched website message."""
    state     = convo["state"]
    phone     = convo["phone"]
    text_lower = message_text.strip().lower()

    if state == "init":
        model = convo.get("engine_model") or "the product you selected"
        name  = convo.get("customer_name") or "there"

        lines = [f"Hi {name}! 👋 I see you're checking out the *{model}* on our website."]
        if convo.get("customer_name"):
            lines.append("I've pulled up your details:")
            lines.append("")
            lines.append(f"🔹 Name: {convo['customer_name']}")
        if convo.get("customer_type"):
            lines.append(f"🔹 Customer Type: {convo['customer_type']}")
        if convo.get("current_engine"):
            lines.append(f"🔹 Current Engine: {convo['current_engine']}")
        if convo.get("serial_number"):
            lines.append(f"🔹 Serial: {convo['serial_number']}")
        lines.append(f"🔹 Interested In: {model}")
        lines.append("")
        lines.append("Does this look right? I can get you a quote right away!")

        convo["state"] = "hot_confirm"
        send_whatsapp_buttons(
            phone,
            "\n".join(lines),
            ["✅ Looks Good — Quote", "✏️ Change Something", "🔄 Something Else"]
        )
        return

    elif state == "hot_confirm":
        if any(w in text_lower for w in ["look", "good", "quote", "✅", "yes", "confirm"]):
            handle_post_confirm(convo)
        elif any(w in text_lower for w in ["change", "✏️", "fix", "wrong", "different"]):
            convo["state"] = "hot_edit"
            send_whatsapp_buttons(
                phone,
                "No problem! What would you like to update?",
                ["Different Model", "Parts Instead", "Need Service"]
            )
        else:
            # Something else
            name = convo.get("customer_name", "there")
            convo["state"] = "awaiting_menu_1"
            convo["temperature"] = "cold"
            send_whatsapp_list(
                phone,
                f"Of course, {name}! How can I help you today?",
                "Choose One",
                ["Engines", "Parts", "Service", "Something Else"]
            )
        return

    elif state == "hot_edit":
        if "model" in text_lower or "different" in text_lower:
            convo["branch"] = "engine_sales"
            convo["state"]  = "branch_engine_a2a"
            send_whatsapp_message(phone, "What model are you interested in instead?")
        elif "part" in text_lower:
            _go_parts_sales(convo)
        elif "service" in text_lower:
            _go_service(convo)
        else:
            convo["state"] = "awaiting_menu_1"
            convo["temperature"] = "cold"
            send_whatsapp_list(
                phone,
                "What are you looking for?",
                "Choose One",
                ["Engines", "Parts", "Service", "Something Else"]
            )
        return

    elif state in ("completed", "post_complete", "completed_new_or_done",
                   "completed_awaiting_follow_up"):
        handle_returning_customer(convo, message_text)
        return

    # Fall through to cold handler for branch states
    process_cold_message(convo, message_text)


# ─── Contact Form Hot Flow ────────────────────────────────────────────────────

def process_contact_form_hot(convo: dict, message_text: str):
    """Handle HOT flow initiated by a website contact form submission."""
    state     = convo["state"]
    phone     = convo["phone"]
    text_lower = message_text.strip().lower()

    if state == "contact_form_sent":
        if any(w in text_lower for w in ["everything", "that's all", "✅", "send", "yes",
                                          "all good", "that's everything"]):
            convo["confirmed"] = True
            handle_post_confirm(convo)
        elif any(w in text_lower for w in ["add", "more", "📝", "additional", "detail"]):
            branch = convo.get("branch", "")
            if branch == "engine_sales":
                convo["state"] = "branch_engine_a1"
                send_whatsapp_buttons(
                    phone,
                    "Do you know which engine model you're looking for, or would you "
                    "like help narrowing down?",
                    ["I Know What I Want", "Help Me Choose"]
                )
            elif branch == "parts_sales":
                _go_parts_sales(convo)
            elif branch == "boat_sales":
                _go_boat_sales(convo)
            elif branch == "service":
                _go_service(convo)
            else:
                _show_menu_1(convo)
        elif any(w in text_lower for w in ["question", "💬", "ask", "query"]):
            convo["state"] = "contact_form_question"
            send_whatsapp_message(
                phone,
                "Of course — go ahead and ask! I'll do my best to help, or connect "
                "you with the right person."
            )
        return

    elif state == "contact_form_question":
        convo["general_inquiry"] = message_text.strip()
        convo["confirmed"] = True
        handle_post_confirm(convo)
        return

    elif state in ("completed", "post_complete", "completed_new_or_done",
                   "completed_awaiting_follow_up"):
        handle_returning_customer(convo, message_text)
        return

    # Fall through to cold handler for branch states
    process_cold_message(convo, message_text)


# ─── Flask Routes ─────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    """Server health check — public endpoint."""
    all_convos = list_all_conversations()
    active = sum(1 for c in all_convos if c.get("state") not in ("completed", "post_complete"))
    return jsonify({
        "status":               "ok",
        "version":              VERSION,
        "conversations_total":  len(all_convos),
        "conversations_active": active,
        "db_path":              DB_PATH,
        "timestamp":            datetime.now(timezone.utc).isoformat()
    })


@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Webhook verification / health check (GET)."""
    return jsonify({
        "status":  "ok",
        "service": "yamaja-whatsapp-chatbot",
        "version": VERSION
    })


@app.route('/webhook', methods=['POST'])
def webhook_incoming():
    """
    Main endpoint — receives every incoming WhatsApp message from Interakt.
    Always returns HTTP 200 to prevent Interakt from retrying.
    """
    global _webhook_counter

    # ── Parse JSON body ────────────────────────────────────────────────────────
    try:
        data = request.get_json(force=True)
    except Exception:
        logger.error("Webhook: failed to parse JSON body")
        return jsonify({"status": "ok", "note": "invalid json"}), 200

    # ── Log raw payload to SQLite ──────────────────────────────────────────────
    log_webhook(data)

    # ── Periodic dedup cleanup (every 100 webhooks) ────────────────────────────
    _webhook_counter += 1
    if _webhook_counter % 100 == 0:
        cleanup_dedup_every_nth(100)

    logger.info(f"Webhook received: {json.dumps(data, default=str)[:600]}")

    # ── Skip non-message webhooks ─────────────────────────────────────────────
    webhook_type = data.get("type", "")
    if webhook_type and webhook_type != "message_received":
        logger.info(f"Ignoring webhook type: {webhook_type}")
        return jsonify({"status": "ok", "note": f"ignored {webhook_type}"}), 200

    # ── Extract Interakt fields ────────────────────────────────────────────────
    msg_data   = data.get("data", {})
    customer   = msg_data.get("customer", {})
    message_obj = msg_data.get("message", {})

    # Extract content type for media detection
    message_content_type = (
        message_obj.get("type", "") or
        message_obj.get("message_type", "") or
        message_obj.get("chat_message_type", "") or ""
    ).lower()

    # ── Parse the actual message text (and whether it's interactive) ───────────
    raw_message_text, is_interactive_reply = parse_interakt_message(message_obj)

    # ── Media / voice note handling ────────────────────────────────────────────
    media_types = {"audio", "voice", "image", "video", "document", "sticker"}
    if not raw_message_text or message_content_type in media_types:
        # Also check for media_url presence as a signal
        has_media = bool(message_obj.get("media_url") or
                         message_obj.get("url") or
                         message_content_type in media_types)
        if has_media and not raw_message_text:
            phone_raw = (customer.get("phone_number", "") or
                         customer.get("channel_phone_number", "") or
                         message_obj.get("from", ""))
            phone = normalize_phone(phone_raw)
            if phone:
                send_whatsapp_message(
                    phone,
                    "Thanks for that! Unfortunately, I can only process text messages "
                    "right now. Could you please type your response instead? If you're "
                    "replying to a menu, just tap one of the options above."
                )
                logger.info(f"Media message detected for {phone}, sent text-only notice")
            return jsonify({"status": "ok", "note": "media message handled"}), 200

    # ── Extract phone number ───────────────────────────────────────────────────
    phone_raw = (customer.get("phone_number", "") or
                 customer.get("channel_phone_number", "") or
                 message_obj.get("from", ""))
    phone = normalize_phone(phone_raw)

    if not phone or not raw_message_text:
        logger.warning(
            f"No phone or message text. phone='{phone_raw}', "
            f"keys={list(message_obj.keys())}"
        )
        return jsonify({"status": "ok", "note": "no actionable content"}), 200

    # ── Deduplication ─────────────────────────────────────────────────────────
    msg_id = message_obj.get("id", "")
    dedup_keys = []
    if msg_id:
        dedup_keys.append(f"id:{msg_id}")
    # Content key: phone + first 100 chars of text (catches ID-different duplicates)
    content_key = f"content:{phone}:{raw_message_text[:100]}"
    dedup_keys.append(content_key)

    for key in dedup_keys:
        if is_duplicate(key):
            logger.info(f"DUPLICATE webhook skipped: key={key[:80]}")
            return jsonify({"status": "ok", "note": "duplicate skipped"}), 200

    for key in dedup_keys:
        mark_seen(key)

    # ── Acquire per-phone lock ────────────────────────────────────────────────
    phone_lock = _get_phone_lock(phone)
    if not phone_lock.acquire(timeout=5):
        logger.warning(f"Could not acquire lock for {phone} — proceeding anyway")
    try:
        _process_message(phone, raw_message_text, is_interactive_reply,
                         customer, msg_data)
    finally:
        phone_lock.release()

    return jsonify({"status": "ok"}), 200


def _process_message(phone: str, message_text: str, is_interactive: bool,
                     customer: dict, msg_data: dict):
    """
    Core message processing — called within phone-level lock.
    Handles buffering, state routing, and conversation persistence.
    """
    # ── Load or create conversation ───────────────────────────────────────────
    convo = get_or_create_conversation(phone)
    convo["message_count"] += 1
    convo["all_messages"].append(message_text[:500])
    convo["last_message_at"] = datetime.now(timezone.utc).isoformat()

    # Store first message
    if convo["message_count"] == 1:
        convo["first_message"] = message_text[:1000]

    # ── Extract traits.name from Interakt payload ─────────────────────────────
    traits = customer.get("traits", {})
    if isinstance(traits, dict):
        traits_name = traits.get("name", "") or traits.get("full_name", "")
        if traits_name and not convo.get("_traits_name"):
            convo["_traits_name"] = traits_name.strip()
            logger.info(f"Interakt traits.name captured: {convo['_traits_name']}")

    logger.info(
        f"Processing: phone={phone}, state={convo['state']}, "
        f"temp={convo['temperature']}, interactive={is_interactive}, "
        f"msg='{message_text[:100]}'"
    )

    # ── Wave Runner check (any state, any temperature) ─────────────────────────
    if check_wave_runner(message_text):
        convo["branch"] = "wave_runner_banned"
        send_whatsapp_buttons(
            phone,
            "Thanks for your interest! Unfortunately, due to a current importation "
            "ban on all Personal Water Crafts (PWCs) in Jamaica, we're unable to "
            "supply wave runners at this time. We'll update our customers as soon "
            "as this changes.\n\nIs there anything else I can help with?",
            ["Yes, Something Else", "No, That's All"]
        )
        convo["state"] = "post_complete"
        forward_lead_to_whatsapp(convo)
        save_conversation(convo)
        return

    # ── First message temperature detection ──────────────────────────────────
    if convo["message_count"] == 1 and convo["state"] == "init":
        temp, branch, extracted, pattern_name = detect_temperature(message_text)
        convo["temperature"]      = temp
        convo["branch"]           = branch or convo["branch"]
        convo["website_referral"] = (temp != "cold")

        if temp != "cold":
            convo["source_page"] = extracted.get("source_page", "engine-detail")

        # Apply extracted fields (engine model, boat model, etc.)
        for field, value in extracted.items():
            if field != "source_page" and value:
                convo[field] = value

        # If still cold, run keyword intent detection on first message
        if temp == "cold" and not branch:
            intent = detect_intent(message_text)
            if intent:
                convo["pending_intent"]  = intent
                convo["pending_details"] = message_text

        logger.info(
            f"Temperature: {temp} | Branch: {branch} | "
            f"Pattern: {pattern_name} | Intent: {convo.get('pending_intent')} | "
            f"Phone: {phone}"
        )

    # ── Message buffering for free-text states ────────────────────────────────
    current_state = convo["state"]

    if not is_interactive and current_state in BUFFERED_STATES:
        # Check if an existing buffer has expired and should be flushed first
        flushed = _flush_buffer(convo)
        if flushed:
            # Process the previously buffered content, then add the new message
            logger.info(f"Processing flushed buffer for {phone}: '{flushed[:80]}'")
            _route_message(convo, flushed)
            save_conversation(convo)
            # After flushing, the state may have changed — add new msg to fresh buffer
            if convo["state"] in BUFFERED_STATES:
                _add_to_buffer(convo, message_text)
                save_conversation(convo)
                return

        # Add to buffer and schedule a background flush timer
        _add_to_buffer(convo, message_text)
        _schedule_buffer_flush(phone)
        save_conversation(convo)
        return

    # Interactive clicks flush any pending buffer first
    if is_interactive:
        # Cancel any pending buffer flush timer
        with _buffer_timers_mutex:
            existing_timer = _buffer_timers.pop(phone, None)
            if existing_timer:
                existing_timer.cancel()
        buffered_text = _flush_buffer(convo)
        if buffered_text:
            logger.info(f"Interactive click flushing buffer for {phone}: '{buffered_text[:80]}'")
            _route_message(convo, buffered_text)
            save_conversation(convo)
            # Now process the interactive click itself
            convo = load_conversation(phone) or convo

    # ── Route message to appropriate handler ──────────────────────────────────
    _route_message(convo, message_text)
    save_conversation(convo)


def _route_message(convo: dict, message_text: str):
    """Route a processed message to the appropriate temperature handler."""
    temp       = convo.get("temperature", "cold")
    source_pg  = convo.get("source_page", "")

    if temp == "hot" and source_pg == "contact-form":
        process_contact_form_hot(convo, message_text)
    elif temp == "hot":
        process_hot_message(convo, message_text)
    elif temp == "warm":
        process_warm_message(convo, message_text)
    else:
        process_cold_message(convo, message_text)


# ─── Website Contact Form Endpoint ────────────────────────────────────────────

@app.route('/webhook/website', methods=['POST'])
def webhook_website():
    """
    Receive website contact form submissions.
    Creates a HOT conversation and sends proactive WhatsApp outreach to the customer.
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    logger.info(f"Website form received: {json.dumps(data, default=str)[:500]}")

    # Required: phone number
    phone_raw = data.get("phone", "")
    phone = normalize_phone(phone_raw)
    if not phone or len(phone) < 7:
        return jsonify({"error": "Phone number required for WhatsApp follow-up"}), 400

    # Build HOT conversation
    convo = new_conversation(phone)
    convo["temperature"]    = "hot"
    convo["source_page"]    = "contact-form"
    convo["customer_name"]  = data.get("name", "")
    convo["email"]          = data.get("email", "")
    convo["website_message"] = data.get("message", "")
    convo["source"]         = data.get("source", "website_contact_form")
    convo["first_message"]  = data.get("message", "")

    inquiry_type = data.get("inquiry_type", "Other")
    convo["branch"] = INQUIRY_TO_BRANCH.get(inquiry_type, "general_inquiry")

    # Try to extract engine model from message
    msg = data.get("message", "")
    model_match = ENGINE_MODEL_PATTERN.search(msg)
    if model_match:
        convo["engine_model"] = model_match.group(1)
    family_match = ENGINE_FAMILY_PATTERN.search(msg)
    if family_match:
        convo["engine_family"] = family_match.group(1)

    convo["state"] = "contact_form_sent"
    save_conversation(convo)

    # Build proactive outreach message
    name_disp = convo["customer_name"] or "there"
    lines = [
        f"Hi {name_disp}! 👋 This is Claudia from Yamaja Engines. "
        f"I received your inquiry from our website:",
        "",
        f"🔹 Inquiry Type: {inquiry_type}",
    ]
    if convo["engine_model"]:
        engine_line = f"🔹 Engine: {convo['engine_model']}"
        if convo["engine_family"]:
            engine_line += f" ({convo['engine_family']})"
        lines.append(engine_line)
    if convo["website_message"]:
        lines.append(f"🔹 Message: {convo['website_message'][:200]}")

    lines.append("")
    lines.append(
        "I've passed this to our team. Would you like to add any details, "
        "or is this all we need?"
    )

    send_whatsapp_buttons(
        phone,
        "\n".join(lines),
        ["✅ That's Everything", "📝 Add More Details", "💬 I Have a Question"]
    )

    return jsonify({
        "status":  "ok",
        "message": "WhatsApp follow-up initiated",
        "phone":   phone
    }), 200


# ─── Admin Endpoints ──────────────────────────────────────────────────────────

@app.route('/leads', methods=['GET'])
def list_leads():
    """View all captured leads/conversations. Requires ADMIN_SECRET."""
    if not _check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401

    all_convos = list_all_conversations()
    leads = []
    for c in all_convos:
        leads.append({
            "phone":         c.get("phone"),
            "name":          c.get("customer_name"),
            "temperature":   c.get("temperature"),
            "branch":        c.get("branch"),
            "state":         c.get("state"),
            "confirmed":     c.get("confirmed"),
            "message_count": c.get("message_count"),
            "created_at":    c.get("created_at"),
            "last_message_at": c.get("last_message_at"),
            "lead_forwarded_at": c.get("lead_forwarded_at")
        })
    return jsonify({
        "total": len(leads),
        "leads": sorted(leads, key=lambda x: x.get("created_at", ""), reverse=True)
    })


@app.route('/leads/<phone>', methods=['GET'])
def get_lead(phone: str):
    """View a single conversation thread. Requires ADMIN_SECRET."""
    if not _check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401

    phone = normalize_phone(phone)
    convo = load_conversation(phone)
    if not convo:
        return jsonify({"error": "Conversation not found"}), 404

    # Return clean copy without internal underscore fields
    result = {k: v for k, v in convo.items() if not k.startswith("_")}
    return jsonify(result)


@app.route('/debug/webhooks', methods=['GET'])
def debug_webhooks():
    """View last 50 raw webhook payloads. Requires ADMIN_SECRET."""
    if not _check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401

    log = get_webhook_log()
    return jsonify({
        "total":    len(log),
        "webhooks": log
    })


@app.route('/reset/<phone>', methods=['POST'])
def reset_phone(phone: str):
    """Reset a specific conversation. Requires ADMIN_SECRET."""
    if not _check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401

    phone = normalize_phone(phone)
    phone_lock = _get_phone_lock(phone)
    with phone_lock:
        delete_conversation(phone)
    logger.info(f"Conversation reset for {phone} by admin")
    return jsonify({"status": "ok", "phone": phone, "action": "reset"})


@app.route('/reset-all', methods=['POST'])
def reset_all():
    """Reset all conversations. Requires ADMIN_SECRET."""
    if not _check_admin_auth():
        return jsonify({"error": "Unauthorized"}), 401

    delete_all_conversations()
    logger.info("All conversations reset by admin")
    return jsonify({"status": "ok", "action": "reset_all"})


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting Yamaja WhatsApp Chatbot v{VERSION} on port {port}")
    logger.info(f"Database: {DB_PATH}")
    app.run(host='0.0.0.0', port=port, debug=False)
