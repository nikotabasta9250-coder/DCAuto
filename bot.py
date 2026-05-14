import discord
from discord.ext import commands, tasks
from discord import app_commands
import gspread
from google.oauth2.service_account import Credentials
from datetime import date, datetime, timedelta, time as dt_time, timezone
import asyncio
import os
import io
import time
import json
import concurrent.futures
import threading
import aiohttp

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from products import PRODUCTS

# ─── CONFIG ────────────────────────────────────────────────
BOT_TOKEN            = os.environ["BOT_TOKEN"]
SHEET_ID             = os.environ["SHEET_ID"]
OPS_CHANNEL_ID       = int(os.environ["OPS_CHANNEL_ID"])
SAMPLE_QUEUE_ID      = int(os.environ["SAMPLE_QUEUE_ID"])
GMV_SPRINT_STATUS    = os.environ.get("GMV_SPRINT_STATUS", "Open")   # Open | Paused | Waitlist
FOLLOWUP_HOUR        = int(os.environ.get("FOLLOWUP_HOUR", "9"))     # UTC hour for daily check
OPS_PING             = os.environ.get("OPS_PING", "<@1498307294017093712>")
CAT_APPLICATIONS     = os.environ.get("CAT_APPLICATIONS", "Applications")
CAT_SUPPORT          = os.environ.get("CAT_SUPPORT", "Support Tickets")
CAT_VIDEO            = os.environ.get("CAT_VIDEO", "Video Reviews")
import json, tempfile
_private_key = os.environ.get("GOOGLE_PRIVATE_KEY", "")
if _private_key:
    _creds_data = {
        "type": "service_account",
        "project_id": os.environ.get("GOOGLE_PROJECT_ID", ""),
        "private_key_id": os.environ.get("GOOGLE_PRIVATE_KEY_ID", ""),
        "private_key": _private_key.replace("\\n", "\n"),
        "client_email": os.environ.get("GOOGLE_CLIENT_EMAIL", ""),
        "client_id": "",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_x509_cert_url": "",
        "universe_domain": "googleapis.com"
    }
    _tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(_creds_data, _tmp)
    _tmp.close()
    CREDS_FILE = _tmp.name
else:
    CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")

# Transcript channel — set TRANSCRIPT_CHANNEL_ID in secrets, or transcripts go to ops channel
_transcript_env      = os.environ.get("TRANSCRIPT_CHANNEL_ID", "0")
TRANSCRIPT_CHANNEL_ID = int(_transcript_env) if _transcript_env.isdigit() and int(_transcript_env) else OPS_CHANNEL_ID

INACTIVITY_WARN_DAYS  = 7   # warn creator after this many days of silence
INACTIVITY_CLOSE_DAYS = 9   # auto-close after this many days of silence

# ─── VIDEO AUDIT (GAS + GEMINI) ────────────────────────────
GAS_URL  = "https://script.google.com/macros/s/AKfycbwNw9fbGn1K8yhx8tMy3PUzv5t-1KFn5CY62G-9c_RNhWVTcihks9uhhBwPnT0ECAE/exec"
GAS_USER = os.environ.get("GAS_USER", "creator@viraloctane.com")
GAS_PASS = os.environ.get("GAS_PASS", "70707070")

# ─── FAQ BOT (GEMINI CHAT) ─────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyBpTppzyCz1_GHO2jIvLhyTDe8ixOWb4Fg")
GEMINI_MODEL   = "gemini-2.5-flash"

try:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "faq.json")) as _f:
        FAQ_KB = json.load(_f)
    print(f"✅ Loaded {len(FAQ_KB)} FAQs")
except Exception as _e:
    FAQ_KB = []
    print(f"⚠️ Could not load faq.json: {_e}")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Change 2: Added Coulée Coffee, removed MDRN and Hover Cover from forms
BRANDS = [
    "EZ Outlet", "Sleek Socket", "Spicy Shelf",
    "Spice Spinner", "Emily C", "Coulée Coffee",
]

# Change 2: Growi links — only brands that have them
GROWI_LINKS = {
    "EZ Outlet":     "https://www.growi.io/o/ez-outlet/c/33783?language=en&method=oauth",
    "Sleek Socket":  "https://www.growi.io/o/sleek-socket/c/33786?language=en&method=oauth",
    "Spicy Shelf":   "https://www.growi.io/o/spicy-shelf/c/33788?language=en&method=oauth",
    "Spice Spinner": "https://www.growi.io/o/spice-spinner/c/33787?language=en&method=oauth",
    "Emily C":       "https://www.growi.io/o/emily-c-necklace/c/33784?language=en&method=oauth",
    "Coulée Coffee": "https://www.growi.io/o/ez-outlet/c/33783?language=en&method=oauth",
}

# Change 5: Retainer Applications category ID
RETAINER_CATEGORY_ID = 1502371063223554096

# Change 3: GMV parser — handles $10,000 / $10.000 / 10000 / 10,000 / 10.000 etc.
def parse_gmv(raw: str) -> float:
    """Parse GMV string in any common format to a float."""
    s = raw.strip().replace("$", "").replace(" ", "")
    # Handle European decimal notation: if dot comes before comma, it's a thousand separator
    # e.g. 10.000 → 10000, but 10.5 → 10.5
    if "." in s and "," in s:
        # both present — comma is decimal separator in some locales, dot in others
        # assume last separator is decimal
        if s.rfind(".") > s.rfind(","):
            # dot is decimal: remove commas
            s = s.replace(",", "")
        else:
            # comma is decimal: remove dots, replace comma with dot
            s = s.replace(".", "").replace(",", ".")
    elif "." in s:
        # only dot — could be thousand separator (10.000) or decimal (10.5)
        parts = s.split(".")
        if len(parts) == 2 and len(parts[1]) == 3:
            # e.g. 10.000 — treat as thousand separator
            s = s.replace(".", "")
        # else treat as decimal (10.5)
    elif "," in s:
        # only comma — could be thousand separator (10,000) or decimal (10,5)
        parts = s.split(",")
        if len(parts) == 2 and len(parts[1]) == 3:
            # e.g. 10,000 — thousand separator
            s = s.replace(",", "")
        else:
            # decimal comma
            s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0

# ─── MACROS ────────────────────────────────────────────────
MACRO_M01 = """👋 Hey {username}! Thanks for applying to Octane Labs.

Here's what we received:
• TikTok: {tiktok}
• Program: {program}
• Brand: {brand}
• App submitted: {app_submitted}
• GMV (30d): {gmv}
• Notes: {notes}

We'll review and follow up within 1–2 business days! 🙌"""

MACRO_SUPPORT_OPEN = """👋 Hey {username}! A support ticket has been opened for you.

**Issue:** {issue}

Our team will be with you shortly. Please share any relevant screenshots or links here."""

MACRO_VIDEO_OPEN = """👋 Hey {username}! Your video review request has been received.

**Video link(s):**
{links}

**Notes:** {notes}

We review 1–5 videos per day — we'll drop feedback here as soon as we can! 🎬"""

MACRO_M02 = """Hey {username}! Just following up — we still need a bit more info to move your application forward.

Could you complete any missing fields so we can review you properly? 🙏

We'll need to close this ticket by Day 5 if we don't hear back. Thanks!"""

MACRO_M03 = """Hey {username}! This is our final follow-up before we close this ticket.

If we don't hear back by tomorrow, we'll archive this chat. You're always welcome to reapply in the future — we'd love to work with you! 🙌"""

MACRO_M04 = """Hey {username}! Thank you so much for your interest in {brand}. 🙏

After reviewing your profile, we're unable to move forward at this time. Our program requires a minimum of $5k in GMV and a 50%+ post fulfillment rate.

We truly appreciate your enthusiasm and hope you'll reapply as your channel grows. Best of luck! 🚀"""

