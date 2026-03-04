#!/usr/bin/env python3
"""
yamaja_cold_intake.py — v3
Yamaja WhatsApp Chatbot Webhook Server (Render)

Sits between customers (via Interakt/WhatsApp) and Make.com.
Detects temperature (cold/warm/hot), parses prefilled website messages,
tracks conversation state, and forwards confirmed leads to Make.com.

Endpoints:
  POST /webhook              — Interakt incoming messages
  GET  /webhook              — Health check
  GET  /leads                — View all captured leads
  GET  /leads/<phone>        — View single conversation
  POST /webhook/website      — Website contact form passthrough
  GET  /health               — Server health check
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify
import requests

# ─── Configuration ───────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# Environment variables (set in Render dashboard)
INTERAKT_API_KEY = os.environ.get('INTERAKT_API_KEY', '')
MAKE_WEBHOOK_URL = os.environ.get('MAKE_WEBHOOK_URL', '')
CLAUDIA_PHONE = '18765642888'
SALES_PHONE = '18763712888'

# In-memory conversation store (Render free tier — resets on cold start)
# For production, swap to Redis or a DB
conversations = {}

# ─── Temperature Detection Patterns ─────────────────────────────────────────

WEBSITE_PATTERNS = [
    {
        # Pattern 1: Bottom CTA "Chat with Claudia" button from engine detail
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
        # Pattern 2: Floating WhatsApp button (enriched — logged in with profile)
        # Must come before basic float pattern (more specific)
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
        # Pattern 3: Floating WhatsApp button (basic — not logged in)
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
        # Pattern 4: Accessories → Trailers prefill
        "name": "accessories_trailers",
        "regex": re.compile(r"interested in boat trailers", re.IGNORECASE),
        "extracts": {},
        "branch": "trailers",
        "temperature": "warm"
    },
    {
        # Pattern 5: Accessories → Electronics prefill
        "name": "accessories_electronics",
        "regex": re.compile(r"interested in (?:Garmin )?marine electronics", re.IGNORECASE),
        "extracts": {},
        "branch": "electronics",
        "temperature": "warm"
    },
    {
        # Pattern 6: Accessories → Fishing Gear prefill
        "name": "accessories_fishing",
        "regex": re.compile(r"interested in fishing gear", re.IGNORECASE),
        "extracts": {},
        "branch": "fishing_gear",
        "temperature": "warm"
    },
    {
        # Pattern 7: Accessories → General prefill
        "name": "accessories_general",
        "regex": re.compile(r"looking for general (?:boat )?accessories", re.IGNORECASE),
        "extracts": {},
        "branch": "general_accessories",
        "temperature": "warm"
    },
    {
        # Pattern 8: ATVs/UTVs prefill
        "name": "atvs",
        "regex": re.compile(r"interested in Yamaha ATVs?\s*/?\s*UTVs?", re.IGNORECASE),
        "extracts": {},
        "branch": "atv_utv",
        "temperature": "warm"
    },
]

# Wave runner / PWC detection
WAVE_RUNNER_PATTERN = re.compile(
    r"\b(wave\s*runner|jet\s*ski|pwc|personal\s*water\s*craft)\b",
    re.IGNORECASE
)

# Machine-readable tag at end of WhatsApp messages (optional enhancement)
YAMJA_TAG_PATTERN = re.compile(
    r"\[yamja:([^\]]+)\]"
)

# Contact form inquiry type → branch mapping
INQUIRY_TO_BRANCH = {
    "Engines": "engine_sales",
    "Boats": "boat_sales",
    "Parts": "parts_sales",
    "Service": "service",
    "Trailers": "trailers",
    "Electronics": "electronics",
    "ATVs": "atv_utv",
    "Fishing": "fishing_gear",
    "Accessories": "general_accessories",
    "Other": "general_inquiry"
}

# Engine model extraction from free-text message (for contact form messages)
ENGINE_MODEL_PATTERN = re.compile(
    r"(?:the\s+)?((?:F|LF|VF|FL)\d{2,3}[A-Z]{0,6})\b",
    re.IGNORECASE
)

ENGINE_FAMILY_PATTERN = re.compile(
    r"\((\w+(?:\s+\w+)?)\s*(?:family|series)?\)",
    re.IGNORECASE
)


# ─── Conversation Object Factory ────────────────────────────────────────────

def new_conversation(phone, country_code="+1"):
    """Create a fresh conversation object for a phone number."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "phone": phone,
        "country_code": country_code,
        "temperature": "cold",
        "source_page": "direct",
        "customer_name": "",
        "email": "",
        "customer_type": "",
        "fisherman_id": "",
        "branch": "",
        "sub_branch": "",
        "confirmed": False,

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

        # Profile / website context
        "current_engine": "",
        "serial_number": "",
        "website_referral": False,
        "referred_model": "",
        "referred_family": "",
        "website_message": "",

        # Tracking
        "all_messages": [],
        "message_count": 0,
        "state": "init",
        "created_at": now,
        "last_message_at": now,
        "source": "whatsapp_cold_intake"
    }


# ─── Temperature Detection ──────────────────────────────────────────────────

def detect_temperature(message_text):
    """
    Analyze the first message to determine temperature and extract fields.
    Returns (temperature, branch, extracted_fields, pattern_name).
    """
    # Check for machine-readable tag first
    tag_match = YAMJA_TAG_PATTERN.search(message_text)
    tag_data = {}
    if tag_match:
        for pair in tag_match.group(1).split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                tag_data[k.strip()] = v.strip()

    # Check website patterns
    for pattern in WEBSITE_PATTERNS:
        match = pattern["regex"].search(message_text)
        if match:
            extracted = {}
            for field_name, group_idx in pattern["extracts"].items():
                val = match.group(group_idx)
                if val:
                    extracted[field_name] = val.strip()

            # Override with tag data if present
            if tag_data:
                if "model" in tag_data:
                    extracted["engine_model"] = tag_data["model"]
                if "family" in tag_data:
                    extracted["engine_family"] = tag_data["family"]
                if "page" in tag_data:
                    extracted["source_page"] = tag_data["page"]

            return pattern["temperature"], pattern["branch"], extracted, pattern["name"]

    # No website pattern matched → cold
    return "cold", "", {}, "none"


def check_wave_runner(message_text):
    """Check if message mentions wave runners / jet skis / PWC."""
    return bool(WAVE_RUNNER_PATTERN.search(message_text))


# ─── Interakt API Helpers ───────────────────────────────────────────────────

def _format_phone_for_interakt(phone):
    """Format phone number for Interakt Send API.
    
    Interakt stores contacts as countryCode + phoneNumber.
    For Jamaica: countryCode="+1", phoneNumber="8769951632" (10 digits).
    Our internal storage uses full 11-digit (18769951632), so we strip the leading 1.
    """
    phone = re.sub(r'[^\d]', '', phone)
    if phone.startswith("1") and len(phone) == 11:
        return "+1", phone[1:]  # Strip leading 1, use +1 as country code
    elif len(phone) == 10:
        return "+1", phone  # Already 10 digits
    else:
        return "+1", phone  # Fallback


