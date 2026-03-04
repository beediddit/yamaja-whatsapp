# Yamaja WhatsApp Chatbot — CHANGELOG

---

## [4.0.0] — 2026-03-04

**Complete architectural rewrite from v3.0.2. All 18 bugs from bug-analysis.md fixed,
plus 4 website integration issues addressed and multiple flow improvements.**

---

### Breaking Changes

- **SQLite replaces in-memory dict** — all conversation data is now stored in
  `yamaja_conversations.db`. The in-memory `conversations = {}` dict is gone.
  Existing in-memory sessions from v3.x are not migrated; all active conversations
  will start fresh on first deploy of v4.0.0.
- **Lead forwarding now goes to WhatsApp (SALES_PHONE) via Interakt**, not to
  Make.com. `forward_to_make()` is disabled (preserved as commented code).
- **Admin endpoints now require authentication** (`?secret=` or `X-Admin-Secret` header).
- **Service branch split** — `branch_service_c3` is now location (free text), and
  `branch_service_c4` is urgency (buttons). Any hardcoded state references to the
  old combined `branch_service_c3` will need updating.

---

### Bug Fixes

#### BUG 1 — SQLite persistence (was: in-memory only)
- Conversations stored in SQLite at `/opt/render/project/src/data/yamaja_conversations.db`
  on Render Starter plan (persistent across deploys).
- Falls back to `./data/yamaja_conversations.db` for local development.
- All reads/writes go through `load_conversation()` / `save_conversation()` helpers.
- `init_db()` called at startup to create tables if they don't exist.
- WAL mode enabled for concurrency safety.

#### BUG 2 — Interactive message parsing (was: raw JSON keyword matching)
- New `parse_interakt_message()` function handles all Interakt payload formats.
- Checks `message_obj.interactive.button_reply.title` and `list_reply.title`.
- Parses `message_obj.message` field as JSON when it contains an interactive reply.
- Supports nested `{"type": "interactive", "interactive": {...}}` format.
- Falls back through `text`, `Initial Message`, plain `message`, then last-resort
  field scan.
- Returns `(text, is_interactive)` tuple so buffering logic can skip interactive clicks.

#### BUG 3 — Intent detection from first message (was: intent ignored)
- `detect_intent()` runs keyword matching on the first cold message.
- Keywords: `part/parts` → parts_sales, `engine/motor/outboard` → engine_sales,
  `service/repair/maintenance/fix` → service, `boat` → boat_sales,
  `trailer` → trailers, `atv/utv/quad` → atv_utv,
  `fishing/lure/reel/rod` → fishing_gear, `accessor` → general_accessories.
- Intent stored as `pending_intent` and first message as `pending_details`.
- After name + fisherman ID collected, `_proceed_after_fisherman_id()` routes
  directly to the detected branch — skipping the three-page menu entirely.
- Engine model extracted from `pending_details` via `ENGINE_MODEL_PATTERN` and
  pre-filled into `engine_model` field.

#### BUG 4 — Phone-level locking (was: race conditions with multiple workers)
- `_get_phone_lock(phone)` returns a per-phone `threading.Lock` from a global dict.
- `webhook_incoming()` acquires the lock before calling `_process_message()` and
  releases it in a `finally` block.
- Configured for single Gunicorn worker (`--workers 1`) but lock prevents any
  future threading issues.

#### BUG 5 — First message not captured (was: context lost)
- `convo["first_message"]` set from `message_text` on `message_count == 1`.
- Included in lead forwarding message to sales team.

#### BUG 6 — Lead forwarding replaced (was: Make.com only)
- `forward_lead_to_whatsapp(convo)` sends a formatted WhatsApp text to `SALES_PHONE`
  via Interakt Send API.
- Message includes: name, phone, category, temperature, fisherman ID, first message,
  branch-specific details block, wa.me link, and timestamp.
- `forward_to_make()` is preserved as commented-out code for future use.

#### BUG 7 — Escalation logic (was: missing)
- `escalate_to_manager(convo)` sends an escalation WhatsApp to `MANAGER_PHONE`.
- `handle_returning_customer()` checks `lead_forwarded_at` timestamp:
  - < 4 hours: shows elapsed time, offers Start New Inquiry.
  - ≥ 4 hours: asks "Did our team get back to you?" with Yes/No buttons.
  - "No" → escalates to manager, marks `escalated = True` to prevent double-escalation.
- `convo["lead_forwarded_at"]` timestamp recorded in `handle_post_confirm()`.