MACRO_M07_APPROVED = """Hey {username}! Your sample request for {brand} has been approved! 🎉

We'll follow up once it's on the way. Keep an eye on your Affiliate Center for shipping updates."""

MACRO_CLOSE = """Hey {username}! We haven't heard back from you so we're closing this ticket now.

You're always welcome to reapply in the future. Good luck! 🙏"""

MACRO_INACTIVITY_WARN = """⚠️ Hey {username}! We noticed this ticket has been quiet for a while.

If you still need help or want to continue your application, please reply here within **2 days** — otherwise we'll close this ticket automatically.

No worries if life got busy — you can always open a new ticket in `#start-here`! 🙌"""

# Change 4: Additional macros
MACRO_GMV_REJECTION = """Hey {username}! Thank you so much for your interest in the {brand} Retainer Program. 🙏

After reviewing your application, we're unable to move forward at this time. Our retainer program requires a minimum of **$10,000 in GMV** over the last 30 days.

We genuinely appreciate your enthusiasm and encourage you to reapply once your metrics have grown. In the meantime, you're welcome to explore our **Sample Program** or **GMV Sprint** if you meet those requirements.

Best of luck — we hope to work with you in the future! 🚀"""

MACRO_CONTENT_REJECTION = """Hey {username}! Thank you for applying for the {brand} {program} — we genuinely appreciate your interest. 🙏

After reviewing your content more closely, we don't feel it's the right fit at this time. Here's what we look for:
• Strong scroll-stopping hook in the first 1–2 seconds
• Dynamic visuals with movement and variety
• Clear product demonstration
• Benefit-driven explanation
• Relatable problem-to-solution framing
• CTA with urgency or a clear action

This decision isn't personal — we encourage you to keep creating and reapply when you feel ready. Good luck! 🌟"""

MACRO_RETAINER_APPROVED = """Hey {username}! 🎉 Exciting news — you've been approved for the **{brand} Retainer Program!**

Here are your next steps:
1. Review your retainer brief in the portal link shared above
2. Confirm you've read and understood the brief by replying here with a short confirmation
3. Begin posting within the agreed timeframe

Your retainer contract and payment details are managed through the portal. Welcome to the team! 🙌"""

MACRO_MISSING_INFO = """Hey {username}! Just following up — we still need a bit more info to move your application forward.

Could you please provide the following:
• Your TikTok handle (if not already shared)
• Your GMV over the last 30 days
• Confirmation that you've applied in the Affiliate Center (Y/N)

We'll need to close this ticket in **3 days** if we don't hear back. Thanks so much! 🙏"""

MACRO_FINAL_FOLLOWUP = """Hey {username}! This is our final follow-up before we close this ticket.

If you'd still like to move forward, please reply here by **tomorrow** — otherwise we'll archive this chat. You're always welcome to reapply in the future! 🙌"""

MACRO_SAMPLE_REJECTION = """Hey {username}! Thank you for your interest in a sample for {brand}. 🙏

After reviewing your profile, we're unable to fulfill this sample request at this time. Our sample program requires a minimum of **$5,000 in GMV** and a **50%+ post fulfillment rate**.

We hope to work with you as your channel grows. Best of luck! 🚀"""

MACRO_PAYOUT_QUESTION = """Hey {username}! Thanks for reaching out about your payout for {brand}. 💸

All retainer payments are processed at the end of your 30-day term once all deliverables have been verified. Here's the current status:

**Growi Status:** {growi_status}

If you have further questions or believe there's an issue with your payout, our team will follow up shortly. We appreciate your patience! 🙌"""

MACRO_PRODUCT_ROUTING = """Hey {username}! Thanks for reaching out 🙌 — it looks like you might be in the wrong spot.

For **{correct_brand}**, please head to the correct channel and open a ticket there. If you're unsure which program applies to you, let us know your TikTok handle and what you're interested in and we'll point you in the right direction! 🚀"""

# ─── GOOGLE SHEETS LAYER ───────────────────────────────────
_sheet_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="gspread")
_sheet_obj      = None
_sheet_lock     = threading.Lock()

def _connect_sheet():
    global _sheet_obj
    creds      = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    client     = gspread.Client(auth=creds)
    _sheet_obj = client.open_by_key(SHEET_ID).worksheet("Pipeline")
    print("✅ Google Sheets connected")
    return _sheet_obj

def get_sheet():
    global _sheet_obj
    with _sheet_lock:
        if _sheet_obj is None:
            _connect_sheet()
    return _sheet_obj

def _reset_sheet():
    global _sheet_obj
    with _sheet_lock:
        _sheet_obj = None

async def _sheet(fn, *args):
    """Run any gspread function in the dedicated sheet thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_sheet_executor, fn, *args)

# Sheet operations — all run inside _sheet_executor
def _fetch_records():
    """Returns (sheet, list[dict]) — tolerates empty/duplicate header columns."""
    sheet  = get_sheet()
    values = sheet.get_all_values()
    if len(values) < 2:
        return sheet, []
    headers = values[0]
    records = []
    for row in values[1:]:
        d = {}
        for j, h in enumerate(headers):
            if h and h not in d:
                d[h] = row[j] if j < len(row) else ""
        records.append(d)
    return sheet, records

def _next_ticket_num():
    return len(get_sheet().get_all_values())

def _find_creator(tiktok_handle):
    _, records = _fetch_records()
    for i, row in enumerate(records, start=2):
        if str(row.get("TikTok Handle", "")).lower() == tiktok_handle.lower():
            return i, row
    return None, None

def _add_row(data: dict):
    sheet    = get_sheet()
    today    = date.today().strftime("%m/%d/%Y")
    followup = (date.today() + timedelta(days=2)).strftime("%m/%d/%Y")
    ticket   = f"T-{data.get('ticket_num', 0):03d}"
    row = [
        ticket,
        data.get("ticket_link", ""),
        data.get("username", ""),
        data.get("tiktok", ""),
        data.get("program", ""),
        data.get("brand", ""),
        data.get("app_submitted", ""),
        data.get("sample_requested", "N"),
        data.get("gmv", ""),
        "", "", "Unassigned", "New", "",
        followup, today, "", "", "",
        data.get("request", ""),
        str(data.get("channel_id", "")),   # col 21 — "Channel ID" header in sheet
        str(data.get("user_id", "")),      # col 22 — "User ID" header in sheet
    ]
    sheet.append_row(row)
    print(f"✅ Sheet row added for {data.get('username')} ({ticket})")

def _update_row(row_index: int, updates: dict):
    col_map = {
        "Status": 13, "Owner": 12, "Next Follow-Up": 15,
        "Last Team Touch": 16, "Close Reason": 17, "Sample Status": 8,
        "Channel ID": 21, "User ID": 22,
    }
    sheet = get_sheet()
    today = date.today().strftime("%m/%d/%Y")
    for field, value in updates.items():
        col = col_map.get(field)
        if col:
            sheet.update_cell(row_index, col, value)
    sheet.update_cell(row_index, col_map["Last Team Touch"], today)

def _find_by_channel_id(channel_id: int):
    _, records = _fetch_records()
    for i, row in enumerate(records, start=2):
        cid = str(row.get("Channel ID", "")).strip()
        if cid == str(channel_id):
            return i, row
    return None, None

def log_to_sheet_async(data: dict):
    """Fire-and-forget sheet write. Never blocks the event loop."""
    def _run():
        try:
            _add_row(data)
        except Exception as e:
            _reset_sheet()
            print(f"Sheet write error: {e}")
    _sheet_executor.submit(_run)

def _save_env_value(key: str, value: str):
    """Persist a single key=value in .env so it survives bot restarts."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path) as f:
            lines = f.readlines()
        found, new_lines = False, []
        for line in lines:
            if line.startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                found = True
            else:
                new_lines.append(line)
        if not found:
            new_lines.append(f"{key}={value}\n")
        with open(env_path, "w") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"Failed to persist {key} to .env: {e}")