def send_whatsapp_message(phone, message):
    """Send a WhatsApp message via Interakt API."""
    if not INTERAKT_API_KEY:
        logger.warning("INTERAKT_API_KEY not set — skipping WhatsApp send")
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
        logger.info(f"Interakt send to {phone}: {resp.status_code} — {resp.text[:300]}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Interakt send failed: {e}")
        return False


def send_whatsapp_buttons(phone, message, buttons):
    """Send a WhatsApp interactive button message via Interakt API."""
    if not INTERAKT_API_KEY:
        logger.warning("INTERAKT_API_KEY not set — skipping button send")
        return False

    country_code, clean_phone = _format_phone_for_interakt(phone)
    url = "https://api.interakt.ai/v1/public/message/"
    headers = {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json"
    }

    # Interakt button format
    button_list = []
    for i, btn_text in enumerate(buttons):
        button_list.append({
            "type": "reply",
            "reply": {
                "id": f"btn_{i}",
                "title": btn_text[:20]  # WhatsApp max 20 chars per button
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
                "body": {
                    "text": message
                },
                "action": {
                    "buttons": button_list
                }
            }
        }
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"Interakt buttons to {phone}: {resp.status_code} — {resp.text[:300]}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Interakt button send failed: {e}")
        return False


def send_whatsapp_list(phone, message, list_title, items):
    """Send a WhatsApp list message via Interakt API."""
    if not INTERAKT_API_KEY:
        logger.warning("INTERAKT_API_KEY not set — skipping list send")
        return False

    country_code, clean_phone = _format_phone_for_interakt(phone)
    url = "https://api.interakt.ai/v1/public/message/"
    headers = {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json"
    }

    rows = []
    for i, item_text in enumerate(items):
        rows.append({
            "id": f"list_{i}",
            "title": item_text[:24]  # WhatsApp max 24 chars per row
        })

    payload = {
        "countryCode": country_code,
        "phoneNumber": clean_phone,
        "callbackData": "yamaja_chatbot",
        "type": "Interactive",
        "data": {
            "interactive": {
                "type": "list",
                "body": {
                    "text": message
                },
                "action": {
                    "button": list_title[:20],
                    "sections": [{
                        "title": "Options",
                        "rows": rows
                    }]
                }
            }
        }
    }

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        logger.info(f"Interakt list to {phone}: {resp.status_code} — {resp.text[:300]}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Interakt list send failed: {e}")
        return False


# ─── Make.com Lead Forwarding ───────────────────────────────────────────────