#### BUG 8 — Voice note / media handling (was: silent failures)
- `webhook_incoming()` checks `message_content_type` for `audio`, `voice`, `image`,
  `video`, `document`, `sticker`.
- Also checks for `media_url` / `url` field presence.
- If media detected without text: sends "I can only process text messages right now"
  reply and returns without advancing state.

#### BUG 9 — No confirmation after lead forwarded (was: generic "thank you")
- `handle_post_confirm()` now sends two messages:
  1. "Your inquiry has been forwarded... If you don't hear from us within a few hours..."
  2. Category-specific website link from `BRANCH_LINKS` dict.

#### BUG 10 — Post-complete loop broken (was: confusing re-entry)
- `handle_returning_customer()` cleanly handles all returning customer scenarios.
- `completed` / `post_complete` states both route to `handle_returning_customer()`.
- `_start_new_inquiry()` resets the conversation but preserves name and fisherman ID.

#### BUG 11 — Debug endpoints unprotected (was: public access)
- `/leads`, `/leads/<phone>`, `/debug/webhooks` all require `ADMIN_SECRET`.
- Auth checked via `_check_admin_auth()` which reads `?secret=` query param or
  `X-Admin-Secret` request header.
- Returns HTTP 401 if secret is missing or wrong.
- New auth-protected endpoints: `POST /reset/<phone>`, `POST /reset-all`.

#### BUG 12 — Interakt name pre-fill not used (was: always asking for name)
- `_process_message()` extracts `data.customer.traits.name` (or `traits.full_name`)
  and stores as `convo["_traits_name"]`.
- Cold `init` state checks `_traits_name`: if present, shows confirmation button
  "Yes, {first_name}" / "Different Name" rather than asking from scratch.
- Warm `init` state also uses `_traits_name` to skip straight to fisherman ID.

#### BUG 13 — Free-text message batching missing (was: each message processed individually)
- 3-second buffer for all free-text input states in `BUFFERED_STATES` set (16 states).
- `_add_to_buffer()` stores text and records `_buffer_started_at` timestamp.
- `_flush_buffer()` returns concatenated buffer if ≥ 3 seconds old.
- `_process_message()` checks for expired buffer before each new message and flushes it.
- Interactive (button/list) clicks always process immediately and flush any pending buffer.

#### BUG 14 — Website prefill patterns incomplete (was: most patterns not matched)
Added 8 new patterns to `WEBSITE_PATTERNS`:
- `homepage_cta`: "learn more about what you offer" → cold (no branch jump)
- `engines_page`: "interested in Yamaha outboard engines" → warm, engine_sales
- `boats_page`: "interested in buying a boat" → warm, boat_sales
- `boats_specific`: "interested in the {model}" (without "specs") → warm, boat_sales, extracts boat_model
- `service_page`: "need help with service" → warm, service
- `parts_page`: "help finding a specific Yamaha part" → warm, parts_sales
- `accessories_page`: "looking for marine accessories" → warm, general_accessories
- `atv_page`: "interested in Yamaha ATVs/UTVs" → warm, atv_utv (replaces old regex)

YAMJA tag parsing improved: `page=boats` now also sets `branch = boat_sales` when
no pattern branch is set.

#### BUG 15 — Fisherman ID step stalling (was: free text only)
- Now uses buttons: "Yes, I Have One" / "No".
- If "Yes" → new state `awaiting_fisherman_id_value` asks for the ID as free text.
- Typed "yes"/"yeah"/"yep"/"yup" treated as button Yes.
- Typed "no"/"nah"/"nope"/"none" treated as button No.
- Any input containing digits accepted as the ID directly (handles "I typed my ID").

#### BUG 16 — Name confirmation missing (was: first message captured as name)
- After name entry, shows confirmation button "Yes, {first_word}" / "That's Wrong".
- States: `awaiting_name` → `awaiting_name_verify` → proceeds.
- Traits pre-fill confirmation: `awaiting_name_confirm` state with "Yes, {first_name}"
  / "Different Name" buttons.
- Same confirmation flow in warm flow (`warm_awaiting_name_verify`).

#### BUG 17 — MANAGER_PHONE not defined (was: undefined variable)
- `MANAGER_PHONE = '18769951632'` added as a module-level constant.

#### BUG 18 — Service branch location/urgency combined (was: single awkward prompt)
- **`branch_service_c3`** — now asks: "Where is the boat/engine located?" (free text).
  Stores to `convo["service_location"]`.
- **`branch_service_c4`** — asks urgency with buttons: "Urgent — Need ASAP",
  "Can Schedule", "Just Getting a Quote". Stores to `convo["urgency"]`.