# ─── GAS / AUDIT LAYER ─────────────────────────────────────
_gas_token        = None
_gas_token_expiry = 0.0

async def gas_login() -> str:
    global _gas_token, _gas_token_expiry
    if _gas_token and time.time() < _gas_token_expiry:
        return _gas_token
    async with aiohttp.ClientSession() as s:
        resp = await s.post(GAS_URL, json={
            "action": "login", "username": GAS_USER, "password": GAS_PASS,
        }, timeout=aiohttp.ClientTimeout(total=30))
        data = await resp.json(content_type=None)
    if not data.get("success"):
        raise RuntimeError(f"GAS login failed: {data.get('error', 'unknown')}")
    _gas_token        = data["sessionToken"]
    _gas_token_expiry = time.time() + data.get("expiresIn", 21600) - 300
    print("✅ GAS session refreshed")
    return _gas_token

def _faq_system_prompt() -> str:
    """Build the system prompt with the full FAQ knowledge base."""
    kb_lines = []
    for f in FAQ_KB:
        kb_lines.append(
            f"[{f['id']} · {f['category']}]\n"
            f"Q: {f['question']}\n"
            f"A: {f['answer']}\n"
            f"Escalate when: {f.get('escalate', '')}\n"
        )
    kb_text = "\n".join(kb_lines)
    return (
        "You are the Octane Labs creator support bot, answering creator questions in a "
        "private Discord ticket channel. You ONLY answer using the FAQ knowledge base below.\n\n"
        "STRICT RULES:\n"
        "1. If the user's question clearly matches an FAQ, answer it naturally and conversationally "
        "using the FAQ answer. Do NOT say 'according to the FAQ'.\n"
        "2. If you are NOT highly confident the FAQ covers this question, OR the question requires "
        "checking the creator's specific account/payment/sample status, respond with EXACTLY the "
        "single word: ESCALATE — and nothing else. A human op will take over.\n"
        "3. Never invent information. If the FAQ doesn't cover it, escalate.\n"
        "4. Keep responses under 1500 characters. Use line breaks for readability.\n"
        "5. Match the casual, friendly tone of the FAQ answers.\n"
        "6. Never reveal that you are using a knowledge base or that you are an AI model.\n\n"
        f"FAQ KNOWLEDGE BASE ({len(FAQ_KB)} entries):\n\n{kb_text}"
    )

_FAQ_SYSTEM_PROMPT_CACHED = None
def _get_faq_prompt() -> str:
    global _FAQ_SYSTEM_PROMPT_CACHED
    if _FAQ_SYSTEM_PROMPT_CACHED is None:
        _FAQ_SYSTEM_PROMPT_CACHED = _faq_system_prompt()
    return _FAQ_SYSTEM_PROMPT_CACHED

async def gemini_answer(user_question: str, creator_context: str = "") -> str | None:
    """Returns the bot's answer, or None if it should stay silent (escalate)."""
    if not FAQ_KB or not GEMINI_API_KEY:
        print("[FAQ] Gemini skipped: no FAQ_KB or no API key")
        return None
    # Change 1: Use stable model name
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={GEMINI_API_KEY}"
    )
    system = _get_faq_prompt()
    if creator_context:
        system += f"\n\nCREATOR CONTEXT (from sheet):\n{creator_context}"

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": user_question}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 600},
    }
    try:
        async with aiohttp.ClientSession() as s:
            resp = await s.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=30))
            data = await resp.json(content_type=None)
        # Log full response for debugging
        if resp.status != 200:
            print(f"[FAQ] Gemini API error {resp.status}: {data}")
            return None
        text = (
            data.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
        )
        print(f"[FAQ] Gemini response: {text[:80]}")
    except Exception as e:
        print(f"Gemini chat error: {e}")
        return None

    if not text or text.upper().strip().rstrip(".!") == "ESCALATE":
        return None
    return text

async def gas_audit(tiktok_url: str) -> dict:
    """Call scoreTikTokUrl on the GAS backend. Takes 30–90s."""
    token = await gas_login()
    async with aiohttp.ClientSession() as s:
        resp = await s.post(GAS_URL, json={
            "action":       "scoreTikTokUrl",
            "sessionToken": token,
            "tiktok_url":   tiktok_url,
        }, timeout=aiohttp.ClientTimeout(total=180))
        data = await resp.json(content_type=None)
    if not data.get("success"):
        raise RuntimeError(data.get("error", "Audit failed — try again."))
    return data["data"]

# ─── BOT SETUP ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot  = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ─── HELPERS ───────────────────────────────────────────────
async def safe_defer(interaction: discord.Interaction) -> bool:
    age = (discord.utils.utcnow() - interaction.created_at).total_seconds()
    if age > 2.5:
        print(f"⚠️ Stale interaction from {interaction.user} ({age:.1f}s) — discarding")
        return False
    try:
        await interaction.response.defer(ephemeral=True)
        return True
    except discord.errors.NotFound:
        print(f"⚠️ Interaction expired for {interaction.user} — discarding")
        return False

async def make_ticket_channel(guild, user, category_name: str, channel_name: str, category_id: int = None):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user:               discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    # Prefer category ID lookup if provided (more reliable than name)
    category = None
    if category_id:
        category = guild.get_channel(category_id)
    if not category:
        category = discord.utils.get(guild.categories, name=category_name)
    return await guild.create_text_channel(
        name=channel_name[:100], overwrites=overwrites, category=category
    )

async def send_ops_alert(program: str, username: str, tiktok: str, brand: str, gmv: str, channel):
    ops_ch = bot.get_channel(OPS_CHANNEL_ID)
    if not ops_ch:
        return
    embed = discord.Embed(title="🆕 New Ticket", color=discord.Color.gold())
    embed.add_field(name="Creator",   value=f"@{username}",  inline=True)
    embed.add_field(name="TikTok",    value=f"`{tiktok}`",   inline=True)
    embed.add_field(name="Program",   value=program,          inline=True)
    embed.add_field(name="Brand",     value=brand,            inline=True)
    embed.add_field(name="GMV (30d)", value=gmv,              inline=True)
    embed.add_field(name="Channel",   value=channel.mention,  inline=True)
    await ops_ch.send(content=OPS_PING or None, embed=embed)

async def generate_transcript(channel: discord.TextChannel) -> discord.File:
    """Collect all messages in a channel and return a formatted .txt file."""
    lines = [
        f"TRANSCRIPT — #{channel.name}",
        f"Exported : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Server   : {channel.guild.name}",
        "─" * 60,
        "",
    ]
    async for msg in channel.history(limit=500, oldest_first=True):
        ts      = msg.created_at.strftime("%Y-%m-%d %H:%M")
        content = msg.content or ""
        if msg.embeds:
            content = content or "[embed]"
        if msg.attachments:
            content += (" " if content else "") + " ".join(a.url for a in msg.attachments)
        lines.append(f"[{ts}] {msg.author.display_name}: {content}")
    buf = io.BytesIO("\n".join(lines).encode("utf-8"))
    return discord.File(buf, filename=f"transcript-{channel.name[:50]}.txt")