def forward_to_make(convo):
    """Forward a confirmed (or timed-out) lead to Make.com webhook."""
    if not MAKE_WEBHOOK_URL:
        logger.warning("MAKE_WEBHOOK_URL not set — skipping Make.com forward")
        return False

    payload = {
        "lead_type": "whatsapp",
        "temperature": convo["temperature"],
        "source_page": convo["source_page"],
        "customer_name": convo["customer_name"],
        "email": convo["email"],
        "phone": convo["phone"],
        "country_code": convo["country_code"],
        "customer_type": convo["customer_type"],
        "fisherman_id": convo["fisherman_id"],
        "branch": convo["branch"],
        "sub_branch": convo["sub_branch"],

        # Engine
        "engine_model": convo["engine_model"],
        "engine_family": convo["engine_family"],
        "current_engine": convo["current_engine"],
        "serial_number": convo["serial_number"],
        "condition_preference": convo["condition_preference"],
        "use_case": convo["use_case"],
        "boat_size": convo["boat_size"],
        "accessories_needed": convo["accessories_needed"],

        # Parts
        "unit_info": convo["unit_info"],
        "parts_list": convo["parts_list"],
        "parts_description": convo["parts_description"],

        # Service
        "service_type": convo["service_type"],
        "service_engine_model": convo["service_engine_model"],
        "service_serial": convo["service_serial"],
        "issue_description": convo["issue_description"],
        "desired_engine": convo["desired_engine"],
        "boat_info": convo["boat_info"],
        "service_location": convo["service_location"],
        "urgency": convo["urgency"],
        "last_service": convo["last_service"],

        # Boat
        "boat_condition": convo["boat_condition"],
        "boat_use": convo["boat_use"],

        # Trailer
        "trailer_boat_info": convo["trailer_boat_info"],

        # Electronics
        "electronics_brand": convo["electronics_brand"],
        "electronics_details": convo["electronics_details"],

        # ATV/UTV
        "atv_type": convo["atv_type"],
        "atv_model": convo["atv_model"],
        "atv_use_case": convo["atv_use_case"],
        "atv_passengers": convo["atv_passengers"],
        "atv_details": convo["atv_details"],

        # Fishing
        "fishing_brand": convo["fishing_brand"],
        "fishing_details": convo["fishing_details"],

        # General
        "accessories_details": convo["accessories_details"],
        "general_inquiry": convo["general_inquiry"],
        "other_details": convo.get("other_details", ""),

        # Meta
        "website_message": convo["website_message"],
        "all_messages": " | ".join(convo["all_messages"][-20:]),
        "message_count": convo["message_count"],
        "confirmed": convo["confirmed"],
        "source": convo["source"],
        "received_at": convo["created_at"]
    }

    try:
        resp = requests.post(MAKE_WEBHOOK_URL, json=payload, timeout=15)
        logger.info(f"Make.com forward for {convo['phone']}: {resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        logger.error(f"Make.com forward failed: {e}")
        return False


# ─── Conversation State Machine ─────────────────────────────────────────────

def get_or_create_conversation(phone):
    """Get existing conversation or create new one. Auto-expire after 60 min."""
    now = time.time()

    if phone in conversations:
        convo = conversations[phone]
        # If conversation is confirmed and older than 60 min, start fresh
        last_ts = convo.get("_last_activity", 0)
        if convo["confirmed"] and (now - last_ts) > 3600:
            logger.info(f"Expired confirmed conversation for {phone}, starting new")
            convo = new_conversation(phone)
            conversations[phone] = convo
        convo["_last_activity"] = now
        return convo

    convo = new_conversation(phone)
    convo["_last_activity"] = now
    conversations[phone] = convo
    return convo


def process_cold_message(convo, message_text):
    """Handle messages for a COLD conversation (full tree)."""
    state = convo["state"]

    if state == "init":
        # Welcome + ask for name
        convo["state"] = "awaiting_name"
        send_whatsapp_message(
            convo["phone"],
            "Welcome to Yamaja Engines Ltd! 🛥️ I'm Claudia, your AI assistant. "
            "Before we get started, could you please provide your full name?"
        )
        return

    elif state == "awaiting_name":
        convo["customer_name"] = message_text.strip()
        convo["state"] = "awaiting_fisherman_id"
        send_whatsapp_message(
            convo["phone"],
            f"Thanks, {convo['customer_name']}! Do you have a Fisherman ID? "
            "If so, please share it — we can apply tax incentives for registered "
            "fishermen before quoting. If not, just type 'No'."
        )
        return

    elif state == "awaiting_fisherman_id":
        fid = message_text.strip()
        convo["fisherman_id"] = "" if fid.lower() in ("no", "n/a", "none", "nope", "-") else fid
        convo["state"] = "awaiting_menu_1"
        send_whatsapp_list(
            convo["phone"],
            f"Great, thank you {convo['customer_name']}! What are you looking for today?",
            "Choose One",
            ["Engines", "Parts", "Service", "Something Else"]
        )
        return

    elif state == "awaiting_menu_1":
        choice = message_text.strip().lower()
        if "engine" in choice:
            convo["branch"] = "engine_sales"
            convo["state"] = "branch_engine_a1"
            send_whatsapp_buttons(
                convo["phone"],
                "Outstanding! We stock a range of new (and sometimes pre-owned) "
                "Yamaha outboard motors. Do you know which model you're looking for, "
                "or would you like help narrowing down?",
                ["I Know What I Want", "Help Me Choose"]
            )
        elif "part" in choice:
            convo["branch"] = "parts_sales"
            convo["state"] = "branch_parts_b1"
            send_whatsapp_buttons(
                convo["phone"],
                "You need parts for your Yamaha engine? Do you have a parts list "
                "already, or do you need help identifying what you need?",
                ["I Have Engine Info", "I Have Part Numbers", "Other Parts Request"]
            )
        elif "service" in choice:
            convo["branch"] = "service"
            convo["state"] = "branch_service_c1"
            send_whatsapp_buttons(
                convo["phone"],
                "We offer full Yamaha-certified service and maintenance. What do you need?",
                ["Routine Maintenance", "Repair / Diagnostic", "Repowering / Rigging"]
            )
        else:
            # Something Else → List 2
            convo["state"] = "awaiting_menu_2"
            send_whatsapp_list(
                convo["phone"],
                "No problem! What about these?",
                "Choose One",
                ["Boats", "Trailers", "Electronics", "Something Else"]
            )
        return

    elif state == "awaiting_menu_2":
        choice = message_text.strip().lower()
        if "boat" in choice:
            convo["branch"] = "boat_sales"
            convo["state"] = "branch_boat_d1"
            send_whatsapp_buttons(
                convo["phone"],
                "We carry new and pre-owned boats from top brands. What are you looking for?",
                ["New", "Used", "Either"]
            )
        elif "trailer" in choice:
            convo["branch"] = "trailers"
            convo["state"] = "branch_trailer_e1"
            send_whatsapp_message(
                convo["phone"],
                "We carry single and tandem axle boat trailers for all sizes. "
                "To recommend the right trailer, can you tell me your boat size, "
                "make, and/or model — whatever you have?"
            )
        elif "electronic" in choice:
            convo["branch"] = "electronics"
            convo["state"] = "branch_electronics_f1"
            send_whatsapp_list(
                convo["phone"],
                "We carry marine electronics and audio from several leading brands. "
                "Which brand are you interested in?",
                "Choose Brand",
                ["Garmin", "JL Audio", "Fusion Audio", "Interstate Batteries", "Other Brand"]
            )
        else:
            # Something Else → List 3
            convo["state"] = "awaiting_menu_3"
            send_whatsapp_list(
                convo["phone"],
                "We've got more! Looking for any of these?",
                "Choose One",
                ["ATVs & UTVs", "Fishing Gear", "General Accessories", "Something Else"]
            )
        return

    elif state == "awaiting_menu_3":
        choice = message_text.strip().lower()
        if "atv" in choice or "utv" in choice:
            convo["branch"] = "atv_utv"
            convo["state"] = "branch_atv_g1"
            send_whatsapp_buttons(
                convo["phone"],
                "We carry Yamaha ATVs and side-by-side UTVs for farming, utility, "
                "and recreation. What are you looking for?",
                ["ATV (Quad)", "UTV / Side-by-Side", "Not Sure — Help Me"]
            )
        elif "fishing" in choice:
            convo["branch"] = "fishing_gear"
            convo["state"] = "branch_fishing_h1"
            send_whatsapp_list(
                convo["phone"],
                "We carry fishing gear from top brands. What are you looking for?",
                "Choose Category",
                ["Iland Lures", "Daiwa Fishing", "General Fishing Gear", "Something Specific"]
            )
        elif "accessor" in choice:
            convo["branch"] = "general_accessories"
            convo["state"] = "branch_gen_acc_i1"
            send_whatsapp_message(
                convo["phone"],
                "We stock a wide range of marine accessories — engine parts, safety "
                "equipment, anchoring gear, boat covers, propellers, lubricants, "
                "and more. What are you looking for?"
            )
        else:
            # General Inquiry catch-all
            convo["branch"] = "general_inquiry"
            convo["state"] = "branch_general_j1"
            send_whatsapp_message(
                convo["phone"],
                "No problem at all! Tell me what you're looking for or how we can "
                "help, and I'll make sure the right person follows up with you."
            )
        return

    # ─── Branch State Handlers ───────────────────────────────────────────

    # ENGINE SALES (Branch A)
    elif state == "branch_engine_a1":
        choice = message_text.strip().lower()
        if "know" in choice or "want" in choice:
            convo["state"] = "branch_engine_a2a"
            send_whatsapp_message(
                convo["phone"],
                "What model do you need? I can forward the information to a "
                "sales representative right away once we have all the details. "
                "Also, will you need any additional accessories with the engine?"
            )
        else:
            convo["state"] = "branch_engine_a2b"
            send_whatsapp_message(
                convo["phone"],
                "No problem! What will the engine be used for — commercial fishing, "
                "pleasure boating, or something else? And what size boat will it "
                "be going on (approximate length in feet)?"
            )
        return

    elif state == "branch_engine_a2a":
        convo["engine_model"] = message_text.strip()
        convo["state"] = "branch_engine_a3"
        send_whatsapp_buttons(
            convo["phone"],
            "Are you looking for brand new, pre-owned, or open to either?",
            ["New", "Pre-Owned", "Either"]
        )
        return

    elif state == "branch_engine_a2b":
        # Parse use case and boat size from free text
        convo["use_case"] = message_text.strip()
        convo["state"] = "branch_engine_a3"
        send_whatsapp_buttons(
            convo["phone"],
            "Are you looking for brand new, pre-owned, or open to either?",
            ["New", "Pre-Owned", "Either"]
        )
        return

    elif state == "branch_engine_a3":
        convo["condition_preference"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # PARTS SALES (Branch B)
    elif state == "branch_parts_b1":
        choice = message_text.strip().lower()
        if "engine info" in choice or "have engine" in choice:
            convo["sub_branch"] = "engine_info"
            convo["state"] = "branch_parts_b2a"
            send_whatsapp_message(
                convo["phone"],
                "Please send me your engine model, year, and serial number, "
                "along with what parts you're looking for. We'll check pricing "
                "and availability right away."
            )
        elif "part number" in choice:
            convo["sub_branch"] = "part_numbers"
            convo["state"] = "branch_parts_b2b"
            send_whatsapp_message(
                convo["phone"],
                "Great — please list the Yamaha part numbers you need quoted."
            )
        else:
            convo["sub_branch"] = "other"
            convo["state"] = "branch_parts_b2c"
            send_whatsapp_message(
                convo["phone"],
                "No problem — describe what you need and we'll track it down."
            )
        return

    elif state in ("branch_parts_b2a", "branch_parts_b2b", "branch_parts_b2c"):
        if state == "branch_parts_b2a":
            convo["unit_info"] = message_text.strip()
        elif state == "branch_parts_b2b":
            convo["parts_list"] = message_text.strip()
        else:
            convo["parts_description"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # SERVICE (Branch C)
    elif state == "branch_service_c1":
        choice = message_text.strip().lower()
        if "routine" in choice or "maintenance" in choice:
            convo["service_type"] = "routine_maintenance"
            convo["state"] = "branch_service_c2a"
            send_whatsapp_message(
                convo["phone"],
                "What engine model and serial number do you have? And when was "
                "the last service, if you remember?"
            )
        elif "repair" in choice or "diagnostic" in choice:
            convo["service_type"] = "repair_diagnostic"
            convo["state"] = "branch_service_c2b"
            send_whatsapp_message(
                convo["phone"],
                "What's going on with the engine? Please describe the issue and "
                "share your engine model and serial number."
            )
        else:
            convo["service_type"] = "repowering_rigging"
            convo["state"] = "branch_service_c2c"
            send_whatsapp_message(
                convo["phone"],
                "Looking to repower or get a new engine installed? Tell me about "
                "your current setup — what boat and engine do you have now, and do "
                "you have a new engine in mind? Note: we offer installation and "
                "rigging on new Yamaha engines purchased from us."
            )
        return

    elif state in ("branch_service_c2a", "branch_service_c2b", "branch_service_c2c"):
        if state == "branch_service_c2a":
            convo["service_engine_model"] = message_text.strip()
        elif state == "branch_service_c2b":
            convo["issue_description"] = message_text.strip()
        else:
            convo["desired_engine"] = message_text.strip()
        convo["state"] = "branch_service_c3"
        send_whatsapp_buttons(
            convo["phone"],
            "Where is the boat/engine located (Kingston, Montego Bay, etc.)? "
            "And how urgent is this?",
            ["Urgent — Need ASAP", "Can Schedule", "Just Getting a Quote"]
        )
        return

    elif state == "branch_service_c3":
        convo["urgency"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # BOAT SALES (Branch D)
    elif state == "branch_boat_d1":
        convo["boat_condition"] = message_text.strip()
        convo["state"] = "branch_boat_d2"
        send_whatsapp_message(
            convo["phone"],
            "What type of boating — commercial fishing, pleasure, or other? "
            "And do you have a size range in mind (length in feet)?"
        )
        return

    elif state == "branch_boat_d2":
        convo["boat_use"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # TRAILERS (Branch E)
    elif state == "branch_trailer_e1":
        convo["trailer_boat_info"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # ELECTRONICS (Branch F)
    elif state == "branch_electronics_f1":
        choice = message_text.strip().lower()
        if "garmin" in choice:
            convo["electronics_brand"] = "Garmin"
            convo["sub_branch"] = "garmin"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                convo["phone"],
                "Fishfinder, GPS chartplotter, combo unit, or something else "
                "from Garmin? Let us know what you're looking for and we'll "
                "check availability."
            )
        elif "jl" in choice:
            convo["electronics_brand"] = "JL Audio"
            convo["sub_branch"] = "jl_audio"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                convo["phone"],
                "What JL Audio marine product are you looking for — speakers, "
                "amplifiers, subwoofers, or a full setup? Tell us about your "
                "boat and what you have in mind."
            )
        elif "fusion" in choice:
            convo["electronics_brand"] = "Fusion Audio"
            convo["sub_branch"] = "fusion_audio"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                convo["phone"],
                "What Fusion marine audio product are you interested in — stereo "
                "head units, speakers, amplifiers, or accessories? Let us know "
                "your setup."
            )
        elif "interstate" in choice or "batter" in choice:
            convo["electronics_brand"] = "Interstate Batteries"
            convo["sub_branch"] = "interstate_batteries"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                convo["phone"],
                "What type of Interstate battery do you need — starting, deep cycle, "
                "dual purpose? If you know your boat/engine setup, that helps us "
                "recommend the right one."
            )
        else:
            convo["electronics_brand"] = "Other"
            convo["sub_branch"] = "other_brand"
            convo["state"] = "branch_electronics_f2"
            send_whatsapp_message(
                convo["phone"],
                "No problem — tell us what electronics or audio equipment you're "
                "looking for, including the brand if you have one in mind."
            )
        return

    elif state == "branch_electronics_f2":
        convo["electronics_details"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # ATV/UTV (Branch G)
    elif state == "branch_atv_g1":
        choice = message_text.strip().lower()
        if "atv" in choice or "quad" in choice:
            convo["atv_type"] = "ATV"
            convo["state"] = "branch_atv_g2"
            send_whatsapp_message(
                convo["phone"],
                "What will the ATV be used for — farming/agriculture, recreation, "
                "or commercial/utility? And do you have a size or model in mind?"
            )
        elif "utv" in choice or "side" in choice:
            convo["atv_type"] = "UTV"
            convo["state"] = "branch_atv_g2"
            send_whatsapp_message(
                convo["phone"],
                "What will the UTV be used for — farming, property maintenance, "
                "or recreation? How many passengers do you need to seat?"
            )
        else:
            convo["atv_type"] = "Need Help"
            convo["state"] = "branch_atv_g2"
            send_whatsapp_message(
                convo["phone"],
                "Tell me about what you'll be using it for and the terrain "
                "(farm, hills, flat land, etc.), and I'll recommend the right model."
            )
        return

    elif state == "branch_atv_g2":
        convo["atv_use_case"] = message_text.strip()
        convo["state"] = "branch_atv_g3"
        send_whatsapp_buttons(
            convo["phone"],
            "Are you looking for brand new or open to pre-owned?",
            ["New", "Pre-Owned", "Either"]
        )
        return

    elif state == "branch_atv_g3":
        convo["condition_preference"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # FISHING GEAR (Branch H)
    elif state == "branch_fishing_h1":
        choice = message_text.strip().lower()
        if "iland" in choice:
            convo["fishing_brand"] = "Iland Lures"
            convo["sub_branch"] = "iland_lures"
            convo["state"] = "branch_fishing_h2"
            send_whatsapp_message(
                convo["phone"],
                "What Iland lures are you interested in? Let us know the type of "
                "fishing (offshore trolling, inshore, etc.) and any specific "
                "products or sizes you're after."
            )
        elif "daiwa" in choice:
            convo["fishing_brand"] = "Daiwa"
            convo["sub_branch"] = "daiwa"
            convo["state"] = "branch_fishing_h2"
            send_whatsapp_message(
                convo["phone"],
                "What Daiwa product are you looking for — reels, rods, line, "
                "or accessories? Let us know what type of fishing you do and "
                "we can recommend."
            )
        elif "general" in choice:
            convo["fishing_brand"] = "General"
            convo["sub_branch"] = "general"
            convo["state"] = "branch_fishing_h2"
            send_whatsapp_message(
                convo["phone"],
                "What fishing gear do you need — rods, reels, tackle, lures, "
                "line, or accessories? Let us know and we'll check what we "
                "have in stock."
            )
        else:
            convo["fishing_brand"] = "Specific"
            convo["sub_branch"] = "specific"
            convo["state"] = "branch_fishing_h2"
            send_whatsapp_message(
                convo["phone"],
                "Tell us exactly what you're after — brand, product, size, "
                "quantity — and we'll track it down."
            )
        return

    elif state == "branch_fishing_h2":
        convo["fishing_details"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # GENERAL ACCESSORIES (Branch I)
    elif state == "branch_gen_acc_i1":
        convo["accessories_details"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # GENERAL INQUIRY (Branch J)
    elif state == "branch_general_j1":
        convo["general_inquiry"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # ─── Confirmation Handler ────────────────────────────────────────────
    elif state == "awaiting_confirm":
        choice = message_text.strip().lower()
        if "yes" in choice or "send" in choice or "✅" in choice or "look" in choice:
            convo["confirmed"] = True
            convo["state"] = "completed"
            forward_to_make(convo)
            send_whatsapp_message(
                convo["phone"],
                f"Thank you, {convo['customer_name']}! I've forwarded your "
                "inquiry to our sales team. They'll follow up with you shortly. "
                "Is there anything else I can help with?"
            )
            convo["state"] = "post_complete"
        else:
            # Let them fix → re-ask what to change
            convo["state"] = "awaiting_fix"
            send_whatsapp_message(
                convo["phone"],
                "No problem! What would you like to update? Just type the "
                "correction and I'll update the inquiry."
            )
        return

    elif state == "awaiting_fix":
        # Accept any correction as free text, update the relevant field
        convo["general_inquiry"] = (
            convo.get("general_inquiry", "") +
            " [Correction: " + message_text.strip() + "]"
        )
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + f"\n\nNote: {message_text.strip()}\n\nDoes this look right now?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    elif state in ("completed", "post_complete"):
        # Post-complete: check if they want something else
        choice = message_text.strip().lower()
        if any(w in choice for w in ["yes", "more", "else", "another"]):
            # Reset for new inquiry but keep name and fisherman ID
            name = convo["customer_name"]
            fid = convo["fisherman_id"]
            phone = convo["phone"]
            convo_new = new_conversation(phone)
            convo_new["customer_name"] = name
            convo_new["fisherman_id"] = fid
            convo_new["state"] = "awaiting_menu_1"
            convo_new["_last_activity"] = time.time()
            conversations[phone] = convo_new
            send_whatsapp_list(
                phone,
                f"Of course, {name}! What are you looking for?",
                "Choose One",
                ["Engines", "Parts", "Service", "Something Else"]
            )
        else:
            send_whatsapp_message(
                convo["phone"],
                "Thanks for reaching out to Yamaja Engines! Have a great day. 🙌"
            )
        return

    # Fallback for unknown states
    logger.warning(f"Unknown state '{state}' for phone {convo['phone']}")
    send_whatsapp_message(
        convo["phone"],
        "Sorry, I got a bit confused! Let me start fresh. "
        "Welcome to Yamaja Engines — what can I help you with today?"
    )
    convo["state"] = "awaiting_name"


def process_warm_message(convo, message_text):
    """Handle WARM flow — model detected from website, need name + express flow."""
    state = convo["state"]

    if state == "init":
        # W1: Confirm model, ask for name
        model = convo["engine_model"] or "the engine you selected"
        convo["state"] = "warm_awaiting_name"
        send_whatsapp_message(
            convo["phone"],
            f"Hey there! 👋 I see you're looking at the *{model}* on our website. "
            f"Great choice!\n\nBefore I get you a quote, could I grab your full name?"
        )
        return

    elif state == "warm_awaiting_name":
        convo["customer_name"] = message_text.strip()
        convo["state"] = "warm_awaiting_fisherman_id"
        send_whatsapp_message(
            convo["phone"],
            f"Thanks, {convo['customer_name']}! Quick question — do you have a "
            "Fisherman ID? It helps us apply tax incentives. If not, just type 'No'."
        )
        return

    elif state == "warm_awaiting_fisherman_id":
        fid = message_text.strip()
        convo["fisherman_id"] = "" if fid.lower() in ("no", "n/a", "none", "nope", "-") else fid

        # Route to the correct branch based on what was detected
        branch = convo["branch"]
        if branch == "engine_sales":
            convo["state"] = "warm_engine_condition"
            send_whatsapp_buttons(
                convo["phone"],
                f"Perfect! So you're interested in the *{convo['engine_model']}*. "
                "Are you looking for brand new, pre-owned, or open to either?",
                ["New", "Pre-Owned", "Either"]
            )
        elif branch == "trailers":
            convo["state"] = "branch_trailer_e1"
            send_whatsapp_message(
                convo["phone"],
                "We carry single and tandem axle boat trailers for all sizes. "
                "To recommend the right trailer, can you tell me your boat size, "
                "make, and/or model — whatever you have?"
            )
        elif branch == "electronics":
            convo["state"] = "branch_electronics_f1"
            send_whatsapp_list(
                convo["phone"],
                "We carry marine electronics and audio from several leading brands. "
                "Which brand are you interested in?",
                "Choose Brand",
                ["Garmin", "JL Audio", "Fusion Audio", "Interstate Batteries", "Other Brand"]
            )
        elif branch == "fishing_gear":
            convo["state"] = "branch_fishing_h1"
            send_whatsapp_list(
                convo["phone"],
                "We carry fishing gear from top brands. What are you looking for?",
                "Choose Category",
                ["Iland Lures", "Daiwa Fishing", "General Fishing Gear", "Something Specific"]
            )
        elif branch == "general_accessories":
            convo["state"] = "branch_gen_acc_i1"
            send_whatsapp_message(
                convo["phone"],
                "We stock a wide range of marine accessories — engine parts, safety "
                "equipment, anchoring gear, boat covers, propellers, lubricants, "
                "and more. What are you looking for?"
            )
        elif branch == "atv_utv":
            convo["state"] = "branch_atv_g1"
            send_whatsapp_buttons(
                convo["phone"],
                "We carry Yamaha ATVs and side-by-side UTVs for farming, utility, "
                "and recreation. What are you looking for?",
                ["ATV (Quad)", "UTV / Side-by-Side", "Not Sure — Help Me"]
            )
        else:
            # Fallback: full menu
            convo["state"] = "awaiting_menu_1"
            send_whatsapp_list(
                convo["phone"],
                f"Thanks, {convo['customer_name']}! What are you looking for today?",
                "Choose One",
                ["Engines", "Parts", "Service", "Something Else"]
            )
        return

    elif state == "warm_engine_condition":
        convo["condition_preference"] = message_text.strip()
        convo["state"] = "warm_engine_accessories"
        send_whatsapp_message(
            convo["phone"],
            "Got it! Will you need any additional accessories with the engine, "
            "or just the standard included accessories?"
        )
        return

    elif state == "warm_engine_accessories":
        convo["accessories_needed"] = message_text.strip()
        convo["state"] = "awaiting_confirm"
        summary = build_summary(convo)
        send_whatsapp_buttons(
            convo["phone"],
            summary + "\n\nDoes this look right?",
            ["✅ Yes, Send It", "✏️ Let Me Fix"]
        )
        return

    # For non-engine warm branches, fall through to cold handler (shared branch states)
    process_cold_message(convo, message_text)


def process_hot_message(convo, message_text):
    """Handle HOT flow — most data already known, just confirm."""
    state = convo["state"]

    if state == "init":
        # H1: Show full summary for confirmation
        model = convo["engine_model"] or "the product you selected"
        name = convo["customer_name"] or "there"

        lines = [f"Hi {name}! 👋 I see you're checking out the *{model}* on our website."]
        if convo["customer_name"]:
            lines.append(f"I've pulled up your details:")
            lines.append("")
            lines.append(f"🔹 Name: {convo['customer_name']}")
        if convo["customer_type"]:
            lines.append(f"🔹 Customer Type: {convo['customer_type']}")
        if convo["current_engine"]:
            lines.append(f"🔹 Current Engine: {convo['current_engine']}")
        if convo["serial_number"]:
            lines.append(f"🔹 Serial: {convo['serial_number']}")
        lines.append(f"🔹 Interested In: {model}")
        lines.append("")
        lines.append("Does this look right? I can get you a quote right away!")

        convo["state"] = "hot_confirm"
        send_whatsapp_buttons(
            convo["phone"],
            "\n".join(lines),
            ["✅ Looks Good — Quote", "✏️ Change Something", "🔄 Something Else"]
        )
        return

    elif state == "hot_confirm":
        choice = message_text.strip().lower()
        if "look" in choice or "good" in choice or "quote" in choice or "✅" in choice:
            # Express close
            convo["confirmed"] = True
            convo["state"] = "completed"
            forward_to_make(convo)
            send_whatsapp_message(
                convo["phone"],
                f"Awesome! I've forwarded your inquiry to our sales team. "
                f"They'll reach out with pricing and availability for the "
                f"*{convo['engine_model']}* shortly.\n\n"
                "Is there anything else I can help with?"
            )
            convo["state"] = "post_complete"
        elif "change" in choice or "✏️" in choice:
            convo["state"] = "hot_edit"
            send_whatsapp_buttons(
                convo["phone"],
                "No problem! What would you like to update?",
                ["Different Model", "Parts Instead", "Need Service", "Other"]
            )
        elif "else" in choice or "🔄" in choice:
            # Jump to main menu with name already captured
            convo["state"] = "awaiting_menu_1"
            convo["temperature"] = "cold"  # Downgrade to cold flow for full menu
            send_whatsapp_list(
                convo["phone"],
                f"Of course, {convo['customer_name'] or 'there'}! How can I help you today?",
                "Choose One",
                ["Engines", "Parts", "Service", "Something Else"]
            )
        return

    elif state == "hot_edit":
        choice = message_text.strip().lower()
        if "model" in choice or "different" in choice:
            convo["branch"] = "engine_sales"
            convo["state"] = "branch_engine_a2a"
            send_whatsapp_message(
                convo["phone"],
                "What model are you interested in instead?"
            )
        elif "part" in choice:
            convo["branch"] = "parts_sales"
            convo["state"] = "branch_parts_b1"
            send_whatsapp_buttons(
                convo["phone"],
                "No problem — switching to parts! Do you have a parts list "
                "already, or do you need help identifying what you need?",
                ["I Have Engine Info", "I Have Part Numbers", "Other Parts Request"]
            )
        elif "service" in choice:
            convo["branch"] = "service"
            convo["state"] = "branch_service_c1"
            send_whatsapp_buttons(
                convo["phone"],
                "What type of service do you need?",
                ["Routine Maintenance", "Repair / Diagnostic", "Repowering / Rigging"]
            )
        else:
            convo["state"] = "awaiting_menu_1"
            convo["temperature"] = "cold"
            send_whatsapp_list(
                convo["phone"],
                "What are you looking for?",
                "Choose One",
                ["Engines", "Parts", "Service", "Something Else"]
            )
        return

    # For post-edit branch flows, use the cold message handler
    process_cold_message(convo, message_text)


def process_contact_form_hot(convo, message_text):
    """Handle HOT flow from website contact form proactive outreach."""
    state = convo["state"]

    if state == "contact_form_sent":
        choice = message_text.strip().lower()
        if "everything" in choice or "that's" in choice or "✅" in choice:
            convo["confirmed"] = True
            convo["state"] = "completed"
            forward_to_make(convo)
            send_whatsapp_message(
                convo["phone"],
                f"Perfect! Our team will be in touch shortly via email"
                f"{' at ' + convo['email'] if convo['email'] else ''} "
                "or WhatsApp. Thank you for reaching out!"
            )
            convo["state"] = "post_complete"
        elif "add" in choice or "more" in choice or "📝" in choice:
            # Route to the relevant branch
            branch = convo["branch"]
            if branch == "engine_sales":
                convo["state"] = "branch_engine_a1"
                send_whatsapp_buttons(
                    convo["phone"],
                    "Do you know which engine model you're looking for, "
                    "or would you like help narrowing down?",
                    ["I Know What I Want", "Help Me Choose"]
                )
            elif branch == "parts_sales":
                convo["state"] = "branch_parts_b1"
                send_whatsapp_buttons(
                    convo["phone"],
                    "Do you have a parts list already, or do you need help "
                    "identifying what you need?",
                    ["I Have Engine Info", "I Have Part Numbers", "Other Parts Request"]
                )
            elif branch == "boat_sales":
                convo["state"] = "branch_boat_d1"
                send_whatsapp_buttons(
                    convo["phone"],
                    "What type of boat are you looking for?",
                    ["New", "Used", "Either"]
                )
            elif branch == "service":
                convo["state"] = "branch_service_c1"
                send_whatsapp_buttons(
                    convo["phone"],
                    "What type of service do you need?",
                    ["Routine Maintenance", "Repair / Diagnostic", "Repowering / Rigging"]
                )
            else:
                convo["state"] = "awaiting_menu_1"
                send_whatsapp_list(
                    convo["phone"],
                    "What are you looking for?",
                    "Choose One",
                    ["Engines", "Parts", "Service", "Something Else"]
                )
        elif "question" in choice or "💬" in choice:
            convo["state"] = "contact_form_question"
            send_whatsapp_message(
                convo["phone"],
                "Of course — go ahead and ask! I'll do my best to help, "
                "or connect you with the right person."
            )
        return

    elif state == "contact_form_question":
        # Free text question → forward as lead
        convo["general_inquiry"] = message_text.strip()
        convo["confirmed"] = True
        convo["state"] = "completed"
        forward_to_make(convo)
        send_whatsapp_message(
            convo["phone"],
            "Great question! I've passed this along to our team — "
            "someone will get back to you shortly. Is there anything else?"
        )
        convo["state"] = "post_complete"
        return

    # For post-form branch flows, use the cold handler
    process_cold_message(convo, message_text)


# ─── Summary Builder ────────────────────────────────────────────────────────

def build_summary(convo):
    """Build a human-readable summary of the conversation data."""
    branch = convo["branch"]
    lines = []

    if branch == "engine_sales":
        lines.append("Here's your engine inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Model: {convo['engine_model'] or 'Need help choosing'}")
        lines.append(f"🔹 Condition: {convo['condition_preference']}")
        if convo["use_case"]:
            lines.append(f"🔹 Use: {convo['use_case']}")
        if convo["boat_size"]:
            lines.append(f"🔹 Boat Size: {convo['boat_size']}")
        if convo["accessories_needed"]:
            lines.append(f"🔹 Accessories: {convo['accessories_needed']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "parts_sales":
        lines.append("Here's your parts inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Request Type: {convo['sub_branch']}")
        if convo["unit_info"]:
            lines.append(f"🔹 Engine Info: {convo['unit_info']}")
        if convo["parts_list"]:
            lines.append(f"🔹 Part Numbers: {convo['parts_list']}")
        if convo["parts_description"]:
            lines.append(f"🔹 Description: {convo['parts_description']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "service":
        lines.append("Your service request:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Service Type: {convo['service_type']}")
        if convo["service_engine_model"]:
            lines.append(f"🔹 Engine: {convo['service_engine_model']}")
        if convo["service_serial"]:
            lines.append(f"🔹 Serial: {convo['service_serial']}")
        if convo["issue_description"]:
            lines.append(f"🔹 Issue: {convo['issue_description']}")
        if convo["desired_engine"]:
            lines.append(f"🔹 Desired Engine: {convo['desired_engine']}")
        if convo["urgency"]:
            lines.append(f"🔹 Urgency: {convo['urgency']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "boat_sales":
        lines.append("Your boat inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Condition: {convo['boat_condition']}")
        lines.append(f"🔹 Usage: {convo['boat_use']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "trailers":
        lines.append("Your trailer inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Boat Info: {convo['trailer_boat_info']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "electronics":
        lines.append("Your electronics inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Brand: {convo['electronics_brand']}")
        lines.append(f"🔹 Details: {convo['electronics_details']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "atv_utv":
        lines.append("Your ATV/UTV inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Type: {convo['atv_type']}")
        lines.append(f"🔹 Use: {convo['atv_use_case']}")
        if convo["atv_model"]:
            lines.append(f"🔹 Model: {convo['atv_model']}")
        lines.append(f"🔹 Condition: {convo['condition_preference']}")

    elif branch == "fishing_gear":
        lines.append("Your fishing gear inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Category: {convo['fishing_brand']}")
        lines.append(f"🔹 Details: {convo['fishing_details']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "general_accessories":
        lines.append("Your accessories inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Details: {convo['accessories_details']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    elif branch == "general_inquiry":
        lines.append("Here's what I have:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        lines.append(f"🔹 Request: {convo['general_inquiry']}")
        lines.append(f"🔹 Fisherman ID: {convo['fisherman_id'] or 'N/A'}")

    else:
        lines.append("Your inquiry:")
        lines.append("")
        lines.append(f"🔹 Name: {convo['customer_name']}")
        if convo.get("general_inquiry"):
            lines.append(f"🔹 Details: {convo['general_inquiry']}")

    return "\n".join(lines)


# ─── Flask Routes ────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    """Server health check."""
    return jsonify({
        "status": "ok",
        "version": "3.0.0",
        "conversations_active": len(conversations),
        "timestamp": datetime.now(timezone.utc).isoformat()
    })


@app.route('/webhook', methods=['GET'])
def webhook_verify():
    """Health check / webhook verification (GET)."""
    return jsonify({"status": "ok", "service": "yamaja-whatsapp-chatbot"})


@app.route('/webhook', methods=['POST'])
def webhook_incoming():
    """
    Incoming WhatsApp message from Interakt.
    This is the main endpoint — receives every customer message.
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    logger.info(f"Webhook received: {json.dumps(data, default=str)[:1000]}")

    # ─── Parse Interakt payload ──────────────────────────────────────────
    # Interakt webhook structure (actual):
    # { "type": "message_received", "data": {
    #     "customer": { "phone_number": "8769951632", "country_code": "+1", ... },
    #     "message": { "Initial Message": "Hi", "id": "...", ... }
    # }}

    # Skip non-message webhooks
    webhook_type = data.get("type", "")
    if webhook_type and webhook_type != "message_received":
        logger.info(f"Ignoring webhook type: {webhook_type}")
        return jsonify({"status": "ok", "note": f"ignored {webhook_type}"}), 200

    msg_data = data.get("data", {})
    customer = msg_data.get("customer", {})
    message_obj = msg_data.get("message", {})

    # Extract phone — try customer.phone_number first (Interakt actual format)
    phone = customer.get("phone_number", "") or customer.get("channel_phone_number", "")
    # Fallback: try message.from (WhatsApp Cloud API format)
    if not phone:
        phone = message_obj.get("from", "")

    # Extract message text — Interakt uses various field names
    message_text = ""
    # Try standard fields first
    if "text" in message_obj:
        message_text = message_obj["text"] if isinstance(message_obj["text"], str) else ""
    # Try "Initial Message" (Interakt's field for customer text)
    if not message_text and "Initial Message" in message_obj:
        message_text = message_obj["Initial Message"]
    # Try "message" key directly
    if not message_text and "message" in message_obj and isinstance(message_obj["message"], str):
        message_text = message_obj["message"]
    # Interactive reply (button click or list selection)
    if not message_text and "interactive" in message_obj:
        interactive = message_obj["interactive"]
        if "button_reply" in interactive:
            message_text = interactive["button_reply"].get("title", "")
        elif "list_reply" in interactive:
            message_text = interactive["list_reply"].get("title", "")
    # Last resort: scan all string values in message_obj for the actual text
    if not message_text:
        for key, val in message_obj.items():
            if isinstance(val, str) and key not in ("id", "country_code", "campaign_name", "raw_template",
                "channel_failure_reason", "message_status", "chat_message_type",
                "_internal_lead_source", "source_url", "campaign_id"):
                if len(val) > 0 and len(val) < 500:
                    message_text = val
                    logger.info(f"Extracted message from field '{key}': {val[:100]}")
                    break

    if not phone or not message_text:
        logger.warning(f"No phone or message_text found. phone='{phone}', keys={list(message_obj.keys())}")
        return jsonify({"status": "ok", "note": "no actionable content"}), 200

    # Clean phone number (remove +, spaces, etc.)
    phone = re.sub(r'[^\d]', '', phone)
    if phone.startswith("1") and len(phone) == 11:
        phone = phone  # Already has country code
    elif len(phone) == 10:
        phone = "1" + phone  # Add Jamaica country code

    # ─── Get/Create Conversation ─────────────────────────────────────────
    convo = get_or_create_conversation(phone)
    convo["message_count"] += 1
    convo["all_messages"].append(message_text[:500])
    convo["last_message_at"] = datetime.now(timezone.utc).isoformat()

    logger.info(f"Processing: phone={phone}, state={convo['state']}, temp={convo['temperature']}, msg='{message_text[:100]}'")

    # ─── Wave Runner Check (any state) ───────────────────────────────────
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
        # Forward as informational lead
        forward_to_make(convo)
        return jsonify({"status": "ok"}), 200

    # ─── First Message: Detect Temperature ───────────────────────────────
    if convo["message_count"] == 1 and convo["state"] == "init":
        temp, branch, extracted, pattern_name = detect_temperature(message_text)
        convo["temperature"] = temp
        convo["branch"] = branch or convo["branch"]
        convo["website_referral"] = temp != "cold"

        if temp != "cold":
            convo["source_page"] = extracted.get("source_page", "engine-detail")

        # Apply extracted fields
        for field, value in extracted.items():
            if field != "source_page" and value:
                convo[field] = value

        logger.info(
            f"Temperature detected: {temp} | Branch: {branch} | "
            f"Pattern: {pattern_name} | Phone: {phone}"
        )

    # ─── Route by Temperature ────────────────────────────────────────────
    temp = convo["temperature"]

    if temp == "hot" and convo.get("source_page") == "contact-form":
        process_contact_form_hot(convo, message_text)
    elif temp == "hot":
        process_hot_message(convo, message_text)
    elif temp == "warm":
        process_warm_message(convo, message_text)
    else:
        process_cold_message(convo, message_text)

    return jsonify({"status": "ok"}), 200


@app.route('/webhook/website', methods=['POST'])
def webhook_website():
    """
    Receive website contact form submissions.
    Creates a HOT conversation and sends proactive WhatsApp outreach.
    """
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    logger.info(f"Website form received: {json.dumps(data, default=str)[:500]}")

    # Required: phone number
    phone = data.get("phone", "")
    phone = re.sub(r'[^\d]', '', phone)
    if not phone or len(phone) < 7:
        return jsonify({"error": "Phone number required for WhatsApp follow-up"}), 400

    if phone.startswith("1") and len(phone) == 11:
        pass
    elif len(phone) == 10:
        phone = "1" + phone

    # Create HOT conversation
    convo = new_conversation(phone)
    convo["temperature"] = "hot"
    convo["source_page"] = "contact-form"
    convo["customer_name"] = data.get("name", "")
    convo["email"] = data.get("email", "")
    convo["website_message"] = data.get("message", "")
    convo["source"] = data.get("source", "website_contact_form")

    # Map inquiry type to branch
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
    convo["_last_activity"] = time.time()
    conversations[phone] = convo

    # ─── Send proactive WhatsApp outreach (Hot Flow Step F1) ─────────────
    lines = [
        f"Hi {convo['customer_name'] or 'there'}! 👋 This is Claudia from Yamaja Engines. "
        f"I received your inquiry from our website:",
        "",
        f"🔹 Inquiry Type: {inquiry_type}",
    ]
    if convo["engine_model"]:
        lines.append(f"🔹 Engine: {convo['engine_model']}"
                     + (f" ({convo['engine_family']})" if convo["engine_family"] else ""))
    if convo["website_message"]:
        # Truncate long messages
        msg_preview = convo["website_message"][:200]
        lines.append(f"🔹 Message: {msg_preview}")

    lines.append("")
    lines.append("I've passed this to our team. Would you like to add any details, "
                 "or is this all we need?")

    send_whatsapp_buttons(
        phone,
        "\n".join(lines),
        ["✅ That's Everything", "📝 Add More Details", "💬 I Have a Question"]
    )

    return jsonify({"status": "ok", "message": "WhatsApp follow-up initiated"}), 200


@app.route('/leads', methods=['GET'])
def list_leads():
    """View all captured leads/conversations."""
    leads = []
    for phone, convo in conversations.items():
        leads.append({
            "phone": convo["phone"],
            "name": convo["customer_name"],
            "temperature": convo["temperature"],
            "branch": convo["branch"],
            "state": convo["state"],
            "confirmed": convo["confirmed"],
            "message_count": convo["message_count"],
            "created_at": convo["created_at"],
            "last_message_at": convo["last_message_at"]
        })
    return jsonify({
        "total": len(leads),
        "leads": sorted(leads, key=lambda x: x["created_at"], reverse=True)
    })


@app.route('/leads/<phone>', methods=['GET'])
def get_lead(phone):
    """View a single conversation thread."""
    phone = re.sub(r'[^\d]', '', phone)
    convo = conversations.get(phone)
    if not convo:
        return jsonify({"error": "Conversation not found"}), 404
    # Return a clean copy without internal fields
    result = {k: v for k, v in convo.items() if not k.startswith("_")}
    return jsonify(result)


# ─── Main ────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    logger.info(f"Starting Yamaja WhatsApp Chatbot on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