- Both steps added to `BUFFERED_STATES` for c3 (c4 is button-driven).

---

### New Features

#### SQLite-Backed Webhook Log
- `webhook_log` table in SQLite stores last 50 raw payloads (replaces in-memory list).
- `log_webhook()` and `get_webhook_log()` helpers.
- Pruning done inline via `DELETE ... NOT IN (SELECT ... LIMIT 50)`.

#### SQLite-Backed Deduplication
- `dedup_keys` table replaces in-memory `_processed_messages` set.
- TTL reduced to 5 minutes (from 30 minutes) — duplicates arrive within seconds.
- Cleanup runs every 100th webhook (`cleanup_dedup_every_nth(100)`) instead of
  on size threshold.

#### Phone Normalisation Utility
- `normalize_phone(phone)` strips non-digits, adds leading `1` if 10 digits.
- Used consistently throughout: `webhook_incoming`, `webhook_website`, `/leads/<phone>`,
  `/reset/<phone>`.

#### New Endpoints
- `POST /reset/<phone>` — Reset a specific conversation (auth required).
- `POST /reset-all` — Reset all conversations (auth required).

#### Returning Customer Flows
Full returning customer state machine in `handle_returning_customer()`:
- **< 4 hours**: Shows elapsed time, reassures, offers new inquiry or done.
- **Waiting complaint**: Shows 4-hour countdown and expiry time.
- **≥ 4 hours**: Asks "Did our team reach out?" (Yes / No buttons).
- **No response**: Escalates to `MANAGER_PHONE` with context, marks `escalated=True`.
- **Yes, all good**: Offers new inquiry.
- Sub-states: `completed_awaiting_follow_up`, `completed_new_or_done`.

#### `_start_new_inquiry(convo)` Helper
Resets conversation for a new inquiry while preserving `customer_name` and
`fisherman_id`. Routes to `awaiting_menu_1` with personalised greeting.

#### Health Endpoint Improvements
`/health` now returns: `version`, `conversations_total`, `conversations_active`,
`db_path`, `timestamp`.

---

### Website Integration Improvements (Issues W1–W4)

#### W2 — Boats-specific WhatsApp YAMJA tag
- `boats_specific` pattern extracts `boat_model` group from
  "interested in the {model}" messages.
- YAMJA tag `page=boats` now correctly sets `branch = boat_sales`.
- `convo["boat_model"]` field added to conversation object and summary builder.

#### W4 — Engine detail family field sometimes empty
- `detect_temperature()` only applies `engine_family` from YAMJA tag when the
  value is non-empty (`tag_data["family"]` truthiness check added).
- Both buttons on the engine detail page should populate `family=` from the same
  data source (see website-side fix notes).

#### W1 / W3 — Parts lookup context, Engines page footer button
- Parts page pattern (`parts_page`) added — "help finding a specific Yamaha part"
  now correctly routes to `parts_sales` branch.
- Engines page pattern (`engines_page`) added — generic engines page WhatsApp button
  now detected as warm lead.

---

### Code Quality Improvements

- **Section headers** throughout the file with `# ─── Section Name ─────` dividers.
- **Comprehensive logging** at every state transition, send, parse step, and error.
- **All WhatsApp sends** wrapped in `try/except` with error logging; never crash the webhook.
- **Always return HTTP 200** from `/webhook` even on errors, preventing Interakt retries.
- **Branch jump helpers** (`_go_engine_sales()`, `_go_parts_sales()`, etc.) eliminate
  duplicated state-setting + send_whatsapp_* code across cold/warm/hot flows.
- **`_show_confirm()`** helper DRYs up the confirmation summary + button pattern.
- **`BRANCH_DISPLAY` dict** maps internal branch names to human-readable display names
  used in lead messages and returning customer flows.
- **`BRANCH_LINKS` dict** maps branches to category-specific website URLs sent
  post-confirmation.
- **Commented-out `forward_to_make()`** preserved for future Make.com integration
  with full field mapping intact.

---

## [3.0.2] — (baseline, GitHub)

- Flask webhook server deployed to Render
- In-memory conversation storage (`conversations = {}`)
- Temperature detection with 8 website patterns
- Full cold conversation flow: name → fisherman ID → 3-level menu → branches A–J
- Warm flow for engine-detected messages
- Hot flow for enriched website messages
- Contact form hot flow
- `forward_to_make()` for lead forwarding to Make.com
- `/leads`, `/debug/webhooks`, `/webhook`, `/webhook/website`, `/health` endpoints
- Deduplication via in-memory set with 30-minute TTL
- Wave runner ban detection
- YAMJA tag parsing