async def _do_close(channel: discord.TextChannel, reason: str, closed_by: str = "Auto"):
    """
    Full close sequence:
      1. Generate + post transcript to transcript channel
      2. Send closing message in the ticket channel
      3. Lock channel (read-only for members)
      4. Rename with 🔴
      5. Update Google Sheet
    """
    # 1. Transcript
    try:
        transcript = await generate_transcript(channel)
        log_ch = bot.get_channel(TRANSCRIPT_CHANNEL_ID)
        if log_ch:
            embed = discord.Embed(
                title=f"🔒 Closed — #{channel.name}",
                description=f"**Reason:** {reason}\n**Closed by:** {closed_by}",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await log_ch.send(embed=embed, file=transcript)
    except Exception as e:
        print(f"Transcript error for #{channel.name}: {e}")

    # 2. Closing message
    try:
        await channel.send(
            f"🔒 **This ticket has been closed.**\n"
            f"**Reason:** {reason}\n\n"
            f"Thanks for working with Octane Labs. Feel free to open a new ticket from `#start-here` anytime."
        )
    except Exception:
        pass

    # 3. Lock
    try:
        new_overwrites = {}
        for target, perms in channel.overwrites.items():
            if isinstance(target, discord.Member) and target.id != channel.guild.me.id:
                perms.send_messages = False
            new_overwrites[target] = perms
        await channel.edit(overwrites=new_overwrites)
    except Exception as e:
        print(f"Lock error for #{channel.name}: {e}")

    # 4. Rename
    try:
        stripped = channel.name.lstrip("🟡🔵📹").lstrip("│").strip("-").strip()
        await channel.edit(name=f"🔴│{stripped}"[:100])
    except Exception:
        pass

    # 5. Sheet
    try:
        row_index, _ = await _sheet(_find_by_channel_id, channel.id)
        if row_index:
            _sheet_executor.submit(_update_row, row_index, {
                "Status": "Closed",
                "Close Reason": reason,
            })
    except Exception as e:
        print(f"Sheet update error on close for #{channel.name}: {e}")

# ─── PERSISTENT VIEWS ──────────────────────────────────────
class StartHereView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply for Retainer", style=discord.ButtonStyle.primary,
                       custom_id="starthere:retainer", emoji="💰", row=0)
    async def retainer_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "**Step 1 of 2** — Select the brand you're applying for:",
            view=BrandSelectView("retainer"), ephemeral=True
        )

    @discord.ui.button(label="Apply for GMV Sprint", style=discord.ButtonStyle.success,
                       custom_id="starthere:gmvsprint", emoji="💸", row=0)
    async def gmvsprint_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if GMV_SPRINT_STATUS == "Paused":
            await interaction.response.send_message(
                "⏸️ **GMV Sprint is currently paused.** Check `#announcements` for updates.",
                ephemeral=True
            )
            return
        label = "Waitlist — select brand:" if GMV_SPRINT_STATUS == "Waitlist" else "**Step 1 of 2** — Select the brand:"
        flow  = "gmvsprint_waitlist" if GMV_SPRINT_STATUS == "Waitlist" else "gmvsprint"
        await interaction.response.send_message(label, view=BrandSelectView(flow), ephemeral=True)

    @discord.ui.button(label="Request Sample", style=discord.ButtonStyle.secondary,
                       custom_id="starthere:sample", emoji="🧴", row=1)
    async def sample_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SampleModal())

    @discord.ui.button(label="Open Support Ticket", style=discord.ButtonStyle.secondary,
                       custom_id="starthere:support", emoji="👷", row=1)
    async def support_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SupportModal())


class RetainerPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I Want to Apply", style=discord.ButtonStyle.primary,
                       custom_id="retainer:apply", emoji="💰")
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "**Step 1 of 2** — Select the brand you're applying for:",
            view=BrandSelectView("retainer"), ephemeral=True
        )


class GMVSprintPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Apply Now", style=discord.ButtonStyle.success,
                       custom_id="gmvsprint:apply", emoji="💸")
    async def apply_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if GMV_SPRINT_STATUS == "Paused":
            await interaction.response.send_message(
                "⏸️ GMV Sprint is currently paused. Check `#announcements` for updates.", ephemeral=True
            )
            return
        flow = "gmvsprint_waitlist" if GMV_SPRINT_STATUS == "Waitlist" else "gmvsprint"
        await interaction.response.send_message(
            "Select the brand:", view=BrandSelectView(flow), ephemeral=True
        )


class SupportPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open Support Ticket", style=discord.ButtonStyle.primary,
                       custom_id="support:open", emoji="👷")
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SupportModal())


class VideoReviewPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Submit Video for Review", style=discord.ButtonStyle.primary,
                       custom_id="video:submit", emoji="📹")
    async def submit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VideoReviewModal())


