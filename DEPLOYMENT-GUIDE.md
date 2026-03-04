# Yamaja Chatbot — Complete Deployment & Setup Guide

## Overview

This system has 4 components that need to be set up in order:

```
1. Render (webhook server)     → Receives messages, manages state
2. Interakt (WhatsApp chatbot) → Sends/receives WhatsApp messages
3. Make.com (automation)       → Routes leads, sends emails & notifications
4. Website (yamja.com)         → Contact form webhook integration
```

---

## Step 1: Deploy Render Webhook Server

### 1a. Create GitHub Repository

```bash
# Create new repo on GitHub: yamaja-chatbot (private)
cd yamaja-chatbot
git init
git add .
git commit -m "Yamaja WhatsApp chatbot webhook server v3"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/yamaja-chatbot.git
git push -u origin main
```

### 1b. Deploy on Render

1. Go to [render.com](https://render.com) → **New** → **Web Service**
2. Connect your GitHub account and select `yamaja-chatbot` repo
3. Render auto-detects `render.yaml`:
   - **Name**: yamaja-whatsapp
   - **Runtime**: Python
   - **Plan**: Free
4. Click **Create Web Service**
5. Wait for first deploy (~2-3 minutes)

### 1c. Set Environment Variables

Go to **Environment** tab in Render dashboard:

| Variable | Value | Notes |
|----------|-------|-------|
| `INTERAKT_API_KEY` | *(from Step 2)* | Get from Interakt dashboard after setup |
| `MAKE_WEBHOOK_URL` | *(from Step 3)* | Get from Make.com after creating scenario |

### 1d. Verify Deployment

Visit: `https://yamaja-whatsapp.onrender.com/health`

You should see:
```json
{"status": "ok", "version": "3.0.0", "conversations_active": 0}
```

**Note**: Free tier Render has a ~30-50 second cold start after 15 min of inactivity. This is fine — the webhook responds 200 immediately and processes async.

---

## Step 2: Set Up Interakt (Claudia WhatsApp)

### 2a. Create Interakt Account

1. Go to [interakt.shop](https://www.interakt.shop/) → Sign Up
2. Connect WhatsApp Business: **18765642888** (Claudia's number)
3. Verify the phone number via SMS/call

### 2b. Get API Key

1. Go to **Settings** → **Developer Settings** → **API Keys**
2. Copy the API key
3. Paste it into Render environment variable: `INTERAKT_API_KEY`
4. **Redeploy** Render service after adding the key

### 2c. Set Up Webhook in Interakt

1. Go to **Settings** → **Webhooks**
2. Add webhook URL: `https://yamaja-whatsapp.onrender.com/webhook`
3. Subscribe to events:
   - ✅ **Message received** (incoming customer messages)
   - ✅ **Message status** (delivery/read receipts — optional)
4. Test the webhook — Interakt should show a green checkmark

### 2d. Build Claudia Chatbot Flows in Interakt

The Render server handles ALL conversation logic (state machine, branching, temperature detection). Interakt just needs to:

1. **Receive messages** and forward them to the webhook
2. **Send messages** when the Render server calls the Interakt API

No flow builder configuration needed in Interakt — the Render server IS the flow builder.

**However**, if you want a backup auto-reply for when Render is cold-starting:

1. Go to **Automation** → **Auto Reply**
2. Set a 30-second delay auto-reply:
   > "Hi! I'm Claudia, getting your info ready... one moment please! 🛥️"
3. This only triggers if the Render cold start takes too long

---

## Step 3: Set Up Make.com Scenario

### 3a. Create Make.com Account

1. Go to [make.com](https://www.make.com/) → Sign Up (free plan works)
2. Create a new **Scenario**

### 3b. Module 1: Custom Webhook (Trigger)

1. Add module: **Webhooks** → **Custom webhook**
2. Click **Add** → Name it: "Yamaja Lead Intake"
3. Copy the webhook URL (looks like: `https://hook.us1.make.com/xxx...`)
4. Paste this URL into Render environment variable: `MAKE_WEBHOOK_URL`
5. **Redeploy** Render service

**Test it**: Send a test message to Claudia's WhatsApp. Complete a conversation. The webhook should receive the lead payload and show sample data in Make.com.

### 3c. Module 2: Router

Add a **Router** after the webhook. Create these routes:

| Route # | Filter Name | Condition | 
|---------|-------------|-----------|
| 1 | Engine Sales | `branch` = `engine_sales` |
| 2 | Parts Sales | `branch` = `parts_sales` |
| 3 | Service | `branch` = `service` |
| 4 | Boat Sales | `branch` = `boat_sales` |
| 5 | Trailers | `branch` = `trailers` |
| 6 | Electronics | `branch` = `electronics` |
| 7 | ATVs/UTVs | `branch` = `atv_utv` |
| 8 | Fishing Gear | `branch` = `fishing_gear` |
| 9 | Accessories | `branch` = `general_accessories` |
| 10 | General Inquiry | `branch` = `general_inquiry` |
| 11 | Wave Runner | `branch` = `wave_runner_banned` |

### 3d. Module 3: Email (per route)

For each route, add **Email** → **Send an Email**:

- **To**: `contact@yamahajamaica.com`
- **Subject**: Use the templates below
- **Body**: Use the templates below

#### Engine Sales Email
```
Subject: 🚤 New Engine Inquiry — {{customer_name}} {{#if temperature == "hot"}}(Website Referral){{/if}}

NEW YAMAJA LEAD — ENGINE SALES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Lead Temperature: {{temperature}} {{#if source_page}}(from {{source_page}}){{/if}}
Confirmed by Customer: {{#if confirmed}}Yes ✅{{else}}No (partial){{/if}}

Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Customer Type: {{customer_type}}
Fisherman ID:  {{fisherman_id}}

ENGINE REQUEST:
Model:         {{engine_model}}
Family:        {{engine_family}}
Condition:     {{condition_preference}}
Use Case:      {{use_case}}
Boat Size:     {{boat_size}}
Accessories:   {{accessories_needed}}

EXISTING ENGINE (if upgrading):
Current:       {{current_engine}}
Serial:        {{serial_number}}

Full Conversation:
{{all_messages}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reply on WhatsApp: https://wa.me/{{phone}}
```

#### Parts Sales Email
```
Subject: 🔧 New Parts Inquiry — {{customer_name}}

NEW YAMAJA LEAD — PARTS SALES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Fisherman ID:  {{fisherman_id}}

PARTS REQUEST:
Type:          {{sub_branch}}
Engine Info:   {{unit_info}}
Part Numbers:  {{parts_list}}
Description:   {{parts_description}}

Full Conversation:
{{all_messages}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reply on WhatsApp: https://wa.me/{{phone}}
```

#### Service Request Email
```
Subject: 🛠️ Service Request — {{customer_name}}

NEW YAMAJA LEAD — SERVICE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Fisherman ID:  {{fisherman_id}}

SERVICE REQUEST:
Type:          {{service_type}}
Engine:        {{service_engine_model}}
Serial:        {{service_serial}}
Issue:         {{issue_description}}
Desired Engine: {{desired_engine}}
Location:      {{service_location}}
Urgency:       {{urgency}}

Full Conversation:
{{all_messages}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reply on WhatsApp: https://wa.me/{{phone}}
```

#### Boat Sales Email
```
Subject: ⛵ New Boat Inquiry — {{customer_name}}

NEW YAMAJA LEAD — BOAT SALES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Fisherman ID:  {{fisherman_id}}

BOAT REQUEST:
Condition:     {{boat_condition}}
Usage:         {{boat_use}}
Size:          {{boat_size}}

Full Conversation:
{{all_messages}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reply on WhatsApp: https://wa.me/{{phone}}
```

#### Trailers Email
```
Subject: 🚛 Trailer Inquiry — {{customer_name}}

Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Boat Info:     {{trailer_boat_info}}
Fisherman ID:  {{fisherman_id}}

Reply on WhatsApp: https://wa.me/{{phone}}
```

#### Electronics Email
```
Subject: 🔊 Electronics Inquiry ({{electronics_brand}}) — {{customer_name}}

Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Brand:         {{electronics_brand}}
Details:       {{electronics_details}}
Fisherman ID:  {{fisherman_id}}

Reply on WhatsApp: https://wa.me/{{phone}}
```

#### ATV/UTV Email
```
Subject: 🏍️ ATV/UTV Inquiry — {{customer_name}}

Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Type:          {{atv_type}}
Use Case:      {{atv_use_case}}
Model:         {{atv_model}}
Condition:     {{condition_preference}}

Reply on WhatsApp: https://wa.me/{{phone}}
```

#### Fishing Gear Email
```
Subject: 🎣 Fishing Gear Inquiry ({{fishing_brand}}) — {{customer_name}}

Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Category:      {{fishing_brand}}
Details:       {{fishing_details}}
Fisherman ID:  {{fisherman_id}}

Reply on WhatsApp: https://wa.me/{{phone}}
```

#### General Accessories Email
```
Subject: ⚓ Accessories Inquiry — {{customer_name}}

Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Details:       {{accessories_details}}
Fisherman ID:  {{fisherman_id}}

Reply on WhatsApp: https://wa.me/{{phone}}
```

#### General Inquiry Email
```
Subject: 📋 General Inquiry — {{customer_name}}

Customer:      {{customer_name}}
Phone:         {{phone}}
Email:         {{email}}
Request:       {{general_inquiry}}
Fisherman ID:  {{fisherman_id}}

Full Conversation:
{{all_messages}}

Reply on WhatsApp: https://wa.me/{{phone}}
```

#### Wave Runner Email
```
Subject: ⚠️ Wave Runner Inquiry (Import Ban) — {{customer_name}}

Customer:      {{customer_name}}
Phone:         {{phone}}
Note: Customer inquired about wave runners/jet skis. They were informed about the Jamaica import ban on PWCs.

Reply on WhatsApp: https://wa.me/{{phone}}
```

### 3e. Module 4: WhatsApp Notification to Sales (per route)

After each email module, add **HTTP** → **Make a request**:

This sends a WhatsApp summary to the general sales line (18763712888) via the Interakt API.

- **URL**: `https://api.interakt.ai/v1/public/message/`
- **Method**: POST
- **Headers**:
  - `Authorization`: `Basic YOUR_INTERAKT_API_KEY`
  - `Content-Type`: `application/json`
- **Body** (JSON):
```json
{
  "countryCode": "+1",
  "phoneNumber": "8763712888",
  "type": "Text",
  "data": {
    "message": "📩 New {{branch}} lead ({{temperature}})\nName: {{customer_name}}\nPhone: {{phone}}\n{{engine_model || boat_condition || electronics_brand || general_inquiry}}\nReply: https://wa.me/{{phone}}"
  }
}
```

### 3f. Module 5: Google Sheets Log (Optional)

Add **Google Sheets** → **Add a Row** at the end of each route:

Create a Google Sheet with columns:
| Timestamp | Temperature | Name | Phone | Email | Branch | Sub-Branch | Model | Details | Confirmed | Source |

Map the webhook fields to each column.

### 3g. Activate the Scenario

1. Turn on the scenario (**ON** toggle)
2. Set scheduling to **Immediately** (webhook-triggered)
3. Save

---

## Step 4: Update Website (yamja.com)

The website files have already been updated with the Render webhook integration. When the contact form or boats form is submitted with a phone number, it now sends a parallel POST to the Render server.

### What Changed

**contact.html**:
- Updated inquiry dropdown with all 10 categories (Engines, Parts, Service, Boats, Trailers, Electronics & Audio, ATVs & UTVs, Fishing Gear, General Accessories, Other)
- Added fire-and-forget POST to `https://yamaja-whatsapp.onrender.com/webhook/website` after form submit (only when phone provided)

**boats.html**:
- Added same fire-and-forget POST to Render webhook with inquiry_type="Boats"

### Deploy to Cloudflare

1. Zip the `yamja-final/` folder (files at root, no subfolder)
2. Go to Cloudflare Pages → yamaha-jamaica project → Upload
3. After deploy: **Caching** → **Configuration** → **Purge Everything**

---

## Step 5: End-to-End Testing

### Test Matrix

| # | Test | Expected |
|---|------|----------|
| 1 | Send "Hello" to Claudia's WhatsApp directly | Cold flow: Welcome → Name → Fisherman ID → Menu |
| 2 | Click "Chat with Claudia" on F115BETL engine detail page (not logged in) | Warm flow: "I see you're looking at the F115BETL" → Name → Express engine flow |
| 3 | Click "Chat with Claudia" on engine page (logged in with profile) | Hot flow: Full summary → "Looks Good?" → Quote Me → Done (2 messages) |
| 4 | Submit contact form with phone (Engines inquiry) | Hot/proactive: Claudia texts "I received your inquiry..." on WhatsApp |
| 5 | Submit contact form without phone | Email only, no WhatsApp triggered |
| 6 | Submit boats form with phone | Hot/proactive: Claudia texts about boat inquiry |
| 7 | Say "jet ski" or "wave runner" at any point | Import ban message + lead logged |
| 8 | Complete any flow to "Does this look right?" → "Yes" | Email arrives at contact@yamahajamaica.com, WhatsApp notification to sales |
| 9 | After completing one inquiry, say "Yes, something else" | New menu appears, name/fisherman ID retained |

### Debugging

- **View active conversations**: `https://yamaja-whatsapp.onrender.com/leads`
- **View specific lead**: `https://yamaja-whatsapp.onrender.com/leads/18761234567`
- **Render logs**: Render Dashboard → yamaja-whatsapp → Logs tab
- **Make.com history**: Make.com → Scenario → History tab

---

## Architecture Recap

```
CUSTOMER                    RENDER                      INTERAKT              MAKE.COM
───────                    ──────                      ────────              ────────
WhatsApp msg ──────────────▶ /webhook                  
                           ├─ Detect temperature       
                           ├─ Parse fields             
                           ├─ Update state machine     
                           └─ Call Interakt API ──────▶ Send WhatsApp msg
                                                        to customer
Customer replies ──────────▶ /webhook                  
                           ├─ Track conversation       
                           ├─ Build summary            
                           └─ (on confirm) ───────────────────────────────▶ Router
                                                                          ├─ Email sales
                                                                          ├─ WhatsApp sales
                                                                          └─ Google Sheets

Website form ──────────────▶ /webhook/website          
                           ├─ Create HOT conversation  
                           └─ Call Interakt API ──────▶ Proactive WhatsApp
                                                        to customer
```

---

## Costs

| Service | Plan | Cost |
|---------|------|------|
| Render | Free tier | $0/mo (spins down after 15 min idle) |
| Interakt | Starter | ~$15-20/mo (includes WhatsApp Business API) |
| Make.com | Free tier | $0/mo (1,000 ops/month) |
| Cloudflare Pages | Free tier | $0/mo |

**Total**: ~$15-20/month (mainly Interakt)