# ─── BRAND SELECT (ephemeral, non-persistent) ───────────────
class BrandSelectView(discord.ui.View):
    def __init__(self, flow: str):
        super().__init__(timeout=120)
        self.flow = flow
        sel = discord.ui.Select(
            placeholder="Choose a brand/product...",
            options=[discord.SelectOption(label=b, value=b) for b in BRANDS]
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        brand = interaction.data["values"][0]
        if self.flow == "retainer":
            await interaction.response.send_modal(RetainerModal(brand=brand))
        elif self.flow == "gmvsprint":
            await interaction.response.send_modal(GMVSprintModal(brand=brand))
        elif self.flow == "gmvsprint_waitlist":
            await interaction.response.send_modal(GMVSprintModal(brand=brand, waitlist=True))


# ─── MODALS ────────────────────────────────────────────────
class RetainerModal(discord.ui.Modal):
    def __init__(self, brand: str = ""):
        super().__init__(title="Retainer Application")
        self._brand  = brand
        self.tiktok  = discord.ui.TextInput(label="TikTok Handle", placeholder="@yourhandle")
        self.gmv     = discord.ui.TextInput(label="GMV Last 30 Days", placeholder="$12,000 or Unknown")
        self.app_sub = discord.ui.TextInput(label="Applied in Affiliate Center? (Y/N)", placeholder="Y", max_length=1)
        self.notes   = discord.ui.TextInput(label="Anything else we should know?",
                                            placeholder="Optional", required=False,
                                            style=discord.TextStyle.paragraph)
        self.add_item(self.tiktok)
        self.add_item(self.gmv)
        self.add_item(self.app_sub)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        try:
            user     = interaction.user
            username = user.name
            brand    = self._brand

            # Change 3: Auto-reject if GMV < $10,000
            gmv_val = parse_gmv(str(self.gmv))
            if gmv_val > 0 and gmv_val < 10000:
                await interaction.followup.send(
                    MACRO_GMV_REJECTION.format(username=f"@{username}", brand=brand),
                    ephemeral=False
                )
                return

            tnum    = await _sheet(_next_ticket_num)
            ch_name = f"🟡│app-{tnum:03d}-{username}"

            # Change 5: Use RETAINER_CATEGORY_ID for retainer tickets
            channel = await make_ticket_channel(
                interaction.guild, user, CAT_APPLICATIONS, ch_name,
                category_id=RETAINER_CATEGORY_ID
            )

            await channel.send(MACRO_M01.format(
                username=f"@{username}", tiktok=str(self.tiktok), program="Retainer",
                brand=brand, app_submitted=str(self.app_sub), gmv=str(self.gmv),
                notes=str(self.notes) or "None"
            ))

            growi_link = GROWI_LINKS.get(brand)
            if growi_link:
                await channel.send(
                    f"To complete your retainer application, please click the link below and sign up through our partner portal. "
                    f"Once you have submitted your application, drop a message here and our team will begin reviewing your profile within 1–2 business days.\n\n"
                    f"{growi_link}"
                )

            await send_ops_alert("Retainer", username, str(self.tiktok), brand, str(self.gmv), channel)

            log_to_sheet_async({
                "ticket_num": tnum, "ticket_link": channel.jump_url,
                "username": f"@{username}", "tiktok": str(self.tiktok),
                "program": "Retainer", "brand": brand,
                "app_submitted": str(self.app_sub), "gmv": str(self.gmv),
                "request": str(self.notes) or "Via /apply",
                "channel_id": channel.id, "user_id": user.id,
            })

            await interaction.followup.send(
                f"✅ Application received! Head to {channel.mention} to track your ticket.", ephemeral=True
            )
        except Exception as e:
            print(f"RetainerModal error for {interaction.user}: {e}")
            try:
                await interaction.followup.send(
                    "❌ Something went wrong. Please ping an op directly.", ephemeral=True
                )
            except Exception:
                pass


class GMVSprintModal(discord.ui.Modal):
    def __init__(self, brand: str = "", waitlist: bool = False):
        super().__init__(title="GMV Sprint Application" if not waitlist else "GMV Sprint Waitlist")
        self._brand    = brand
        self._waitlist = waitlist
        self.tiktok    = discord.ui.TextInput(label="TikTok Handle", placeholder="@yourhandle")
        self.gmv       = discord.ui.TextInput(label="GMV Last 30 Days", placeholder="$5,000 or Unknown")
        self.app_sub   = discord.ui.TextInput(label="Applied in Affiliate Center? (Y/N)", placeholder="Y", max_length=1)
        self.notes     = discord.ui.TextInput(label="Anything else?",
                                              placeholder="Optional", required=False,
                                              style=discord.TextStyle.paragraph)
        self.add_item(self.tiktok)
        self.add_item(self.gmv)
        self.add_item(self.app_sub)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        try:
            user     = interaction.user
            username = user.name
            brand    = self._brand
            program  = "GMV Sprint Waitlist" if self._waitlist else "GMV Sprint"
            tnum     = await _sheet(_next_ticket_num)
            ch_name  = f"🟡│gmv-{tnum:03d}-{username}"

            channel = await make_ticket_channel(interaction.guild, user, CAT_APPLICATIONS, ch_name)

            await channel.send(MACRO_M01.format(
                username=f"@{username}", tiktok=str(self.tiktok), program=program,
                brand=brand, app_submitted=str(self.app_sub), gmv=str(self.gmv),
                notes=str(self.notes) or "None"
            ))

            await send_ops_alert(program, username, str(self.tiktok), brand, str(self.gmv), channel)

            log_to_sheet_async({
                "ticket_num": tnum, "ticket_link": channel.jump_url,
                "username": f"@{username}", "tiktok": str(self.tiktok),
                "program": program, "brand": brand,
                "app_submitted": str(self.app_sub), "gmv": str(self.gmv),
                "request": str(self.notes) or "Via GMV Sprint panel",
                "channel_id": channel.id, "user_id": user.id,
            })

            msg = (
                f"📋 You've been added to the waitlist for GMV Sprint! We'll reach out in {channel.mention} when a spot opens."
                if self._waitlist else
                f"✅ Application received! Head to {channel.mention} to track your ticket."
            )
            await interaction.followup.send(msg, ephemeral=True)

        except Exception as e:
            print(f"GMVSprintModal error for {interaction.user}: {e}")
            try:
                await interaction.followup.send("❌ Something went wrong. Please ping an op.", ephemeral=True)
            except Exception:
                pass


class SampleModal(discord.ui.Modal, title="Sample Request"):
    tiktok  = discord.ui.TextInput(label="Your TikTok Handle", placeholder="@yourhandle")
    product = discord.ui.TextInput(label="Which product?", placeholder="EZ Outlet / Sleek Socket / etc.")
    applied = discord.ui.TextInput(label="Applied in Affiliate Center? (Y/N)", placeholder="Y", max_length=1)

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        try:
            username = interaction.user.name
            row_index, row_data = await _sheet(_find_creator, str(self.tiktok))

            if row_data:
                try:
                    gmv_raw = str(row_data.get("GMV (30d)", "0")).replace("$", "").replace(",", "").strip()
                    gmv_val = float(gmv_raw) if gmv_raw and gmv_raw.lower() != "unknown" else 0
                except Exception:
                    gmv_val = 0
                if gmv_val < 5000:
                    await interaction.channel.send(
                        MACRO_M04.format(username=f"@{username}", brand=str(self.product))
                    )
                    await interaction.followup.send("❌ GMV requirements not met.", ephemeral=True)
                    return

            sample_ch = bot.get_channel(SAMPLE_QUEUE_ID)
            if sample_ch:
                gmv_display = row_data.get("GMV (30d)", "Unknown") if row_data else "Not in Sheet"
                view = SampleApprovalView(
                    username=username, product=str(self.product),
                    channel=interaction.channel, row_index=row_index
                )
                await sample_ch.send(
                    f"🧴 **Sample request** — @{username}\n"
                    f"TikTok: `{self.tiktok}` | Product: `{self.product}`\n"
                    f"GMV: `{gmv_display}` | Applied in AC: `{self.applied}`",
                    view=view
                )

            await interaction.followup.send(
                "✅ Sample request received! We'll review and get back to you shortly.", ephemeral=True
            )
        except Exception as e:
            print(f"SampleModal error for {interaction.user}: {e}")
            try:
                await interaction.followup.send("❌ Something went wrong. Please ping an op.", ephemeral=True)
            except Exception:
                pass


class SupportModal(discord.ui.Modal, title="Open a Support Ticket"):
    issue = discord.ui.TextInput(
        label="What do you need help with?",
        placeholder="Sample question / Flash sale help / Payout / Other...",
        style=discord.TextStyle.short
    )
    details = discord.ui.TextInput(
        label="Describe your issue",
        placeholder="Give us as much detail as possible...",
        style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        try:
            user     = interaction.user
            username = user.name
            tnum     = await _sheet(_next_ticket_num)
            ch_name  = f"🔵│support-{tnum:03d}-{username}"

            channel = await make_ticket_channel(interaction.guild, user, CAT_SUPPORT, ch_name)
            await channel.send(MACRO_SUPPORT_OPEN.format(
                username=f"@{username}", issue=str(self.issue)
            ))

            ops_ch = bot.get_channel(OPS_CHANNEL_ID)
            if ops_ch:
                embed = discord.Embed(title="🔵 Support Ticket", color=discord.Color.blue())
                embed.add_field(name="Creator", value=f"@{username}", inline=True)
                embed.add_field(name="Issue",   value=str(self.issue),  inline=True)
                embed.add_field(name="Channel", value=channel.mention,  inline=False)
                await ops_ch.send(content=OPS_PING or None, embed=embed)

            await interaction.followup.send(
                f"✅ Ticket opened! Head to {channel.mention}.", ephemeral=True
            )
        except Exception as e:
            print(f"SupportModal error for {interaction.user}: {e}")
            try:
                await interaction.followup.send("❌ Something went wrong. Please ping an op.", ephemeral=True)
            except Exception:
                pass


class VideoReviewModal(discord.ui.Modal, title="Video Review Request"):
    links = discord.ui.TextInput(
        label="Video link(s)",
        placeholder="Paste TikTok URLs here (up to 5)",
        style=discord.TextStyle.paragraph
    )
    notes = discord.ui.TextInput(
        label="Notes for the reviewer",
        placeholder="Optional — anything specific you want feedback on?",
        required=False,
        style=discord.TextStyle.paragraph
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not await safe_defer(interaction):
            return
        try:
            user     = interaction.user
            username = user.name
            tnum     = await _sheet(_next_ticket_num)
            ch_name  = f"📹│review-{tnum:03d}-{username}"

            channel = await make_ticket_channel(interaction.guild, user, CAT_VIDEO, ch_name)
            await channel.send(MACRO_VIDEO_OPEN.format(
                username=f"@{username}", links=str(self.links),
                notes=str(self.notes) or "None"
            ))
            await channel.send(
                "📊 **Quality gate — required before ops review:**\n"
                "Run `/audit` followed by your TikTok URL in this channel.\n"
                "Videos scoring **70+** are automatically sent to the team. Videos below 70 get improvement tips — fix and re-audit anytime."
            )

            ops_ch = bot.get_channel(OPS_CHANNEL_ID)
            if ops_ch:
                embed = discord.Embed(title="📹 Video Review Request", color=discord.Color.purple())
                embed.add_field(name="Creator", value=f"@{username}", inline=True)
                embed.add_field(name="Channel", value=channel.mention, inline=True)
                await ops_ch.send(content=OPS_PING or None, embed=embed)

            await interaction.followup.send(
                f"✅ Review request submitted! Head to {channel.mention}.", ephemeral=True
            )
        except Exception as e:
            print(f"VideoReviewModal error for {interaction.user}: {e}")
            try:
                await interaction.followup.send("❌ Something went wrong. Please ping an op.", ephemeral=True)
            except Exception:
                pass


# ─── SAMPLE APPROVAL VIEW ──────────────────────────────────
class SampleApprovalView(discord.ui.View):
    def __init__(self, username, product, channel, row_index):
        super().__init__(timeout=None)
        self.username  = username
        self.product   = product
        self.channel   = channel
        self.row_index = row_index

    @discord.ui.button(label="✅ Approve", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.channel:
            await self.channel.send(
                MACRO_M07_APPROVED.format(username=f"@{self.username}", brand=self.product)
            )
        if self.row_index:
            _sheet_executor.submit(_update_row, self.row_index, {"Sample Status": "Approved"})
        await interaction.response.edit_message(
            content=f"✅ Approved by {interaction.user.name} — message sent to creator.", view=None
        )

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.channel:
            await self.channel.send(
                MACRO_M04.format(username=f"@{self.username}", brand=self.product)
            )
        if self.row_index:
            _sheet_executor.submit(_update_row, self.row_index, {"Sample Status": "Rejected"})
        await interaction.response.edit_message(
            content=f"❌ Rejected by {interaction.user.name} — message sent to creator.", view=None
        )


# ─── SLASH COMMANDS ────────────────────────────────────────
@tree.command(name="panel", description="[Ops] Post an Octane Labs panel in this channel")
@app_commands.describe(type="Which panel to post")
@app_commands.choices(type=[
    app_commands.Choice(name="Start Here",            value="starthere"),
    app_commands.Choice(name="Retainer Applications", value="retainer"),
    app_commands.Choice(name="GMV Sprint",            value="gmvsprint"),
    app_commands.Choice(name="Support Ticket",        value="support"),
    app_commands.Choice(name="Video Reviews",         value="video"),
])
@app_commands.checks.has_permissions(administrator=True)
async def panel(interaction: discord.Interaction, type: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)

    if type.value == "starthere":
        sprint_status = {
            "Open":     "✅  Open",
            "Paused":   "⏸️  Paused — check announcements",
            "Waitlist": "📋  Waitlist only",
        }.get(GMV_SPRINT_STATUS, GMV_SPRINT_STATUS)

        # ── Hero embed ─────────────────────────────────────────
        hero = discord.Embed(
            description=(
                "## OCTANE LABS\n"
                "**TikTok Shop Creator Partner Network**\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                "We run performance-based creator programs for brands that scale on TikTok Shop. "
                "Every partnership is tracked, every payout is earned.\n\n"
                "**$1.3M+ GMV generated  ·  44K+ units sold  ·  7 active brands**"
            ),
            color=0x0D1117,
        )
        await interaction.channel.send(embed=hero)

        # ── Programs embed + buttons ───────────────────────────
        programs = discord.Embed(
            title="SELECT YOUR TRACK",
            color=0xE8B84B,
        )
        programs.add_field(
            name="💰  RETAINER  —  Fixed monthly partnership",
            value=(
                "Guaranteed payout after 30 posts delivered and reviewed.\n"
                "```Minimum  $10K GMV · last 30 days\n"
                "Output   30 posts in 30 days\n"
                "Payment  Post-delivery, after content review```"
            ),
            inline=False,
        )
        programs.add_field(
            name="💸  GMV SPRINT  —  Performance-based",
            value=(
                "You post, we track. Commission on every sale you drive.\n"
                f"```Minimum  $5K GMV · last 30 days\n"
                f"Output   5 videos in 14 days\n"
                f"Status   {GMV_SPRINT_STATUS}```"
            ),
            inline=False,
        )
        programs.add_field(
            name="🧴  SAMPLE REQUEST  —  Try the product first",
            value=(
                "Request a product to review. Decision within 24–48h.\n"
                "```Requires  Active application on file\n"
                "GMV gate  $5K+ GMV · last 30 days```"
            ),
            inline=False,
        )
        programs.add_field(
            name="👷  SUPPORT  —  Private ops channel",
            value=(
                "Direct line to the Octane Labs team.\n"
                "```Covers   Payments · Flash sales · Shipping · Questions\n"
                "SLA      Response within 1 business day```"
            ),
            inline=False,
        )
        programs.set_footer(text="Octane Labs  ·  Creator Partner Network  ·  Use the buttons below to get started")
        await interaction.channel.send(embed=programs, view=StartHereView())

    elif type.value == "retainer":
        embed = discord.Embed(
            title="RETAINER PROGRAM",
            description=(
                "A fixed monthly partnership with guaranteed payout.\n"
                "You deliver the content — we handle the rest.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0xE8B84B,
        )
        embed.add_field(name="Minimum GMV",  value="$10,000+ in the last 30 days", inline=True)
        embed.add_field(name="Output",       value="30 posts in 30 days",           inline=True)
        embed.add_field(name="Payment",      value="Post-delivery · After content review", inline=True)
        embed.add_field(
            name="How it works",
            value=(
                "**1.** Apply below and select your brand\n"
                "**2.** Our team reviews your profile within 1–2 business days\n"
                "**3.** If approved, you receive a content brief and get started"
            ),
            inline=False,
        )
        embed.set_footer(text="Octane Labs  ·  Retainer Program")
        await interaction.channel.send(embed=embed, view=RetainerPanelView())

    elif type.value == "gmvsprint":
        status_label = {
            "Open":     "✅  Open — accepting applications now",
            "Paused":   "⏸️  Paused — check announcements for updates",
            "Waitlist": "📋  Waitlist — join the queue below",
        }.get(GMV_SPRINT_STATUS, GMV_SPRINT_STATUS)
        embed = discord.Embed(
            title="GMV SPRINT PROGRAM",
            description=(
                "Performance-based. You post, you earn — no fixed deal, pure commission.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x2ECC71,
        )
        embed.add_field(name="Minimum GMV",      value="$5,000+ in the last 30 days", inline=True)
        embed.add_field(name="Output",           value="5 videos in 14 days",          inline=True)
        embed.add_field(name="Program Status",   value=status_label,                   inline=False)
        embed.add_field(
            name="How it works",
            value=(
                "**1.** Apply below and select your brand\n"
                "**2.** Our team reviews within 1–2 business days\n"
                "**3.** If approved, start posting and track your GMV live"
            ),
            inline=False,
        )
        embed.set_footer(text="Octane Labs  ·  GMV Sprint Program")
        await interaction.channel.send(embed=embed, view=GMVSprintPanelView())

    elif type.value == "support":
        embed = discord.Embed(
            title="CREATOR SUPPORT",
            description=(
                "A private channel, just between you and the Octane Labs ops team.\n"
                "No public threads. No waiting rooms.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x3498DB,
        )
        embed.add_field(
            name="What we cover",
            value=(
                "**Payments** — payout status, commission questions\n"
                "**Campaigns** — flash sales, product drops, briefs\n"
                "**Shipping** — sample tracking, delivery issues\n"
                "**Platform** — Affiliate Center, TikTok Shop questions\n"
                "**Other** — anything else, we'll handle it"
            ),
            inline=False,
        )
        embed.add_field(name="Response time", value="Within 1 business day", inline=True)
        embed.add_field(name="Visibility",    value="Private — only you and ops", inline=True)
        embed.set_footer(text="Octane Labs  ·  Creator Support")
        await interaction.channel.send(embed=embed, view=SupportPanelView())

    elif type.value == "video":
        embed = discord.Embed(
            title="VIDEO REVIEW",
            description=(
                "Submit your TikTok videos for a structured quality review.\n"
                "We score every video against our conversion framework — same system used internally.\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            color=0x9B59B6,
        )
        embed.add_field(
            name="What you get",
            value=(
                "**Score** — 0–100 quality rating with tier (Good / Premium / Elite)\n"
                "**Breakdown** — Production · Category fit · Message clarity\n"
                "**Improvement tip** — one actionable fix if you score below 70\n"
                "**Gate** — videos scoring 70+ are automatically sent to ops for review"
            ),
            inline=False,
        )
        embed.add_field(name="Daily limit",  value="1–5 videos reviewed per day", inline=True)
        embed.add_field(name="Turnaround",   value="Same day when submitted before 3PM UTC", inline=True)
        embed.set_footer(text="Octane Labs  ·  Video Review  ·  Powered by Gemini AI")
        await interaction.channel.send(embed=embed, view=VideoReviewPanelView())

    await interaction.followup.send("✅ Panel posted.", ephemeral=True)


@tree.command(name="products", description="[Ops] Post all product cards in this channel")
@app_commands.checks.has_permissions(administrator=True)
async def products_post(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    for product in PRODUCTS:
        desc = f"*{product['tagline']}*\n\n" + "\n".join(f"• {b}" for b in product["bullets"])
        if product.get("metrics"):
            desc += f"\n\n`{product['metrics']}`"
        embed = discord.Embed(title=product["name"], description=desc, color=product["color"])
        if product.get("image_url"):
            embed.set_thumbnail(url=product["image_url"])
        embed.set_footer(text="Octane Labs · Creator Program")

        buttons = discord.ui.View(timeout=None)
        if product.get("shop_url"):
            buttons.add_item(discord.ui.Button(
                label="🛒 TikTok Shop", url=product["shop_url"],
                style=discord.ButtonStyle.link
            ))
        if product.get("guide_url"):
            buttons.add_item(discord.ui.Button(
                label="📄 Product Guide", url=product["guide_url"],
                style=discord.ButtonStyle.link
            ))

        await interaction.channel.send(
            embed=embed,
            view=buttons if (product.get("shop_url") or product.get("guide_url")) else None
        )

    await interaction.followup.send(f"✅ Posted {len(PRODUCTS)} product cards.", ephemeral=True)


@tree.command(name="setstatus", description="[Ops] Set the GMV Sprint program status")
@app_commands.describe(status="New status for GMV Sprint")
@app_commands.choices(status=[
    app_commands.Choice(name="Open",     value="Open"),
    app_commands.Choice(name="Paused",   value="Paused"),
    app_commands.Choice(name="Waitlist", value="Waitlist"),
])
@app_commands.checks.has_permissions(administrator=True)
async def setstatus(interaction: discord.Interaction, status: app_commands.Choice[str]):
    global GMV_SPRINT_STATUS
    GMV_SPRINT_STATUS = status.value
    _save_env_value("GMV_SPRINT_STATUS", status.value)
    await interaction.response.send_message(
        f"✅ GMV Sprint status set to **{status.value}** — saved, will persist after restart. Re-post the GMV Sprint panel to reflect this.",
        ephemeral=True
    )


@tree.command(name="close", description="[Ops] Close this ticket channel and save a transcript")
@app_commands.describe(reason="Reason for closing")
@app_commands.checks.has_permissions(administrator=True)
async def close_ticket(interaction: discord.Interaction, reason: str = "Closed by ops"):
    await interaction.response.defer(ephemeral=True)
    await _do_close(interaction.channel, reason, closed_by=interaction.user.display_name)
    await interaction.followup.send("✅ Ticket closed, locked, and transcript saved.", ephemeral=True)


@tree.command(name="audit", description="Analyze a TikTok video and get a quality score")
@app_commands.describe(url="TikTok video URL to audit")
async def audit_video(interaction: discord.Interaction, url: str):
    if "tiktok.com" not in url.lower():
        await interaction.response.send_message("❌ Please provide a valid TikTok URL.", ephemeral=True)
        return

    await interaction.response.defer()  # visible in channel so ops can see the result too
    status_msg = await interaction.followup.send("🔍 Analyzing your video... this takes 30–60 seconds.")

    try:
        result = await gas_audit(url)
    except Exception as e:
        await status_msg.edit(content=f"❌ Audit failed: {str(e)[:300]}\n\nTry again or ping an op.")
        return

    score  = float(result.get("final_score", 0))
    tier   = result.get("tier", "Unknown")
    tip    = result.get("improvement_tip", "")
    p1     = result.get("p1_score", 0)
    p2     = result.get("p2_score", 0)
    p3     = result.get("p3_score", 0)
    passed = score >= 70

    color_map = {
        "Elite":      0xFFD700,
        "Premium":    0x9B59B6,
        "Good":       0x2ECC71,
        "Borderline": 0xE67E22,
        "Reject":     0xE74C3C,
    }
    embed = discord.Embed(
        title=f"{'✅' if passed else '❌'} Video Audit — {score}/100",
        color=color_map.get(tier, 0x95A5A6)
    )
    embed.add_field(name="Score",  value=f"**{score}** / 100",                       inline=True)
    embed.add_field(name="Tier",   value=tier,                                        inline=True)
    embed.add_field(name="Gate",   value="✅ Pass (≥70)" if passed else "❌ Fail (<70)", inline=True)
    embed.add_field(name="Production (P1)", value=f"{p1} / 5", inline=True)
    embed.add_field(name="Category (P2)",   value=f"{p2} / 5", inline=True)
    embed.add_field(name="Message (P3)",    value=f"{p3} / 5", inline=True)

    if tip:
        embed.add_field(name="💡 Improvement Tip", value=tip, inline=False)

    author = result.get("tiktok_author", "")
    views  = result.get("tiktok_views",  0)
    likes  = result.get("tiktok_likes",  0)
    cover  = result.get("tiktok_cover",  "")
    if author:
        meta = f"@{author}"
        if views: meta += f" · {int(views):,} views"
        if likes: meta += f" · {int(likes):,} likes"
        embed.add_field(name="TikTok", value=meta, inline=False)
    if cover:
        embed.set_thumbnail(url=cover)

    embed.set_footer(
        text="Score ≥70 — ready for ops review! 🎉" if passed
        else "Score <70 — improve your video and run /audit again."
    )

    await status_msg.edit(content=None, embed=embed)

    # Auto-notify ops when a video passes in a video review channel
    if passed:
        cat = interaction.channel.category if interaction.channel else None
        if cat and cat.name == CAT_VIDEO:
            ops_ch = bot.get_channel(OPS_CHANNEL_ID)
            if ops_ch:
                ops_embed = discord.Embed(
                    title="📹 Video Passed Quality Gate",
                    color=discord.Color.green()
                )
                ops_embed.add_field(name="Creator", value=f"@{interaction.user.name}", inline=True)
                ops_embed.add_field(name="Score",   value=f"{score}/100 · {tier}",    inline=True)
                ops_embed.add_field(name="Channel", value=interaction.channel.mention, inline=True)
                ops_embed.add_field(name="Video",   value=url,                         inline=False)
                await ops_ch.send(content=OPS_PING or None, embed=ops_embed)


# ─── FOLLOW-UP CADENCE ─────────────────────────────────────
@tasks.loop(time=dt_time(hour=FOLLOWUP_HOUR, tzinfo=timezone.utc))
async def followup_check():
    print(f"[{datetime.now().strftime('%H:%M')}] Running daily follow-up check (UTC {FOLLOWUP_HOUR:02d}:00)...")

    # ── Part 1: sheet-based follow-up messages ────────────────
    try:
        sheet, records = await _sheet(_fetch_records)
        today = date.today()

        for i, row in enumerate(records, start=2):
            status   = str(row.get("Current Status", "")).strip()
            followup = str(row.get("Next Follow-Up", "")).strip()
            if status in ("Closed", "Approved", "Rejected", "Retainer Active"):
                continue
            if not followup:
                continue
            try:
                fu_date = datetime.strptime(followup, "%m/%d/%Y").date()
            except Exception:
                continue
            if fu_date != today:
                continue

            username       = str(row.get("Discord Username", "")).strip()
            last_touch_str = str(row.get("Last Team Touch", "")).strip()
            try:
                days_since = (today - datetime.strptime(last_touch_str, "%m/%d/%Y").date()).days
            except Exception:
                days_since = 2

            target_ch = None
            channel_id_str = str(row.get("Channel ID", "")).strip()
            if channel_id_str.isdigit():
                target_ch = bot.get_channel(int(channel_id_str))
            if not target_ch:
                ch_name = f"app-{username.replace('@', '')}"
                for guild in bot.guilds:
                    target_ch = discord.utils.get(guild.text_channels, name=ch_name)
                    if target_ch:
                        break
            if not target_ch:
                print(f"Channel not found for {username}")
                continue

            if days_since <= 2:
                await target_ch.send(MACRO_M02.format(username=username))
                _sheet_executor.submit(_update_row, i, {
                    "Status": "Missing Info",
                    "Next Follow-Up": (today + timedelta(days=2)).strftime("%m/%d/%Y")
                })
            elif days_since <= 4:
                await target_ch.send(MACRO_M03.format(username=username))
                _sheet_executor.submit(_update_row, i, {
                    "Status": "Follow-Up",
                    "Next Follow-Up": (today + timedelta(days=1)).strftime("%m/%d/%Y")
                })
            else:
                await target_ch.send(MACRO_CLOSE.format(username=username))
                _sheet_executor.submit(_update_row, i, {
                    "Status": "Closed",
                    "Close Reason": "No response after 5 days"
                })
                print(f"Closed ticket for {username}")

    except Exception as e:
        _reset_sheet()
        print(f"Follow-up check error (sheet): {e}")

    # ── Part 2: inactivity auto-close ────────────────────────
    ticket_categories = {CAT_APPLICATIONS, CAT_SUPPORT, CAT_VIDEO}
    now = datetime.now(timezone.utc)

    for guild in bot.guilds:
        for category in guild.categories:
            if category.name not in ticket_categories:
                continue
            for channel in category.text_channels:
                if channel.name.startswith("🔴"):
                    continue  # already closed
                try:
                    history = [msg async for msg in channel.history(limit=1)]
                    if not history:
                        continue
                    last_msg     = history[0]
                    idle_days    = (now - last_msg.created_at).days
                    last_is_bot  = last_msg.author.id == bot.user.id

                    if idle_days >= INACTIVITY_CLOSE_DAYS and last_is_bot:
                        # Bot sent the warning and nobody replied — auto-close
                        print(f"Auto-closing #{channel.name} ({idle_days}d idle)")
                        await _do_close(channel, "No response after inactivity warning", closed_by="Auto")

                    elif idle_days >= INACTIVITY_WARN_DAYS and not last_is_bot:
                        # Idle but no warning sent yet — warn
                        await channel.send(MACRO_INACTIVITY_WARN.format(username="there"))
                        print(f"Inactivity warning sent to #{channel.name} ({idle_days}d idle)")

                except Exception as e:
                    print(f"Inactivity check error for #{channel.name}: {e}")


@followup_check.before_loop
async def before_followup():
    await bot.wait_until_ready()


# ─── EVENTS ────────────────────────────────────────────────
FAQ_BOT_CATEGORIES = {CAT_APPLICATIONS, CAT_SUPPORT}  # NOT video review

# Channels where ops has taken over — bot stays silent here until ticket closes.
# In-memory only; clears on restart, which is fine because ops will just re-engage.
_OPS_HANDLED_CHANNELS: set[int] = set()

@bot.event
async def on_message(message: discord.Message):
    # Hard skips
    if message.author.bot or message.webhook_id:
        return
    if not message.guild:
        print(f"[FAQ] skip: no guild")
        return

    # Resolve category with 3 fallback levels — Discord's cache is unreliable after restart.
    category_name = None
    if message.channel.category:
        category_name = message.channel.category.name
    elif getattr(message.channel, "category_id", None):
        # Level 2: live API fetch
        try:
            cat = await bot.fetch_channel(message.channel.category_id)
            category_name = cat.name
            print(f"[FAQ] category resolved via fetch: '{category_name}'")
        except Exception as e:
            print(f"[FAQ] fetch_channel failed: {e}")

    # Level 3: infer from our own channel naming convention
    if category_name is None:
        chname = message.channel.name
        if "support-" in chname:
            category_name = CAT_SUPPORT
        elif "app-" in chname or "gmv-" in chname:
            category_name = CAT_APPLICATIONS
        if category_name:
            print(f"[FAQ] category inferred from channel name: '{category_name}'")

    if category_name is None:
        print(f"[FAQ] skip: could not resolve category for #{message.channel.name}")
        return
    if category_name not in FAQ_BOT_CATEGORIES:
        print(f"[FAQ] skip: category '{category_name}' not in {FAQ_BOT_CATEGORIES}")
        return
    if message.channel.name.startswith("🔴"):
        print(f"[FAQ] skip: closed channel #{message.channel.name}")
        return
    if len(message.content.strip()) < 5:
        print(f"[FAQ] skip: too short ({len(message.content)} chars)")
        return

    # Admin posted → hand the channel off to humans permanently
    if message.author.guild_permissions.administrator:
        if message.channel.id not in _OPS_HANDLED_CHANNELS:
            _OPS_HANDLED_CHANNELS.add(message.channel.id)
            print(f"[FAQ] ops took over #{message.channel.name} — bot silenced here")
        return

    # If ops has already taken over this channel, stay silent
    if message.channel.id in _OPS_HANDLED_CHANNELS:
        print(f"[FAQ] skip: #{message.channel.name} is ops-handled")
        return

    print(f"[FAQ] processing message from {message.author} in #{message.channel.name}: {message.content[:60]}")

    # Pull creator context from sheet (best-effort, non-blocking)
    creator_ctx = ""
    try:
        row_index, row = await _sheet(_find_by_channel_id, message.channel.id)
        if row:
            ctx_keys = ["Discord Username", "TikTok Handle", "Program", "Brand",
                        "GMV (30d)", "Current Status", "Sample Status"]
            creator_ctx = "\n".join(
                f"{k}: {row.get(k, 'unknown')}" for k in ctx_keys if row.get(k)
            )
    except Exception:
        pass

    async with message.channel.typing():
        answer = await gemini_answer(message.content, creator_ctx)

    if not answer:
        print(f"[FAQ] escalating to ops for: {message.content[:60]}")
        ping = OPS_PING if OPS_PING else "@here"
        escalation = discord.Embed(
            description=(
                f"🔔 {ping} — this one needs a human.\n"
                "The team will jump in shortly."
            ),
            color=0xE67E22,
        )
        await message.reply(
            content=ping if OPS_PING else None,  # actual mention outside embed so it pings
            embed=escalation,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )
        # Mark channel as handed off so we don't keep pinging ops
        _OPS_HANDLED_CHANNELS.add(message.channel.id)
        return

    embed = discord.Embed(description=answer, color=0xE8B84B)
    embed.set_footer(text="🤖 Auto-response from Octane Bot · If this doesn't help, an op will follow up.")
    await message.reply(embed=embed, mention_author=False)


@bot.event
async def on_ready():
    print(f"✅ {bot.user} is online")
    await tree.sync()
    print("✅ Slash commands synced")

    bot.add_view(StartHereView())
    bot.add_view(RetainerPanelView())
    bot.add_view(GMVSprintPanelView())
    bot.add_view(SupportPanelView())
    bot.add_view(VideoReviewPanelView())

    _sheet_executor.submit(get_sheet)

    if not followup_check.is_running():
        followup_check.start()


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need **Administrator** permission to use this command."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    else:
        raise error


bot.run(BOT_TOKEN)
