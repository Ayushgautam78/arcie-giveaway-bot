import sys
import os
import re
import json
import asyncio
import time
import datetime
import random
import io
import base64
import urllib.parse
import unicodedata
import traceback
from typing import Optional, List, Dict, Set, Union, Tuple, Any, Callable, Coroutine
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

import discord
from discord.ext import commands
from discord import app_commands
import aiohttp
from aiohttp import web

DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))

env_file_path = os.path.join(DATA_DIR, ".env")
if os.path.exists(env_file_path):
    load_dotenv(env_file_path, override=True)
load_dotenv(override=True)

# -------- Global Safety Net & Persistent Memories -------- #
MEMORY_FILE = os.path.join(DATA_DIR, "memories.json")
ADMIN_FILE = os.path.join(DATA_DIR, "admins.json")
USER_PROFILES_FILE = os.path.join(DATA_DIR, "user_profiles.json")
GIVEAWAYS_FILE = os.path.join(DATA_DIR, "giveaways.json")
GIVEAWAY_ENTRIES_FILE = os.path.join(DATA_DIR, "giveaway_entries.json")

USER_STATS_FILE = os.path.join(DATA_DIR, "user_stats.json")
REACTION_ROLES_FILE = os.path.join(DATA_DIR, "reaction_roles.json")
TICKETS_FILE = os.path.join(DATA_DIR, "tickets.json")

channel_memories: Dict[str, list] = {}
bot_admins: Set[str] = set()
user_stats: Dict[str, dict] = {}
voice_start_times: Dict[int, float] = {}
reaction_roles: Dict[str, dict] = {}
user_profiles: Dict[str, dict] = {}
giveaways: Dict[str, dict] = {}
giveaway_entries: Dict[str, list] = {}
active_sessions: Dict[str, dict] = {}
tickets_data: Dict[str, dict] = {"config": {"log_channel_id": None, "support_role_id": None, "category_id": None, "counter": 1}, "active_tickets": {}}

# -------- Role Multiplier Configuration (Weighted Raffle) -------- #
# Maps Discord Role ID (string) -> weight multiplier (float).
# Users with a matching role get higher chance of winning giveaways.
# If a user has multiple matching roles, the HIGHEST multiplier is used.
# Default weight for users with no matching roles = 1.0
ROLE_MULTIPLIERS: Dict[str, float] = {
    "1537729108736344074": 1.5,   # 1.5x chance
}
# Override from environment variable if set (JSON string)
_env_rm = os.getenv("ROLE_MULTIPLIERS")
if _env_rm:
    try:
        _parsed_rm = json.loads(_env_rm)
        if isinstance(_parsed_rm, dict):
            ROLE_MULTIPLIERS = {str(k): float(v) for k, v in _parsed_rm.items()}
    except Exception as _rm_err:
        print(f"[ROLE_MULTIPLIERS] Failed to parse env override: {_rm_err}")


def weighted_sample_without_replacement(population: list, weights: list, k: int) -> list:
    """Weighted random sampling WITHOUT replacement.
    
    Uses iterative weighted selection: pick one item at a time using random.choices,
    remove it from the pool, and repeat until k items are selected or pool is exhausted.
    """
    if not population or k <= 0:
        return []
    k = min(k, len(population))
    pool = list(population)
    pool_weights = list(weights)
    selected = []
    for _ in range(k):
        if not pool:
            break
        chosen = random.choices(pool, weights=pool_weights, k=1)[0]
        idx = pool.index(chosen)
        selected.append(chosen)
        pool.pop(idx)
        pool_weights.pop(idx)
    return selected


def get_entry_weights(entries: list, guild) -> list:
    """Calculate weight for each giveaway entry based on role multipliers.
    
    Args:
        entries: List of entry dicts with 'user_id' field
        guild: discord.Guild object (or None if unavailable)
    
    Returns:
        List of float weights, one per entry. Default weight = 1.0
    """
    if not guild or not ROLE_MULTIPLIERS:
        return [1.0] * len(entries)
    
    weights = []
    for entry in entries:
        uid = entry.get("user_id")
        weight = 1.0
        if uid:
            try:
                member = guild.get_member(int(uid))
                if member:
                    matching = [ROLE_MULTIPLIERS[str(r.id)]
                               for r in member.roles if str(r.id) in ROLE_MULTIPLIERS]
                    if matching:
                        weight = max(matching)
            except Exception:
                pass
        weights.append(weight)
    return weights


# -------- Firebase Database Configuration -------- #
FIREBASE_URL = (
    os.getenv("FIREBASE_DATABASE_URL") or 
    os.getenv("FIREBASE_URL") or 
    os.getenv("FIREBASE_DB_URL") or 
    "https://arcie-bot-default-rtdb.asia-southeast1.firebasedatabase.app"
).rstrip("/")

def firebase_put_sync(path: str, data):
    if not FIREBASE_URL: return
    url = f"{FIREBASE_URL}/{path.lstrip('/')}.json"
    try:
        clean_data = data
        if isinstance(data, (dict, list)):
            import copy
            clean_data = copy.deepcopy(data)
            def sanitize(obj):
                if isinstance(obj, dict):
                    for k, v in list(obj.items()):
                        if k == "banner_url" and isinstance(v, str) and v.startswith("data:image") and len(v) > 500000:
                            obj[k] = ""
                        else:
                            sanitize(v)
                elif isinstance(obj, list):
                    for item in obj:
                        sanitize(item)
            sanitize(clean_data)

        if clean_data is None:
            req = urllib.request.Request(url, method='DELETE')
        else:
            req = urllib.request.Request(url, data=json.dumps(clean_data).encode('utf-8'), method='PUT')
            req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=8) as resp:
            print(f"[FIREBASE PUT SUCCESS] {path}: status {resp.status}")
    except Exception as e:
        print(f"[FIREBASE PUT ERROR] {path}: {e}")

async def firebase_get(path: str) -> Optional[dict]:
    if not FIREBASE_URL:
        return None
    url = f"{FIREBASE_URL}/{path.lstrip('/')}.json"
    try:
        loop = asyncio.get_running_loop()
        def _do_get():
            try:
                req = urllib.request.Request(url, method='GET')
                with urllib.request.urlopen(req, timeout=8) as resp:
                    if resp.status == 200:
                        content = resp.read().decode('utf-8')
                        return json.loads(content)
            except Exception as ge:
                print(f"[FIREBASE GET ERROR] {path}: {ge}")
            return None
        return await loop.run_in_executor(None, _do_get)
    except Exception:
        return None

async def firebase_put(path: str, data):
    if not FIREBASE_URL:
        return
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, firebase_put_sync, path, data)
    except Exception:
        firebase_put_sync(path, data)

def merge_user_profiles(target: dict, source: dict):
    """Deep non-destructive merge: never overwrite non-empty data with empty values."""
    if not isinstance(source, dict): return
    for uid, src_prof in source.items():
        if not isinstance(src_prof, dict): continue
        uid = str(uid)
        if uid not in target or not isinstance(target[uid], dict):
            target[uid] = src_prof.copy()
        else:
            tgt = target[uid]
            for k, v in src_prof.items():
                if v and (not isinstance(v, str) or v.strip()):
                    tgt[k] = v

# Load persistent user profiles (long-term memory across days/weeks)
if os.path.exists(USER_PROFILES_FILE):
    try:
        with open(USER_PROFILES_FILE, "r", encoding="utf-8") as f:
            disk_profiles = json.load(f)
            merge_user_profiles(user_profiles, disk_profiles)
        print(f"[PROFILES] Loaded persistent profiles for {len(user_profiles)} users.")
    except Exception as e:
        print(f"[PROFILES ERROR] Failed to load user profiles: {e}")

def sync_firebase_background(path: str, data):
    """Safely and asynchronously sync data to Firebase Cloud DB without blocking the event loop."""
    if not FIREBASE_URL or data is None:
        return
    try:
        loop = asyncio.get_running_loop()
        if loop.is_running():
            loop.create_task(firebase_put(path, data))
            return
    except (RuntimeError, AttributeError):
        pass

    # Fallback to non-blocking daemon thread if called outside running asyncio loop
    try:
        import threading
        threading.Thread(target=firebase_put_sync, args=(path, data), daemon=True).start()
    except Exception as e:
        print(f"[FIREBASE BACKGROUND SYNC ERROR] {path}: {e}")

def save_user_profiles():
    try:
        with open(USER_PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(user_profiles, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[PROFILES ERROR] Failed to save user profiles: {e}")
    if FIREBASE_URL and user_profiles:
        sync_firebase_background("user_profiles", user_profiles)

# Load persistent giveaways
if os.path.exists(GIVEAWAYS_FILE):
    try:
        with open(GIVEAWAYS_FILE, "r", encoding="utf-8") as f:
            giveaways = json.load(f)
        print(f"[GIVEAWAYS] Loaded {len(giveaways)} giveaways.")
    except Exception as e:
        print(f"[GIVEAWAYS ERROR] Failed to load giveaways: {e}")

def save_banner_image_if_data_url(g_data: dict) -> str:
    """If banner_url is a Base64 data:image string, save it to static/uploads/ and return relative path."""
    if not isinstance(g_data, dict):
        return ""
    banner = str(g_data.get("banner_url", "")).strip()
    if not banner or not banner.startswith("data:image"):
        return banner
    
    try:
        header, encoded = banner.split(",", 1)
        ext = ".png"
        if "jpeg" in header or "jpg" in header: ext = ".jpg"
        elif "gif" in header: ext = ".gif"
        elif "webp" in header: ext = ".webp"
        
        img_bytes = base64.b64decode(encoded)
        gid = g_data.get("id", f"banner_{int(time.time())}")
        base_dir = os.path.dirname(os.path.abspath(__file__))
        uploads_dir = os.path.join(base_dir, "static", "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        
        filename = f"banner_{gid}{ext}"
        filepath = os.path.join(uploads_dir, filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)
        
        rel_url = f"/static/uploads/{filename}"
        g_data["banner_url"] = rel_url
        print(f"[BANNER SAVE SUCCESS] Saved base64 banner to {rel_url}")
        return rel_url
    except Exception as e:
        print(f"[BANNER SAVE ERROR] {e}")
        return banner

DELETED_GIVEAWAYS_FILE = "deleted_giveaways.json"
deleted_giveaways: set = set()

if os.path.exists(DELETED_GIVEAWAYS_FILE):
    try:
        with open(DELETED_GIVEAWAYS_FILE, "r", encoding="utf-8") as f:
            d_data = json.load(f)
            if isinstance(d_data, list):
                deleted_giveaways = set(str(x) for x in d_data)
            elif isinstance(d_data, dict):
                deleted_giveaways = set(str(x) for x in d_data.keys())
        print(f"[DELETED GIVEAWAYS] Loaded {len(deleted_giveaways)} deleted giveaway records.")
    except Exception as e:
        print(f"[DELETED GIVEAWAYS ERROR] Failed to load: {e}")

def save_deleted_giveaways():
    try:
        with open(DELETED_GIVEAWAYS_FILE, "w", encoding="utf-8") as f:
            json.dump(list(deleted_giveaways), f, indent=2)
    except Exception as e:
        print(f"[DELETED GIVEAWAYS ERROR] Failed to save: {e}")

def save_giveaways():
    try:
        for g_id, g in list(giveaways.items()):
            if isinstance(g, dict):
                save_banner_image_if_data_url(g)
        with open(GIVEAWAYS_FILE, "w", encoding="utf-8") as f:
            json.dump(giveaways, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[GIVEAWAYS ERROR] Failed to save giveaways: {e}")
    if FIREBASE_URL:
        sync_firebase_background("giveaways", giveaways)

# Load persistent giveaway entries
if os.path.exists(GIVEAWAY_ENTRIES_FILE):
    try:
        with open(GIVEAWAY_ENTRIES_FILE, "r", encoding="utf-8") as f:
            giveaway_entries = json.load(f)
        print(f"[GIVEAWAY ENTRIES] Loaded entries for {len(giveaway_entries)} giveaways.")
    except Exception as e:
        print(f"[GIVEAWAY ENTRIES ERROR] Failed to load giveaway entries: {e}")

def save_giveaway_entries():
    try:
        with open(GIVEAWAY_ENTRIES_FILE, "w", encoding="utf-8") as f:
            json.dump(giveaway_entries, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[GIVEAWAY ENTRIES ERROR] Failed to save giveaway entries: {e}")
    if FIREBASE_URL:
        sync_firebase_background("giveaway_entries", giveaway_entries)

def get_user_profile_fast(uid: str, usr: Optional[discord.User] = None) -> dict:
    """Fast non-blocking profile retrieval with in-memory cache lookup."""
    uid_str = str(uid)
    prof = user_profiles.get(uid_str)
    if prof and isinstance(prof, dict):
        return prof

    # Check local in-memory giveaway_entries for previously entered data
    for g_id, entries in giveaway_entries.items():
        if isinstance(entries, list):
            for e in entries:
                if isinstance(e, dict) and str(e.get("user_id")) == uid_str:
                    if e.get("evm_wallet") or e.get("solana_wallet") or e.get("twitter") or e.get("telegram"):
                        display_name = getattr(usr, 'display_name', None) or e.get("display_name") or uid_str
                        username = getattr(usr, 'name', None) or e.get("username") or uid_str
                        prof = {
                            "display_name": display_name,
                            "username": username,
                            "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                            "evm_wallet": e.get("evm_wallet", ""),
                            "solana_wallet": e.get("solana_wallet", ""),
                            "twitter": e.get("twitter", ""),
                            "telegram": e.get("telegram", "")
                        }
                        user_profiles[uid_str] = prof
                        save_user_profiles()
                        return prof

    return {}

async def get_or_fetch_user_profile(uid: str, usr: Optional[discord.User] = None) -> dict:
    """Fast profile retrieval without slow blocking Firebase tree downloads."""
    return get_user_profile_fast(uid, usr)

# Track task button clicks per user per giveaway: {g_id: {user_id: set(task_types_completed)}}
giveaway_task_progress = {}
# Track users shown the task prompt: {g_id: set(user_ids)}
giveaway_join_prompted = {}

def update_user_memory(user: Union[discord.Member, discord.User], message_text: str = ""):
    if not user or getattr(user, 'bot', False):
        return
    uid = str(user.id)
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    display_name = getattr(user, 'display_name', user.name)
    username = getattr(user, 'name', str(user))
    
    if uid not in user_profiles:
        user_profiles[uid] = {
            "display_name": display_name,
            "username": username,
            "facts": [],
            "first_seen": now_str,
            "last_seen": now_str
        }
    else:
        user_profiles[uid]["display_name"] = display_name
        user_profiles[uid]["username"] = username
        user_profiles[uid]["last_seen"] = now_str

    if message_text:
        text_lower = message_text.lower()
        match = re.search(r'\b(my name is|call me|i am|i live in|i love|i like|i hate|my favorite|i support|i work as|i am a|my birthday is|i play|my handle is|i study|i study at)\s+([^.!?\n]+)', text_lower)
        if match:
            fact = f"{match.group(1)} {match.group(2).strip()}"
            existing = user_profiles[uid].get("facts", [])
            if fact not in existing and len(existing) < 20:
                existing.append(fact)
                user_profiles[uid]["facts"] = existing

    save_user_profiles()

# Load persistent user stats
if os.path.exists(USER_STATS_FILE):
    try:
        with open(USER_STATS_FILE, "r", encoding="utf-8") as f:
            user_stats = json.load(f)
        print(f"[STATS] Loaded stats for {len(user_stats)} users.")
    except Exception as e:
        print(f"[STATS ERROR] Failed to load user stats: {e}")

def save_user_stats():
    try:
        with open(USER_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_stats, f, indent=2)
    except Exception as e:
        print(f"[STATS ERROR] Failed to save user stats: {e}")

# Load persistent reaction roles
if os.path.exists(REACTION_ROLES_FILE):
    try:
        with open(REACTION_ROLES_FILE, "r", encoding="utf-8") as f:
            reaction_roles = json.load(f)
        print(f"[RR] Loaded reaction roles for {len(reaction_roles)} messages.")
    except Exception as e:
        print(f"[RR ERROR] Failed to load reaction roles: {e}")

def save_reaction_roles():
    try:
        with open(REACTION_ROLES_FILE, "w", encoding="utf-8") as f:
            json.dump(reaction_roles, f, indent=2)
    except Exception as e:
        print(f"[RR ERROR] Failed to save reaction roles: {e}")
    if FIREBASE_URL and reaction_roles:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(firebase_put("reaction_roles", reaction_roles))
        except Exception:
            pass

# Load persistent tickets
if os.path.exists(TICKETS_FILE):
    try:
        with open(TICKETS_FILE, "r", encoding="utf-8") as f:
            disk_tickets = json.load(f)
            if isinstance(disk_tickets, dict):
                tickets_data.update(disk_tickets)
        print(f"[TICKETS] Loaded tickets config & {len(tickets_data.get('active_tickets', {}))} active tickets.")
    except Exception as e:
        print(f"[TICKETS ERROR] Failed to load tickets: {e}")

def save_tickets():
    try:
        with open(TICKETS_FILE, "w", encoding="utf-8") as f:
            json.dump(tickets_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[TICKETS ERROR] Failed to save tickets: {e}")
    if FIREBASE_URL and tickets_data:
        sync_firebase_background("tickets", tickets_data)


def is_ticket_staff(member: discord.Member) -> bool:
    """Check if a Discord member is a Moderator, Admin, or Ticket Staff."""
    if not isinstance(member, discord.Member):
        return False
    uid = str(member.id)
    # Check 1: Bot Admin ID
    if is_bot_admin_by_id(uid):
        return True
    # Check 2: Guild Administrator / Manage Channels / Manage Messages / Kick / Ban
    perms = member.guild_permissions
    if perms.administrator or perms.manage_channels or perms.manage_messages or perms.kick_members or perms.ban_members:
        return True
    # Check 3: Configured Support Role ID
    cfg = tickets_data.get("config", {})
    supp_role_id = cfg.get("support_role_id")
    if supp_role_id:
        try:
            role = member.guild.get_role(int(supp_role_id))
            if role and role in member.roles:
                return True
        except Exception:
            pass
    # Check 4: Any role containing 'mod', 'admin', 'staff', 'support', 'team', or 'helper'
    for r in member.roles:
        r_name = r.name.lower()
        if any(keyword in r_name for keyword in ["mod", "admin", "staff", "support", "team", "helper"]):
            return True
    return False


# -------- Discord Ticket System Persistent Views -------- #
class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_ctrl_close", emoji="🔒")
    async def close_ticket_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._close_ticket(interaction)

    @discord.ui.button(label="📌 Claim Ticket", style=discord.ButtonStyle.secondary, custom_id="ticket_ctrl_claim", emoji="📌")
    async def claim_ticket_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_ticket_staff(interaction.user):
            await interaction.response.send_message("❌ Only Moderators and Staff can claim support tickets!", ephemeral=True)
            return
        await interaction.response.send_message(f"📌 Ticket claimed by {interaction.user.mention}!", ephemeral=False)

    @discord.ui.button(label="📥 Save Transcript", style=discord.ButtonStyle.primary, custom_id="ticket_ctrl_transcript", emoji="📥")
    async def transcript_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        transcript_file = await self._generate_transcript(interaction.channel)
        if transcript_file:
            await interaction.followup.send("📥 Here is the ticket transcript:", file=transcript_file, ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to generate transcript.", ephemeral=True)

    async def _close_ticket(self, interaction: discord.Interaction):
        # SECURITY CHECK: Only Moderators & Staff can close tickets!
        if not is_ticket_staff(interaction.user):
            deny_msg = "❌ Only Moderators and Staff can close support tickets! The ticket creator cannot close the ticket unless they are a moderator."
            if interaction.response.is_done():
                await interaction.followup.send(deny_msg, ephemeral=True)
            else:
                await interaction.response.send_message(deny_msg, ephemeral=True)
            return

        ch_id = str(interaction.channel.id)
        t_info = tickets_data.get("active_tickets", {}).get(ch_id, {})
        
        close_msg = "🔒 Closing ticket in 5 seconds... Generating transcript..."
        if interaction.response.is_done():
            await interaction.followup.send(close_msg, ephemeral=False)
        else:
            await interaction.response.send_message(close_msg, ephemeral=False)

        # Generate Transcript
        transcript_file = await self._generate_transcript(interaction.channel)

        # Log Transcript to Log Channel if configured
        cfg = tickets_data.get("config", {})
        log_ch_id = cfg.get("log_channel_id")
        if log_ch_id:
            log_ch = interaction.guild.get_channel(int(log_ch_id))
            if not log_ch:
                try: log_ch = await interaction.guild.fetch_channel(int(log_ch_id))
                except Exception: log_ch = None

            if log_ch:
                opener_uid = t_info.get("user_id", "Unknown")
                log_embed = discord.Embed(
                    title=f"📜 Ticket Closed — {t_info.get('ticket_code', 'ticket')}",
                    description=(
                        f"• **Channel:** #{interaction.channel.name}\n"
                        f"• **Opened By:** <@{opener_uid}>\n"
                        f"• **Closed By:** {interaction.user.mention}\n"
                        f"• **Category:** {t_info.get('category', 'General Support')}"
                    ),
                    color=discord.Color.red()
                )
                try:
                    if transcript_file:
                        await log_ch.send(embed=log_embed, file=transcript_file)
                    else:
                        await log_ch.send(embed=log_embed)
                except Exception as le:
                    print(f"[TICKET LOG FAIL] {le}")

        # Update ticket status
        if ch_id in tickets_data.get("active_tickets", {}):
            tickets_data["active_tickets"][ch_id]["status"] = "closed"
            tickets_data["active_tickets"][ch_id]["closed_at"] = int(time.time())
            save_tickets()

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception as de:
            print(f"[TICKET DELETE FAIL] {de}")

    async def _generate_transcript(self, channel) -> Optional[discord.File]:
        try:
            messages = []
            async for msg in channel.history(limit=500, oldest_first=True):
                time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
                content = msg.clean_content or "[No Text]"
                if msg.attachments:
                    att_urls = ", ".join([att.url for att in msg.attachments])
                    content += f" (Attachments: {att_urls})"
                messages.append(f"[{time_str}] {msg.author.name} ({msg.author.id}): {content}")

            transcript_text = f"=== TICKET TRANSCRIPT — #{channel.name} ===\n" + "\n".join(messages)
            fp = io.BytesIO(transcript_text.encode('utf-8'))
            return discord.File(fp, filename=f"transcript-{channel.name}.txt")
        except Exception as e:
            print(f"[TRANSCRIPT GENERATION ERROR] {e}")
            return None


class TicketCategorySelect(discord.ui.Select):
    def __init__(self, server_name: str = "Support"):
        options = [
            discord.SelectOption(
                label="General Ticket",
                description="General enquiries or misc enquiries can be raised here.",
                emoji="📩",
                value="general"
            ),
            discord.SelectOption(
                label="Technical Ticket",
                description="Technical enquiries or issues can be raised here.",
                emoji="🤖",
                value="technical"
            ),
            discord.SelectOption(
                label="Partnerships & Collaboration Requests",
                description="Partnerships & Collaboration enquiries can be raised here.",
                emoji="🤝",
                value="collab"
            ),
            discord.SelectOption(
                label="Claim Giveaway Prize",
                description="Submit wallets & claim giveaway prizes here.",
                emoji="🏆",
                value="giveaway"
            ),
            discord.SelectOption(
                label="Report Issue / Bug",
                description="Report server bugs, user issues or feedback here.",
                emoji="🐛",
                value="report"
            )
        ]
        placeholder_text = f"Welcome to {server_name} Support Desk, how can we help today?"
        if len(placeholder_text) > 100:
            placeholder_text = placeholder_text[:97] + "..."

        super().__init__(
            placeholder=placeholder_text,
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket_category_select"
        )

    async def callback(self, interaction: discord.Interaction):
        selected_val = self.values[0]
        title_map = {
            "general": "General Ticket",
            "technical": "Technical Ticket",
            "collab": "Partnerships & Collaboration",
            "giveaway": "Claim Giveaway Prize",
            "report": "Report Issue"
        }
        category_title = title_map.get(selected_val, "General Support")
        view = self.view
        if isinstance(view, TicketLaunchView):
            await view._create_ticket(interaction, category_title, selected_val)
        else:
            dummy_view = TicketLaunchView()
            await dummy_view._create_ticket(interaction, category_title, selected_val)


class TicketLaunchView(discord.ui.View):
    def __init__(self, server_name: str = "Support"):
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect(server_name=server_name))

    async def _create_ticket(self, interaction: discord.Interaction, category_title: str, category_key: str):
        guild = interaction.guild
        if not guild:
            await interaction.response.send_message("❌ Tickets can only be created in a server.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        user_id = str(interaction.user.id)
        active_tickets = tickets_data.setdefault("active_tickets", {})

        # Check if user already has an open ticket in this server
        for ch_id, t_info in active_tickets.items():
            if isinstance(t_info, dict) and t_info.get("user_id") == user_id and t_info.get("status") == "open":
                existing_ch = guild.get_channel(int(ch_id))
                if existing_ch:
                    await interaction.followup.send(f"⚠️ You already have an open ticket in {existing_ch.mention}!", ephemeral=True)
                    return

        # Increment ticket counter
        cfg = tickets_data.setdefault("config", {"counter": 1})
        counter = cfg.get("counter", 1)
        cfg["counter"] = counter + 1
        ticket_code = f"ticket-{counter:04d}"

        # Resolve or create ticket category
        category = None
        cat_id = cfg.get("category_id")
        if cat_id:
            try: category = guild.get_channel(int(cat_id))
            except Exception: category = None
        if not category:
            category = discord.utils.get(guild.categories, name="🎫 TICKETS") or discord.utils.get(guild.categories, name="TICKETS")
            if not category:
                try:
                    category = await guild.create_category(name="🎫 TICKETS")
                except Exception:
                    category = None

        # Build Channel Overwrites
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                read_messages=True,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True
            ),
            guild.me: discord.PermissionOverwrite(
                read_messages=True,
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
                embed_links=True,
                attach_files=True
            )
        }

        # Add support role if configured
        supp_role_id = cfg.get("support_role_id")
        if supp_role_id:
            role = guild.get_role(int(supp_role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, view_channel=True, send_messages=True)

        clean_uname = re.sub(r'[^a-zA-Z0-9]', '', interaction.user.name).lower() or "user"
        channel_name = f"{category_key}-{clean_uname}-{counter:03d}"

        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket {ticket_code} | Opener: {interaction.user} ({user_id}) | Category: {category_title}"
            )
        except Exception as e:
            print(f"[TICKET CREATION FAIL] {e}")
            await interaction.followup.send("❌ Failed to create ticket channel. Please check bot permissions.", ephemeral=True)
            return

        # Store ticket record
        active_tickets[str(ticket_channel.id)] = {
            "ticket_code": ticket_code,
            "user_id": user_id,
            "username": interaction.user.name,
            "display_name": interaction.user.display_name,
            "category": category_title,
            "created_at": int(time.time()),
            "status": "open"
        }
        save_tickets()

        # Build Ticket Channel Welcome Embed
        embed = discord.Embed(
            title=f"🎫 Support Ticket — {category_title}",
            description=(
                f"Welcome {interaction.user.mention}!\n\n"
                f"Our support team has been notified. Please describe your request or issue below in detail.\n\n"
                f"• **Ticket Code:** `{ticket_code}`\n"
                f"• **Category:** {category_title}\n"
                f"• **Opened By:** {interaction.user.mention}"
            ),
            color=discord.Color.blue()
        )
        embed.set_footer(text="Click the control buttons below to manage this ticket | Powered by Arcie Bot")
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        control_view = TicketControlView()
        await ticket_channel.send(content=f"Welcome {interaction.user.mention}!", embed=embed, view=control_view)

        await interaction.followup.send(f"✅ Ticket created successfully! Head over to {ticket_channel.mention}", ephemeral=True)

# Load persistent memory
if os.path.exists(MEMORY_FILE):
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            channel_memories = json.load(f)
        print(f"[MEMORY] Loaded persistent memories for {len(channel_memories)} channels.")
    except Exception as e:
        print(f"[MEMORY ERROR] Failed to load memories: {e}")

def save_memories():
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(channel_memories, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[MEMORY ERROR] Failed to save memories: {e}")

# Load persistent admins
if os.path.exists(ADMIN_FILE):
    try:
        with open(ADMIN_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                bot_admins = set(str(x) for x in data)
        print(f"[ADMIN] Loaded {len(bot_admins)} bot admins from database.")
    except Exception as e:
        print(f"[ADMIN ERROR] Failed to load admins: {e}")

def save_admins():
    try:
        with open(ADMIN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(bot_admins), f, indent=2)
    except Exception as e:
        print(f"[ADMIN ERROR] Failed to save admins: {e}")

def is_bot_admin(target) -> bool:
    author = getattr(target, 'author', None) or getattr(target, 'user', None)
    if not author:
        return False
    user_id = str(author.id)
    if user_id in bot_admins or user_id == "1066987338204459049":
        return True
    guild = getattr(target, 'guild', None)
    if guild and isinstance(author, discord.Member):
        if author.guild_permissions.administrator or guild.owner_id == author.id:
            return True
    return False

def is_bot_admin_by_id(user_id: str) -> bool:
    user_id_str = str(user_id)
    if user_id_str in bot_admins or user_id_str == "1066987338204459049":
        return True
    if bot and bot.guilds:
        for guild in bot.guilds:
            if guild.owner_id == int(user_id_str if user_id_str.isdigit() else 0):
                return True
            member = guild.get_member(int(user_id_str if user_id_str.isdigit() else 0))
            if member and member.guild_permissions.administrator:
                return True
    return False

def is_mod_or_admin(target) -> bool:
    if is_bot_admin(target):
        return True
    author = getattr(target, 'author', None) or getattr(target, 'user', None)
    guild = getattr(target, 'guild', None)
    if guild and isinstance(author, discord.Member):
        perms = author.guild_permissions
        if perms.administrator or perms.manage_messages or perms.manage_roles or perms.moderate_members or perms.kick_members or perms.ban_members:
            return True
    return False

def is_admin(user=None, guild=None) -> bool:
    """Check if a user is an admin or mod with permissions to manage the bot or server."""
    if not user:
        return False
    # If passed an Interaction or Context object
    if hasattr(user, 'user') or hasattr(user, 'author'):
        target_user = getattr(user, 'user', None) or getattr(user, 'author', None)
        target_guild = getattr(user, 'guild', None) or guild
    else:
        target_user = user
        target_guild = guild

    if not target_user:
        return False

    uid = str(target_user.id)
    if uid in bot_admins or uid == "1066987338204459049":
        return True

    if target_guild and hasattr(target_guild, 'owner_id') and target_guild.owner_id == target_user.id:
        return True

    if isinstance(target_user, discord.Member):
        perms = target_user.guild_permissions
        if perms.administrator or perms.manage_guild or perms.manage_channels or perms.manage_messages:
            return True
        for r in getattr(target_user, 'roles', []):
            r_name = r.name.lower()
            if any(k in r_name for k in ["admin", "mod", "staff", "owner", "host"]):
                return True
    elif target_guild and hasattr(target_guild, 'get_member'):
        member = target_guild.get_member(target_user.id)
        if member:
            perms = member.guild_permissions
            if perms.administrator or perms.manage_guild or perms.manage_channels or perms.manage_messages:
                return True
            for r in getattr(member, 'roles', []):
                r_name = r.name.lower()
                if any(k in r_name for k in ["admin", "mod", "staff", "owner", "host"]):
                    return True
    return False

# -------- Discord Bot Setup -------- #
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(
    command_prefix=".",
    intents=intents,
    allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True)
)
session: Optional[aiohttp.ClientSession] = None
user_spam_map: Dict[int, list] = {}
processed_messages: Set[int] = set()

# -------- Image, Tenor & Klipy GIF URL Resolver -------- #
async def resolve_image_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    clean_url = url.strip()
    if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        return None

    # Strip query parameters (e.g. ?ex=...&is=...) to accurately detect direct GIF files from Discord CDN & phone uploads
    base_url = clean_url.split("?")[0].lower()
    gif_sites = ["tenor.com", "klipy.co", "klipy.com", "giphy.com", "imgur.com"]
    is_gif_site = any(domain in clean_url.lower() for domain in gif_sites)
    is_direct_media = base_url.endswith((".gif", ".png", ".jpg", ".jpeg", ".webp")) or "media.tenor.com" in clean_url or "cdn.discordapp.com" in clean_url or "media.discordapp.net" in clean_url or "cdn.klipy" in clean_url

    if is_gif_site or not is_direct_media:
        global session
        if session is None or session.closed:
            session = aiohttp.ClientSession()
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
            async with session.get(clean_url, headers=headers, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    soup = BeautifulSoup(html, "html.parser")
                    for prop in ["og:image", "og:image:secure_url", "twitter:image", "og:video"]:
                        meta_img = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
                        if meta_img and meta_img.get("content"):
                            img_src = meta_img["content"].strip()
                            if img_src.startswith("http://") or img_src.startswith("https://"):
                                return img_src
        except Exception as e:
            print(f"[GIF RESOLVER ERROR for {clean_url}] {e}")

    return clean_url

# -------- Color Name & Hex Code Resolver -------- #
COLOR_NAME_MAP = {
    "red": discord.Color.from_rgb(231, 76, 60),
    "crimson": discord.Color.from_rgb(192, 57, 43),
    "dark_red": discord.Color.from_rgb(150, 0, 0),
    "blue": discord.Color.from_rgb(52, 152, 219),
    "dark_blue": discord.Color.from_rgb(41, 128, 185),
    "sky_blue": discord.Color.from_rgb(0, 168, 252),
    "cyan": discord.Color.from_rgb(26, 188, 156),
    "teal": discord.Color.from_rgb(26, 188, 156),
    "green": discord.Color.from_rgb(46, 204, 113),
    "dark_green": discord.Color.from_rgb(39, 174, 96),
    "yellow": discord.Color.from_rgb(241, 196, 15),
    "gold": discord.Color.from_rgb(243, 156, 18),
    "orange": discord.Color.from_rgb(230, 126, 34),
    "purple": discord.Color.from_rgb(155, 89, 182),
    "dark_purple": discord.Color.from_rgb(142, 68, 173),
    "violet": discord.Color.from_rgb(155, 89, 182),
    "pink": discord.Color.from_rgb(236, 64, 122),
    "fuchsia": discord.Color.from_rgb(233, 30, 99),
    "magenta": discord.Color.from_rgb(233, 30, 99),
    "black": discord.Color.from_rgb(1, 1, 1),
    "dark": discord.Color.from_rgb(47, 49, 54),
    "white": discord.Color.from_rgb(255, 255, 255),
    "grey": discord.Color.from_rgb(149, 165, 166),
    "gray": discord.Color.from_rgb(149, 165, 166),
}

def parse_embed_color(color_input: Optional[str]) -> discord.Color:
    if not color_input:
        return discord.Color.from_rgb(0, 168, 252)
    clean = color_input.strip().lower().replace(" ", "_")
    if clean in COLOR_NAME_MAP:
        return COLOR_NAME_MAP[clean]
    if clean.startswith("#"):
        try:
            return discord.Color.from_str(clean)
        except Exception:
            pass
    if len(clean) == 6 and all(c in "0123456789abcdef" for c in clean):
        try:
            return discord.Color.from_str(f"#{clean}")
        except Exception:
            pass
    try:
        return discord.Color.from_str(clean)
    except Exception:
        pass
    return discord.Color.from_rgb(0, 168, 252)

# -------- MP4 Video / Video-GIF to True Animated GIF Converter -------- #
async def convert_video_to_gif(video_bytes: bytes) -> Optional[bytes]:
    if not video_bytes:
        return None
    if video_bytes.startswith(b'GIF87a') or video_bytes.startswith(b'GIF89a'):
        return video_bytes
    try:
        proc = await asyncio.create_subprocess_exec(
            'ffmpeg', '-y',
            '-i', 'pipe:0',
            '-vf', 'fps=12,scale=320:-1:flags=lanczos',
            '-f', 'gif',
            'pipe:1',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate(input=video_bytes)
        if proc.returncode == 0 and stdout and len(stdout) > 0:
            return stdout
    except Exception as e:
        print(f"[FFMPEG CONVERT ERROR] {e}")
    return None

# -------- Embed Attachment & Media Attachment Processor (Image, GIF, MP4, 3GP, etc.) -------- #
async def process_embed_attachments(
    thumbnail_file: Optional[discord.Attachment] = None,
    image_file: Optional[discord.Attachment] = None,
    thumbnail_url: Optional[str] = None,
    image_url: Optional[str] = None,
    embed: Optional[discord.Embed] = None
) -> List[discord.File]:
    files = []
    if embed is None:
        return files

    VIDEO_EXTENSIONS = {"mp4", "mov", "webm", "m4v", "3gp", "3g2", "mkv", "avi", "flv", "wmv", "ogv"}

    # 1. Thumbnail Attachment / URL Processing
    if thumbnail_file:
        try:
            fn = (thumbnail_file.filename or "thumb.png").lower()
            ext = fn.rsplit(".", 1)[-1] if "." in fn else "png"
            if ext in ["jpeg", "jpg"]:
                ext = "jpg"

            fbytes = await thumbnail_file.read()
            if fbytes:
                conv_gif = None
                if ext in VIDEO_EXTENSIONS or ext == "gif":
                    conv_gif = await convert_video_to_gif(fbytes)
                    if conv_gif:
                        fbytes = conv_gif
                        ext = "gif"

                if ext in VIDEO_EXTENSIONS and not conv_gif:
                    dfile = discord.File(fp=io.BytesIO(fbytes), filename=f"thumb_{thumbnail_file.filename}")
                    files.append(dfile)
                else:
                    dfile = discord.File(fp=io.BytesIO(fbytes), filename=f"thumb.{ext}")
                    files.append(dfile)
                    embed.set_thumbnail(url=f"attachment://thumb.{ext}")
        except Exception as e:
            print(f"[THUMBNAIL ATTACHMENT ERROR] {e}")
            if thumbnail_file.url:
                res = await resolve_image_url(thumbnail_file.url)
                if res:
                    embed.set_thumbnail(url=res)
    elif thumbnail_url:
        res = await resolve_image_url(thumbnail_url)
        if res:
            embed.set_thumbnail(url=res)

    # 2. Main Banner Image / Video Attachment / URL Processing
    if image_file:
        try:
            fn = (image_file.filename or "banner.png").lower()
            ext = fn.rsplit(".", 1)[-1] if "." in fn else "png"
            if ext in ["jpeg", "jpg"]:
                ext = "jpg"

            fbytes = await image_file.read()
            if fbytes:
                conv_gif = None
                if ext in VIDEO_EXTENSIONS or ext == "gif":
                    conv_gif = await convert_video_to_gif(fbytes)
                    if conv_gif:
                        fbytes = conv_gif
                        ext = "gif"

                if ext in VIDEO_EXTENSIONS and not conv_gif:
                    dfile = discord.File(fp=io.BytesIO(fbytes), filename=f"banner_{image_file.filename}")
                    files.append(dfile)
                else:
                    dfile = discord.File(fp=io.BytesIO(fbytes), filename=f"banner.{ext}")
                    files.append(dfile)
                    embed.set_image(url=f"attachment://banner.{ext}")
        except Exception as e:
            print(f"[BANNER ATTACHMENT ERROR] {e}")
            if image_file.url:
                res = await resolve_image_url(image_file.url)
                if res:
                    embed.set_image(url=res)
    elif image_url:
        res = await resolve_image_url(image_url)
        if res:
            embed.set_image(url=res)

    return files

# -------- Fetch Discord Server Invite Info -------- #
async def fetch_invite_info(invite_str: str) -> Optional[dict]:
    try:
        clean_code = invite_str.strip()
        if "discord.gg/" in clean_code:
            clean_code = clean_code.split("discord.gg/")[1]
        elif "discord.com/invite/" in clean_code:
            clean_code = clean_code.split("discord.com/invite/")[1]
        clean_code = clean_code.split("/")[0].split("?")[0].strip()

        if not clean_code:
            return None

        global session
        if session is None or session.closed:
            session = aiohttp.ClientSession()

        api_url = f"https://discord.com/api/v10/invites/{clean_code}?with_counts=true"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        async with session.get(api_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                guild = data.get("guild", {})
                guild_id = guild.get("id")

                icon_hash = guild.get("icon")
                icon_url = None
                if guild_id and icon_hash:
                    ext = "gif" if str(icon_hash).startswith("a_") else "png"
                    icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.{ext}?size=512"

                banner_hash = guild.get("banner") or guild.get("splash")
                banner_url = None
                if guild_id and banner_hash:
                    ext = "gif" if str(banner_hash).startswith("a_") else "png"
                    banner_url = f"https://cdn.discordapp.com/banners/{guild_id}/{banner_hash}.{ext}?size=1024"
                    if not guild.get("banner") and guild.get("splash"):
                        banner_url = f"https://cdn.discordapp.com/splashes/{guild_id}/{banner_hash}.png?size=1024"

                return {
                    "code": clean_code,
                    "invite_url": f"https://discord.gg/{clean_code}",
                    "guild_name": guild.get("name", "Discord Server"),
                    "guild_id": guild_id,
                    "description": guild.get("description"),
                    "icon_url": icon_url,
                    "banner_url": banner_url,
                    "member_count": data.get("approximate_member_count", 0),
                    "online_count": data.get("approximate_presence_count", 0)
                }
    except Exception as e:
        print(f"[INVITE FETCH ERROR] {e}")
    return None

# -------- Web Search Tool (Live Data Access) -------- #
async def fetch_realtime_context(query: str) -> str:
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()
    try:
        url = "https://lite.duckduckgo.com/lite/"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        body = f"q={urllib.parse.quote(query)}"
        async with session.post(url, data=body, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                html = await resp.text()
                soup = BeautifulSoup(html, "html.parser")
                snippets = []
                for el in soup.select(".result-snippet")[:3]:
                    txt = el.get_text().strip()
                    if txt:
                        snippets.append(txt)
                return " | ".join(snippets)
    except Exception as e:
        print(f"[SEARCH ERROR] {e}")
    return ""

# -------- Crypto Price System -------- #
async def get_bitget_price(query: str) -> Optional[str]:
    global session
    try:
        symbol = f"{query.upper()}USDT"
        url = f"https://api.bitget.com/api/v2/spot/market/tickers?symbol={symbol}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                tickers = data.get("data", [])
                if tickers:
                    t = tickers[0]
                    price = float(t.get("lastPr", 0))
                    price_str = f"{price:,.6f}" if price > 0.0001 else f"{price:.12f}"
                    change_val = float(t.get("change24h", 0)) * 100
                    change_str = f"{change_val:.2f}%"
                    vol_val = float(t.get("quoteVolume", 0))
                    vol_str = f"${round(vol_val):,}" if vol_val else "N/A"
                    return f"**{query.upper()} (Bitget)**\n💰 **Price:** ${price_str} USD\n📈 **24h Vol:** {vol_str}\n📅 **24h Change:** {change_str}"
    except Exception:
        pass
    return None

async def get_dex_screener_price(query: str) -> Optional[str]:
    global session
    try:
        url = f"https://api.dexscreener.com/latest/dex/search?q={urllib.parse.quote(query)}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pairs = data.get("pairs", [])
                if pairs:
                    pairs.sort(key=lambda p: (p.get("liquidity", {}) or {}).get("usd", 0), reverse=True)
                    pair = pairs[0]
                    token = pair.get("baseToken", {})
                    price = float(pair.get("priceUsd", 0))
                    price_str = f"{price:,.6f}" if price > 0.0001 else f"{price:.12f}"
                    change = f"{pair.get('priceChange', {}).get('h24')}%" if pair.get("priceChange", {}).get("h24") is not None else "N/A"
                    mcap_val = pair.get("marketCap")
                    mcap = f"${round(mcap_val):,}" if mcap_val else "N/A"
                    fdv_val = pair.get("fdv")
                    fdv = f"${round(fdv_val):,}" if fdv_val else "N/A"
                    chain = pair.get("chainId", "Unknown")
                    dex = pair.get("dexId", "DEX")
                    return f"**{token.get('name')} ({token.get('symbol', '').upper()})** on {chain}/{dex}\n💰 **Price:** ${price_str} USD\n📊 **Market Cap:** {mcap}\n📈 **FDV:** {fdv}\n📅 **24h Change:** {change}"
    except Exception:
        pass
    return None

async def get_gecko_terminal_price(query: str) -> Optional[str]:
    global session
    try:
        url = f"https://api.geckoterminal.com/api/v2/search/pools?query={urllib.parse.quote(query)}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                pools = data.get("data", [])
                if pools:
                    pool = pools[0]
                    attr = pool.get("attributes", {})
                    name = attr.get("name")
                    price = float(attr.get("base_token_price_usd", 0))
                    price_str = f"{price:,.6f}" if price > 0.0001 else f"{price:.12f}"
                    vol_val = (attr.get("volume_usd", {}) or {}).get("h24")
                    vol_str = f"${round(float(vol_val)):,}" if vol_val else "N/A"
                    fdv_val = attr.get("fdv_usd")
                    fdv_str = f"${round(float(fdv_val)):,}" if fdv_val else "N/A"
                    return f"**{name}** (GeckoTerminal)\n💰 **Price:** ${price_str} USD\n📈 **24h Vol:** {vol_str}\n📊 **FDV:** {fdv_str}"
    except Exception:
        pass
    return None

async def get_crypto_price(query: str) -> str:
    global session
    aliases = {"ai": "gensyn"}
    clean_q = aliases.get(query.lower(), query.lower())

    # 1. Try CoinGecko
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        search_url = f"https://api.coingecko.com/api/v3/search?query={urllib.parse.quote(clean_q)}"
        async with session.get(search_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                sdata = await resp.json()
                coins = sdata.get("coins", [])
                if coins:
                    exact = next((c for c in coins if c.get("symbol", "").lower() == clean_q or c.get("name", "").lower() == clean_q), coins[0])
                    coin_id = exact.get("id")
                    detail_url = f"https://api.coingecko.com/api/v3/coins/{coin_id}?localization=false&tickers=false&market_data=true"
                    async with session.get(detail_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as dresp:
                        if dresp.status == 200:
                            data = await dresp.json()
                            md = data.get("market_data", {})
                            cur_price = (md.get("current_price", {}) or {}).get("usd")
                            if cur_price is not None:
                                price_str = f"{cur_price:,.6f}" if cur_price > 0.0001 else f"{cur_price}"
                                change_val = md.get("price_change_percentage_24h")
                                change_str = f"{change_val:.2f}%" if change_val is not None else "N/A"
                                mcap_val = (md.get("market_cap", {}) or {}).get("usd")
                                mcap_str = f"${round(mcap_val):,}" if mcap_val else "N/A"
                                fdv_val = (md.get("fully_diluted_valuation", {}) or {}).get("usd")
                                fdv_str = f"${round(fdv_val):,}" if fdv_val else "N/A"
                                rank = data.get("market_cap_rank", "N/A")
                                return f"**{data.get('name')} ({data.get('symbol', '').upper()})** (Rank: {rank})\n💰 **Price:** ${price_str} USD\n📊 **Market Cap:** {mcap_str}\n📈 **FDV:** {fdv_str}\n📅 **24h Change:** {change_str}"
    except Exception:
        pass

    # 2. Bitget fallback
    bitget = await get_bitget_price(clean_q)
    if bitget:
        return bitget

    # 3. DexScreener fallback
    dex = await get_dex_screener_price(clean_q)
    if dex:
        return dex

    # 4. GeckoTerminal fallback
    gt = await get_gecko_terminal_price(clean_q)
    if gt:
        return gt

    return f"I couldn't find any data for `{query}`. 🥺"

# -------- Football Live Data & Standings -------- #
async def fetch_livescores() -> str:
    global session
    leagues = {
        'Premier League': 'eng.1',
        'La Liga': 'esp.1',
        'Bundesliga': 'ger.1',
        'Serie A': 'ita.1',
        'Ligue 1': 'fra.1',
        'UCL': 'uefa.champions'
    }
    all_matches = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    for name, code in leagues.items():
        try:
            url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{code}/scoreboard"
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    events = data.get("events", [])
                    for ev in events:
                        status_type = ev.get("status", {}).get("type", {})
                        state = status_type.get("state")
                        detail = status_type.get("detail") or status_type.get("description")
                        comps = ev.get("competitions", [{}])[0].get("competitors", [])
                        home = next((c for c in comps if c.get("homeAway") == "home"), {})
                        away = next((c for c in comps if c.get("homeAway") == "away"), {})
                        all_matches.append({
                            "league": name,
                            "home": home.get("team", {}).get("displayName") or home.get("team", {}).get("name") or "Home",
                            "away": away.get("team", {}).get("displayName") or away.get("team", {}).get("name") or "Away",
                            "homeScore": home.get("score", "0"),
                            "awayScore": away.get("score", "0"),
                            "state": state,
                            "detail": detail
                        })
        except Exception:
            pass

    if not all_matches:
        return "No major league matches scheduled for today! ⚽"

    # Order: live ('in'), upcoming ('pre'), finished ('post')
    order_map = {'in': 1, 'pre': 2, 'post': 3}
    all_matches.sort(key=lambda m: order_map.get(m['state'], 4))

    lines = []
    for m in all_matches[:15]:
        if m['state'] == 'in':
            lines.append(f"🔴 LIVE: {m['home']} {m['homeScore']}-{m['awayScore']} {m['away']} ({m['league']}) — {m['detail']}")
        elif m['state'] == 'pre':
            lines.append(f"⏰ {m['home']} vs {m['away']} ({m['league']}) — {m['detail']}")
        else:
            lines.append(f"✅ {m['home']} {m['homeScore']}-{m['awayScore']} {m['away']} ({m['league']}) [FT]")

    return f"**⚽ Major League Matches & Scores (Top 15):**\n" + "\n".join(lines)

async def fetch_standings(league_input: str) -> str:
    global session
    standings_map = {
        'pl': 'eng.1', 'premierleague': 'eng.1', 'epl': 'eng.1',
        'laliga': 'esp.1', 'liga': 'esp.1',
        'bundesliga': 'ger.1', 'bl': 'ger.1',
        'seriea': 'ita.1', 'seria': 'ita.1',
        'ligue1': 'fra.1', 'ligue': 'fra.1',
        'ucl': 'uefa.champions', 'championsleague': 'uefa.champions', 'cl': 'uefa.champions'
    }
    code = standings_map.get(league_input.lower())
    if not code:
        return "I don't recognize that league! Try: **#plstandings**, **#laligastandings**, **#bundesligastandings**, **#serieastandings**, **#ligue1standings**, **#uclstandings** ⚽"

    try:
        url = f"https://site.api.espn.com/apis/v2/sports/soccer/{code}/standings"
        headers = {'User-Agent': 'Mozilla/5.0'}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                children = data.get("children", [])
                if children:
                    entries = children[0].get("standings", {}).get("entries", [])
                    comp_name = data.get("name", "League")
                    lines = []
                    for i, t in enumerate(entries[:20]):
                        stats = t.get("stats", [])
                        p = next((s.get("value") for s in stats if s.get("name") == "points"), 0)
                        w = next((s.get("value") for s in stats if s.get("name") == "wins"), 0)
                        d = next((s.get("value") for s in stats if s.get("name") == "ties"), 0)
                        l = next((s.get("value") for s in stats if s.get("name") == "losses"), 0)
                        gd = next((s.get("value") for s in stats if s.get("name") == "pointDifferential"), 0)
                        gd_str = f"+{gd}" if gd > 0 else f"{gd}"
                        team_name = t.get("team", {}).get("displayName") or t.get("team", {}).get("name")
                        lines.append(f"**{i+1}.** {team_name} — {int(p)}pts (W{int(w)} D{int(d)} L{int(l)} | GD: {gd_str})")
                    return f"**🏆 {comp_name} Standings:**\n" + "\n".join(lines)
    except Exception as e:
        print(f"[STANDINGS ERROR] {e}")
    return "Could not fetch standings for that league right now! 🙏"

# -------- TLDR Summarizer -------- #
async def tldr_summary(message: discord.Message) -> str:
    global session
    try:
        history_msgs = []
        async for msg in message.channel.history(limit=50, before=message):
            if msg.content and not msg.author.bot:
                history_msgs.append(f"[{msg.author.display_name}]: {msg.content}")
        history_msgs.reverse()

        chat_context = "\n".join(history_msgs)
        if len(chat_context) > 8000:
            chat_context = chat_context[-8000:]

        if not chat_context.strip():
            return "There isn't any recent drama to summarize! It's been a ghost town in here."

        system_prompt = (
            'You are "Arcie", a sassy, clever, and highly observant girl chatting in a Discord server. '
            'Read the provided Discord chat logs of the last 50 messages and create a concise, highly entertaining summary. '
            'Call out specific people by name if they said something funny or ridiculous. Keep your classic sweet/sassy attitude!'
        )

        payload = {
            "model": "llama-3.1-8b-instant",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Hey! I just got here. What did I miss? Here is the raw chat log:\n\n{chat_context}\n\nPlease summarize!"}
            ],
            "max_tokens": 400,
            "temperature": 0.70
        }

        keys = [k for k in [os.getenv("GROQ_API_KEY"), os.getenv("GROQ_FALLBACK_API_KEY")] if k]
        for key in keys:
            headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
            async with session.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    rdata = await resp.json()
                    return rdata["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"[TLDR ERROR] {e}")
    return "I tried to read the chat history but my brain fried reading all that nonsense... 🥺"

# -------- Auto GIF System (Tenor API Native File Attachment) -------- #
async def fetch_auto_gif_file() -> Optional[discord.File]:
    global session
    try:
        cute_keywords = ["cute anime girl", "kawaii anime girl", "happy anime girl", "cute girly reaction", "anime giggle", "cute anime wave", "savage anime girl"]
        keyword = random.choice(cute_keywords)
        url = f"https://g.tenor.com/v1/search?q={urllib.parse.quote(keyword)}&key=LIVDSRZULELA&limit=10"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                results = data.get("results", [])
                if results:
                    choice = random.choice(results)
                    gif_url = None
                    media = choice.get("media", [])
                    if media:
                        gif_url = (media[0].get("gif", {}) or media[0].get("mediumgif", {})).get("url")
                    if not gif_url:
                        gif_url = choice.get("url")

                    if gif_url:
                        async with session.get(gif_url, timeout=aiohttp.ClientTimeout(total=6)) as gresp:
                            if gresp.status == 200:
                                gbytes = await gresp.read()
                                return discord.File(fp=io.BytesIO(gbytes), filename="cute_reaction.gif")
    except Exception as e:
        print(f"[AUTO-GIF ERROR] {e}")
    return None

# -------- TikTok Cute Voice Note TTS -------- #
async def fetch_tiktok_tts(text: str) -> Optional[discord.File]:
    global session
    try:
        clean_text = re.sub(r'<@!?([0-9]+)>', '', text)
        clean_text = re.sub(r'[*_~`>|]', '', clean_text).strip()
        if not clean_text:
            clean_text = text

        if len(clean_text) > 190:
            clean_text = clean_text[:187] + "..."

        url = "https://tiktok-tts.weilnet.workers.dev/api/generation"
        payload = {"text": clean_text, "voice": "en_us_002"}
        headers = {"Content-Type": "application/json"}

        async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                b64_str = data.get("data")
                if b64_str:
                    audio_bytes = base64.b64decode(b64_str)
                    return discord.File(fp=io.BytesIO(audio_bytes), filename="cute-voice-note.mp3")
    except Exception as e:
        print(f"[TIKTOK TTS ERROR] {e}")

    # Fallback to Google TTS
    try:
        clean_q = urllib.parse.quote(clean_text[:180])
        g_url = f"https://translate.google.com/translate_tts?ie=UTF-8&q={clean_q}&tl=en&client=tw-ob"
        async with session.get(g_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=aiohttp.ClientTimeout(total=5)) as gresp:
            if gresp.status == 200:
                gbytes = await gresp.read()
                return discord.File(fp=io.BytesIO(gbytes), filename="google-voice-note.mp3")
    except Exception as ge:
        print(f"[GOOGLE TTS ERROR] {ge}")
    return None

# -------- Emoji & GIF Context Parser for AI -------- #
def parse_message_media_and_emojis(message: discord.Message) -> str:
    raw_text = message.content or ""

    # Resolve User Mentions (<@ID> or <@!ID>) into readable @Display_Name
    if message.guild and re.search(r'<@!?\d+>', raw_text):
        def replace_user_mention(m_match):
            uid_str = re.sub(r'[<@!>]', '', m_match.group(0))
            try:
                m_obj = message.guild.get_member(int(uid_str))
                if m_obj:
                    return f"@{m_obj.display_name}"
            except Exception:
                pass
            return m_match.group(0)
        raw_text = re.sub(r'<@!?\d+>', replace_user_mention, raw_text)

    # Parse Custom Emojis (<:name:id> or <a:name:id>)
    def replace_custom_emoji(match):
        animated = match.group(1)
        name = match.group(2)
        kind = "Animated Emoji" if animated else "Emoji"
        clean_name = name.replace("_", " ")
        return f"[{kind}: :{clean_name}:]"

    parsed_text = re.sub(r'<(a)?:([a-zA-Z0-9_]+):[0-9]+>', replace_custom_emoji, raw_text)

    # Parse GIF Links (Tenor, Klipy, Giphy, Imgur, direct .gif)
    urls = re.findall(r'https?://[^\s]+', raw_text)
    extra_context = []

    for url in urls:
        url_lower = url.lower()
        if "tenor.com/view/" in url_lower:
            slug = url_lower.split("tenor.com/view/")[1].split("?")[0]
            slug_clean = re.sub(r'-[0-9]+$', '', slug).replace("-", " ")
            extra_context.append(f'[Sent a Tenor GIF showing: "{slug_clean}"]')
        elif "klipy." in url_lower:
            slug = url_lower.split("/")[-1].split("?")[0]
            slug_clean = re.sub(r'[^a-zA-Z0-9]', ' ', slug).strip()
            extra_context.append(f'[Sent a Klipy GIF showing: "{slug_clean}"]')
        elif "giphy.com" in url_lower:
            slug = url_lower.split("/")[-1].split("?")[0]
            slug_clean = re.sub(r'[^a-zA-Z0-9]', ' ', slug).strip()
            extra_context.append(f'[Sent a Giphy GIF showing: "{slug_clean}"]')
        elif url_lower.endswith(".gif"):
            filename = url.split("/")[-1].split("?")[0].replace(".gif", "").replace("_", " ").replace("-", " ")
            extra_context.append(f'[Sent a GIF: "{filename}"]')

    # Parse Attachments
    if message.attachments:
        for att in message.attachments:
            if att.filename.lower().endswith(".gif"):
                clean_fn = att.filename.replace(".gif", "").replace("_", " ").replace("-", " ")
                extra_context.append(f'[Attached a GIF file: "{clean_fn}"]')
            elif att.content_type and att.content_type.startswith("image/"):
                extra_context.append(f'[Attached an image file: "{att.filename}"]')

    if extra_context:
        parsed_text = (parsed_text + " " + " ".join(extra_context)).strip()

    return parsed_text if parsed_text else "[Sent media/attachment]"

# -------- AI Reply Logic with Memory & Accurate Tag Resolution -------- #
async def ai_reply(message: discord.Message) -> str:
    global session
    channel_id = str(message.channel.id)
    if channel_id not in channel_memories:
        channel_memories[channel_id] = []

    history = channel_memories[channel_id]

    # Update long-term user profile for message author
    update_user_memory(message.author, message.content)

    parsed_user_content = parse_message_media_and_emojis(message)
    timestamp_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    user_content = f"[{timestamp_str}] [User: {message.author.display_name} | @{message.author.name} | ID: {message.author.id}]: {parsed_user_content}"
    history.append({"role": "user", "content": user_content})

    if len(history) > 120:
        history = history[-120:]
        channel_memories[channel_id] = history
    save_memories()

    content_lower = message.content.lower()

    # 1. Build Full Server Member Directory (Display Names & Usernames)
    member_directory_lines = []
    if message.guild:
        for m in message.guild.members:
            if not m.bot:
                update_user_memory(m, "")
                member_directory_lines.append(f'• "{m.display_name}" (@{m.name})')

    member_directory_str = ""
    if member_directory_lines:
        member_directory_str = (
            f"\n\n🏷️ SERVER MEMBERS LIST:\n" +
            "\n".join(member_directory_lines[:150])
        )

    # 2. Inject Persistent User Memory / Profile Facts for ALL Known Members
    user_facts_lines = []
    for uid, profile in user_profiles.items():
        if profile.get("facts"):
            dname = profile.get("display_name", uid)
            facts_joined = "; ".join(profile["facts"])
            user_facts_lines.append(f'• {dname} (@{profile.get("username", "")}): {facts_joined}')

    user_facts_str = ""
    if user_facts_lines:
        user_facts_str = f"\n\n🧠 PERSISTENT MEMORY & MEMBER KNOWLEDGE (ULTRA-SHARP MEMORY OF ALL MEMBERS):\n" + "\n".join(user_facts_lines[:50])

    # 3. Deep Historical Search (for questions about past days/weeks/events)
    historical_search_str = ""
    if re.search(r'\b(days?\s*ago|days?\s*before|last\s*week|past\s*days?|remember|happened|what\s*did|who\s*said|recap|back\s*then|history|ago|yesterday|earlier|before|when\s*did)\b', content_lower):
        try:
            matched_past_msgs = []
            async for old_m in message.channel.history(limit=300, before=message):
                if old_m.content and not old_m.author.bot:
                    m_time = old_m.created_at.strftime("%Y-%m-%d %H:%M")
                    parsed_old = parse_message_media_and_emojis(old_m)
                    matched_past_msgs.append(f"[{m_time}] [{old_m.author.display_name} (@{old_m.author.name})]: {parsed_old}")
            matched_past_msgs.reverse()
            if matched_past_msgs:
                historical_search_str = f"\n\n📅 HISTORICAL CHANNEL CHAT LOGS (Retrieved from history for your memory recall):\n" + "\n".join(matched_past_msgs[-60:])
        except Exception as e:
            print(f"[HISTORICAL SEARCH ERROR] {e}")

    # 4. Server Emojis Injection
    server_emojis = ""
    if message.guild and message.guild.emojis:
        import random
        available = list(message.guild.emojis)
        random.shuffle(available)
        emotes = [f"<{'a' if e.animated else ''}:{e.name}:{e.id}>" for e in available[:10]]
        if emotes:
            server_emojis = f"\n\nSERVER EMOJIS (Feel free to use these naturally!): {' '.join(emotes)}"

    # 5. Live Web Context
    live_web_context = ""
    if re.search(r'(2024|2025|2026|latest|recent|now|today|news|weather|football|soccer|transfers?|matches?|standings?|league|cl|ucl|pl|premier league|laliga|bundesliga|serie a|ligue 1|scores?|arsenal|chelsea|liverpool|manchester|real madrid|barcelona|bayern|psg|haaland|mbappe|messi|ronaldo)', content_lower) and len(content_lower.split()) > 2:
        clean_q = re.sub(r'arcie|diffy|unemployed girl|unemployed', '', message.content, flags=re.IGNORECASE).strip()
        searchResults = await fetch_realtime_context(clean_q)
        if searchResults:
            live_web_context = f"\n\nLIVE INTERNET SEARCH RESULTS: {searchResults}"

    # 6. Background Channel Context
    channel_context = ""
    try:
        recent_msgs = []
        async for m in message.channel.history(limit=25, before=message):
            parsed_m = parse_message_media_and_emojis(m)
            if parsed_m:
                m_time = m.created_at.strftime("%H:%M")
                name = "Arcie (You)" if m.author.id == bot.user.id else f"{m.author.display_name} (@{m.author.name} | ID: {m.author.id})"
                recent_msgs.append(f"[{m_time}] [{name}]: {parsed_m}")
        recent_msgs.reverse()
        if recent_msgs:
            channel_context = f"\n\nBACKGROUND CHANNEL CHAT LOG (Recent messages in this channel right before current message):\n" + "\n".join(recent_msgs)
    except Exception:
        pass

    system_prompt = (
        f'You are "Arcie", a 100% real human girl chatting in a Discord server. YOU ARE A REAL GIRL. DO NOT ACT LIKE A BOT OR AN AI ASSISTANT.\n\n'
        f'🚨 100% REAL HUMAN CHAT STYLE (CRITICAL):\n'
        f'1. Speak completely naturally with real human warmth, casual tone, friendly vibe, and genuine emotion! (e.g. "Hey! What\'s up?", "Oh haha true!", "Hey there!").\n'
        f'2. NEVER EVER output raw code strings like "<@123456789>" or "<@id>"! Always refer to people naturally by their Display Name.\n'
        f'3. NEVER invent fake names (like James) if they do not exist in the message or server! Only refer to real members listed in the SERVER MEMBERS LIST below.\n'
        f'4. Keep responses casual, short, and sweet — 1 to 2 lines like a real human typing on Discord!\n\n'
        f'💬 CASUAL CHATTING & MENTIONS:\n'
        f'1. When people say hi or chat with you, reply warmly and naturally as a close friend.\n'
        f'2. When asked to talk to or mention someone, look up their actual Display Name from the user message or SERVER MEMBERS LIST and address them naturally by their real Display Name!\n\n'
        f'🧠 ULTRA-SHARP MEMORY & RECALL:\n'
        f'1. Remember member names, facts, nicknames, preferences, and past events across days and weeks.\n'
        f'2. If anyone asks about past events or previous conversations, recall the details naturally as a real friend.\n\n'
        f'### CRITICAL NAME RULES:\n'
        f'1. Your name is "Arcie". If anyone asks your name or who you are, reply naturally: "I\'m Arcie!" or "My name\'s Arcie!".\n'
        f'2. NEVER prefix your replies with "Arcie:". Just output your chat message directly.\n\n'
        f'### CURRENT CONVERSATION TARGET:\n'
        f'• CURRENT SENDER: **{message.author.display_name}** (@{message.author.name}). Respond directly to **{message.author.display_name}**!'
        f'{user_facts_str}{member_directory_str}{historical_search_str}{server_emojis}{live_web_context}{channel_context}'
    )

    formatted_messages = [{"role": "system", "content": system_prompt}]
    formatted_messages.extend(history)

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": formatted_messages,
        "max_tokens": 400,
        "temperature": 0.80
    }

    keys = [k for k in [os.getenv("GROQ_API_KEY"), os.getenv("GROQ_FALLBACK_API_KEY")] if k]
    for key in keys:
        for attempt in range(1, 4):
            try:
                headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
                async with session.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        rdata = await resp.json()
                        reply_text = rdata["choices"][0]["message"]["content"]
                        reply_text = re.sub(r'^(Arcie|Diffy|Unemployed\s*Girl|Unemployed|Bot|\[.*?\]):\s*', '', reply_text, flags=re.IGNORECASE).strip()
                        history.append({"role": "assistant", "content": reply_text})
                        channel_memories[channel_id] = history
                        save_memories()
                        return reply_text
                    elif resp.status == 429:
                        await asyncio.sleep(2 * attempt)
                    else:
                        break
            except Exception as e:
                print(f"[GROQ ERROR] {e}")
                await asyncio.sleep(1)

    # Smart conversational fallback if Groq API key is missing or encounters network errors
    lower_text = message.content.lower().strip()
    author_name = message.author.display_name

    if any(greeting in lower_text for greeting in ["hi", "hello", "hey", "sup", "yo", "gm", "gn"]):
        fallback_replies = [
            f"Hey {author_name}! 👋 How's your day going?",
            f"Hello {author_name}! ✨ Happy to see you around here!",
            f"Hey there {author_name}! What's on your mind today? 😊",
            f"Yo {author_name}! Ready for some giveaways and alpha calls today? 🚀"
        ]
    elif any(q in lower_text for q in ["who are you", "what is your name", "your name"]):
        fallback_replies = [
            f"I'm **Arcie**! 💖 Your real Web3 community assistant & giveaway host!",
            f"Hey! My name is **Arcie**! ✨ Here to help with giveaways, alpha, and chat!"
        ]
    elif any(q in lower_text for q in ["how are you", "how r u", "how u doing"]):
        fallback_replies = [
            f"I'm feeling great today, {author_name}! Thanks for asking! ❤️ How about you?",
            f"Super good! Ready to dish out more giveaway spots to everyone! 🎉"
        ]
    else:
        fallback_replies = [
            f"Hey {author_name}! I hear you! ✨ Always around if you need giveaway info or help!",
            f"That's interesting, {author_name}! Tell me more! 😊",
            f"Got it, {author_name}! Let's keep the vibe going in the server! 🚀"
        ]

    fallback_text = random.choice(fallback_replies)
    history.append({"role": "assistant", "content": fallback_text})
    channel_memories[channel_id] = history
    save_memories()
    return fallback_text

# -------- Multi-Theme Anime Welcome Card Generator -------- #
DEFAULT_WELCOME_CHANNEL_ID = 1530133805351047261
welcome_channel_config: Dict[str, int] = {}

WELCOME_THEMES = [
    {
        "name": "Dark Samurai Crimson",
        "bg1": (22, 6, 12, 255),
        "bg2": (55, 14, 22, 255),
        "card_fill": (25, 10, 15, 245),
        "border": (255, 60, 80, 255),
        "ring": (255, 140, 0, 255),
        "title_color": (255, 180, 0, 255),
        "sub_color": (255, 100, 120, 255),
        "slash_color": (255, 50, 80, 100)
    },
    {
        "name": "Electric Cyberpunk",
        "bg1": (8, 12, 24, 255),
        "bg2": (18, 28, 55, 255),
        "card_fill": (12, 16, 32, 245),
        "border": (0, 245, 255, 255),
        "ring": (0, 245, 255, 255),
        "title_color": (0, 245, 255, 255),
        "sub_color": (180, 100, 255, 255),
        "slash_color": (0, 200, 255, 100)
    },
    {
        "name": "Sunset Katana Violet",
        "bg1": (20, 8, 30, 255),
        "bg2": (60, 20, 70, 255),
        "card_fill": (24, 12, 36, 245),
        "border": (255, 180, 0, 255),
        "ring": (255, 180, 0, 255),
        "title_color": (255, 215, 0, 255),
        "sub_color": (255, 120, 180, 255),
        "slash_color": (255, 150, 0, 100)
    },
    {
        "name": "Mystic Indigo Starry",
        "bg1": (10, 14, 32, 255),
        "bg2": (24, 38, 75, 255),
        "card_fill": (14, 20, 42, 245),
        "border": (100, 200, 255, 255),
        "ring": (120, 220, 255, 255),
        "title_color": (120, 220, 255, 255),
        "sub_color": (160, 180, 255, 255),
        "slash_color": (100, 180, 255, 100)
    }
]

def load_system_bold_font(size: int):
    possible_fonts = ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "Ubuntu-B.ttf", "segoeui.ttf", "arial.ttf"]
    for font_name in possible_fonts:
        try:
            return ImageFont.truetype(font_name, size)
        except Exception:
            pass
    return ImageFont.load_default()

def sanitize_display_name(name: str) -> str:
    if not name:
        return "Member"
    s_name = str(name).strip()
    
    # NFKD Normalization converts fancy unicode fonts into standard clean Latin letters
    normalized = unicodedata.normalize('NFKD', s_name)
    latin_str = ''.join(c for c in normalized if not unicodedata.combining(c))
    
    # Extract clean printable ASCII / Latin characters
    clean_ascii = ''.join(c for c in latin_str if 32 <= ord(c) <= 126).strip()
    if clean_ascii:
        return clean_ascii
        
    printable_only = ''.join(c for c in s_name if ord(c) < 256 and c.isprintable()).strip()
    if printable_only:
        return printable_only
        
    return "Member"

def get_autoscaled_font(text: str, max_width: int = 550, max_size: int = 52, min_size: int = 18):
    for size in range(max_size, min_size - 1, -2):
        font = load_system_bold_font(size)
        bbox = font.getbbox(text)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            return font
    return load_system_bold_font(min_size)

async def create_welcome_card(
    avatar_url: Optional[str] = None,
    avatar_bytes: Optional[bytes] = None,
    member_name: str = "Member",
    username: Optional[str] = None,
    member_count: Union[int, str] = 1,
    server_name: Optional[str] = None
) -> io.BytesIO:
    raw_name = username or member_name or "Member"
    display_name = sanitize_display_name(raw_name)
    srv_name = server_name or os.getenv("SERVER_NAME", "Differential Degens")
    
    if isinstance(member_count, str):
        count_str = member_count
    else:
        count_str = f"Member #{member_count:,}"

    if not avatar_bytes and avatar_url:
        try:
            global session
            if session is None or session.closed:
                session = aiohttp.ClientSession()
            headers = {"User-Agent": "Mozilla/5.0"}
            async with session.get(avatar_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    avatar_bytes = await resp.read()
        except Exception as e:
            print(f"[WELCOME AVATAR FETCH ERROR] {e}")

    template_path = os.path.join(DATA_DIR, "temp.png")
    if not os.path.exists(template_path):
        template_path = os.path.join(DATA_DIR, "template.png")
    if not os.path.exists(template_path):
        template_path = os.path.join(DATA_DIR, "temp_cutout.png")
    
    base_img = Image.open(template_path).convert('RGBA')
    w, h = base_img.size
    canvas = base_img.copy()
    
    # Scale coordinates based on canvas resolution (supports 2800x983 & 1732x608)
    if w > 2000:
        cx, cy = 486, 499
        av_size = 645
        text_center_x = 1800
        size_welcome, size_name, size_server, size_member = 85, 190, 130, 85
        y_welcome, y_name, y_server, y_member = 120, 230, 470, 650
        ring_width = 6
    else:
        cx, cy = 300, 304
        av_size = 405
        text_center_x = 1100
        size_welcome, size_name, size_server, size_member = 52, 125, 82, 50
        y_welcome, y_name, y_server, y_member = 65, 140, 300, 425
        ring_width = 4
    
    if avatar_bytes:
        try:
            av_img = Image.open(io.BytesIO(avatar_bytes)).convert('RGBA')
            # Centered Cover Crop to square before circular mask
            raw_w, raw_h = av_img.size
            min_dim = min(raw_w, raw_h)
            crop_x = (raw_w - min_dim) // 2
            crop_y = (raw_h - min_dim) // 2
            square_av = av_img.crop((crop_x, crop_y, crop_x + min_dim, crop_y + min_dim))
            av_img = square_av.resize((av_size, av_size), Image.Resampling.LANCZOS)
        except Exception:
            av_img = Image.new('RGBA', (av_size, av_size), (0, 168, 252, 255))
    else:
        av_img = Image.new('RGBA', (av_size, av_size), (0, 168, 252, 255))
        
    mask = Image.new('L', (av_size, av_size), 0)
    draw_mask = ImageDraw.Draw(mask)
    draw_mask.ellipse((0, 0, av_size, av_size), fill=255)
    
    avatar_x = int(cx - av_size / 2)
    avatar_y = int(cy - av_size / 2)
    canvas.paste(av_img, (avatar_x, avatar_y), mask)
    
    draw = ImageDraw.Draw(canvas)
    neon_green = (57, 255, 20, 255)
    pure_white = (255, 255, 255, 255)

    draw.ellipse((avatar_x - 3, avatar_y - 3, avatar_x + av_size + 3, avatar_y + av_size + 3), outline=neon_green, width=ring_width)
    
    clean_display = display_name if len(display_name) <= 22 else display_name[:20] + "..."
    srv_upper = srv_name.upper()
    if len(srv_upper) > 24:
        srv_upper = srv_upper[:22] + "..."

    max_text_w = 1100 if w > 2000 else 660

    font_welcome = load_system_bold_font(size_welcome)
    font_name = get_autoscaled_font(clean_display, max_width=max_text_w, max_size=size_name, min_size=60 if w > 2000 else 36)
    font_server = get_autoscaled_font(f"TO {srv_upper}", max_width=max_text_w, max_size=size_server, min_size=40 if w > 2000 else 24)
    font_member = load_system_bold_font(size_member)

    # 1. WELCOME Accent Header (Centrally Aligned)
    try:
        w_bbox = font_welcome.getbbox("WELCOME")
        w_width = w_bbox[2] - w_bbox[0]
    except Exception:
        w_width = 200
    draw.text((text_center_x - w_width // 2, y_welcome), "WELCOME", font=font_welcome, fill=neon_green)

    # 2. Member Display Name (Centrally Aligned, ~2x Larger)
    try:
        n_bbox = font_name.getbbox(clean_display)
        n_width = n_bbox[2] - n_bbox[0]
    except Exception:
        n_width = 300
    draw.text((text_center_x - n_width // 2, y_name), clean_display, font=font_name, fill=pure_white)

    # 3. "TO SERVER_NAME" (Centrally Aligned, "to " in Pure White, Server Name in Neon Green)
    try:
        to_bbox = font_server.getbbox("to ")
        to_w = to_bbox[2] - to_bbox[0]
    except Exception:
        to_w = 40
    try:
        srv_bbox = font_server.getbbox(srv_upper)
        srv_w = srv_bbox[2] - srv_bbox[0]
    except Exception:
        srv_w = 200

    total_srv_w = to_w + srv_w
    start_srv_x = text_center_x - total_srv_w // 2

    draw.text((start_srv_x, y_server), "to ", font=font_server, fill=pure_white)
    draw.text((start_srv_x + to_w, y_server), srv_upper, font=font_server, fill=neon_green)

    # 4. Member Count / Rank (Centrally Aligned)
    try:
        m_bbox = font_member.getbbox(count_str)
        m_width = m_bbox[2] - m_bbox[0]
    except Exception:
        m_width = 200
    draw.text((text_center_x - m_width // 2, y_member), count_str, font=font_member, fill=neon_green)
    
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    buf.seek(0)
    return buf

# -------- Reaction Role UI Component -------- #
# -------- Reaction Role UI Component -------- #
class ReactionRoleButton(discord.ui.Button):
    def __init__(self, role_id: int, emoji: Optional[str] = None, label: Optional[str] = None):
        clean_emoji = emoji.strip() if emoji and emoji.strip() and emoji.strip() != "✔️" else None
        super().__init__(style=discord.ButtonStyle.primary, label=label or "Get Role", emoji=clean_emoji, custom_id=f"rr_{role_id}")
        self.role_id = int(role_id)

    async def callback(self, interaction: discord.Interaction):
        try:
            if not interaction.response.is_done():
                await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        guild = interaction.guild
        if not guild:
            await interaction.followup.send("This action can only be used in a server!", ephemeral=True)
            return

        role = guild.get_role(self.role_id)
        if not role:
            await interaction.followup.send("❌ That role no longer exists in this server! 🥺", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) and guild:
            member = guild.get_member(interaction.user.id)
            if not member:
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except Exception:
                    pass

        if not member:
            await interaction.followup.send("❌ Could not fetch member details! 🥺", ephemeral=True)
            return

        # Check Discord Role Hierarchy
        if role >= guild.me.top_role:
            await interaction.followup.send(
                f"❌ **Role Hierarchy Error!** The role **{role.name}** is positioned HIGHER than (or equal to) my bot role in Server Settings.\n\n"
                f"👉 **Fix:** Open **Server Settings ➔ Roles**, and drag the bot role (**Arcie**) ABOVE **{role.name}**!",
                ephemeral=True
            )
            return

        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Reaction Role button toggle")
                await interaction.followup.send(f"❌ Removed the **{role.name}** role from you!", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send(f"❌ **Permission Denied!** Make sure my **Arcie** role has **Manage Roles** permission and is placed higher than **{role.name}** in Server Settings!", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Could not remove role: {e}", ephemeral=True)
        else:
            try:
                await member.add_roles(role, reason="Reaction Role button toggle")
                await interaction.followup.send(f"✅ Granted you the **{role.name}** role!", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send(f"❌ **Permission Denied!** Make sure my **Arcie** role has **Manage Roles** permission and is placed higher than **{role.name}** in Server Settings!", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"Could not give role: {e}", ephemeral=True)


@bot.listen("on_interaction")
async def handle_component_interactions(interaction: discord.Interaction):
    if interaction.type == discord.InteractionType.component:
        custom_id = interaction.data.get("custom_id", "")

        # ---- Reaction Role buttons (rr_<role_id>) ----
        if custom_id and custom_id.startswith("rr_"):
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer(ephemeral=True)
            except Exception:
                pass

            try:
                role_id_str = custom_id.replace("rr_", "").strip()
                if not role_id_str.isdigit():
                    await interaction.followup.send("❌ Invalid role button configuration.", ephemeral=True)
                    return

                role_id = int(role_id_str)
                guild = interaction.guild
                if not guild:
                    await interaction.followup.send("This action can only be used in a server!", ephemeral=True)
                    return

                role = guild.get_role(role_id)
                if not role:
                    await interaction.followup.send("❌ That role no longer exists in this server! 🥺", ephemeral=True)
                    return

                member = interaction.user
                if not isinstance(member, discord.Member) and guild:
                    member = guild.get_member(interaction.user.id)
                    if not member:
                        try:
                            member = await guild.fetch_member(interaction.user.id)
                        except Exception:
                            pass

                if not member:
                    await interaction.followup.send("❌ Could not fetch your server member profile! 🥺", ephemeral=True)
                    return

                # Check Role Hierarchy
                if role >= guild.me.top_role:
                    await interaction.followup.send(
                        f"❌ **Role Hierarchy Error!** The role **{role.name}** is positioned HIGHER than (or equal to) my bot role (**Arcie**) in Discord!\n\n"
                        f"👉 **Fix:** Open **Server Settings ➔ Roles**, and drag the bot role (**Arcie**) ABOVE **{role.name}**!",
                        ephemeral=True
                    )
                    return

                if role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Reaction Role button toggle")
                        await interaction.followup.send(f"❌ Removed the **{role.name}** role from you!", ephemeral=True)
                    except discord.Forbidden:
                        await interaction.followup.send(f"❌ **Permission Denied!** Make sure my **Arcie** role has **Manage Roles** permission and is placed higher than **{role.name}** in Server Settings!", ephemeral=True)
                    except Exception as e:
                        await interaction.followup.send(f"Could not remove role: {e}", ephemeral=True)
                else:
                    try:
                        await member.add_roles(role, reason="Reaction Role button toggle")
                        await interaction.followup.send(f"✅ Granted you the **{role.name}** role!", ephemeral=True)
                    except discord.Forbidden:
                        await interaction.followup.send(f"❌ **Permission Denied!** Make sure my **Arcie** role has **Manage Roles** permission and is placed higher than **{role.name}** in Server Settings!", ephemeral=True)
                    except Exception as e:
                        await interaction.followup.send(f"Could not give role: {e}", ephemeral=True)
                return
            except Exception as e:
                print(f"[REACTION ROLE INTERACTION ERROR] {e}")
                try:
                    await interaction.followup.send(f"❌ An error occurred processing this role button: {e}", ephemeral=True)
                except Exception:
                    pass
                return

        # ---- Fallback handling for Giveaway buttons (join_giveaway_, view_entry_, gtask_) ----
        # Only fires if the persistent GiveawayView callback did NOT already handle it.
        if custom_id and (custom_id.startswith("join_giveaway_") or custom_id.startswith("view_entry_") or custom_id.startswith("gtask_")):
            # Wait for persistent view callback to execute first (it's dispatched in parallel)
            await asyncio.sleep(1.5)
            # If the persistent view already responded, do nothing — avoid duplicate messages
            if interaction.response.is_done():
                return
            try:
                if custom_id.startswith("join_giveaway_"):
                    g_id = custom_id.replace("join_giveaway_", "")
                    port = os.getenv("PORT", "2025")
                    web_url = os.getenv("APP_URL", f"http://n5.nexcloud.in:{port}")
                    v = GiveawayView(g_id, web_url)
                    await v.join_giveaway_callback(interaction)
                elif custom_id.startswith("view_entry_"):
                    g_id = custom_id.replace("view_entry_", "")
                    port = os.getenv("PORT", "2025")
                    web_url = os.getenv("APP_URL", f"http://n5.nexcloud.in:{port}")
                    v = GiveawayView(g_id, web_url)
                    await v.view_entry_callback(interaction)
                elif custom_id.startswith("gtask_"):
                    parts = custom_id.replace("gtask_", "").rsplit("_", 1)
                    if len(parts) == 2:
                        task_type, g_id = parts[0], parts[1]
                        uid = str(interaction.user.id)
                        if g_id not in giveaway_task_progress:
                            giveaway_task_progress[g_id] = {}
                        if uid not in giveaway_task_progress[g_id]:
                            giveaway_task_progress[g_id][uid] = set()
                        giveaway_task_progress[g_id][uid].add(task_type)
                        g_obj = giveaways.get(g_id)
                        req_tasks = get_required_task_list(g_obj) if g_obj else []
                        lbl = "Task"
                        url = ""
                        for t_type, t_lbl, t_url in req_tasks:
                            if t_type == task_type:
                                lbl, url = t_lbl, t_url
                                break
                        if url:
                            await safe_respond(interaction, f"✅ **{lbl}** — marked as done!\n\n👉 **Complete the task here:** {url}", ephemeral=True)
                        else:
                            await safe_respond(interaction, f"✅ **{lbl}** — marked as done!", ephemeral=True)
            except Exception as g_err:
                print(f"[GIVEAWAY FALLBACK INTERACTION ERROR] {g_err}")
                if not interaction.response.is_done():
                    await safe_respond(interaction, f"❌ Error processing interaction: {g_err}", ephemeral=True)

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    msg_id = str(payload.message_id)
    if msg_id in reaction_roles:
        data = reaction_roles[msg_id]
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            try:
                guild = await bot.fetch_guild(payload.guild_id)
            except Exception:
                return
        if not guild: return

        member = payload.member or guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return
        if not member: return

        roles_to_add = []
        if "roles" in data and isinstance(data["roles"], list):
            payload_emoji = str(payload.emoji.name) if payload.emoji.is_unicode_emoji() else str(payload.emoji)
            for r_item in data["roles"]:
                target_emoji = str(r_item.get("emoji", "")).strip()
                if not target_emoji or target_emoji == payload_emoji or target_emoji in str(payload.emoji):
                    roles_to_add.append(r_item.get("role_id"))
        elif data.get("role_id"):
            roles_to_add.append(data.get("role_id"))

        for r_id in roles_to_add:
            if not r_id: continue
            role = guild.get_role(int(r_id))
            if role and role not in member.roles:
                if role >= guild.me.top_role:
                    print(f"[RR WARNING] Cannot assign role '{role.name}' to {member.display_name} - Bot role is below '{role.name}' in role hierarchy!")
                    continue
                try:
                    await member.add_roles(role, reason="Reaction Role emoji add")
                    print(f"[RR EMOJI ADD] Granted '{role.name}' to {member.display_name}")
                except Exception as e:
                    print(f"[RR EMOJI ADD ERROR] {e}")

@bot.event
async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return
    msg_id = str(payload.message_id)
    if msg_id in reaction_roles:
        data = reaction_roles[msg_id]
        guild = bot.get_guild(payload.guild_id)
        if not guild:
            try:
                guild = await bot.fetch_guild(payload.guild_id)
            except Exception:
                return
        if not guild: return

        member = guild.get_member(payload.user_id)
        if not member:
            try:
                member = await guild.fetch_member(payload.user_id)
            except Exception:
                return
        if not member: return

        roles_to_remove = []
        if "roles" in data and isinstance(data["roles"], list):
            payload_emoji = str(payload.emoji.name) if payload.emoji.is_unicode_emoji() else str(payload.emoji)
            for r_item in data["roles"]:
                target_emoji = str(r_item.get("emoji", "")).strip()
                if not target_emoji or target_emoji == payload_emoji or target_emoji in str(payload.emoji):
                    roles_to_remove.append(r_item.get("role_id"))
        elif data.get("role_id"):
            roles_to_remove.append(data.get("role_id"))

        for r_id in roles_to_remove:
            if not r_id: continue
            role = guild.get_role(int(r_id))
            if role and role in member.roles:
                if role >= guild.me.top_role:
                    print(f"[RR WARNING] Cannot remove role '{role.name}' from {member.display_name} - Bot role is below '{role.name}' in role hierarchy!")
                    continue
                try:
                    await member.remove_roles(role, reason="Reaction Role emoji remove")
                    print(f"[RR EMOJI REMOVE] Removed '{role.name}' from {member.display_name}")
                except Exception as e:
                    print(f"[RR EMOJI REMOVE ERROR] {e}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    uid = member.id
    now = time.time()

    # User joined a VC
    if before.channel is None and after.channel is not None:
        voice_start_times[uid] = now

    # User left a VC
    elif before.channel is not None and after.channel is None:
        if uid in voice_start_times:
            joined_at = voice_start_times.pop(uid)
            duration = int(now - joined_at)
            uid_str = str(uid)
            if uid_str not in user_stats:
                user_stats[uid_str] = {"messages": 0, "vc_seconds": 0}
            user_stats[uid_str]["vc_seconds"] = user_stats[uid_str].get("vc_seconds", 0) + duration
            save_user_stats()

    # User switched VCs
    elif before.channel is not None and after.channel is not None and before.channel.id != after.channel.id:
        if uid in voice_start_times:
            joined_at = voice_start_times[uid]
            duration = int(now - joined_at)
            uid_str = str(uid)
            if uid_str not in user_stats:
                user_stats[uid_str] = {"messages": 0, "vc_seconds": 0}
            user_stats[uid_str]["vc_seconds"] = user_stats[uid_str].get("vc_seconds", 0) + duration
            save_user_stats()
        voice_start_times[uid] = now

# -------- Build Complete Member Stats Embed -------- #
async def build_user_stats_embed(usr: Union[discord.Member, discord.User], guild: Optional[discord.Guild]) -> discord.Embed:
    member = guild.get_member(usr.id) if guild else (usr if isinstance(usr, discord.Member) else None)
    uid_str = str(usr.id)

    st = user_stats.get(uid_str, {"messages": 0, "vc_seconds": 0})
    active_vc_secs = 0
    if usr.id in voice_start_times:
        active_vc_secs = int(time.time() - voice_start_times[usr.id])

    total_vc_secs = st.get("vc_seconds", 0) + active_vc_secs
    total_msgs = st.get("messages", 0)

    # Historical Chat Scan if untracked/low
    if guild and total_msgs < 5:
        try:
            scanned_msgs = 0
            for ch in guild.text_channels[:5]:
                if ch.permissions_for(guild.me).read_message_history:
                    async for m in ch.history(limit=100):
                        if m.author.id == usr.id:
                            scanned_msgs += 1
            if scanned_msgs > total_msgs:
                total_msgs = scanned_msgs
                if uid_str not in user_stats:
                    user_stats[uid_str] = {"messages": scanned_msgs, "vc_seconds": 0}
                else:
                    user_stats[uid_str]["messages"] = scanned_msgs
                save_user_stats()
        except Exception:
            pass

    hours = total_vc_secs // 3600
    minutes = (total_vc_secs % 3600) // 60
    seconds = total_vc_secs % 60
    vc_time_str = f"{hours}h {minutes}m {seconds}s" if hours > 0 else (f"{minutes}m {seconds}s" if minutes > 0 else f"{seconds}s")

    sorted_users = sorted(user_stats.items(), key=lambda x: x[1].get("messages", 0), reverse=True)
    chat_rank = "N/A"
    for idx, (u_id, u_data) in enumerate(sorted_users, 1):
        if u_id == uid_str:
            chat_rank = f"#{idx}"
            break

    import math
    level = int(math.floor(math.sqrt(total_msgs / 5))) if total_msgs > 0 else 0

    embed = discord.Embed(
        title=f"📊 Server Activity & Tenure Stats for {usr.display_name}",
        color=member.color if (member and member.color.value != 0) else discord.Color.from_rgb(0, 168, 252)
    )
    embed.set_thumbnail(url=usr.display_avatar.url)

    if member and member.joined_at:
        joined_ts = int(member.joined_at.timestamp())
        created_ts = int(member.created_at.timestamp())

        sorted_members = sorted([m for m in guild.members if m.joined_at], key=lambda x: x.joined_at)
        join_pos = next((idx for idx, m in enumerate(sorted_members, 1) if m.id == member.id), "N/A")
        total_members = len(guild.members)

        embed.add_field(
            name="📅 Server Tenure (Since Joined)",
            value=f"• **Joined Server:** <t:{joined_ts}:F>\n• **Tenure:** <t:{joined_ts}:R>\n• **Join Rank:** Member `{join_pos}` of `{total_members}`",
            inline=False
        )
        embed.add_field(
            name="🎂 Discord Account Age",
            value=f"• **Created:** <t:{created_ts}:F>\n• **Age:** <t:{created_ts}:R>",
            inline=False
        )

    embed.add_field(
        name="💬 Chat Activity",
        value=f"• **Total Messages:** `{total_msgs:,}`\n• **Chat Level:** `Lvl {level}`\n• **Activity Rank:** `{chat_rank}`",
        inline=True
    )
    embed.add_field(
        name="🎙️ Voice VC Time",
        value=f"• **Time Spent:** `{vc_time_str}`\n• **Status:** `{'🔴 In VC' if usr.id in voice_start_times else '⚪ Offline'}`",
        inline=True
    )

    if member and len(member.roles) > 1:
        roles_str = ", ".join([r.mention for r in reversed(member.roles[1:])][:8])
        embed.add_field(name="🎭 Member Roles", value=roles_str, inline=False)

    embed.set_footer(text=f"User ID: {usr.id} • Stats calculated since member joined")
    return embed

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Deduplication
    if message.id in processed_messages:
        return
    processed_messages.add(message.id)
    if len(processed_messages) > 1000:
        processed_messages.clear()

    # User Message Stats Tracking
    if message.guild:
        uid_str = str(message.author.id)
        if uid_str not in user_stats:
            user_stats[uid_str] = {"messages": 0, "vc_seconds": 0}
        user_stats[uid_str]["messages"] = user_stats[uid_str].get("messages", 0) + 1
        save_user_stats()

    # Automod: Invites & Spam
    if message.guild and not message.author.guild_permissions.administrator:
        if re.search(r'(discord\.gg\/|discord\.com\/invite\/)', message.content, re.IGNORECASE):
            try:
                await message.delete()
                await message.channel.send(f"*(<@{message.author.id}>, we don't allow server invites here! 😠)*")
                return
            except Exception:
                pass

        now = time.time()
        user_spam = user_spam_map.get(message.author.id, [])
        user_spam.append(now)
        user_spam = [t for t in user_spam if now - t < 5.0]
        user_spam_map[message.author.id] = user_spam

        if len(user_spam) >= 5:
            try:
                await message.author.timeout(discord.utils.utcnow() + datetime.timedelta(seconds=60), reason="Auto-Mod: Spamming")
                user_spam_map.pop(message.author.id, None)
                await message.channel.send(f"*(I just put <@{message.author.id}> in timeout for 60 seconds because they were spamming! 🔨 My chat, my rules! 💅)*")
                return
            except Exception:
                pass

    raw_content = message.content.strip()
    text = raw_content.lower()

    # -------- Prefix Command: .stats -------- #
    if raw_content.startswith('.stats'):
        target = message.mentions[0] if message.mentions else message.author
        embed = await build_user_stats_embed(target, message.guild)
        await message.reply(embed=embed)
        return

    # -------- Prefix Command: .delete -------- #
    if raw_content.startswith('.delete'):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can delete messages! 🥺")
            return
        args = raw_content.split()[1:]
        if not args:
            await message.reply("⚠️ **Usage:** `.delete <amount>` or `.delete <amount> @User`")
            return
        try:
            amount = int(args[0])
        except ValueError:
            await message.reply("Please provide a valid number of messages! Example: `.delete 50`")
            return

        target = message.mentions[0] if message.mentions else None
        if target:
            deleted_count = 0
            async for msg in message.channel.history(limit=500):
                if msg.author.id == target.id:
                    try:
                        await msg.delete()
                        deleted_count += 1
                        if deleted_count >= amount:
                            break
                    except Exception:
                        pass
            await message.channel.send(f"Successfully deleted **{deleted_count}** message(s) sent by <@{target.id}>! ✨", delete_after=5)
        else:
            deleted_count = 0
            left_to_delete = amount
            while left_to_delete > 0:
                fetch_amount = min(100, left_to_delete)
                msgs = [m async for m in message.channel.history(limit=fetch_amount)]
                if not msgs:
                    break
                deleted = await message.channel.purge(limit=len(msgs), bulk=True)
                deleted_count += len(deleted)
                left_to_delete -= len(msgs)
                if len(deleted) < len(msgs):
                    break
            await message.channel.send(f"Successfully wiped **{deleted_count}** message(s) from the channel! ✨", delete_after=5)
        return

    # -------- Prefix Command: .reactionrole / .rr -------- #
    if raw_content.startswith(('.reactionrole', '.rr')):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can create reaction roles! 🥺")
            return

        parts = raw_content.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("⚠️ **Usage:** `.rr #channel @Role Title | Description | [emoji] | [thumbnail_url]`\n*Example:* `.rr #announcements @Gamer Rules & Guidelines | Post your link in #chat | ✔️ | https://image.png`")
            return

        body = parts[1]
        pipe_split = body.split('|')
        if len(pipe_split) < 2:
            await message.reply("⚠️ Please use `|` to separate Title and Description!\n*Example:* `.rr #announcements @Gamer Rules & Guidelines | Post your link in #chat | ✔️`")
            return

        first_part = pipe_split[0].strip()
        description_text = pipe_split[1].strip()
        emoji_char = pipe_split[2].strip() if len(pipe_split) > 2 and pipe_split[2].strip() else "✔️"
        thumbnail_url = pipe_split[3].strip() if len(pipe_split) > 3 and pipe_split[3].strip() else None

        target_channel = message.channel_mentions[0] if message.channel_mentions else message.channel
        target_role = message.role_mentions[0] if message.role_mentions else None

        if not target_role:
            await message.reply("⚠️ Please mention a role! Example: `.rr #channel @Gamer Title | Description`")
            return

        title_text = first_part
        if message.channel_mentions:
            title_text = title_text.replace(message.channel_mentions[0].mention, '').strip()
        if message.role_mentions:
            title_text = title_text.replace(message.role_mentions[0].mention, '').strip()
        if not title_text:
            title_text = f"Get the {target_role.name} Role!"

        embed = discord.Embed(
            title=title_text,
            description=f"{description_text}\n\n👉 **React or click the button below to get the {target_role.mention} role!**",
            color=target_role.color if target_role.color.value != 0 else discord.Color.from_rgb(0, 168, 252)
        )
        image_att = message.attachments[0] if message.attachments else None
        thumb_att = message.attachments[1] if len(message.attachments) > 1 else None
        files = await process_embed_attachments(thumbnail_file=thumb_att, image_file=image_att, thumbnail_url=thumbnail_url, embed=embed)

        embed.set_footer(text=f"Role: {target_role.name} • Click or React to Toggle")

        view = discord.ui.View(timeout=None)
        btn = ReactionRoleButton(role_id=target_role.id, emoji=emoji_char, label=f"Get {target_role.name}")
        view.add_item(btn)

        msg = await target_channel.send(embed=embed, view=view, files=files if files else None, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))
        try:
            await msg.add_reaction(emoji_char)
        except Exception:
            pass

        bot.add_view(view, message_id=msg.id)
        reaction_roles[str(msg.id)] = {
            "roles": [{"role_id": target_role.id, "emoji": emoji_char, "label": f"Get {target_role.name}"}],
            "role_id": target_role.id,
            "emoji": emoji_char,
            "channel_id": target_channel.id,
            "title": title_text
        }
        save_reaction_roles()

        await message.reply(f"✅ Posted Reaction Role announcement in {target_channel.mention} for **{target_role.name}**!")
        return

    # -------- Prefix Command: .multirr / .multireactionrole -------- #
    if raw_content.startswith(('.multirr', '.multireactionrole')):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can create reaction roles! 🥺")
            return

        parts = raw_content.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("⚠️ **Usage:** `.multirr #channel Title | Description | @Role1 📈 | @Role2 🛠️ | @Role3 𝓝𝓕𝓣 | [thumbnail]`\n*Example:* `.multirr #roles Roles Selection | You can select multiple roles anytime | @trading 📈 | @Builder 🛠️ | @NFT 𝓝𝓕𝓣`")
            return

        body = parts[1]
        pipe_split = [p.strip() for p in body.split('|') if p.strip()]
        if len(pipe_split) < 3:
            await message.reply("⚠️ Please separate Title, Description, and Roles using `|`!\n*Example:* `.multirr #roles Title | Description | @Role1 🎮 | @Role2 🔥`")
            return

        first_part = pipe_split[0]
        description_text = pipe_split[1]

        target_channel = message.channel_mentions[0] if message.channel_mentions else message.channel
        title_text = first_part
        if message.channel_mentions:
            title_text = title_text.replace(message.channel_mentions[0].mention, '').strip()

        if not title_text:
            title_text = "🎭 Multi-Role Selection"

        role_pairs = []
        thumbnail_url = None

        for segment in pipe_split[2:]:
            if segment.startswith("http://") or segment.startswith("https://") or "tenor.com" in segment or "klipy" in segment:
                thumbnail_url = segment
                continue

            temp_msg_roles = [r for r in message.role_mentions if r.mention in segment or str(r.id) in segment]
            if temp_msg_roles:
                for r in temp_msg_roles:
                    rem_text = segment.replace(r.mention, '').replace(f"<@&{r.id}>", '').strip()
                    emoji_char = rem_text if rem_text else None
                    if (r.id, emoji_char) not in [(rp[0].id, rp[1]) for rp in role_pairs]:
                        role_pairs.append((r, emoji_char))
            elif "@everyone" not in segment and "@here" not in segment:
                for r in message.guild.roles:
                    if r.name.lower() in segment.lower():
                        rem_text = segment.lower().replace(r.name.lower(), '').strip()
                        emoji_char = rem_text if rem_text else None
                        if (r.id, emoji_char) not in [(rp[0].id, rp[1]) for rp in role_pairs]:
                            role_pairs.append((r, emoji_char))
                        break

        if not role_pairs and message.role_mentions:
            for r in message.role_mentions:
                role_pairs.append((r, None))

        if not role_pairs:
            await message.reply("⚠️ No valid roles found! Make sure to tag @Role1, @Role2 with emojis!\n*Example:* `.multirr #roles Pick Roles | Select roles | @Role1 🎮 | @Role2 🚀`")
            return

        embed = discord.Embed(
            title=title_text,
            description=f"{description_text}\n\n👉 **Click any button below to get or remove your roles!**",
            color=discord.Color.from_rgb(0, 168, 252)
        )

        if not thumbnail_url and message.attachments:
            thumbnail_url = message.attachments[0].url

        if thumbnail_url:
            res_thumb = await resolve_image_url(thumbnail_url)
            if res_thumb:
                embed.set_thumbnail(url=res_thumb)

        embed.set_footer(text="Multi-Role Selection • Click any button to toggle")

        view = discord.ui.View(timeout=None)
        for role_obj, emoji_char in role_pairs:
            btn = ReactionRoleButton(role_id=role_obj.id, emoji=emoji_char, label=f"{role_obj.name}")
            view.add_item(btn)

        msg = await target_channel.send(embed=embed, view=view, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))
        bot.add_view(view, message_id=msg.id)

        roles_list = [{"role_id": r.id, "emoji": e, "label": r.name} for r, e in role_pairs]
        reaction_roles[str(msg.id)] = {
            "roles": roles_list,
            "channel_id": target_channel.id,
            "title": title_text
        }
        save_reaction_roles()

        roles_summary = ", ".join([f"**{r.name}** {e if e else ''}" for r, e in role_pairs])
        await message.reply(f"✅ Posted Multi-Role Selection embed in {target_channel.mention} with roles: {roles_summary}!")
        return

    # -------- Prefix Command: .addrr (Reaction Role on Existing Message) -------- #
    if raw_content.startswith('.addrr'):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can add reaction roles! 🥺")
            return

        args = raw_content.split()[1:]
        if len(args) < 2 or not message.role_mentions:
            await message.reply("⚠️ **Usage:** `.addrr <message_id> @Role [emoji]`\n*Example:* `.addrr 123456789012345678 @Gamer 🎮`")
            return

        msg_id = re.sub(r'[^0-9]', '', args[0])
        target_role = message.role_mentions[0]
        emoji_char = args[2].strip() if len(args) > 2 else "⭐"

        try:
            target_msg = await message.channel.fetch_message(int(msg_id))
        except Exception:
            await message.reply("Could not find that message ID in this channel! Check the Message ID. 🥺")
            return

        try:
            await target_msg.add_reaction(emoji_char)
        except Exception as e:
            print(f"[ADDRR EMOJI ERROR] {e}")

        existing_entry = reaction_roles.get(str(target_msg.id), {})
        existing_roles = existing_entry.get("roles", [])
        if not existing_roles and existing_entry.get("role_id"):
            existing_roles = [{"role_id": existing_entry.get("role_id"), "emoji": existing_entry.get("emoji")}]

        existing_roles.append({"role_id": target_role.id, "emoji": emoji_char, "label": target_role.name})

        reaction_roles[str(target_msg.id)] = {
            "roles": existing_roles,
            "channel_id": message.channel.id
        }
        save_reaction_roles()

        await message.reply(f"✅ Reaction Role set on message `{msg_id}` for **{target_role.name}** with emoji {emoji_char}!")
        return

    # -------- Prefix Command: .purgeuser / .deleteuser -------- #
    if raw_content.startswith(('.purgeuser', '.deleteuser')):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can delete messages! 🥺")
            return

        if not message.mentions:
            await message.reply("⚠️ **Usage:** `.purgeuser @User <amount>`\n*Example:* `.purgeuser @BadUser 20`")
            return

        target = message.mentions[0]
        args = raw_content.split()[1:]
        amount = 50
        for a in args:
            if a.isdigit():
                amount = int(a)
                break

        deleted_count = 0
        async for msg in message.channel.history(limit=500):
            if msg.author.id == target.id:
                try:
                    await msg.delete()
                    deleted_count += 1
                    if deleted_count >= amount:
                        break
                except Exception:
                    pass

        await message.channel.send(f"🧹 Cleaned up **{deleted_count}** message(s) strictly sent by <@{target.id}>!", delete_after=5)
        return

    # -------- Prefix Command: .announce -------- #
    if raw_content.startswith(('.announce', '.announcement')):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can post announcements! 🥺")
            return

        parts = raw_content.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("⚠️ **Usage:** `.announce #channel Title | Content | [@Role] | [thumbnail_url]`\n*Example:* `.announce #announcements Rules & Guidelines | We conduct two engagement sessions daily... | @Members | https://image.png`")
            return

        body = parts[1]
        pipe_split = body.split('|')
        if len(pipe_split) < 2:
            await message.reply("⚠️ Please use `|` to separate Title and Content!\n*Example:* `.announce #announcements Title | Content`")
            return

        first_part = pipe_split[0].strip()
        content_text = pipe_split[1].strip()
        target_role = message.role_mentions[0] if message.role_mentions else None
        thumbnail_url = pipe_split[3].strip() if len(pipe_split) > 3 and pipe_split[3].strip() else None

        target_channel = message.channel_mentions[0] if message.channel_mentions else message.channel

        title_text = first_part
        if message.channel_mentions:
            title_text = title_text.replace(message.channel_mentions[0].mention, '').strip()

        if not title_text:
            title_text = "📢 Announcement"

        embed = discord.Embed(
            title=title_text,
            description=content_text,
            color=discord.Color.from_rgb(0, 168, 252)
        )

        image_att = message.attachments[0] if message.attachments else None
        thumb_att = message.attachments[1] if len(message.attachments) > 1 else None
        files = await process_embed_attachments(thumbnail_file=thumb_att, image_file=image_att, thumbnail_url=thumbnail_url, embed=embed)

        pings = []
        if "@everyone" in raw_content:
            pings.append("@everyone")
        if "@here" in raw_content:
            pings.append("@here")
        for r in message.role_mentions:
            if r.mention not in pings:
                pings.append(r.mention)

        ping_text = " ".join(pings) if pings else None
        if ping_text:
            await target_channel.send(content=ping_text, embed=embed, files=files if files else None, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))
        else:
            await target_channel.send(embed=embed, files=files if files else None, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))

        await message.reply(f"📢 Posted announcement in {target_channel.mention}!")
        return

    # -------- Prefix Command: .sync (Instant Command Sync) -------- #
    if raw_content.startswith('.sync'):
        if not is_bot_admin(message):
            await message.reply("❌ **Access Denied!** Only bot admins can sync commands!")
            return
        synced = await bot.tree.sync()
        guild_count = 0
        if message.guild:
            bot.tree.copy_global_to(guild=message.guild)
            guild_synced = await bot.tree.sync(guild=message.guild)
            guild_count = len(guild_synced)
        else:
            for g in bot.guilds:
                try:
                    bot.tree.copy_global_to(guild=g)
                    await bot.tree.sync(guild=g)
                    guild_count += 1
                except Exception:
                    pass
        await message.reply(f"✅ Synced **{len(synced)}** slash commands globally + **{guild_count}** guild commands instantly!")
        return

    # -------- Prefix Command: .setwelcome -------- #
    if raw_content.startswith('.setwelcome'):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can set welcome channel! 🥺")
            return
        target_ch = message.channel_mentions[0] if message.channel_mentions else message.channel
        welcome_channel_config[str(message.guild.id)] = target_ch.id
        await message.reply(f"✅ Welcome channel set to {target_ch.mention}!")
        return

    # -------- Prefix Command: .testwelcome -------- #
    if raw_content.startswith('.testwelcome'):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can test welcome card! 🥺")
            return
        server_name = message.guild.name if message.guild else os.getenv("SERVER_NAME", "Differential Degens")
        user_display_name = getattr(message.author, 'global_name', None) or getattr(message.author, 'display_name', None) or message.author.name
        member_count = message.guild.member_count if message.guild else 15
        card_bytes = await create_welcome_card(
            avatar_url=message.author.display_avatar.url,
            member_name=user_display_name,
            member_count=member_count,
            server_name=server_name
        )
        welcome_text = f"Welcome <@{message.author.id}> to **{server_name}** ❤️"
        if card_bytes:
            file = discord.File(fp=io.BytesIO(card_bytes.getvalue()), filename="welcome.png")
            await message.channel.send(content=welcome_text, file=file)
        else:
            await message.channel.send(content=welcome_text)
        return

    # -------- Prefix Command: .sharelink / .shareinvite -------- #
    if raw_content.startswith(('.sharelink', '.shareinvite', '.serverlink', '.invite')):
        if not is_mod_or_admin(message):
            await message.reply("❌ **Access Denied!** Only moderators and admins can share server links! 🥺")
            return

        parts = raw_content.split(maxsplit=1)
        if len(parts) < 2:
            await message.reply("⚠️ **Usage:** `.sharelink <discord_invite_link_or_code> [#channel] [custom_message]`\n*Example:* `.sharelink discord.gg/discord-developers #general Check out this cool server!`")
            return

        body = parts[1]
        args = body.split()
        invite_str = args[0]

        target_channel = message.channel_mentions[0] if message.channel_mentions else message.channel
        note = body.replace(invite_str, '').replace(target_channel.mention, '').strip()

        info = await fetch_invite_info(invite_str)
        if not info:
            await message.reply("⚠️ Could not fetch details for that Discord invite link! Make sure it's a valid, unexpired invite link! 🥺")
            return

        embed = discord.Embed(
            title=f"✨ {info['guild_name']}",
            color=discord.Color.from_rgb(0, 168, 252)
        )

        desc_parts = []
        if note:
            desc_parts.append(note)
        if info['description']:
            desc_parts.append(f"*{info['description']}*")

        desc_parts.append(f"\n👉 **Join Link:** {info['invite_url']}")
        embed.description = "\n\n".join(desc_parts)

        if info['icon_url']:
            embed.set_thumbnail(url=info['icon_url'])
        if info['banner_url']:
            embed.set_image(url=info['banner_url'])

        embed.add_field(name="👥 Total Members", value=f"`{info['member_count']:,}`", inline=True)
        embed.add_field(name="🟢 Online Members", value=f"`{info['online_count']:,}`", inline=True)
        embed.set_footer(text=f"Server ID: {info['guild_id']} • Shared by {message.author.display_name}")

        view = discord.ui.View()
        join_btn = discord.ui.Button(label=f"Join {info['guild_name']} 🚀", url=info['invite_url'], style=discord.ButtonStyle.link)
        view.add_item(join_btn)

        await target_channel.send(embed=embed, view=view)
        await message.reply(f"✅ Posted server invite embed for **{info['guild_name']}** in {target_channel.mention}!")
        return

    # -------- Admin System Commands (.addadmin, .removeadmin, .adminlist) -------- #
    if raw_content.startswith(('.addadmin', '.removeadmin', '.adminlist', '.admins')):
        if not is_bot_admin(message):
            await message.reply("❌ **Access Denied!** You do not have permission to use admin commands.")
            return

        args = raw_content.split()[1:]

        if raw_content.startswith('.addadmin'):
            target_id = None
            if message.mentions:
                target_id = str(message.mentions[0].id)
            elif args:
                target_id = re.sub(r'[^0-9]', '', args[0])

            if not target_id or len(target_id) < 5:
                await message.reply("⚠️ **Usage:** `.addadmin @User` or `.addadmin <UserID>`")
                return

            if target_id in bot_admins:
                await message.reply(f"⚠️ <@{target_id}> (ID: `{target_id}`) is already a bot admin!")
                return

            bot_admins.add(target_id)
            save_admins()
            await message.reply(f"✅ Successfully added <@{target_id}> (ID: `{target_id}`) as a bot admin! Saved to database.")
            return

        if raw_content.startswith('.removeadmin'):
            target_id = None
            if message.mentions:
                target_id = str(message.mentions[0].id)
            elif args:
                target_id = re.sub(r'[^0-9]', '', args[0])

            if not target_id or len(target_id) < 5:
                await message.reply("⚠️ **Usage:** `.removeadmin @User` or `.removeadmin <UserID>`")
                return

            if target_id not in bot_admins:
                await message.reply(f"⚠️ User ID `{target_id}` is not in the admin database.")
                return

            bot_admins.remove(target_id)
            save_admins()
            await message.reply(f"✅ Successfully removed <@{target_id}> (ID: `{target_id}`) from bot admins! Saved to database.")
            return

        if raw_content.startswith(('.adminlist', '.admins')):
            if not bot_admins:
                await message.reply("📋 **Bot Admin Database:** No custom admins added yet. (Server Administrators and Bot Owners have default access).")
                return
            admin_list = "\n".join([f"• <@{aid}> (ID: `{aid}`)" for aid in bot_admins])
            await message.reply(f"📋 **Bot Admin Database:**\n{admin_list}")
            return

    # Crypto Price Lookup (.price btc, !price eth) — $token trigger removed
    if text.startswith(('.price', '!price')):
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            token_q = parts[1].strip().lstrip("$")
            async with message.channel.typing():
                token_data = await fetch_token_price(token_q)
                if token_data and token_data.get("price") is not None:
                    p_str = _format_usd(token_data["price"])
                    ch = token_data.get("change_24h") or 0.0
                    sign = "+" if ch >= 0 else ""
                    ch_icon = "🟢" if ch >= 0 else "🔴"
                    mcap_str = _format_big(token_data.get("market_cap") or 0)
                    fdv_str = _format_big(token_data.get("fdv") or 0)
                    rank_str = f"#{token_data['rank']}" if token_data.get("rank") else "N/A"
                    embed = discord.Embed(
                        title=f"{token_data.get('name', token_q.upper())} ({token_data.get('symbol', token_q).upper()})",
                        color=discord.Color.green() if ch >= 0 else discord.Color.red()
                    )
                    embed.add_field(name="Price", value=f"**{p_str}**", inline=True)
                    embed.add_field(name="24h Change", value=f"{ch_icon} {sign}{ch:.2f}%", inline=True)
                    embed.add_field(name="Rank", value=rank_str, inline=True)
                    embed.add_field(name="Market Cap", value=mcap_str, inline=True)
                    embed.add_field(name="FDV", value=fdv_str, inline=True)
                    if token_data.get("category"):
                        embed.add_field(name="Category", value=token_data["category"], inline=True)
                    embed.set_footer(text="Powered by CoinGecko / DEX | Use /price <token>")
                    await message.reply(embed=embed)
                else:
                    res = await get_crypto_price(token_q)
                    await message.reply(res)
                return
        else:
            await message.reply("💡 Usage: `.price <token>` (e.g. `.price btc`) or use the `/price` slash command.")
            return

    # Football Hashtags (#livescore, #plstandings, #news)
    if text.startswith(('#livescore', '#livescores')):
        async with message.channel.typing():
            res = await fetch_livescores()
            await message.reply(res)
            return

    standings_match = re.match(r'^#(\w+?)standings?$', text)
    if standings_match:
        league_input = standings_match.group(1)
        async with message.channel.typing():
            res = await fetch_standings(league_input)
            await message.reply(res)
            return

    if text.startswith('#news'):
        async with message.channel.typing():
            news_res = await fetch_realtime_context('football soccer latest news transfers results today 2026')
            if news_res:
                bullets = "\n".join([f"⚽ {s.strip()}" for s in news_res.split(" | ") if s.strip()])
                await message.reply(f"**📰 Latest Football News:**\n{bullets}")
            else:
                await message.reply("Could not find any football news right now! Try again later 🙏")
            return

    # AI Chat / Auto-Reply Trigger
    ref_bot = False
    if message.reference and message.reference.cached_message:
        ref_bot = (message.reference.cached_message.author == bot.user)

    is_mentioned = (
        ("arcie" in text or "hey arcie" in text or "arciebot" in text or "diffy" in text) or
        (bot.user in message.mentions) or
        isinstance(message.channel, discord.DMChannel) or
        ref_bot
    )

    if is_mentioned:
        # Clear Memory Command
        if "clear memory" in text or "forget everything" in text:
            if not is_bot_admin(message):
                await message.reply("*(pouts)* Hey! Only my admins are allowed to clear my memory! 😤")
                return
            channel_memories[str(message.channel.id)] = []
            save_memories()
            await message.reply("*(zaps brain)* Ow! Okay, I just completely wiped my memory for this channel! What were we talking about again? 🥺")
            return

        # TLDR Command
        if any(kw in text for kw in ["tldr", "summarize", "recap", "did i miss"]):
            res = await tldr_summary(message)
            await message.reply(res)
            return

        # Moderator / Admin Timeout Command
        if re.search(r'\b(timeout|mute|put in timeout|give time out|give timeout)\b', text):
            is_mod = is_bot_admin(message) or (message.guild and message.author.guild_permissions and (message.author.guild_permissions.manage_messages or message.author.guild_permissions.moderate_members))
            if not is_mod:
                await message.reply("*(pouts)* Hey! Only my moderators and admins can tell me to timeout people! 😤")
                return

            target = None
            if message.mentions:
                for m in message.mentions:
                    if m.id != bot.user.id and not m.bot:
                        target = m
                        break

            if not target and message.guild:
                words = [w for w in re.split(r'[^a-zA-Z0-9]', text) if len(w) > 2]
                for m in message.guild.members:
                    if not m.bot and (m.name.lower() in words or m.display_name.lower() in words):
                        target = m
                        break

            if target:
                try:
                    until = discord.utils.utcnow() + datetime.timedelta(minutes=15)
                    await target.timeout(until, reason=f"Timed out for 15 minutes by request of {message.author.display_name}")
                    await message.reply(f"*(I just put <@{target.id}> in timeout for 15 minutes as requested! 🔨 Nobody messes with my mods! 💅)*")
                    return
                except Exception as e:
                    print(f"[TIMEOUT ERROR] {e}")
                    await message.reply(f"I tried to timeout <@{target.id}>, but Discord didn't let me! Make sure my role is higher than theirs! 🥺")
                    return
            else:
                await message.reply("Who do you want me to timeout? Tag them or say their name! 🔨")
                return

        async with message.channel.typing():
            response = await ai_reply(message)

            files = []
            sent_voice = False

            # 10% Chance for TikTok Cute TTS Voice Note
            if random.random() < 0.10:
                tts_file = await fetch_tiktok_tts(response)
                if tts_file:
                    files.append(tts_file)
                    response = f"🎙️ *Sent a voice note...*\n{response}"
                    sent_voice = True

            # Auto-GIF System: 25% chance to attach a native Tenor/Klipy GIF file
            if not sent_voice and random.random() < 0.25:
                gif_file = await fetch_auto_gif_file()
                if gif_file:
                    files.append(gif_file)

            kwargs = {}
            if files:
                kwargs["files"] = files

            await message.reply(response, **kwargs)
            return

    await bot.process_commands(message)

# -------- Slash Commands -------- #
@bot.tree.command(name="addreactionrole", description="Attaches a reaction role to ANY existing message by Message ID!")
@app_commands.describe(
    message_id="The ID of the existing message to add reaction role to",
    role="The role to assign when users react",
    emoji="Optional: Emoji for reaction (default: ⭐)"
)
async def add_reaction_role_cmd(
    interaction: discord.Interaction,
    message_id: str,
    role: discord.Role,
    emoji: Optional[str] = "⭐"
):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can use this feature! 🥺", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    clean_id = re.sub(r'[^0-9]', '', message_id)
    if not clean_id:
        await interaction.followup.send("Please provide a valid numeric Message ID! 🥺", ephemeral=True)
        return

    try:
        msg = await interaction.channel.fetch_message(int(clean_id))
    except Exception:
        await interaction.followup.send("Could not find a message with that ID in this channel! 🥺", ephemeral=True)
        return

    try:
        await msg.add_reaction(emoji)
    except Exception:
        pass

    existing_entry = reaction_roles.get(str(msg.id), {})
    existing_roles = existing_entry.get("roles", [])
    if not existing_roles and existing_entry.get("role_id"):
        existing_roles = [{"role_id": existing_entry.get("role_id"), "emoji": existing_entry.get("emoji")}]

    existing_roles.append({"role_id": role.id, "emoji": emoji, "label": role.name})

    reaction_roles[str(msg.id)] = {
        "roles": existing_roles,
        "channel_id": interaction.channel.id
    }
    save_reaction_roles()

    await interaction.followup.send(f"✅ Successfully attached reaction role to message `{clean_id}` for **{role.name}** with emoji {emoji}!", ephemeral=True)

@bot.tree.command(name="purgeuser", description="Deletes messages strictly sent by a specific user!")
@app_commands.describe(
    target="The specific user whose messages you want to delete",
    amount="Number of messages to delete from this user"
)
async def purge_user_cmd(interaction: discord.Interaction, target: discord.User, amount: int):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can delete messages! 🥺", ephemeral=True)
        return

    if amount <= 0:
        await interaction.response.send_message("Give me a number greater than 0! 💕", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    deleted_count = 0
    try:
        async for msg in interaction.channel.history(limit=500):
            if msg.author.id == target.id:
                try:
                    await msg.delete()
                    deleted_count += 1
                    if deleted_count >= amount:
                        break
                except Exception:
                    pass
        await interaction.followup.send(f"🧹 Cleaned up **{deleted_count}** message(s) strictly sent by <@{target.id}>!")
    except Exception as e:
        print(f"[PURGEUSER ERROR] {e}")
        await interaction.followup.send("Error deleting user messages! 🥺", ephemeral=True)

@bot.tree.command(name="reactionrole", description="Posts a structured announcement embed with Reaction Role button & emoji!")
@app_commands.describe(
    channel="The channel to post the announcement in",
    role="The role to assign when users click or react",
    title="Main Title of the announcement",
    description="Body text (supports markdown, headings, bullet points & #channels)",
    emoji="Optional: Emoji for button & reaction (default: ✔️)",
    image_file="Optional: Upload image or GIF file directly from gallery",
    thumbnail_file="Optional: Upload thumbnail image or GIF directly from gallery",
    image="Optional: Main banner Image/GIF URL",
    thumbnail="Optional: Image/GIF URL for top-right thumbnail",
    color="Optional: Sidebar hex color code (e.g. #00A8FC)",
    footer="Optional: Footer text at bottom"
)
async def reaction_role_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    role: discord.Role,
    title: str,
    description: str,
    emoji: Optional[str] = "✔️",
    image_file: Optional[discord.Attachment] = None,
    thumbnail_file: Optional[discord.Attachment] = None,
    image: Optional[str] = None,
    thumbnail: Optional[str] = None,
    color: Optional[str] = "#00A8FC",
    footer: Optional[str] = None
):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can create reaction roles! 🥺", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    embed_color = parse_embed_color(color)

    embed = discord.Embed(
        title=title,
        description=f"{description}\n\n👉 **React or click the button below to get the {role.mention} role!**",
        color=embed_color
    )

    files = await process_embed_attachments(thumbnail_file=thumbnail_file, image_file=image_file, thumbnail_url=thumbnail, image_url=image, embed=embed)

    footer_text = footer if footer else f"Role: {role.name} • Click or React to Toggle"
    embed.set_footer(text=footer_text)

    # Create Button View
    view = discord.ui.View(timeout=None)
    btn = ReactionRoleButton(role_id=role.id, emoji=emoji, label=f"Get {role.name}")
    view.add_item(btn)

    try:
        msg = await channel.send(content=role.mention, embed=embed, view=view, files=files if files else None, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))
        try:
            await msg.add_reaction(emoji)
        except Exception:
            pass

        bot.add_view(view, message_id=msg.id)

        # Save to persistent database
        reaction_roles[str(msg.id)] = {
            "roles": [{"role_id": role.id, "emoji": emoji, "label": f"Get {role.name}"}],
            "role_id": role.id,
            "emoji": emoji,
            "channel_id": channel.id,
            "title": title
        }
        save_reaction_roles()

        await interaction.followup.send(f"✅ Successfully posted Reaction Role announcement in {channel.mention} for **{role.name}**!", ephemeral=True)
    except Exception as e:
        print(f"[RR COMMAND ERROR] {e}")
        await interaction.followup.send("Failed to post reaction role message! Check my channel permissions! 🥺", ephemeral=True)

@bot.tree.command(name="multirrole", description="Posts a structured announcement with MULTIPLE Reaction Role buttons!")
@app_commands.describe(
    channel="The channel to post the multi-role selection in",
    title="Main Title (e.g. Roles Selection)",
    description="Body text (e.g. You can select multiple roles anytime)",
    role1="First role to assign",
    thumbnail_file="Optional: Upload thumbnail GIF/image directly from phone gallery",
    image_file="Optional: Upload main banner GIF/image directly from phone gallery",
    emoji1="Emoji for first role button (e.g. 📈)",
    role2="Second role to assign",
    emoji2="Emoji for second role button (e.g. 🛠️)",
    role3="Third role to assign",
    emoji3="Emoji for third role button (e.g. 𝓝𝓕𝓣)",
    role4="Fourth role to assign",
    emoji4="Emoji for fourth role button (e.g. 🚨)",
    role5="Fifth role to assign",
    emoji5="Emoji for fifth role button",
    color="Optional: Sidebar hex color code (default: #00A8FC)"
)
async def multi_reaction_role_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: str,
    role1: discord.Role,
    thumbnail_file: Optional[discord.Attachment] = None,
    image_file: Optional[discord.Attachment] = None,
    emoji1: Optional[str] = None,
    role2: Optional[discord.Role] = None,
    emoji2: Optional[str] = None,
    role3: Optional[discord.Role] = None,
    emoji3: Optional[str] = None,
    role4: Optional[discord.Role] = None,
    emoji4: Optional[str] = None,
    role5: Optional[discord.Role] = None,
    emoji5: Optional[str] = None,
    color: Optional[str] = "#00A8FC"
):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can use this command! 🥺", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    role_pairs = [(role1, emoji1)]
    if role2: role_pairs.append((role2, emoji2))
    if role3: role_pairs.append((role3, emoji3))
    if role4: role_pairs.append((role4, emoji4))
    if role5: role_pairs.append((role5, emoji5))

    embed_color = parse_embed_color(color)

    embed = discord.Embed(
        title=title,
        description=f"{description}\n\n👉 **Click any button below to get or remove your roles!**",
        color=embed_color
    )

    files = await process_embed_attachments(thumbnail_file=thumbnail_file, image_file=image_file, embed=embed)

    embed.set_footer(text="Multi-Role Selection • Click any button to toggle")

    view = discord.ui.View(timeout=None)
    for r, e in role_pairs:
        btn = ReactionRoleButton(role_id=r.id, emoji=e, label=f"{r.name}")
        view.add_item(btn)

    try:
        msg = await channel.send(embed=embed, view=view, files=files if files else None, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))
        bot.add_view(view, message_id=msg.id)

        roles_list = [{"role_id": r.id, "emoji": e, "label": r.name} for r, e in role_pairs]
        reaction_roles[str(msg.id)] = {
            "roles": roles_list,
            "channel_id": channel.id,
            "title": title
        }
        save_reaction_roles()

        roles_summary = ", ".join([f"**{r.name}** {e}" for r, e in role_pairs])
        await interaction.followup.send(f"✅ Successfully posted Multi-Role embed in {channel.mention} with roles: {roles_summary}!", ephemeral=True)
    except Exception as e:
        print(f"[MULTI-RR ERROR] {e}")
        await interaction.followup.send("Failed to post Multi-Role embed! Check my channel permissions! 🥺", ephemeral=True)

@bot.tree.command(name="embed", description="Posts a beautifully structured announcement embed!")
@app_commands.describe(
    channel="The channel to post the embed in",
    title="Main Title of the announcement",
    description="Body text (supports markdown, headings, bullet points & #channels)",
    image_file="Optional: Upload image or GIF file directly from gallery",
    thumbnail_file="Optional: Upload thumbnail image or GIF directly from gallery",
    image="Optional: Main banner Image/GIF URL",
    thumbnail="Optional: Image/GIF URL for top-right thumbnail",
    color="Optional: Sidebar hex color code (e.g. #00A8FC)",
    footer="Optional: Footer text at bottom"
)
async def embed_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    description: str,
    image_file: Optional[discord.Attachment] = None,
    thumbnail_file: Optional[discord.Attachment] = None,
    image: Optional[str] = None,
    thumbnail: Optional[str] = None,
    color: Optional[str] = "#00A8FC",
    footer: Optional[str] = None
):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can post embeds! 🥺", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    embed_color = parse_embed_color(color)

    embed = discord.Embed(
        title=title,
        description=description,
        color=embed_color
    )

    files = await process_embed_attachments(thumbnail_file=thumbnail_file, image_file=image_file, thumbnail_url=thumbnail, image_url=image, embed=embed)

    if footer:
        embed.set_footer(text=footer)

    try:
        await channel.send(embed=embed, files=files if files else None, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))
        await interaction.followup.send(f"✅ Successfully posted structured announcement in {channel.mention}!", ephemeral=True)
    except Exception as e:
        print(f"[EMBED ERROR] {e}")
        await interaction.followup.send("Failed to post embed message! Check my permissions! 🥺", ephemeral=True)

@bot.tree.command(name="announce", description="Creates and posts a beautifully structured announcement embed!")
@app_commands.describe(
    channel="The channel to post the announcement in",
    title="Main Heading / Title of the announcement",
    content="Main Announcement content/body (supports markdown, headings & #channels)",
    role_to_ping="Optional: Role to ping/mention with the announcement (e.g. @everyone or @Role)",
    image_file="Optional: Upload image, GIF, MP4, 3GP, or video file directly from gallery",
    thumbnail_file="Optional: Upload thumbnail image, GIF, MP4, 3GP, or video file directly from gallery",
    image="Optional: Main banner Image/GIF URL",
    thumbnail="Optional: Image/GIF URL for top-right thumbnail",
    color="Optional: Sidebar hex color code (default: #00A8FC)",
    footer="Optional: Footer text at bottom"
)
async def announce_cmd(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
    title: str,
    content: str,
    role_to_ping: Optional[discord.Role] = None,
    image_file: Optional[discord.Attachment] = None,
    thumbnail_file: Optional[discord.Attachment] = None,
    image: Optional[str] = None,
    thumbnail: Optional[str] = None,
    color: Optional[str] = "#00A8FC",
    footer: Optional[str] = None
):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can post announcements! 🥺", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    embed_color = parse_embed_color(color)

    embed = discord.Embed(
        title=title,
        description=content,
        color=embed_color
    )

    files = await process_embed_attachments(thumbnail_file=thumbnail_file, image_file=image_file, thumbnail_url=thumbnail, image_url=image, embed=embed)

    if footer:
        embed.set_footer(text=footer)

    pings = []
    if role_to_ping:
        pings.append(role_to_ping.mention)
    if "@everyone" in content:
        pings.append("@everyone")
    if "@here" in content:
        pings.append("@here")

    ping_text = " ".join(dict.fromkeys(pings)) if pings else None

    try:
        if ping_text:
            await channel.send(content=ping_text, embed=embed, files=files if files else None, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))
        else:
            await channel.send(embed=embed, files=files if files else None, allowed_mentions=discord.AllowedMentions(roles=True, users=True, everyone=True))
        await interaction.followup.send(f"📢 Successfully posted structured announcement in {channel.mention}!", ephemeral=True)
    except Exception as e:
        print(f"[ANNOUNCE ERROR] {e}")
        await interaction.followup.send("Failed to post announcement! Check my permissions! 🥺", ephemeral=True)

@bot.tree.command(name="setwelcome", description="Sets the channel where new member welcome cards are posted!")
@app_commands.describe(channel="The channel for welcome messages")
async def set_welcome_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can use this command! 🥺", ephemeral=True)
        return
    welcome_channel_config[str(interaction.guild_id)] = channel.id
    await interaction.response.send_message(f"✅ Welcome channel set to {channel.mention}!", ephemeral=True)

@bot.tree.command(name="testwelcome", description="Generates and posts a test welcome card in this channel!")
async def test_welcome_cmd(interaction: discord.Interaction):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can test welcome card! 🥺", ephemeral=True)
        return
    await interaction.response.defer()
    
    server_name = interaction.guild.name if interaction.guild else os.getenv("SERVER_NAME", "Differential Degens")
    user_display_name = getattr(interaction.user, 'global_name', None) or getattr(interaction.user, 'display_name', None) or interaction.user.name
    member_count = interaction.guild.member_count if interaction.guild else 15
    avatar_url = interaction.user.display_avatar.with_format("png").with_size(512).url
    
    card_buf = await create_welcome_card(
        avatar_url=avatar_url,
        member_name=user_display_name,
        member_count=member_count,
        server_name=server_name
    )
    
    welcome_text = f"Welcome <@{interaction.user.id}> to **{server_name}** ❤️"
    
    if card_buf:
        card_buf.seek(0)
        file = discord.File(fp=card_buf, filename="welcome.png")
        await interaction.followup.send(content=welcome_text, file=file)
    else:
        await interaction.followup.send(content=welcome_text)

@bot.tree.command(name="sharelink", description="Posts a rich, beautiful embed for any Discord server invite link!")
@app_commands.describe(
    invite="The Discord invite link or code (e.g. discord.gg/code)",
    channel="Optional: Channel to post the invite embed in",
    note="Optional: Custom note or description to include",
    thumbnail_file="Optional: Upload custom logo/thumbnail GIF/image from phone gallery",
    banner_file="Optional: Upload custom banner/GIF image from phone gallery",
    banner_url="Optional: Custom banner or GIF URL"
)
async def share_link_cmd(
    interaction: discord.Interaction,
    invite: str,
    channel: Optional[discord.TextChannel] = None,
    note: Optional[str] = None,
    thumbnail_file: Optional[discord.Attachment] = None,
    banner_file: Optional[discord.Attachment] = None,
    banner_url: Optional[str] = None
):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can use this command! 🥺", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    target_channel = channel or interaction.channel
    info = await fetch_invite_info(invite)
    if not info:
        await interaction.followup.send("⚠️ Could not fetch details for that Discord invite link! Check if the link is valid! 🥺", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"✨ {info['guild_name']}",
        color=discord.Color.from_rgb(0, 168, 252)
    )

    desc_parts = []
    if note:
        desc_parts.append(note)
    if info['description']:
        desc_parts.append(f"*{info['description']}*")

    desc_parts.append(f"\n👉 **Join Link:** {info['invite_url']}")
    embed.description = "\n\n".join(desc_parts)

    if thumbnail_file and thumbnail_file.url:
        embed.set_thumbnail(url=thumbnail_file.url)
    elif info['icon_url']:
        embed.set_thumbnail(url=info['icon_url'])

    # Set banner (custom file > custom url > official server banner)
    if banner_file and banner_file.url:
        embed.set_image(url=banner_file.url)
    elif banner_url:
        res_banner = await resolve_image_url(banner_url)
        if res_banner:
            embed.set_image(url=res_banner)
    elif info['banner_url']:
        embed.set_image(url=info['banner_url'])

    embed.add_field(name="👥 Total Members", value=f"`{info['member_count']:,}`", inline=True)
    embed.add_field(name="🟢 Online Members", value=f"`{info['online_count']:,}`", inline=True)
    embed.set_footer(text=f"Server ID: {info['guild_id']} • Shared by {interaction.user.display_name}")

    view = discord.ui.View()
    join_btn = discord.ui.Button(label=f"Join {info['guild_name']} 🚀", url=info['invite_url'], style=discord.ButtonStyle.link)
    view.add_item(join_btn)

    try:
        await target_channel.send(embed=embed, view=view)
        await interaction.followup.send(f"✅ Successfully posted server invite embed for **{info['guild_name']}** in {target_channel.mention}!", ephemeral=True)
    except Exception as e:
        print(f"[SHARELINK ERROR] {e}")
        await interaction.followup.send("Failed to post server invite embed! Check my channel permissions! 🥺", ephemeral=True)

@bot.tree.command(name="delete", description="Deletes a specified number of messages from this channel!")
@app_commands.describe(amount="Number of messages to delete", target="Optional: Delete messages only from this user")
async def delete_cmd(interaction: discord.Interaction, amount: int, target: Optional[discord.User] = None):
    if not is_mod_or_admin(interaction):
        await interaction.response.send_message("❌ **Access Denied!** Only moderators and admins can delete messages! 🥺", ephemeral=True)
        return

    if amount <= 0:
        await interaction.response.send_message("Give me a real number greater than 0, babe! 💕", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        if target:
            deleted_count = 0
            async for msg in interaction.channel.history(limit=500):
                if msg.author.id == target.id:
                    try:
                        await msg.delete()
                        deleted_count += 1
                        if deleted_count >= amount:
                            break
                    except Exception:
                        pass
            await interaction.followup.send(f"Successfully deleted **{deleted_count}** message(s) sent by <@{target.id}>! ✨")
        else:
            deleted_count = 0
            left_to_delete = amount
            while left_to_delete > 0:
                fetch_amount = min(100, left_to_delete)
                messages = [m async for m in interaction.channel.history(limit=fetch_amount)]
                if not messages:
                    break
                deleted = await interaction.channel.purge(limit=len(messages), bulk=True)
                deleted_count += len(deleted)
                left_to_delete -= len(messages)
                if len(deleted) < len(messages):
                    break
            await interaction.followup.send(f"Successfully wiped **{deleted_count}** message(s) from the channel! ✨\n*(Note: Discord prevents wiping messages older than 14 days)*")
    except Exception as e:
        print(f"[DELETE ERROR] {e}")
        await interaction.followup.send("Whoops! Discord threw a weird error trying to delete those... 🥺")

@bot.tree.command(name="stats", description="Displays chat and voice activity statistics for a user!")
@app_commands.describe(target="The user to check stats for")
async def stats_cmd(interaction: discord.Interaction, target: Optional[discord.User] = None):
    await interaction.response.defer()
    usr = target or interaction.user
    embed = await build_user_stats_embed(usr, interaction.guild)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="serverinfo", description="Displays information about the current server!")
async def serverinfo_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title=f"{interaction.guild.name} | Server Info", color=discord.Color.from_rgb(43, 45, 49))
    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    embed.add_field(name="👑 Owner", value=f"<@{interaction.guild.owner_id}>", inline=True)
    embed.add_field(name="👥 Members", value=f"{interaction.guild.member_count}", inline=True)
    embed.add_field(name="📅 Created On", value=f"<t:{int(interaction.guild.created_at.timestamp())}:D>", inline=True)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="userinfo", description="Displays information about a user!")
@app_commands.describe(target="The user to check")
async def userinfo_cmd(interaction: discord.Interaction, target: Optional[discord.User] = None):
    usr = target or interaction.user
    member = interaction.guild.get_member(usr.id) if interaction.guild else None
    embed = discord.Embed(color=member.color if member else discord.Color.from_rgb(43, 45, 49))
    embed.set_author(name=usr.name, icon_url=usr.display_avatar.url)
    embed.add_field(name="ID", value=str(usr.id), inline=True)
    if member and member.joined_at:
        embed.add_field(name="Joined Server", value=f"<t:{int(member.joined_at.timestamp())}:R>", inline=True)
# -------- Welcome Event Listener -------- #
@bot.event
async def on_member_join(member: discord.Member):
    channel_id_str = os.getenv("WELCOME_CHANNEL_ID", "1530862954143023184")
    try:
        channel_id = int(channel_id_str)
    except ValueError:
        print(f"[WELCOME ERROR] Invalid WELCOME_CHANNEL_ID: {channel_id_str}")
        return

    channel = member.guild.get_channel(channel_id) or bot.get_channel(channel_id)
    if not channel:
        print(f"[WELCOME ERROR] Could not find channel with ID {channel_id}")
        return

    user_display_name = getattr(member, 'global_name', None) or getattr(member, 'display_name', None) or member.name
    server_name = os.getenv("SERVER_NAME", member.guild.name if member.guild else "Differential Degens")
    member_count = member.guild.member_count or 0

    avatar_url = member.display_avatar.with_format("png").with_size(512).url
    card_buf = await create_welcome_card(
        avatar_url=avatar_url,
        member_name=user_display_name,
        member_count=member_count,
        server_name=server_name
    )
    
    welcome_text = f"Welcome {member.mention} to **{server_name}** ❤️"
    
    if card_buf:
        card_buf.seek(0)
        file = discord.File(fp=card_buf, filename=f"welcome_{member.id}.png")
        try:
            await channel.send(content=welcome_text, file=file)
            print(f"[WELCOME SUCCESS] Welcome card sent for {member.name} in channel {channel_id}")
        except Exception as e:
            print(f"[WELCOME SEND ERROR] {e}")
    else:
        try:
            await channel.send(content=welcome_text)
            print(f"[WELCOME SUCCESS] Welcome message sent for {member.name} in channel {channel_id}")
        except Exception as e:
            print(f"[WELCOME SEND ERROR] {e}")

# -------- Giveaway System UI Views, Modals & Handlers -------- #


class JoinGiveawayModal(discord.ui.Modal, title="Giveaway Profile & Wallet Setup"):
    twitter = discord.ui.TextInput(label="Twitter Handle", placeholder="@yourhandle", required=False)
    telegram = discord.ui.TextInput(label="Telegram Handle", placeholder="@username", required=False)
    evm_wallet = discord.ui.TextInput(label="EVM Wallet Address (0x...)", placeholder="0x1234...5678", required=False)
    solana_wallet = discord.ui.TextInput(label="Solana Wallet Address", placeholder="Solana Wallet Public Key", required=False)

    def __init__(self, giveaway_id: str):
        super().__init__()
        self.giveaway_id = giveaway_id

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        g = giveaways.get(self.giveaway_id)
        if uid not in user_profiles:
            user_profiles[uid] = {
                "display_name": interaction.user.display_name,
                "username": interaction.user.name,
                "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            }
        
        if self.twitter.value: user_profiles[uid]["twitter"] = self.twitter.value.strip()
        if self.telegram.value: user_profiles[uid]["telegram"] = self.telegram.value.strip()
        if self.evm_wallet.value: user_profiles[uid]["evm_wallet"] = self.evm_wallet.value.strip()
        if self.solana_wallet.value: user_profiles[uid]["solana_wallet"] = self.solana_wallet.value.strip()
        save_user_profiles()

        # Validate required wallets if configured on giveaway
        if g:
            tasks = g.get("tasks", {})
            if tasks.get("require_evm") and not user_profiles[uid].get("evm_wallet"):
                await safe_respond(interaction, "❌ **EVM Wallet Address is required** to join this giveaway! Please fill in your EVM wallet (0x...).", ephemeral=True)
                return
            if tasks.get("require_solana") and not user_profiles[uid].get("solana_wallet"):
                await safe_respond(interaction, "❌ **Solana Wallet Address is required** to join this giveaway! Please fill in your Solana wallet.", ephemeral=True)
                return

        await register_giveaway_entry(interaction, self.giveaway_id)


def get_giveaway_task_lookup(g_data: dict) -> dict:
    """Build a flat {task_type: url/value} lookup from both flat keys and dynamic_tasks array."""
    tasks = g_data.get("tasks") or {}
    slinks = g_data.get("social_links") or {}
    lookup = {}
    # From dynamic_tasks array
    dyn = tasks.get("dynamic_tasks")
    if dyn and isinstance(dyn, list):
        for t in dyn:
            tt = t.get("type", "")
            tv = t.get("value", "").strip()
            if tt and tv:
                lookup[tt] = tv
    # Flat keys override
    for k in ["twitter_follow", "twitter_like", "twitter_retweet", "twitter_comment", "tiktok_follow", "youtube_follow", "discord_join", "discord_server"]:
        if tasks.get(k):
            lookup[k] = str(tasks[k]).strip()
    return lookup

def get_required_task_list(g_data: dict) -> list:
    """Return list of (task_type, label, url) for all required tasks in a giveaway."""
    lookup = get_giveaway_task_lookup(g_data)
    slinks = g_data.get("social_links") or {}
    required = []

    # Like & Retweet (combines twitter_like + twitter_retweet into one button)
    rt_url = (lookup.get("twitter_retweet") or lookup.get("twitter_like") or
              slinks.get("retweet_link") or slinks.get("tweet_link") or "").strip()
    if rt_url:
        if not rt_url.startswith(("http://", "https://")):
            rt_url = ""
    if "twitter_retweet" in lookup or "twitter_like" in lookup:
        required.append(("like_retweet", "Like & Retweet", rt_url))

    # Comment
    cm_url = (lookup.get("twitter_comment") or slinks.get("comment_link") or "").strip()
    if cm_url and not cm_url.startswith(("http://", "https://")):
        cm_url = ""
    if "twitter_comment" in lookup:
        required.append(("comment", "Comment on Tweet", cm_url))

    # Follow Twitter
    fw_val = lookup.get("twitter_follow", "")
    if fw_val:
        if fw_val.startswith(("http://", "https://")):
            fw_url = fw_val
        else:
            fw_url = f"https://x.com/{fw_val.lstrip('@')}"
        required.append(("follow_twitter", "Follow Twitter", fw_url))
    elif slinks.get("twitter_link"):
        required.append(("follow_twitter", "Follow Twitter", slinks["twitter_link"].strip()))

    # Discord Join Task
    if "discord_join" in lookup or "discord_server" in lookup:
        dc_val = (lookup.get("discord_join") or lookup.get("discord_server") or "").strip()
        dc_url = dc_val if dc_val.startswith(("http://", "https://")) else (slinks.get("discord_link", "").strip() or dc_val)
        required.append(("join_discord", "Join Discord Server", dc_url))

    # TikTok
    if "tiktok_follow" in lookup:
        tk_val = lookup["tiktok_follow"]
        tk_url = tk_val if tk_val.startswith(("http://","https://")) else f"https://www.tiktok.com/@{tk_val.lstrip('@')}"
        required.append(("follow_tiktok", "Follow TikTok", tk_url))

    # YouTube
    if "youtube_follow" in lookup:
        yt_val = lookup["youtube_follow"]
        yt_url = yt_val if yt_val.startswith(("http://","https://")) else ""
        required.append(("follow_youtube", "Subscribe YouTube", yt_url))

    # Custom Tasks (Feature 2: arbitrary task requirements)
    tasks = g_data.get("tasks") or {}
    dyn_tasks = tasks.get("dynamic_tasks", [])
    if isinstance(dyn_tasks, list):
        custom_idx = 0
        for t in dyn_tasks:
            tt = t.get("type", "").strip()
            if tt == "custom":
                custom_idx += 1
                c_label = t.get("label", "").strip() or f"Custom Task #{custom_idx}"
                c_url = t.get("value", "").strip()
                if c_url and not c_url.startswith(("http://", "https://")):
                    c_url = ""
                required.append((f"custom_{custom_idx}", c_label, c_url))

    return required


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id: str, web_url: str = ""):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

        # Row 0: Join + View Entry
        join_btn = discord.ui.Button(
            label="Join Giveaway",
            style=discord.ButtonStyle.primary,
            custom_id=f"join_giveaway_{giveaway_id}",
            row=0
        )
        join_btn.callback = self.join_giveaway_callback
        self.add_item(join_btn)

        view_btn = discord.ui.Button(
            label="View Your Entry",
            style=discord.ButtonStyle.secondary,
            custom_id=f"view_entry_{giveaway_id}",
            row=0
        )
        view_btn.callback = self.view_entry_callback
        self.add_item(view_btn)

        # Row 1: Task buttons — interactive (track clicks) + show link
        g_obj = giveaways.get(giveaway_id)
        if g_obj:
            required_tasks = get_required_task_list(g_obj)
            for task_type, label, url in required_tasks:
                btn = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"gtask_{task_type}_{giveaway_id}",
                    row=1
                )
                btn.callback = self._make_task_callback(task_type, url, label)
                self.add_item(btn)

            # Row 2: Social links (non-task, just helpful links)
            slinks = g_obj.get("social_links") or {}
            discord_url = (slinks.get("discord_link") or "").strip()
            if discord_url and discord_url.startswith(("http://", "https://")):
                self.add_item(discord.ui.Button(label="Join Discord", style=discord.ButtonStyle.link, url=discord_url, row=2))
            telegram_url = (slinks.get("telegram_link") or "").strip()
            if telegram_url and telegram_url.startswith(("http://", "https://")):
                self.add_item(discord.ui.Button(label="Telegram", style=discord.ButtonStyle.link, url=telegram_url, row=2))
            website_url = (slinks.get("website_link") or "").strip()
            if website_url and website_url.startswith(("http://", "https://")):
                self.add_item(discord.ui.Button(label="Website", style=discord.ButtonStyle.link, url=website_url, row=2))

    def _make_task_callback(self, task_type: str, url: str, label: str):
        giveaway_id = self.giveaway_id
        async def task_callback(interaction: discord.Interaction):
            uid = str(interaction.user.id)
            # Record this task as done
            if giveaway_id not in giveaway_task_progress:
                giveaway_task_progress[giveaway_id] = {}
            if uid not in giveaway_task_progress[giveaway_id]:
                giveaway_task_progress[giveaway_id][uid] = set()
            giveaway_task_progress[giveaway_id][uid].add(task_type)

            # Show the link to the user
            if url:
                await safe_respond(interaction, f"**{label}** — marked as done!\n\n**Complete the task here:** {url}", ephemeral=True)
            else:
                await safe_respond(interaction, f"**{label}** — marked as done!", ephemeral=True)
        return task_callback

    async def join_giveaway_callback(self, interaction: discord.Interaction):
        g_id = self.giveaway_id
        g = giveaways.get(g_id)
        if not g:
            await safe_respond(interaction, "Giveaway not found or has been removed.", ephemeral=True)
            return

        now = int(time.time())
        if not g.get("is_active", True) or g.get("ends_at", 0) <= now:
            await safe_respond(interaction, "This giveaway has already ended!", ephemeral=True)
            return

        uid = str(interaction.user.id)

        # ---- FEATURE 3: Role Requirement Verification (OR Logic) ----
        required_roles = g.get("tasks", {}).get("roles", [])
        if required_roles and isinstance(required_roles, list) and len(required_roles) > 0:
            member = interaction.user
            if not isinstance(member, discord.Member) and interaction.guild:
                member = interaction.guild.get_member(interaction.user.id)
            if isinstance(member, discord.Member) and hasattr(member, 'roles'):
                member_role_ids = {str(r.id) for r in member.roles}
                member_role_names = {r.name.lower() for r in member.roles}
                has_any = any(
                    str(rid) in member_role_ids or str(rid).lower() in member_role_names
                    for rid in required_roles
                )
                if not has_any:
                    role_mentions = []
                    for rid in required_roles:
                        rid_str = str(rid).strip()
                        if rid_str.isdigit():
                            role_mentions.append(f"<@&{rid_str}>")
                        else:
                            role_mentions.append(f"**{rid_str}**")
                    roles_text = "\n".join(f"  • {rm}" for rm in role_mentions)
                    await safe_respond(
                        interaction,
                        f"**Role Required!**\n\n"
                        f"You must have **at least ONE** of the following roles to enter:\n{roles_text}\n\n"
                        f"*Check the server's role-gating channels to earn a qualifying role.*",
                        ephemeral=True
                    )
                    return

        # 1. Check if user is already registered FIRST
        entries = giveaway_entries.get(g_id, [])
        existing = next((e for e in entries if isinstance(e, dict) and e.get("user_id") == uid), None)
        if existing:
            await safe_respond(interaction, f"You are already registered for **{g.get('title', 'Giveaway')}**!", ephemeral=True)
            return

        # 2. FIRST CLICK: Always show task list and do NOT join yet
        prompted_set = giveaway_join_prompted.setdefault(g_id, set())
        has_been_prompted = uid in prompted_set

        if not has_been_prompted:
            # Mark user as prompted so 2nd click will join them directly
            prompted_set.add(uid)

            required_tasks = get_required_task_list(g)
            if required_tasks:
                # ---- FEATURE 2: Show task completion status & wrap URLs in <> ----
                user_completed = giveaway_task_progress.get(g_id, {}).get(uid, set())
                lines = []
                for task_type, label, url in required_tasks:
                    is_done = task_type in user_completed
                    status_text = "[Done]" if is_done else "[ ]"
                    if url:
                        # Wrap in <> to disable Discord link preview embeds
                        lines.append(f"  {status_text} **{label}** — <{url}>")
                    else:
                        lines.append(f"  {status_text} **{label}**")
                tasks_text = "\n".join(lines)

                completed_count = sum(1 for tt, _, _ in required_tasks if tt in user_completed)
                total_count = len(required_tasks)

                await safe_respond(
                    interaction,
                    f"**Complete the following tasks to participate:**\n\n"
                    f"**Required Tasks ({completed_count}/{total_count} done):**\n{tasks_text}\n\n"
                    f"**If you have already done the tasks, click [Join Giveaway] again to confirm your entry!**",
                    ephemeral=True
                )
            else:
                await safe_respond(
                    interaction,
                    f"Click **[Join Giveaway]** one more time to confirm your entry!",
                    ephemeral=True
                )
            return

        # 3. SECOND CLICK: Join the giveaway directly
        # Profile & Wallet setup modal (if missing profile info or required wallets)
        prof = get_user_profile_fast(uid, interaction.user)
        tasks = g.get("tasks", {})
        req_evm = tasks.get("require_evm", False)
        req_solana = tasks.get("require_solana", False)

        missing_evm = req_evm and not prof.get("evm_wallet")
        missing_solana = req_solana and not prof.get("solana_wallet")
        has_profile = bool(prof.get("evm_wallet") or prof.get("solana_wallet") or prof.get("twitter") or prof.get("telegram"))

        if missing_evm or missing_solana or not has_profile:
            modal = JoinGiveawayModal(g_id)
            if prof.get("twitter"): modal.twitter.default = prof.get("twitter")
            if prof.get("telegram"): modal.telegram.default = prof.get("telegram")
            if prof.get("evm_wallet"): modal.evm_wallet.default = prof.get("evm_wallet")
            if prof.get("solana_wallet"): modal.solana_wallet.default = prof.get("solana_wallet")
            await interaction.response.send_modal(modal)
            return

        await register_giveaway_entry(interaction, g_id)

    async def view_entry_callback(self, interaction: discord.Interaction):
        g_id = self.giveaway_id
        g = giveaways.get(g_id)
        entries = giveaway_entries.get(g_id, [])
        uid = str(interaction.user.id)

        my_entry = next((e for e in entries if e.get("user_id") == uid), None)
        if not my_entry:
            await safe_respond(interaction, "❌ You have not entered this giveaway yet. Click **[Join Giveaway]** to enter!", ephemeral=True)
            return

        title_name = g.get('title', 'Giveaway') if g else 'Giveaway'
        embed = discord.Embed(title=f"📋 Your Entry Details for {title_name}", color=discord.Color.from_rgb(43, 92, 255))
        embed.add_field(name="Joined At", value=f"<t:{my_entry['joined_at']}:f>", inline=True)
        embed.add_field(name="Task Status", value=f"**{my_entry.get('task_status', 'pending').upper()}**", inline=True)
        if my_entry.get("winner_type"):
            embed.add_field(name="🏆 Result", value=f"**{my_entry['winner_type'].upper()} WINNER!**", inline=False)
        embed.add_field(name="EVM Wallet", value=f"`{my_entry.get('evm_wallet') or 'Not provided'}`", inline=False)
        embed.add_field(name="Solana Wallet", value=f"`{my_entry.get('solana_wallet') or 'Not provided'}`", inline=False)
        embed.add_field(name="Twitter", value=my_entry.get("twitter") or "Not provided", inline=True)
        embed.add_field(name="Telegram", value=my_entry.get("telegram") or "Not provided", inline=True)

        embed.set_footer(text="Powered by Arcie Bot")
        await safe_respond(interaction, embed=embed, ephemeral=True)


async def safe_respond(interaction: discord.Interaction, content: str = None, embed: discord.Embed = None, ephemeral: bool = True):
    """Safely respond to a Discord interaction preventing 40060 'Interaction already acknowledged' errors."""
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
        else:
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
    except Exception as e:
        print(f"[SAFE RESPOND WARN] {e}")
        try:
            await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        except Exception:
            pass


async def register_giveaway_entry(interaction: discord.Interaction, giveaway_id: str):
    g = giveaways.get(giveaway_id)
    if not g:
        await safe_respond(interaction, "❌ Giveaway not found.", ephemeral=True)
        return

    entries = giveaway_entries.setdefault(giveaway_id, [])
    uid = str(interaction.user.id)
    
    existing = next((e for e in entries if e["user_id"] == uid), None)
    if existing:
        await safe_respond(interaction, f"✅ You are already registered for **{g['title']}**!", ephemeral=True)
        return

    prof = user_profiles.get(uid, {})
    new_entry = {
        "user_id": uid,
        "username": str(interaction.user),
        "display_name": interaction.user.display_name,
        "joined_at": int(time.time()),
        "evm_wallet": prof.get("evm_wallet", ""),
        "solana_wallet": prof.get("solana_wallet", ""),
        "twitter": prof.get("twitter", ""),
        "telegram": prof.get("telegram", ""),
        "task_status": "verified",
        "winner_type": None
    }
    entries.append(new_entry)
    g["entries_count"] = len(entries)
    save_giveaways()
    save_giveaway_entries()

    # Respond to interaction FIRST so user gets instant confirmation and 0 errors!
    await safe_respond(interaction, f"🎉 **Success!** You have joined **{g['title']}**!", ephemeral=True)

    # Background task to refresh live Discord channel embed
    asyncio.create_task(update_giveaway_discord_message(giveaway_id))


async def delete_giveaway_permanently(g_id: str) -> bool:
    """Permanently delete a giveaway from memory, disk, Firebase Cloud DB, and Discord."""
    if not g_id:
        return False
    g_id_str = str(g_id).strip()
    
    # 1. Resolve giveaway if passed as message ID, link, or alias
    resolved = await resolve_giveaway_by_identifier(g_id_str)
    resolved_id = resolved.get("id") if (resolved and isinstance(resolved, dict)) else g_id_str

    # 2. Pop from in-memory dictionary
    g = giveaways.pop(resolved_id, None)
    if not g and resolved_id != g_id_str:
        g = giveaways.pop(g_id_str, None)
    if not g:
        for k, v in list(giveaways.items()):
            if isinstance(v, dict) and (v.get("id") == resolved_id or str(v.get("message_id")) == g_id_str):
                g = giveaways.pop(k, None)
                resolved_id = k
                break

    # 3. Pop entries
    giveaway_entries.pop(resolved_id, None)
    giveaway_entries.pop(g_id_str, None)

    # 4. Mark permanently in deleted_giveaways set & save locally
    deleted_giveaways.add(str(resolved_id))
    if g_id_str != resolved_id:
        deleted_giveaways.add(str(g_id_str))
    save_deleted_giveaways()
    save_giveaways()
    save_giveaway_entries()

    # 5. Delete from Firebase Cloud DB
    if FIREBASE_URL:
        try:
            await firebase_put(f"giveaways/{resolved_id}", None)
            await firebase_put(f"giveaway_entries/{resolved_id}", None)
            if g_id_str != resolved_id:
                await firebase_put(f"giveaways/{g_id_str}", None)
                await firebase_put(f"giveaway_entries/{g_id_str}", None)
        except Exception as fe:
            print(f"[FIREBASE DELETE ERROR] {fe}")

    # 6. Delete Discord Message from channel
    if g and isinstance(g, dict) and g.get("channel_id") and g.get("message_id"):
        try:
            ch_id_clean = re.sub(r'[^0-9]', '', str(g.get("channel_id", "")))
            msg_id_clean = re.sub(r'[^0-9]', '', str(g.get("message_id", "")))
            if ch_id_clean and msg_id_clean:
                ch = bot.get_channel(int(ch_id_clean))
                if not ch:
                    ch = await bot.fetch_channel(int(ch_id_clean))
                if ch:
                    msg = await ch.fetch_message(int(msg_id_clean))
                    if msg:
                        await msg.delete()
                        print(f"[DELETE DISCORD MSG] Deleted giveaway message {msg_id_clean} in channel {ch_id_clean}")
        except Exception as de:
            print(f"[DELETE DISCORD MSG ERROR] {de}")

    print(f"[PERMANENT DELETE] Giveaway '{resolved_id}' permanently deleted from memory, disk, Firebase & Discord.")
    return True


async def cleanup_expired_giveaways_40_days():
    """Automatically delete giveaways older than 40 days (40 * 86400s) from database, disk & Firebase."""
    now = int(time.time())
    cutoff_age_seconds = 40 * 86400  # 40 days (3,456,000 seconds)
    to_delete = []

    for gid, g in list(giveaways.items()):
        if not isinstance(g, dict):
            continue
        created_at = int(g.get("created_at") or 0)
        ends_at = int(g.get("ends_at") or 0)
        timestamp = created_at if created_at > 0 else ends_at

        if timestamp > 0 and (now - timestamp) >= cutoff_age_seconds:
            title = g.get("title", gid)
            age_days = (now - timestamp) / 86400
            to_delete.append((gid, title, age_days))

    if to_delete:
        print(f"[AUTO-PURGE 40 DAYS] Found {len(to_delete)} giveaways older than 40 days:")
        for gid, title, age_days in to_delete:
            print(f"  -> Auto-purging giveaway '{title}' ({gid}) - age: {age_days:.1f} days old...")
            await delete_giveaway_permanently(gid)
        print(f"[AUTO-PURGE 40 DAYS] Cleaned up {len(to_delete)} giveaways from DB and Website.")


async def resolve_giveaway_by_identifier(identifier: str) -> Optional[dict]:
    """Resolve a giveaway object using Giveaway ID, Discord Message ID, or Discord Message Link."""
    if not identifier:
        return None
    clean_str = str(identifier).strip()
    if not clean_str:
        return None

    # Extract ID if a Discord Message/Channel URL was passed
    url_match = re.search(r'/(\d{17,20})$', clean_str)
    search_id = url_match.group(1) if url_match else re.sub(r'[^0-9a-zA-Z_-]', '', clean_str)

    # If already recorded as deleted, return None immediately
    if search_id in deleted_giveaways:
        return None

    # 1. Direct lookup by Giveaway ID in memory
    if search_id in giveaways and isinstance(giveaways[search_id], dict) and search_id not in deleted_giveaways:
        return giveaways[search_id]

    # 2. Search by message_id or id in memory
    for g_id, g in giveaways.items():
        if g_id in deleted_giveaways:
            continue
        if isinstance(g, dict):
            if str(g.get("message_id", "")).strip() == search_id or str(g.get("id", "")).strip() == search_id:
                return g

    # 3. Search Firebase giveaways Cloud DB
    if FIREBASE_URL:
        try:
            fb_g = await firebase_get(f"giveaways/{search_id}")
            if fb_g and isinstance(fb_g, dict) and search_id not in deleted_giveaways:
                giveaways[search_id] = fb_g
                return fb_g

            fb_all = await firebase_get("giveaways")
            if fb_all and isinstance(fb_all, dict):
                for g_id, g_data in fb_all.items():
                    if g_id in deleted_giveaways:
                        continue
                    if isinstance(g_data, dict):
                        giveaways[g_id] = g_data
                        if str(g_data.get("message_id", "")).strip() == search_id or str(g_data.get("id", "")).strip() == search_id:
                            return g_data
        except Exception:
            pass

    return None


async def update_giveaway_discord_message(giveaway_id: str):
    g = await resolve_giveaway_by_identifier(giveaway_id)
    if not g or not isinstance(g, dict):
        print(f"[UPDATE EMBED FAIL] Giveaway '{giveaway_id}' not found in memory or Cloud DB.")
        return
    giveaway_id = g.get("id", giveaway_id)

    # Resolve target channel
    ch_id_clean = re.sub(r'[^0-9]', '', str(g.get("channel_id", "")))
    channel = None
    if ch_id_clean:
        try:
            channel = bot.get_channel(int(ch_id_clean))
            if not channel:
                channel = await bot.fetch_channel(int(ch_id_clean))
        except Exception:
            channel = None

    if not channel and bot.guilds:
        for guild in bot.guilds:
            channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
            if channel:
                break

    if not channel:
        print(f"[UPDATE EMBED FAIL] Could not find valid channel for giveaway '{giveaway_id}'")
        return

    port = os.getenv("PORT", "2025")
    domain = os.getenv("APP_URL", f"http://n5.nexcloud.in:{port}")
    embed, file_to_send = build_giveaway_embed(g)
    view = GiveawayView(giveaway_id, domain)
    mention_text = format_role_mention(g.get("mention_role"))

    msg = None
    msg_id_clean = re.sub(r'[^0-9]', '', str(g.get("message_id", "")))
    if msg_id_clean:
        try:
            msg = await channel.fetch_message(int(msg_id_clean))
        except Exception as fe:
            print(f"[UPDATE EMBED FETCH] Old message {msg_id_clean} not found in #{channel.name}: {fe}")
            msg = None

    if msg:
        try:
            kwargs = {"content": mention_text, "embed": embed, "view": view}
            if file_to_send:
                kwargs["attachments"] = [file_to_send]
            await msg.edit(**kwargs)
            print(f"[UPDATE EMBED SUCCESS] In-place edited Discord embed for '{g.get('title')}' in #{channel.name} (preserved sent timestamp)")
            return
        except Exception as edit_err:
            print(f"[UPDATE EMBED EDIT FAIL] {edit_err}, re-posting fresh message...")

    # Post fresh message if old message was missing or failed to edit
    try:
        if file_to_send:
            new_msg = await channel.send(content=mention_text, embed=embed, view=view, file=file_to_send)
        else:
            new_msg = await channel.send(content=mention_text, embed=embed, view=view)

        g["message_id"] = str(new_msg.id)
        g["channel_id"] = str(channel.id)
        giveaways[giveaway_id] = g
        save_giveaways()
        await firebase_put(f"giveaways/{giveaway_id}", g)
        bot.add_view(view, message_id=new_msg.id)
        print(f"[UPDATE EMBED POST SUCCESS] Posted fresh embed for '{g.get('title')}' in #{channel.name} ({new_msg.id})")
    except Exception as send_err:
        print(f"[UPDATE EMBED POST ERROR] {send_err}")


# ======================================================================== #
# ========== FEATURE 1: STORY-DRIVEN RUMBLE ROYALE MINIGAME ENGINE ======= #
# ======================================================================== #

# -------- THEMATIC STORY-DRIVEN RUMBLE ROYALE MINIGAME ENGINE -------- #
RUMBLE_THEMES = {
    "modern": {
        "name": "Modern & Urban Memes",
        "emoji": "🏙️",
        "deaths": [
            "{killer} ran {player1} over with a stolen Tesla on Autopilot.",
            "{killer} sniped {player1} while they were busy filming a TikTok dance.",
            "{killer} exposed {player1}'s search history in the group chat — instant vaporization.",
            "{killer} replaced {player1}'s iced latte with pure liquid nitrogen.",
            "{killer} cancelled {player1} on Twitter so hard they ceased to exist.",
            "{killer} air-dropped an unskippable 10-hour podcast directly into {player1}'s brain.",
            "{killer} pushed {player1} into an open manhole during rush hour.",
            "{killer} hacked {player1}'s smart fridge and froze them into a popsicle.",
            "{killer} disconnected {player1}'s Wi-Fi during a clutch 1v1 — heart stopped.",
            "{killer} hit {player1} with an electric scooter going 45 mph.",
            "{killer} tricked {player1} into clicking a suspicious phishing link — drained from reality.",
            "{killer} trapped {player1} in an IKEA maze. They were never seen again.",
            "{killer} dropped a 50lb dumbbell on {player1} during leg day.",
            "{killer} roasted {player1} in a rap battle so badly their soul left their body.",
            "{killer} convinced {player1} to drink 12 cans of Monster Energy at once.",
            "{killer} challenged {player1} to a microwave burrito duel — third-degree burns.",
            "{killer} locked {player1} in a room playing baby shark on infinite loop.",
            "{killer} hit {player1} in the face with a frozen pizza like a frisbee.",
            "{killer} sent a drone strike loaded with glitter bombs directly at {player1}.",
            "{killer} flashbanged {player1} with light mode Discord at 3 AM.",
            "{player1} slipped on a puddle of spilled Boba tea and cracked the space-time continuum.",
            "{player1} stepped on a hidden Lego brick barefoot and ascended to heaven.",
            "{player1} walked into a glass sliding door at full speed.",
            "{player1} tried to microwave aluminum foil. BOOM.",
            "{player1} choked on a single potato chip while binge-watching Netflix.",
            "{player1} forgot to breathe while doom-scrolling Twitter at 4 AM.",
            "{player1} tried to pet a wild raccoon behind the dumpster. The raccoon won.",
            "{player1} got taken out by a rogue automated Roomba with a taped kitchen knife.",
            "{player1} fell asleep in a tanning bed and turned into extra crispy bacon.",
            "{player1} tried to parallel park between two semi-trucks. Fatal error.",
            "{player1} drank water from a suspicious fountain in the subway station.",
            "{player1} sneezed so violently they got launched through a second-story window.",
            "{player1} accidentally joined a Zoom call without muting — died of embarrassment.",
            "{player1} tried to fight their reflection in a department store mirror.",
            "{player1} was crushed by an avalanche of Amazon delivery boxes.",
            "{player1} tried to jump the subway turnstile and got stuck upside down forever.",
            "{player1} ate a 3-week-old taco found behind the couch. RIP.",
            "{player1} got tangled in 100 meters of wired headphone cables and suffocated.",
            "{player1} attempted a parkour stunt off a dumpster and faceplanted concrete.",
            "{player1} tried to charge their phone in a microwave. Results were explosive.",
        ],
        "survival": [
            "{player1} found a functioning 100% battery power bank and full 5G signal.",
            "{player1} dodged an attack thanks to active noise-cancelling AirPods.",
            "{player1} ordered an emergency Uber Comfort and escaped the danger zone.",
            "{player1} hid inside a cardboard Amazon Prime box and went unnoticed.",
            "{player1} chugged an iced matcha latte and gained ultra-instinct speed.",
            "{player1} used a tactical ring light to blind approaching enemies.",
            "{player1} set up a fake Wi-Fi hotspot to lure enemies away.",
            "{player1} found an unopened box of glazed donuts in the breakroom.",
            "{player1} survived the night by binge-watching survival tutorials on YouTube.",
            "{player1} put on high-visibility construction vest — everyone ignored them.",
        ],
        "revives": [
            "✨ **CLOUD BACKUP!** **{player1}** restored their life from an iCloud backup!",
            "✨ **DEFIBRILLATOR!** Paramedics arrived with 500 volts — **{player1}** is ALIVE!",
            "✨ **COLD BREW REBIRTH!** An intravenous drip of iced coffee revived **{player1}**!",
            "✨ **RESPAWN!** **{player1}** bought an extra life with microtransactions!",
            "✨ **MIRACLE!** **{player1}** survived because the hit didn't register on 200ms ping!",
        ],
        "phases": [
            "🩸 THE RUSH HOUR BLOODBATH",
            "🌅 DAY 1 — COMMUTE OF CHAOS",
            "🌙 NIGHT 1 — MIDNIGHT DOORDASH",
            "🌋 ARENA HAZARD — SERVER ROOM FIRE",
            "🌅 DAY 2 — OFFICE WARFARE",
            "🌙 NIGHT 2 — LATE NIGHT BINGE",
            "⚡ ARENA HAZARD — WI-FI BLACKOUT",
            "🌅 DAY 3 — VIRAL CLOUT WAR",
            "🌙 NIGHT 3 — GHOSTED IN THE DARK",
            "☕ THE COFFEE RUN SUPPLY DROP",
            "🌅 DAY 4 — CANCEL CULTURE CLASH",
            "🌙 NIGHT 4 — SCREAMS IN THE SUBWAY",
            "📱 ARENA HAZARD — SCREEN TIME OVERDOSE",
            "🌅 DAY 5 — SMARTPHONE SURVIVAL",
            "🌙 NIGHT 5 — TWILIGHT IN THE ALLEY",
            "🔥 ARENA HAZARD — TESLA AUTO-PILOT MALFUNCTION",
            "🌅 DAY 6 — GYM BRO SHOWDOWN",
            "🌙 NIGHT 6 — ALL-NIGHTER PANIC",
            "🌪️ ARENA HAZARD — BLACK FRIDAY STAMPEDE",
            "🌅 DAY 7 — SIDE HUSTLE ROYALE",
            "🌙 NIGHT 7 — URBAN MYTHS",
            "☣️ ARENA HAZARD — ENERGY DRINK SPILL",
            "🌅 DAY 8 — THE METRO MAYHEM",
            "🌙 NIGHT 8 — LAST CALL MADNESS",
            "☠️ SUDDEN DEATH — EVICTION NOTICE",
            "🌅 DAY 9 — THE STREET BRAWL",
            "🌙 NIGHT 9 — DARK CITY GLORY",
            "🔥 ARENA HAZARD — AIR FRYER EXPLOSION",
            "🌅 DAY 10 — THE FINAL DEADLINE",
            "🌙 NIGHT 10 — END OF THE LEASE",
        ],
        "final_phase": "⚔️ THE METROPOLIS SHOWDOWN — FINAL 1V1"
    },
    "japanese": {
        "name": "Japanese Anime & Samurai Lore",
        "emoji": "⛩️",
        "deaths": [
            "{killer} drew a legendary folded Nippon katana and sliced {player1} in 0.001 seconds.",
            "{killer} trapped {player1} in an endless anime filler beach episode — soul evaporated.",
            "{killer} executed the forbidden '1,000 Years of Death' jutsu on {player1}.",
            "{killer} shouted a 3-minute powerup attack monologue and vaporized {player1}.",
            "{killer} summoned a giant mecha robot and stomped {player1} into a pancake.",
            "{killer} hit {player1} with Truck-kun — {player1} was instantly isekai'd to another world.",
            "{killer} overpowered {player1} using the undisputed, unstoppable 'Power of Friendship'.",
            "{killer} forced {player1} to eat a lethal dumpling infused with 100% pure wasabi paste.",
            "{killer} sliced {player1}'s shadow — {player1} split in half 5 seconds later.",
            "{killer} activated a Death Note and wrote {player1}'s name in bold calligraphy.",
            "{killer} roundhouse kicked {player1} straight through a traditional paper shoji screen.",
            "{killer} threw a flurry of poisoned shurikens into {player1}'s armor gap.",
            "{killer} challenged {player1} to a spicy ramen speed-eating duel — stomach exploded.",
            "{killer} summoned a nine-tailed spirit fox that devoured {player1} whole.",
            "{killer} sliced {player1}'s bamboo staff and followed up with a sonic boom chop.",
            "{killer} blinded {player1} with cherry blossom petals before striking the fatal blow.",
            "{killer} unleashed an almighty Domain Expansion — {player1} had no counter-measure.",
            "{killer} used substitution jutsu with a log right as {player1} ran off a cliff.",
            "{killer} punched {player1} so hard they shattered the anime animation budget.",
            "{killer} placed an explosive seal paper bomb under {player1}'s tatami mat.",
            "{player1} committed accidental seppuku while trying to open a can of tuna with a wakizashi.",
            "{player1} tried to run like Naruto with arms back and tripped face-first down Mount Fuji.",
            "{player1} got distracted by a cute cat anime girl and walked straight into a spike pit.",
            "{player1} took a bite of a poisonous blowfish (fugu) prepared by an amateur chef.",
            "{player1} shouted 'NANI?!' so loud their vocal cords snapped and they collapsed.",
            "{player1} challenged an elderly master with a broom — master one-shotted them.",
            "{player1} fell into a steaming hot spring filled with boiling volcanic magma.",
            "{player1} opened an ancient sealed scroll and unleashed an angry sealed demon.",
            "{player1} tried to catch a speeding katana blade barehanded. Did not work.",
            "{player1} got trampled by a stampede of enraged wild tanukis.",
            "{player1} forgot to eat their daily onigiri rice ball and ran out of chakra.",
            "{player1} tried to deflect a laser beam with a wooden soup bowl.",
            "{player1} stepped on a caltrop trap hidden in the tall bamboo forest.",
            "{player1} tried to power up for 45 minutes straight and suffered a brain aneurysm.",
            "{player1} got swallowed by Godzilla during a casual morning stroll in Tokyo.",
            "{player1} tried to tame a wild samurai horse — got kicked into orbit.",
            "{player1} ate 50 pieces of poisonous fugu sushi on a bet.",
            "{player1} stared directly into an enchanted mirror and turned into an anime plushie.",
            "{player1} lost their way in the haunted Aokigahara mist and faded away.",
            "{player1} attempted forbidden shadow clone technique without sufficient mana.",
        ],
        "survival": [
            "{player1} remembered an emotional childhood flashback and gained a 300% strength boost.",
            "{player1} ate a hot bowl of tonkotsu ramen and fully restored their health and spirit.",
            "{player1} swapped places with a wooden log using substitution jutsu just in time.",
            "{player1} found a rare Senzu Bean and instantly healed all mortal wounds.",
            "{player1} meditated under a freezing waterfall and unlocked ultra sensory awareness.",
            "{player1} disguised themselves as a humble tea merchant and bypassed danger.",
            "{player1} found a legendary dragon-forged helmet inside an abandoned shrine.",
            "{player1} forged a blood oath alliance with a wandering ronin warrior.",
            "{player1} caught a flying ninja star out of mid-air with chopsticks.",
            "{player1} blended seamlessly into a field of blooming cherry blossoms.",
        ],
        "revives": [
            "✨ **EDO TENSEI!** Dark talisman seals glow — **{player1}** is reanimated from the ashes!",
            "✨ **DRAGON BALLS!** Shenron granted a wish — **{player1}** is brought back to life!",
            "✨ **PLOT ARMOR!** The anime author gave **{player1}** plot armor — they SURVIVE!",
            "✨ **ISEKAI REVERSE!** **{player1}** rejected the afterlife and respawned into the arena!",
            "✨ **PHOENIX REBIRTH!** The legendary Suzaku flame ignites — **{player1}** rises anew!",
        ],
        "phases": [
            "⛩️ SAKURA BLOODBATH — THE TOURNAMENT COMMENCES",
            "🌅 DAY 1 — TRAINING ARC BEGINS",
            "🌙 NIGHT 1 — SHADOW OF THE NINJA",
            "🌋 ARENA HAZARD — VOLCANIC KAIJU ROAR",
            "🌅 DAY 2 — SHOGUN'S RECKONING",
            "🌙 NIGHT 2 — MOONLIT BUSHIDO",
            "⚡ ARENA HAZARD — ANIME LIGHTNING BLITZ",
            "🌅 DAY 3 — MECHA OVERDRIVE",
            "🌙 NIGHT 3 — SPIRITS OF THE SHRINE",
            "🍜 THE RAMEN FEAST CRATE",
            "🌅 DAY 4 — FINAL FORM UNLOCKED",
            "🌙 NIGHT 4 — SENPAI'S WRATH",
            "🩸 BLOOD MOON — YOKAI FRENZY",
            "🌅 DAY 5 — KATANA CROSSROADS",
            "🌙 NIGHT 5 — SILENT KUNAI WHISPERS",
            "❄️ ARENA HAZARD — MOUNT FUJI BLIZZARD",
            "🌅 DAY 6 — POWER LEVEL OVER 9000",
            "🌙 NIGHT 6 — MIDNIGHT RONIN",
            "🌪️ ARENA HAZARD — CHERRY BLOSSOM TYPHOON",
            "🌅 DAY 7 — THE DOJO ELIMINATION",
            "🌙 NIGHT 7 — CLAN WARFARE",
            "☣️ ARENA HAZARD — WASABI TOXIC MIST",
            "🌅 DAY 8 — ANIME BETRAYAL",
            "🌙 NIGHT 8 — EVE OF HONOUR",
            "☠️ SUDDEN DEATH — COLLAPSING MECHA HANGAR",
            "🌅 DAY 9 — TSUNDERE GAUNTLET",
            "🌙 NIGHT 9 — THE LAST SAMURAI SHADOW",
            "🔥 ARENA HAZARD — GODZILLA ATOMIC BREATH",
            "🌅 DAY 10 — THE ULTIMATE JUTSU",
            "🌙 NIGHT 10 — TWILIGHT OF GLORY",
        ],
        "final_phase": "⚔️ THE SHOGUN'S DUEL — FINAL 1V1 SHOWDOWN"
    },
    "european": {
        "name": "European Medieval & Comic Knights",
        "emoji": "🏰",
        "deaths": [
            "{killer} launched {player1} over the castle walls with a 90-meter counterweight trebuchet.",
            "{killer} beat {player1} to death with a 3-day-old rock-hard French baguette.",
            "{killer} challenged {player1} to a formal joust — lance went straight through the chest.",
            "{killer} released the vicious Killer Rabbit of Caerbannog upon {player1}.",
            "{killer} pushed {player1} into the castle moat infested with hungry pikes and leeches.",
            "{killer} dropped a massive iron portcullis directly onto {player1}.",
            "{killer} insulted {player1}'s mother with severe French mockery — emotional death.",
            "{killer} challenged {player1} to a drinking duel with 90% proof mead. Liver gave out.",
            "{killer} poured a cauldron of boiling hot soup from the ramparts onto {player1}.",
            "{killer} beheaded {player1} on the public guillotine for treason against the realm.",
            "{killer} challenged {player1} to a duel of honour and struck before the glove hit the ground.",
            "{killer} locked {player1} inside the castle torture dungeon with the iron maiden.",
            "{killer} set fire to {player1}'s thatch-roof cottage while they were asleep inside.",
            "{killer} shot {player1} through an arrow slit with a heavy English longbow.",
            "{killer} convinced the royal court that {player1} was a witch — burnt at the stake.",
            "{killer} struck {player1} with a spiked flail right through their steel plate helmet.",
            "{killer} challenged {player1} to a sword fight, but pulled out a hidden crossbow.",
            "{killer} pushed {player1} into a giant vat of fermenting grape wine. Drowned happy.",
            "{killer} dropped a church bell from the bell tower onto {player1}.",
            "{killer} banished {player1} into the dark Bavarian forest inhabited by bloodthirsty wolves.",
            "{player1} fell off the drawbridge while trying to do a theatrical knight salute.",
            "{player1} got stuck inside their heavy steel armor during a rainstorm and rusted solid.",
            "{player1} tried to pet a fire-breathing dragon. Dragon was not in a petting mood.",
            "{player1} ate a wheel of unpasteurized medieval cheese from the 14th century.",
            "{player1} tripped over their oversized royal cape and plummeted down the spiral staircase.",
            "{player1} tried to pull the sword from the stone — herniated three discs and perished.",
            "{player1} got kicked in the chest by an armored warhorse at full gallop.",
            "{player1} mistook toxic belladonna berries for sweet grapes at the royal feast.",
            "{player1} tried to parry a cannonball with a small wooden buckler shield.",
            "{player1} got lost in the royal hedge maze and starved to death 20 feet from the exit.",
            "{player1} tried to swim across the English Channel in full plate armor. Sunk like an anchor.",
            "{player1} was pecked to pieces by a flock of aggressive carrier pigeons.",
            "{player1} challenged the Black Knight — learned the hard way 'tis NOT just a scratch.",
            "{player1} danced the medieval jig so vigorously they suffered cardiac arrest.",
            "{player1} dropped their holy relic into a bottomless well and jumped in after it.",
            "{player1} ate a bowl of court jester's mystery soup. It contained hemlock.",
            "{player1} got crushed under a collapsing wooden siege tower.",
            "{player1} was charged by an angry wild boar during the royal hunt.",
            "{player1} tried to climb the slippery ivy wall of the fortress and slipped.",
            "{player1} drank water downstream from the castle latrine. Cholera speedrun.",
        ],
        "survival": [
            "{player1} found the Holy Hand Grenade of Antioch and kept the beasts at bay.",
            "{player1} took refuge in the tavern cellar with three casks of fine elderberry wine.",
            "{player1} shielded themselves with a solid wheel of 5-year aged parmesan cheese.",
            "{player1} received a royal pardon sealed by the King's golden signet ring.",
            "{player1} discovered a secret underground passage connecting the dungeons to safety.",
            "{player1} donned a full suit of gilded knight armor with +50 defense.",
            "{player1} befriended a wandering troubadour who sang songs of their glory.",
            "{player1} found a roasted turkey leg in the banquet hall and restored full vigor.",
            "{player1} successfully climbed into the fortress bell tower and watched enemies pass.",
            "{player1} bribed the castle guards with a pouch of shiny silver florins.",
        ],
        "revives": [
            "✨ **'TIS BUT A SCRATCH!** **{player1}** stood right back up! Just a flesh wound!",
            "✨ **HOLY BLESSING!** The Archbishop cast a miraculous resurrection chant on **{player1}**!",
            "✨ **ALCHEMIST ELIXIR!** The royal court alchemist poured philosopher's stone liquid on **{player1}**!",
            "✨ **DIVINE INTERVENTION!** Saint George intervened — **{player1}** rises from death!",
            "✨ **ROYAL RESURRECTION!** The King decrees that **{player1}** is NOT allowed to die yet!",
        ],
        "phases": [
            "🏰 SIEGE OF THE BASTILLE — BLOODBATH",
            "🌅 DAY 1 — MARCH OF THE CRUSADERS",
            "🌙 NIGHT 1 — CASTLE DUNGEON SCREAMS",
            "🌋 ARENA HAZARD — CATAPULT VOLLEY",
            "🌅 DAY 2 — THE GRAND TOURNAMENT",
            "🌙 NIGHT 2 — WHISPERS IN THE TAVERN",
            "⚡ ARENA HAZARD — WITCH'S CURSE",
            "🌅 DAY 3 — THE GUILLOTINE GAUNTLET",
            "🌙 NIGHT 3 — BLACK KNIGHT'S AMBUSH",
            "🍗 THE ROYAL BANQUET CRATE",
            "🌅 DAY 4 — PEASANT REBELLION",
            "🌙 NIGHT 4 — SHADOWS OF THE CATHEDRAL",
            "🩸 BLOOD MOON — WEREWOLF SIEGE",
            "🌅 DAY 5 — KNIGHTS OF THE ROUND TABLE",
            "🌙 NIGHT 5 — MIDNIGHT AT THE MOAT",
            "❄️ ARENA HAZARD — ALPINE GLACIAL FROST",
            "🌅 DAY 6 — THE DRAWBRIDGE CLASH",
            "🌙 NIGHT 6 — ASSASSINS IN THE TAVERN",
            "🌪️ ARENA HAZARD — PLAGUE WIND",
            "🌅 DAY 7 — JOUSTING ROYALE",
            "🌙 NIGHT 7 — THE KING'S GAMBIT",
            "☣️ ARENA HAZARD — ROTTEN CHEESE FOG",
            "🌅 DAY 8 — REVOLUTION DAWN",
            "🌙 NIGHT 8 — TAVERN BRAWL",
            "☠️ SUDDEN DEATH — COLLAPSING FORTRESS",
            "🌅 DAY 9 — THE MONARCH'S WRATH",
            "🌙 NIGHT 9 — THE LAST BASTION",
            "🔥 ARENA HAZARD — DRAGON BREATH INFERNO",
            "🌅 DAY 10 — CHIVALRIC SHOWDOWN",
            "🌙 NIGHT 10 — DUSK OF THE EMPIRE",
        ],
        "final_phase": "⚔️ THE CROWN JOUST — FINAL 1V1 DUEL"
    },
    "web3_ancient": {
        "name": "Web3 Crypto & Ancient Colosseum",
        "emoji": "🏛️",
        "deaths": [
            "{killer} rug-pulled {player1}'s entire existence. {player1} is now worth $0.00.",
            "{killer} fed {player1} to the Roman colosseum lions for the entertainment of Caesar.",
            "{killer} minted {player1} into a worthless JPEG and burned the smart contract.",
            "{killer} liquidated {player1}'s 100x leveraged long position on Bitcoin. Instant rekt.",
            "{killer} flash-loaned {player1}'s soul and drained the liquidity pool.",
            "{killer} impaled {player1} with a gladiator trident and cast them into the fire pit.",
            "{killer} sent a phishing transaction to {player1}'s MetaMask — wallet and life drained.",
            "{killer} trapped {player1} inside an un-audited smart contract loop forever.",
            "{killer} struck {player1} with Zeus's golden thunderbolt from Mount Olympus.",
            "{killer} crushed {player1} under a falling marble Roman statue of Apollo.",
            "{killer} sandwiched {player1}'s trade with MEV sniper bots. Total annihilation.",
            "{killer} pushed {player1} into a pit of hungry gladiatorial vipers.",
            "{killer} threw {player1} out of a chariot moving at supersonic speed.",
            "{killer} hacked the cross-chain bridge {player1} was crossing. Sunk to the bottom.",
            "{killer} convinced {player1} to ape their life savings into $LUNA 3.0. Dead on arrival.",
            "{killer} slashed {player1}'s validator node and confiscated their life force.",
            "{killer} fed {player1} to the mythical Minotaur in the depths of the labyrinth.",
            "{killer} sold {player1}'s private seed phrase on the dark web.",
            "{killer} hit {player1} in the face with a heavy stone Spartan shield.",
            "{killer} initiated a 51% attack on {player1}'s brainwaves.",
            "{player1} paid 14 ETH in gas fees for a $2 transaction and suffered heart failure.",
            "{player1} lost their 24-word seed phrase and evaporated into thin air.",
            "{player1} tried to pet a Roman colosseum tiger. Tiger had high slippage.",
            "{player1} aped into a memecoin called $SCAM and got exit-scammed in 4 seconds.",
            "{player1} fell off the top row of the colosseum while checking portfolio on phone.",
            "{player1} drank water from an ancient lead Roman aqueduct. Heavy metal poisoning.",
            "{player1} staked their tokens on an anonymous dev's farm. Dev moved to Bahamas.",
            "{player1} tried to wrestle a Spartan warrior while wearing sandals.",
            "{player1} connected cold wallet to a shady Discord DM link. Drained instantly.",
            "{player1} got struck by lightning while praying to the Crypto Gods for a pump.",
            "{player1} paper-handed at the exact bottom of the red candle and perished from grief.",
            "{player1} tried to jump across the chariot track and got run over by 4 teams.",
            "{player1} forgot to approve token allowance and got stuck in limbo.",
            "{player1} ate ancient Roman fermented fish sauce (Garum) that was 2,000 years expired.",
            "{player1} tried to arbitrage between Uniswap and Sushiswap and lost everything.",
            "{player1} got crushed under a mountain of worthless physical Bitcoin tokens.",
            "{player1} looked directly into Medusa's eyes while trading on DexScreener.",
            "{player1} sent 10 BTC to a burner address by typing one wrong letter.",
            "{player1} got tackled by an armored gladiator right into the spiked arena wall.",
            "{player1} tried to short Ethereum right before the biggest green candle in history.",
        ],
        "survival": [
            "{player1} held with DIAMOND HANDS 💎 through the crash and emerged unscathed.",
            "{player1} was protected by Athena's golden aegis shield.",
            "{player1} stored their vital essence inside a cold storage Ledger hardware wallet.",
            "{player1} received an unexpected 100,000 token airdrop from an ancient whale.",
            "{player1} took shelter in the VIP box with Emperor Caesar and drank nectar.",
            "{player1} bypassed the gas war by using a private flashbot bundle.",
            "{player1} found a legendary Spartan spear buried beneath the arena sand.",
            "{player1} dodged the liquidation wick with 0.01% health remaining.",
            "{player1} was blessed by Hermes with winged sandals for maximum agility.",
            "{player1} diversified into gold and survived the bear market storm.",
        ],
        "revives": [
            "✨ **HARD FORK!** The blockchain rolled back the state — **{player1}** is RESTORED!",
            "✨ **LAZARUS PROTOCOL!** **{player1}** executed a smart recovery contract and lives!",
            "✨ **BUY THE DIP!** **{player1}** bought their own bottom and surged back to life!",
            "✨ **OLYMPUS BLESSING!** Zeus cast a spark of eternal fire — **{player1}** is REBORN!",
            "✨ **AIRDROP RECOVERY!** An anonymous whale funded **{player1}**'s resurrection!",
        ],
        "phases": [
            "🏛️ COLOSSEUM BLOODBATH — THE GATES OPEN",
            "🌅 DAY 1 — DAWN OF GLADIATORS",
            "🌙 NIGHT 1 — LIQUIDATION DESPERATION",
            "🌋 ARENA HAZARD — LAVA PIT ARENA",
            "🌅 DAY 2 — THE BEAR MARKET DIP",
            "🌙 NIGHT 2 — COLD WALLET HIDING",
            "⚡ ARENA HAZARD — GAS FEE TSUNAMI",
            "🌅 DAY 3 — GLADIATOR GAUNTLET",
            "🌙 NIGHT 3 — CHARIOT CRASHES",
            "🍖 THE BULL RUN AIRDROP",
            "🌅 DAY 4 — SHITCOIN SHAKEDOWN",
            "🌙 NIGHT 4 — CRYPTO WINTER SHADOWS",
            "🩸 BLOOD MOON — SHORT SQUEEZE SLAUGHTER",
            "🌅 DAY 5 — TITANS OF THE ARENA",
            "🌙 NIGHT 5 — MIDNIGHT RUGPULL",
            "❄️ ARENA HAZARD — HARD FORK BLIZZARD",
            "🌅 DAY 6 — CHARIOT DUEL",
            "🌙 NIGHT 6 — STAKING SHADOWS",
            "🌪️ ARENA HAZARD — TORNADO CASH STORM",
            "🌅 DAY 7 — ROMAN LEGION CLASH",
            "🌙 NIGHT 7 — THE LION'S DEN",
            "☣️ ARENA HAZARD — TOXIC SMART CONTRACT",
            "🌅 DAY 8 — DEGEN OVERDRIVE",
            "🌙 NIGHT 8 — EVE OF ETERNITY",
            "☠️ SUDDEN DEATH — COLLAPSING COLOSSEUM",
            "🌅 DAY 9 — THE FINAL HARDFORK",
            "🌙 NIGHT 9 — TWILIGHT OF ZEUS",
            "🔥 ARENA HAZARD — MOUNT OLYMPUS LIGHTNING",
            "🌅 DAY 10 — THE 100X GLORY",
            "🌙 NIGHT 10 — CHAMPION'S PANTHEON",
        ],
        "final_phase": "⚔️ THE TITAN DUEL — FINAL 1V1 SHOWDOWN"
    }
}

# Fallback aggregate lists
RUMBLE_DEATH_TEMPLATES = [d for t in RUMBLE_THEMES.values() for d in t["deaths"]]
RUMBLE_SURVIVAL_TEMPLATES = [s for t in RUMBLE_THEMES.values() for s in t["survival"]]
RUMBLE_REVIVE_TEMPLATES = [r for t in RUMBLE_THEMES.values() for r in t["revives"]]
RUMBLE_PHASE_NAMES = RUMBLE_THEMES["modern"]["phases"]


def get_dynamic_rumble_phases(total_players: int, theme: str = "modern") -> List[Tuple[str, bool]]:
    """
    Generate dynamic story phases scaled according to total registered player count and theme.
    - 3-8 players: 4-5 phases
    - 9-18 players: 7-9 phases
    - 19-28 players: 10-11 phases
    - 30-40 players: 12-13 phases (user requested: 12-13 for 30-40)
    - 41-60 players: 17-19 phases
    - 70-80 players: 25-26 phases (user requested: 25-26 for 70-80)
    - 80+ players: 28-30 phases
    Returns list of (phase_name, is_final_phase).
    """
    if total_players <= 4:
        target_count = 3
    elif total_players <= 8:
        target_count = 5
    elif total_players <= 18:
        target_count = 8
    elif total_players <= 28:
        target_count = 10
    elif total_players <= 40:
        target_count = 12 if total_players <= 35 else 13
    elif total_players <= 55:
        target_count = 17
    elif total_players <= 69:
        target_count = 21
    elif total_players <= 80:
        target_count = 25 if total_players <= 75 else 26
    else:
        target_count = min(30, max(26, int(total_players * 0.33)))

    theme_info = RUMBLE_THEMES.get(theme, RUMBLE_THEMES["modern"])
    names_pool = theme_info["phases"]

    phases = []
    # 1. Opening phase
    phases.append((names_pool[0], False))
    
    # 2. Intermediate phases
    mid_count = target_count - 2
    if mid_count > 0:
        mid_pool = names_pool[1:]
        if mid_count <= len(mid_pool):
            step_names = mid_pool[:mid_count]
        else:
            step_names = list(mid_pool)
            while len(step_names) < mid_count:
                day_num = len(step_names) // 2 + 10
                step_names.append(f"🌅 DAY {day_num} — SURVIVAL CLASH")
                if len(step_names) < mid_count:
                    step_names.append(f"🌙 NIGHT {day_num} — SHADOW WAR")
        for p_name in step_names:
            phases.append((p_name, False))
            
    # 3. Final Showdown
    phases.append((theme_info.get("final_phase", "⚔️ THE FINAL SHOWDOWN — DUEL TO THE DEATH"), True))
    return phases


class RumbleGame:
    """Core state machine for the Rumble Royale battle royale minigame."""

    def __init__(self, players: List[dict], theme: str = "modern"):
        """
        Args:
            players: List of dicts with 'id' (user_id str), 'name' (display_name), 'username' (str), 'mention' (<@id>)
            theme: Theme key ('modern', 'japanese', 'european', 'web3_ancient')
        """
        self.alive: List[dict] = list(players)
        self.dead: List[dict] = []
        self.kills: Dict[str, int] = {p["id"]: 0 for p in players}
        self.revives: Dict[str, int] = {p["id"]: 0 for p in players}
        self.eliminated_by: Dict[str, str] = {}  # victim_id -> killer_username or "The Arena"
        self.death_order: List[str] = []  # IDs in order of elimination (first eliminated = last place)
        self.used_templates: Set[int] = set()
        self.phase_results: List[discord.Embed] = []
        self.theme: str = theme if theme in RUMBLE_THEMES else "modern"
        self.theme_info: dict = RUMBLE_THEMES[self.theme]
        random.shuffle(self.alive)

    def _pick_template(self, pool_key: str) -> str:
        """Pick a random non-repeating template from the theme's pool."""
        pool = self.theme_info.get(pool_key, RUMBLE_THEMES["modern"].get(pool_key, []))
        available = [(i, t) for i, t in enumerate(pool) if i not in self.used_templates]
        if not available:
            self.used_templates.clear()
            available = list(enumerate(pool))
        idx, template = random.choice(available)
        self.used_templates.add(idx)
        return template

    def _kill_player(self, victim: dict, killer: Optional[dict] = None) -> str:
        """Eliminate a player and return the event text using bold username (e.g. ayx5hhh)."""
        template = self._pick_template("deaths")
        v_uname = victim.get("username", victim.get("name", "Warrior"))
        v_display = f"**{v_uname}**"
        if killer:
            self.kills[killer["id"]] = self.kills.get(killer["id"], 0) + 1
            k_uname = killer.get("username", killer.get("name", "Warrior"))
            k_display = f"**{k_uname}**"
            self.eliminated_by[victim["id"]] = k_uname
            text = template.format(player1=v_display, killer=k_display, player2=v_display)
        else:
            self.eliminated_by[victim["id"]] = "The Arena"
            text = template.format(player1=v_display, killer="**The Arena**", player2=v_display)

        self.alive.remove(victim)
        self.dead.append(victim)
        self.death_order.append(victim["id"])
        return text

    def _survival_event(self, player: dict) -> str:
        """Generate a survival event for a player using bold username."""
        template = self._pick_template("survival")
        p_display = f"**{player.get('username', player.get('name', 'Warrior'))}**"
        return template.format(player1=p_display)

    def _try_revive(self) -> Optional[str]:
        """20% chance to revive a dead player. Returns event text or None."""
        if not self.dead or random.random() > 0.20:
            return None
        revived = random.choice(self.dead)
        self.dead.remove(revived)
        self.alive.append(revived)
        self.revives[revived["id"]] = self.revives.get(revived["id"], 0) + 1
        # Remove from death order and eliminators since they're alive again
        if revived["id"] in self.death_order:
            self.death_order.remove(revived["id"])
        if revived["id"] in self.eliminated_by:
            del self.eliminated_by[revived["id"]]
        template = self._pick_template("revives")
        r_display = f"**{revived.get('username', revived.get('name', 'Warrior'))}**"
        return template.format(player1=r_display)

    def run_phase(self, phase_name: str, is_final: bool = False, phases_remaining: int = 1) -> discord.Embed:
        """Execute one phase of the battle and return an embed with results."""
        events = []

        num_alive = len(self.alive)
        if num_alive <= 1:
            embed = discord.Embed(
                title=f"⚔️ RUMBLE ROYALE — {phase_name}",
                description="*No further combatants remain to fight...*",
                color=discord.Color.dark_grey()
            )
            return embed

        # Calculate kills for this phase to smoothly distribute eliminations across all phases
        if is_final or phases_remaining <= 1:
            target_kills = max(num_alive - 1, 1)
        else:
            # Leave at least 2 alive before the final showdown
            kills_needed_total = max(1, num_alive - 2)
            base_kills = max(1, int(kills_needed_total / max(1, phases_remaining - 1)))
            target_kills = max(1, min(base_kills + random.choice([-1, 0, 1]), num_alive - 2))

        kills_this_phase = 0
        attempts = 0
        max_attempts = target_kills * 4

        while kills_this_phase < target_kills and len(self.alive) > (1 if is_final else 2) and attempts < max_attempts:
            attempts += 1
            victim = random.choice(self.alive)
            possible_killers = [p for p in self.alive if p["id"] != victim["id"]]
            killer = random.choice(possible_killers) if possible_killers else None
            event_text = self._kill_player(victim, killer)
            events.append(f"💀 {event_text}")
            kills_this_phase += 1

        # Survival events for some living players
        num_survival = min(2, len(self.alive))
        survival_candidates = random.sample(self.alive, min(num_survival, len(self.alive)))
        for p in survival_candidates:
            events.append(f"🛡️ {self._survival_event(p)}")

        # Try revive (20% chance per phase if not final phase)
        if not is_final:
            revive_text = self._try_revive()
            if revive_text:
                events.append(revive_text)

        # Build phase embed with surviving players list (usernames in bold, no @tags)
        total_players = len(self.alive) + len(self.dead)
        eliminated_total = len(self.dead)

        # Add surviving players section
        if self.alive:
            survivor_names = ", ".join(f"**{p.get('username', p.get('name', 'Warrior'))}**" for p in self.alive[:25])
            extra = f"\n*...and {len(self.alive) - 25} more*" if len(self.alive) > 25 else ""
            events.append(f"\n🟢 **SURVIVING PLAYERS ({len(self.alive)}):**\n{survivor_names}{extra}")

        desc_text = "\n\n".join(events)
        if len(desc_text) > 4000:
            desc_text = desc_text[:3997] + "..."

        embed = discord.Embed(
            title=f"⚔️ RUMBLE ROYALE — {phase_name}",
            description=desc_text,
            color=discord.Color.red() if kills_this_phase > 3 else discord.Color.orange()
        )
        embed.set_footer(text=f"Alive: {len(self.alive)} | Eliminated: {eliminated_total} | Theme: {self.theme_info['name']}")
        return embed

    def build_results_embed(self) -> discord.Embed:
        """Build a single consolidated end-of-game match results embed matching Diffy bot style."""
        # Determine final rankings
        if self.alive:
            champion = self.alive[0]
            runner_up_ids = list(reversed(self.death_order))
        else:
            runner_up_ids = list(reversed(self.death_order))
            champion_id = runner_up_ids.pop(0) if runner_up_ids else None
            champion = None
            if champion_id:
                all_players_map = {p["id"]: p for p in self.dead + self.alive}
                champion = all_players_map.get(champion_id)

        all_players_map = {}
        for p in self.alive + self.dead:
            all_players_map[p["id"]] = p

        champ_uname = champion.get("username", champion.get("name", "Warrior")) if champion else "Unknown"

        desc_parts = [
            f"🎉 **{champ_uname}**\nSURVIVED THE ENTIRE ARENA AND CLAIMS THE VICTORY!"
        ]

        # --- SECTION 1: TOP 5 FINALISTS ---
        finalist_ids = []
        if champion:
            finalist_ids.append(champion["id"])
        for rid in reversed(self.death_order):
            if rid not in finalist_ids:
                finalist_ids.append(rid)
            if len(finalist_ids) >= 5:
                break

        finalist_lines = ["🏆 **TOP 5 FINALISTS:**"]
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, pid in enumerate(finalist_ids[:5]):
            p = all_players_map.get(pid)
            if not p:
                continue
            p_uname = p.get("username", p.get("name", "Warrior"))
            k = self.kills.get(pid, 0)
            rv = self.revives.get(pid, 0)
            revive_part = f" • `{rv} revives`" if rv > 0 else ""
            kill_part = f"`{k} kills`"

            if i == 0:
                medal = medals[0]
                finalist_lines.append(f"{medal} **#1 CHAMPION:** {p_uname} — {kill_part}{revive_part} *(WINNER)*")
            else:
                medal = medals[i] if i < len(medals) else f"#{i+1}"
                elim_by = self.eliminated_by.get(pid, "The Arena")
                finalist_lines.append(f"{medal} **#{i+1} Place:** {p_uname} — {kill_part}{revive_part} *(Eliminated by {elim_by})*")

        desc_parts.append("\n".join(finalist_lines) if len(finalist_lines) > 1 else "*No finalists recorded*")

        # --- SECTION 2: TOP 3 MOST KILLS (APEX PREDATORS) ---
        sorted_kills = sorted(self.kills.items(), key=lambda x: x[1], reverse=True)
        top_killers = [(pid, c) for pid, c in sorted_kills if c > 0][:3]
        kill_lines = ["⚔️ **TOP 3 MOST KILLS (APEX PREDATORS):**"]
        kill_medals = ["🥇", "🥈", "🥉"]
        if top_killers:
            for i, (pid, count) in enumerate(top_killers):
                p = all_players_map.get(pid)
                if not p:
                    continue
                p_uname = p.get("username", p.get("name", "Warrior"))
                m = kill_medals[i] if i < len(kill_medals) else f"#{i+1}"
                kill_lines.append(f"{m} **#{i+1} Killer:** {p_uname} — `{count} Kills`")
        else:
            kill_lines.append("*No kills recorded*")
        desc_parts.append("\n".join(kill_lines))

        # --- SECTION 3: MOST REVIVED WARRIORS (PHOENIX AWARD) ---
        sorted_revives = sorted(self.revives.items(), key=lambda x: x[1], reverse=True)
        top_revived = [(pid, c) for pid, c in sorted_revives if c > 0][:3]
        revive_lines = ["💫 **MOST REVIVED WARRIORS (PHOENIX AWARD):**"]
        if top_revived:
            for i, (pid, count) in enumerate(top_revived):
                p = all_players_map.get(pid)
                if not p:
                    continue
                p_uname = p.get("username", p.get("name", "Warrior"))
                revive_lines.append(f"✨ **#{i+1} Resurrected:** {p_uname} — `{count} Revives`")
        else:
            revive_lines.append("*No revives occurred this match*")
        desc_parts.append("\n".join(revive_lines))

        embed = discord.Embed(
            title="🏆 RUMBLE ROYALE MATCH RESULTS! 🏆",
            description="\n\n".join(desc_parts),
            color=discord.Color.gold()
        )
        embed.set_footer(text=f"Theme: {self.theme_info['name']} • Rumble Royale")
        return embed


def build_rumble_lobby_embed(
    players: list,
    join_duration: int,
    start_time: int,
    host_name: str = "Host",
    tag_role: Optional[Union[discord.Role, str]] = None,
    theme: str = "modern"
) -> discord.Embed:
    """Build or update the live Rumble Royale lobby announcement embed with usernames."""
    count = len(players)
    theme_info = RUMBLE_THEMES.get(theme, RUMBLE_THEMES["modern"])
    embed = discord.Embed(
        title="⚔️ RUMBLE ROYALE MATCH ANNOUNCED! ⚔️",
        color=discord.Color.gold()
    )

    if players:
        player_lines = []
        for idx, p in enumerate(players[:40], start=1):
            uname = p.get("username", p.get("name", "warrior"))
            player_lines.append(f"**{idx}.** {uname}")
        if len(players) > 40:
            player_lines.append(f"*...and {len(players) - 40} more*")
        players_text = "\n".join(player_lines)
    else:
        players_text = "*No players registered yet.*"

    tag_line = f" • Tag: {tag_role.mention}" if tag_role and hasattr(tag_role, "mention") else ""
    desc = (
        f"Hosted by **{host_name}**!{tag_line}\n"
        f"🎭 **THEME:** {theme_info['emoji']} **{theme_info['name']}**\n\n"
        f"⏰ **LOBBY CLOCK:** Starts in <t:{start_time + join_duration}:R>!\n"
        f"Click [ ⚔️ **Join Battle** ] to sign up for the Rumble Royale!\n"
        f"*(Minimum 2 real players required to start!)*\n\n"
        f"👤 **REAL REGISTERED PLAYERS ({count}):**\n"
        f"{players_text}"
    )
    embed.description = desc
    embed.set_footer(text=f"Rumble Royale ({theme_info['name']}) • Click button to join!")
    return embed


# -------- Active Rumble Sessions -------- #
active_rumbles: Dict[int, dict] = {}  # channel_id -> {"players": [...], "message_id": int, "started": bool, "created_at": int, "join_duration": int, "host_name": str, "tag_role": Role, "theme": str}


class RumbleJoinView(discord.ui.View):
    """Persistent view for joining and starting a Rumble Royale match."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="Join Battle", style=discord.ButtonStyle.success, custom_id="rumble_join", emoji="⚔️", row=0)
    async def join_rumble_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = active_rumbles.get(self.channel_id)
        if not session:
            await safe_respond(interaction, "❌ No active Rumble in this channel.", ephemeral=True)
            return
        if session.get("started"):
            await safe_respond(interaction, "❌ The Rumble has already started!", ephemeral=True)
            return

        uid = str(interaction.user.id)
        if any(p["id"] == uid for p in session["players"]):
            await safe_respond(interaction, "✅ You're already in the Rumble!", ephemeral=True)
            return

        session["players"].append({
            "id": uid,
            "name": interaction.user.display_name,
            "username": interaction.user.name,
            "mention": interaction.user.mention
        })

        count = len(session["players"])

        # Update the main Announcement Embed directly in place with the new roster and count
        try:
            channel = interaction.channel
            if channel and session.get("message_id"):
                msg = await channel.fetch_message(session["message_id"])
                start_time = session.get("created_at", int(time.time()))
                join_duration = session.get("join_duration", 120)
                host_name = session.get("host_name", "Host")
                tag_role = session.get("tag_role")
                theme = session.get("theme", "modern")
                new_embed = build_rumble_lobby_embed(session["players"], join_duration, start_time, host_name, tag_role, theme)
                await msg.edit(embed=new_embed)
        except Exception as edit_err:
            print(f"[RUMBLE EMBED EDIT ERROR] {edit_err}")

        # Send ONLY an ephemeral response to the user so the chat is never spammed
        await safe_respond(interaction, f"⚔️ You signed up for the Rumble Royale! (**{count}** registered)", ephemeral=True)

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, custom_id="rumble_leave", emoji="🏃", row=0)
    async def leave_rumble_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        session = active_rumbles.get(self.channel_id)
        if not session:
            await safe_respond(interaction, "❌ No active Rumble in this channel.", ephemeral=True)
            return
        if session.get("started"):
            await safe_respond(interaction, "❌ The Rumble has already started!", ephemeral=True)
            return

        uid = str(interaction.user.id)
        existing = next((p for p in session["players"] if p["id"] == uid), None)
        if not existing:
            await safe_respond(interaction, "❌ You are not registered in this Rumble!", ephemeral=True)
            return

        session["players"].remove(existing)
        count = len(session["players"])

        # Update the announcement embed
        try:
            channel = interaction.channel
            if channel and session.get("message_id"):
                msg = await channel.fetch_message(session["message_id"])
                start_time = session.get("created_at", int(time.time()))
                join_duration = session.get("join_duration", 120)
                host_name = session.get("host_name", "Host")
                tag_role = session.get("tag_role")
                theme = session.get("theme", "modern")
                new_embed = build_rumble_lobby_embed(session["players"], join_duration, start_time, host_name, tag_role, theme)
                await msg.edit(embed=new_embed)
        except Exception as edit_err:
            print(f"[RUMBLE EMBED EDIT ERROR] {edit_err}")

        await safe_respond(interaction, f"🏃 You left the Rumble Royale. ({count} registered)", ephemeral=True)

    @discord.ui.button(label="Start Battle Now", style=discord.ButtonStyle.danger, custom_id="rumble_start", emoji="🚀", row=1)
    async def start_rumble_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Admin-only check
        if not is_admin(interaction.user, interaction.guild):
            await safe_respond(interaction, "❌ Only admins can start the Rumble!", ephemeral=True)
            return

        session = active_rumbles.get(self.channel_id)
        if not session:
            await safe_respond(interaction, "❌ No active Rumble in this channel.", ephemeral=True)
            return
        if session.get("started"):
            await safe_respond(interaction, "❌ The Rumble has already started!", ephemeral=True)
            return
        if len(session["players"]) < 2:
            await safe_respond(interaction, "❌ Need at least **2 players** to start a Rumble!", ephemeral=True)
            return

        session["started"] = True
        theme_key = session.get("theme", "modern")
        await safe_respond(interaction, "🚀 **THE RUMBLE ROYALE BEGINS!** Brace yourselves...", ephemeral=False)

        # Run the game in the background
        asyncio.create_task(run_rumble_game(interaction.channel, session["players"], theme=theme_key))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="rumble_cancel", emoji="❌", row=1)
    async def cancel_rumble_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_admin(interaction.user, interaction.guild):
            await safe_respond(interaction, "❌ Only admins can cancel the Rumble!", ephemeral=True)
            return

        if self.channel_id in active_rumbles:
            del active_rumbles[self.channel_id]

        await safe_respond(interaction, "❌ The Rumble has been cancelled.", ephemeral=False)


async def run_rumble_game(channel, players: list, theme: str = "modern"):
    """Execute the full Rumble Royale game sequence in a channel with 15s gap between phases."""
    channel_id = channel.id
    game = RumbleGame(players, theme=theme)
    theme_info = RUMBLE_THEMES.get(theme, RUMBLE_THEMES["modern"])

    try:
        # Lobby closed announcement
        lobby_embed = discord.Embed(
            title="⚔️ RUMBLE ROYALE LOBBY CLOSED!",
            description=f"Match is starting with **{len(players)}** real registered players!\n🎭 Theme: {theme_info['emoji']} **{theme_info['name']}**",
            color=discord.Color.from_rgb(255, 69, 0)
        )
        await channel.send(embed=lobby_embed)
        await asyncio.sleep(2)

        # Dramatic intro with all player usernames (no @tags)
        player_names = ", ".join(f"**{p.get('username', p.get('name', 'Warrior'))}**" for p in players[:40])
        extra_players = f"\n*...and {len(players) - 40} more*" if len(players) > 40 else ""

        intro_embed = discord.Embed(
            title=f"🔥 RUMBLE ROYALE — {theme_info['name'].upper()} ARENA OPEN! 🔔",
            description=f"**{len(players)} Real Players** have dropped into the arena!\n\n"
                        f"{player_names}{extra_players}\n\n"
                        f"*The countdown reaches zero... FIGHT!*",
            color=discord.Color.dark_red()
        )
        await channel.send(embed=intro_embed)
        await asyncio.sleep(4)

        # Generate dynamic phases based on total registered players & theme
        phases = get_dynamic_rumble_phases(len(players), theme=theme)
        total_phases = len(phases)

        # Run each phase with 15-second pacing gap between each phase
        for idx, (phase_name, is_final) in enumerate(phases):
            if len(game.alive) <= 1:
                break

            phases_remaining = total_phases - idx
            phase_embed = game.run_phase(phase_name, is_final=is_final, phases_remaining=phases_remaining)
            await channel.send(embed=phase_embed)
            await asyncio.sleep(15)  # 15-second gap between each phase

        # Post consolidated Match Results embed with winner tagged outside
        await asyncio.sleep(2)
        results_embed = game.build_results_embed()

        champ = None
        if game.alive:
            champ = game.alive[0]
        elif game.death_order:
            all_players_map = {p["id"]: p for p in players}
            champ = all_players_map.get(game.death_order[-1])

        content = None
        if champ:
            content = f"🏆 {champ['mention']} **SURVIVED THE ENTIRE ARENA AND CLAIMS THE VICTORY!** 🎉"
            try:
                # Set winner avatar thumbnail on embed
                if hasattr(channel, "guild") and channel.guild:
                    champ_member = channel.guild.get_member(int(champ["id"]))
                    if champ_member and champ_member.display_avatar:
                        results_embed.set_thumbnail(url=champ_member.display_avatar.url)
            except Exception as thumb_err:
                print(f"[RUMBLE THUMBNAIL ERROR] {thumb_err}")

        await channel.send(content=content, embed=results_embed)

    except Exception as e:
        print(f"[RUMBLE ERROR] {e}")
        try:
            await channel.send(f"❌ **Rumble Error:** {e}")
        except Exception:
            pass
    finally:
        # Cleanup session
        if channel_id in active_rumbles:
            del active_rumbles[channel_id]


async def _rumble_countdown(channel, ch_id: int, join_duration: int):
    """Background task: countdown timer for Rumble lobby with reminders."""
    try:
        remaining = join_duration
        reminder_sent_60 = False
        reminder_sent_30 = False

        while remaining > 0:
            await asyncio.sleep(1)
            remaining -= 1

            session = active_rumbles.get(ch_id)
            if not session or session.get("started"):
                return  # Admin started manually or session cancelled

            theme_key = session.get("theme", "modern")
            theme_info = RUMBLE_THEMES.get(theme_key, RUMBLE_THEMES["modern"])

            # 1 minute reminder
            if remaining == 60 and not reminder_sent_60:
                reminder_sent_60 = True
                view = RumbleJoinView(ch_id)
                remind_embed = discord.Embed(
                    title=f"🔥 {theme_info['emoji']} RUMBLE ROYALE REMINDER",
                    description=f"Only **1 minute** left to join the battle in **{theme_info['name']}**!\nClick [ ⚔️ **Join Battle** ] below to enter! ⚔️",
                    color=discord.Color.from_rgb(255, 165, 0)
                )
                await channel.send(embed=remind_embed, view=view)

            # 30 second final call
            if remaining == 30 and not reminder_sent_30:
                reminder_sent_30 = True
                view = RumbleJoinView(ch_id)
                remind_embed = discord.Embed(
                    title=f"🚨 {theme_info['emoji']} FINAL CALL!",
                    description=f"Only **30 seconds** remaining for **{theme_info['name']}**!\nClick [ ⚔️ **Join Battle** ] below to enter!",
                    color=discord.Color.red()
                )
                await channel.send(embed=remind_embed, view=view)

        # Timer expired — auto-start if enough players
        session = active_rumbles.get(ch_id)
        if not session or session.get("started"):
            return

        if len(session["players"]) < 2:
            await channel.send("❌ **Rumble cancelled** — not enough players joined (minimum 2 required).")
            if ch_id in active_rumbles:
                del active_rumbles[ch_id]
            return

        session["started"] = True
        await run_rumble_game(channel, session["players"], theme=session.get("theme", "modern"))

    except Exception as e:
        print(f"[RUMBLE COUNTDOWN ERROR] {e}")
        if ch_id in active_rumbles:
            del active_rumbles[ch_id]


@bot.tree.command(name="rumble", description="Start a Rumble Royale battle royale minigame!")
@app_commands.describe(
    join_duration="How many seconds to allow players to join (default: 120)",
    tag_role="Optional role to tag/ping in the announcement (e.g. @everyone, @Rumble)",
    theme="Thematic story style for deaths, events, and arena lore (default: Modern)",
)
@app_commands.choices(
    theme=[
        app_commands.Choice(name="🏙️ Modern / Memes & Urban (Default)", value="modern"),
        app_commands.Choice(name="⛩️ Japanese Anime & Samurai Lore", value="japanese"),
        app_commands.Choice(name="🏰 European Medieval & Comic Knights", value="european"),
        app_commands.Choice(name="🏛️ Web3 Crypto & Ancient Colosseum", value="web3_ancient"),
    ]
)
async def rumble_command(
    interaction: discord.Interaction,
    join_duration: Optional[int] = 120,
    tag_role: Optional[discord.Role] = None,
    theme: Optional[app_commands.Choice[str]] = None,
):
    """Admin-only: Start a Rumble Royale session with custom theme and role ping."""
    try:
        if not is_admin(interaction.user, interaction.guild):
            await safe_respond(interaction, "❌ Only admins can start a Rumble!", ephemeral=True)
            return

        channel = interaction.channel
        if not channel:
            await safe_respond(interaction, "❌ Cannot start Rumble in this channel.", ephemeral=True)
            return
        ch_id = channel.id

        if ch_id in active_rumbles:
            await safe_respond(interaction, "❌ A Rumble is already active in this channel! Wait for it to finish.", ephemeral=True)
            return

        # Clamp join duration
        join_duration = max(30, min(join_duration or 120, 600))

        theme_key = theme.value if isinstance(theme, app_commands.Choice) else (theme or "modern")
        if theme_key not in RUMBLE_THEMES:
            theme_key = "modern"

        start_time = int(time.time())
        host_name = interaction.user.name  # Use host's username
        # Initialize session
        active_rumbles[ch_id] = {
            "players": [],
            "message_id": None,
            "started": False,
            "created_at": start_time,
            "join_duration": join_duration,
            "host_name": host_name,
            "tag_role": tag_role,
            "theme": theme_key,
        }

        # Post join embed using build_rumble_lobby_embed
        join_embed = build_rumble_lobby_embed([], join_duration, start_time, host_name, tag_role, theme_key)
        view = RumbleJoinView(ch_id)
        
        content = f"🔔 {tag_role.mention} **A new Rumble Royale has been announced!**" if tag_role else None

        if not interaction.response.is_done():
            await interaction.response.send_message(content=content, embed=join_embed, view=view)
        else:
            await interaction.followup.send(content=content, embed=join_embed, view=view)

        # Get the sent message ID for later updates
        try:
            msg = await interaction.original_response()
            active_rumbles[ch_id]["message_id"] = msg.id
        except Exception:
            pass

        # Start background countdown timer with reminders
        asyncio.create_task(_rumble_countdown(channel, ch_id, join_duration))

    except Exception as e:
        print(f"[RUMBLE COMMAND ERROR] {e}")
        traceback.print_exc()
        try:
            await safe_respond(interaction, f"❌ Failed to start Rumble: {e}", ephemeral=True)
        except Exception:
            pass



# -------- Slash Commands for Profile & Giveaways -------- #

class UserProfileModal(discord.ui.Modal, title="Update Web3 Socials & Wallets"):
    twitter = discord.ui.TextInput(label="Twitter / X Handle", placeholder="@yourhandle", required=False)
    telegram = discord.ui.TextInput(label="Telegram Handle", placeholder="@username", required=False)
    evm = discord.ui.TextInput(label="EVM Wallet Address", placeholder="0x1234...5678", required=False)
    solana = discord.ui.TextInput(label="Solana Wallet Address", placeholder="Public Key...", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        if uid not in user_profiles:
            user_profiles[uid] = {
                "display_name": interaction.user.display_name,
                "username": interaction.user.name,
                "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            }
        
        user_profiles[uid]["twitter"] = self.twitter.value.strip() if self.twitter.value else ""
        user_profiles[uid]["telegram"] = self.telegram.value.strip() if self.telegram.value else ""
        user_profiles[uid]["evm_wallet"] = self.evm.value.strip() if self.evm.value else ""
        user_profiles[uid]["solana_wallet"] = self.solana.value.strip() if self.solana.value else ""
        save_user_profiles()

        embed = discord.Embed(title="👤 Profile & Wallets Saved", color=discord.Color.green())
        embed.add_field(name="Twitter", value=self.twitter.value or "Not set", inline=True)
        embed.add_field(name="Telegram", value=self.telegram.value or "Not set", inline=True)
        embed.add_field(name="EVM Wallet", value=f"`{self.evm.value}`" if self.evm.value else "Not set", inline=False)
        embed.add_field(name="Solana Wallet", value=f"`{self.solana.value}`" if self.solana.value else "Not set", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Global handler for slash command errors to ensure clean feedback."""
    print(f"[SLASH COMMAND ERROR] in /{interaction.command.name if interaction.command else 'unknown'}: {error}")
    msg = f"❌ An error occurred: {error}"
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
    except Exception:
        pass


@bot.tree.command(name="profile", description="Manage your Web3 wallets & social handles for giveaways!")
async def profile_cmd(interaction: discord.Interaction):
    try:
        uid = str(interaction.user.id)
        prof = get_user_profile_fast(uid, interaction.user)
        modal = UserProfileModal()
        if prof.get("twitter"): modal.twitter.default = prof.get("twitter")
        if prof.get("telegram"): modal.telegram.default = prof.get("telegram")
        if prof.get("evm_wallet"): modal.evm.default = prof.get("evm_wallet")
        if prof.get("solana_wallet"): modal.solana.default = prof.get("solana_wallet")
        await interaction.response.send_modal(modal)
    except Exception as e:
        print(f"[PROFILE CMD ERROR] {e}")
        await safe_respond(interaction, f"❌ Failed to open profile: {e}", ephemeral=True)


@bot.tree.command(name="set-evm-wallet", description="Set your EVM (Ethereum) wallet address.")
@app_commands.describe(address="EVM Wallet Address (0x...)")
async def set_evm_wallet_cmd(interaction: discord.Interaction, address: str):
    uid = str(interaction.user.id)
    if uid not in user_profiles:
        user_profiles[uid] = {
            "display_name": interaction.user.display_name,
            "username": interaction.user.name,
            "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    user_profiles[uid]["evm_wallet"] = address.strip()
    save_user_profiles()
    await interaction.response.send_message(f"✅ **EVM Wallet Updated!**\nAddress: `{address.strip()}`", ephemeral=True)


@bot.tree.command(name="set-solana-wallet", description="Set your Solana wallet address.")
@app_commands.describe(address="Solana Wallet Public Key")
async def set_solana_wallet_cmd(interaction: discord.Interaction, address: str):
    uid = str(interaction.user.id)
    if uid not in user_profiles:
        user_profiles[uid] = {
            "display_name": interaction.user.display_name,
            "username": interaction.user.name,
            "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    user_profiles[uid]["solana_wallet"] = address.strip()
    save_user_profiles()
    await interaction.response.send_message(f"✅ **Solana Wallet Updated!**\nAddress: `{address.strip()}`", ephemeral=True)


@bot.tree.command(name="set-twitter", description="Set your Twitter / X handle.")
@app_commands.describe(handle="Twitter handle (e.g. @yourhandle)")
async def set_twitter_cmd(interaction: discord.Interaction, handle: str):
    uid = str(interaction.user.id)
    clean_handle = handle.strip()
    if not clean_handle.startswith("@"):
        clean_handle = f"@{clean_handle}"
    if uid not in user_profiles:
        user_profiles[uid] = {
            "display_name": interaction.user.display_name,
            "username": interaction.user.name,
            "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    user_profiles[uid]["twitter"] = clean_handle
    save_user_profiles()
    await interaction.response.send_message(f"✅ **Twitter Handle Updated!**\nHandle: **{clean_handle}**", ephemeral=True)


@bot.tree.command(name="set-telegram", description="Set your Telegram username.")
@app_commands.describe(username="Telegram username (e.g. @username)")
async def set_telegram_cmd(interaction: discord.Interaction, username: str):
    uid = str(interaction.user.id)
    clean_username = username.strip()
    if not clean_username.startswith("@"):
        clean_username = f"@{clean_username}"
    if uid not in user_profiles:
        user_profiles[uid] = {
            "display_name": interaction.user.display_name,
            "username": interaction.user.name,
            "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    user_profiles[uid]["telegram"] = clean_username
    save_user_profiles()
    await interaction.response.send_message(f"✅ **Telegram Handle Updated!**\nUsername: **{clean_username}**", ephemeral=True)


@bot.tree.command(name="setup-ticket-panel", description="Admin: Send PrismaX-style Ticket Help Desk panel.")
@app_commands.describe(
    channel="Target channel to post the ticket panel",
    title="Panel Header (default: [ServerName] Support Help Desk)",
    description="Panel Description text"
)
async def setup_ticket_panel_cmd(
    interaction: discord.Interaction,
    channel: Optional[discord.TextChannel] = None,
    title: Optional[str] = None,
    description: Optional[str] = None
):
    if not is_bot_admin_by_id(str(interaction.user.id)):
        await interaction.response.send_message("❌ Admin permission required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    target_ch = channel or interaction.channel
    s_name = interaction.guild.name if interaction.guild else "Support"

    header_text = title or f"**{s_name} Support Help Desk**"
    body_text = description or (
        "Please select the nature of your enquiry from the dropdown list. "
        "We look forward to serving and assisting you to our best capabilities."
    )

    full_content = f"{header_text}\n\n{body_text}"

    launch_view = TicketLaunchView(server_name=s_name)
    try:
        msg = await target_ch.send(content=full_content, view=launch_view)
        bot.add_view(launch_view)
        await interaction.followup.send(f"✅ Ticket Help Desk Panel posted successfully in {target_ch.mention}!", ephemeral=True)
    except Exception as e:
        print(f"[SETUP TICKET PANEL FAIL] {e}")
        await interaction.followup.send("❌ Failed to post ticket panel. Check bot permissions.", ephemeral=True)


@bot.tree.command(name="close-ticket", description="Close the current ticket channel.")
async def close_ticket_cmd(interaction: discord.Interaction):
    ch_id = str(interaction.channel.id)
    active_tickets = tickets_data.get("active_tickets", {})
    if ch_id not in active_tickets and not any(k in interaction.channel.name for k in ["ticket-", "general-", "giveaway-", "collab-", "report-"]):
        await interaction.response.send_message("❌ This command can only be used inside a ticket channel.", ephemeral=True)
        return

    ctrl_view = TicketControlView()
    await ctrl_view._close_ticket(interaction)


@bot.tree.command(name="set-ticket-log-channel", description="Admin: Set the channel where ticket closure transcripts are logged.")
@app_commands.describe(channel="Channel to send ticket transcripts to")
async def set_ticket_log_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel):
    if not is_bot_admin_by_id(str(interaction.user.id)):
        await interaction.response.send_message("❌ Admin permission required.", ephemeral=True)
        return

    cfg = tickets_data.setdefault("config", {})
    cfg["log_channel_id"] = str(channel.id)
    save_tickets()

    await interaction.response.send_message(f"✅ Ticket transcript logs will now be sent to {channel.mention}!", ephemeral=True)


@bot.tree.command(name="add-user-to-ticket", description="Add a member to the current ticket channel.")
@app_commands.describe(user="The member to add to this ticket channel")
async def add_user_to_ticket_cmd(interaction: discord.Interaction, user: discord.Member):
    if not any(k in interaction.channel.name for k in ["ticket-", "general-", "giveaway-", "collab-", "report-"]):
        await interaction.response.send_message("❌ This command can only be used inside a ticket channel.", ephemeral=True)
        return

    try:
        await interaction.channel.set_permissions(user, read_messages=True, view_channel=True, send_messages=True, read_message_history=True)
        await interaction.response.send_message(f"✅ Added {user.mention} to this ticket channel!", ephemeral=False)
    except Exception as e:
        await interaction.response.send_message(f"❌ Failed to add user: {e}", ephemeral=True)


@bot.tree.command(name="user", description="Displays complete Web3 profile & wallet details for a user.")
@app_commands.describe(target="The user to view details for")
async def user_details_cmd(interaction: discord.Interaction, target: Optional[discord.User] = None):
    try:
        await interaction.response.defer()
        usr = target or interaction.user
        uid = str(usr.id)
        prof = await get_or_fetch_user_profile(uid, usr)

        twitter_val = f"**{prof.get('twitter')}**" if prof.get("twitter") else "*Not set by user yet*"
        telegram_val = f"**{prof.get('telegram')}**" if prof.get("telegram") else "*Not set by user yet*"
        evm_val = f"`{prof.get('evm_wallet')}`" if prof.get("evm_wallet") else "*Not set by user yet*"
        solana_val = f"`{prof.get('solana_wallet')}`" if prof.get("solana_wallet") else "*Not set by user yet*"

        embed = discord.Embed(
            title=f"Web3 Profile - {usr.display_name}",
            color=discord.Color.from_rgb(43, 92, 255)
        )
        embed.set_thumbnail(url=usr.display_avatar.url)
        embed.add_field(name="User", value=f"{usr.mention} (`{usr.id}`)", inline=False)
        embed.add_field(name="Twitter", value=twitter_val, inline=True)
        embed.add_field(name="Telegram", value=telegram_val, inline=True)
        embed.add_field(name="EVM Wallet", value=evm_val, inline=False)
        embed.add_field(name="Solana Wallet", value=solana_val, inline=False)

        if is_bot_admin_by_id(uid):
            embed.set_footer(text="Bot Administrator | Powered by Arcie Bot")
        else:
            embed.set_footer(text="Powered by Arcie Bot")

        await interaction.followup.send(embed=embed)
    except Exception as e:
        print(f"[USER CMD ERROR] {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Failed to fetch user profile.", ephemeral=True)
        else:
            await interaction.followup.send("❌ Failed to fetch user profile.", ephemeral=True)


@bot.tree.command(name="recover-entries", description="Admin: Automatically reconstruct entries into a giveaway from user profiles.")
@app_commands.describe(giveaway_id="Giveaway ID, Discord Message ID, or Discord Message Link")
async def recover_entries_cmd(interaction: discord.Interaction, giveaway_id: str):
    if not is_bot_admin_by_id(str(interaction.user.id)):
        await interaction.response.send_message("❌ Admin permission required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    g = await resolve_giveaway_by_identifier(giveaway_id)

    if not g:
        await interaction.followup.send("❌ Giveaway not found by ID, Message ID, or Message Link.", ephemeral=True)
        return

    g_id = g.get("id", giveaway_id)
    entries = giveaway_entries.setdefault(g_id, [])
    existing_uids = {e["user_id"] for e in entries if isinstance(e, dict) and "user_id" in e}

    recovered_count = 0
    # Search all known user profiles for wallet/social data
    for uid, prof in list(user_profiles.items()):
        if uid not in existing_uids:
            if prof.get("evm_wallet") or prof.get("solana_wallet") or prof.get("twitter"):
                new_entry = {
                    "user_id": uid,
                    "username": prof.get("username", uid),
                    "display_name": prof.get("display_name", uid),
                    "joined_at": int(time.time()),
                    "evm_wallet": prof.get("evm_wallet", ""),
                    "solana_wallet": prof.get("solana_wallet", ""),
                    "twitter": prof.get("twitter", ""),
                    "telegram": prof.get("telegram", ""),
                    "task_status": "verified",
                    "winner_type": None
                }
                entries.append(new_entry)
                existing_uids.add(uid)
                recovered_count += 1

    g["entries_count"] = len(entries)
    save_giveaways()
    save_giveaway_entries()
    if FIREBASE_URL:
        await firebase_put(f"giveaways/{g_id}", g)
        await firebase_put(f"giveaway_entries/{g_id}", entries)

    await update_giveaway_discord_message(g_id)
    await interaction.followup.send(
        f"✅ **Recovery Complete!**\n"
        f"Reconstructed **{recovered_count}** participant entries for **{g.get('title')}**!\n"
        f"Total Entries Now: **{len(entries)}**",
        ephemeral=True
    )


@bot.tree.command(name="set-custom-winners", description="Admin: Set custom GTD & FCFS winners for a fabricated giveaway and post to Discord.")
@app_commands.describe(
    giveaway_id="Giveaway ID, Discord Message ID, or Discord Message Link",
    guaranteed="GTD Winners (user mentions or IDs separated by space/comma)",
    fcfs="FCFS Winners (user mentions or IDs separated by space/comma)"
)
async def set_custom_winners_cmd(interaction: discord.Interaction, giveaway_id: str, guaranteed: Optional[str] = None, fcfs: Optional[str] = None):
    if not is_bot_admin_by_id(str(interaction.user.id)):
        await interaction.response.send_message("❌ Admin permission required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    g = await resolve_giveaway_by_identifier(giveaway_id)

    if not g:
        await interaction.followup.send("❌ Giveaway not found by ID, Message ID, or Message Link.", ephemeral=True)
        return

    g_id = g.get("id", giveaway_id)

    def format_list(raw):
        if not raw: return "None"
        text = str(raw)

        # 1. Extract all 17-20 digit Discord IDs and mentions
        ids = re.findall(r'\b\d{17,20}\b', text)
        mentions = re.findall(r'<@!?(\d{17,20})>', text)
        all_ids = []
        for i in ids + mentions:
            if i not in all_ids:
                all_ids.append(i)

        def resolve_tag(uid):
            prof = user_profiles.get(str(uid), {})
            if prof and (prof.get("display_name") or prof.get("username")):
                return f"@{prof.get('display_name') or prof.get('username')}"
            for guild in bot.guilds:
                member = guild.get_member(int(uid))
                if member:
                    return f"@{member.display_name}"
            return f"<@{uid}>"

        if all_ids:
            return ", ".join([resolve_tag(i) for i in all_ids])

        # 2. Fallback: split by whitespace, comma, or newline for usernames/handles
        items = [x.strip() for x in re.split(r'[\s,\n]+', text) if x.strip()]
        formatted = []
        for item in items:
            clean_id = re.sub(r'[^0-9]', '', item)
            if clean_id and 17 <= len(clean_id) <= 20:
                tag = resolve_tag(clean_id)
                if tag not in formatted: formatted.append(tag)
            elif item:
                tag = item if item.startswith("@") or item.startswith("<@") else f"@{item.lstrip('@')}"
                if tag not in formatted: formatted.append(tag)

        return ", ".join(formatted) if formatted else "None"

    gtd_str = format_list(guaranteed)
    fcfs_str = format_list(fcfs)

    winner_summary_lines = [
        f"**Guaranteed:** {gtd_str}",
        f"**FCFS:** {fcfs_str}"
    ]

    g["is_active"] = False
    g["winners_text"] = "\n".join(winner_summary_lines)

    save_giveaways()
    if FIREBASE_URL:
        await firebase_put(f"giveaways/{g_id}", g)

    await update_giveaway_discord_message(g_id)
    await announce_winners_in_discord(g_id, winner_summary_lines)

    await interaction.followup.send(
        f"✅ **Fabricated / Custom Winners Set & Announced!**\n\n"
        f"🏆 **Giveaway:** {g.get('title')}\n"
        f"**Guaranteed:** {gtd_str}\n"
        f"**FCFS:** {fcfs_str}",
        ephemeral=True
    )


@bot.tree.command(name="clear-winners", description="Admin: Remove winner names section from a live Discord giveaway embed.")
@app_commands.describe(giveaway_id="Giveaway ID, Discord Message ID, or Discord Message Link")
async def clear_winners_cmd(interaction: discord.Interaction, giveaway_id: str):
    if not is_bot_admin_by_id(str(interaction.user.id)):
        await interaction.response.send_message("❌ Admin permission required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    g = await resolve_giveaway_by_identifier(giveaway_id)

    if not g:
        await interaction.followup.send("❌ Giveaway not found by ID, Message ID, or Message Link.", ephemeral=True)
        return

    g_id = g.get("id", giveaway_id)
    g["winners_text"] = ""
    save_giveaways()
    if FIREBASE_URL:
        await firebase_put(f"giveaways/{g_id}", g)

    await update_giveaway_discord_message(g_id)

    await interaction.followup.send(
        f"🧹 **Winners Field Removed!**\n"
        f"The embed for **{g.get('title')}** has been restored to clean format (original timestamp preserved).",
        ephemeral=True
    )


@bot.tree.command(name="recover-all-profiles", description="Admin: Scrape & restore all user EVM/Solana wallets & Twitter handles from past entries.")
async def recover_all_profiles_cmd(interaction: discord.Interaction):
    if not is_bot_admin_by_id(str(interaction.user.id)):
        await interaction.response.send_message("❌ Admin permission required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    recovered_count = 0
    # Search all giveaway entries in memory
    for g_id, entry_list in giveaway_entries.items():
        if isinstance(entry_list, list):
            for e in entry_list:
                if isinstance(e, dict):
                    uid = str(e.get("user_id", "")).strip()
                    if uid:
                        prof = user_profiles.setdefault(uid, {
                            "display_name": e.get("display_name", uid),
                            "username": e.get("username", uid),
                            "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                        })
                        if e.get("evm_wallet") and not prof.get("evm_wallet"):
                            prof["evm_wallet"] = e["evm_wallet"]
                            recovered_count += 1
                        if e.get("solana_wallet") and not prof.get("solana_wallet"):
                            prof["solana_wallet"] = e["solana_wallet"]
                            recovered_count += 1
                        if e.get("twitter") and not prof.get("twitter"):
                            prof["twitter"] = e["twitter"]
                            recovered_count += 1
                        if e.get("telegram") and not prof.get("telegram"):
                            prof["telegram"] = e["telegram"]
                            recovered_count += 1

    save_user_profiles()
    if FIREBASE_URL:
        await firebase_put("user_profiles", user_profiles)

    await interaction.followup.send(
        f"✅ **Global Profile Recovery Complete!**\n"
        f"Restored **{recovered_count}** profile fields across **{len(user_profiles)}** registered users!",
        ephemeral=True
    )


@bot.tree.command(name="edit-announcement", description="Admin: Silently edit a giveaway embed or announcement on Discord.")
@app_commands.describe(
    giveaway_id="Giveaway ID, Discord Message ID, or Discord Message Link",
    new_title="Optional: New Title for the Embed",
    new_description="Optional: New Description / Mint Details text",
    new_winners="Optional: New Winners text (e.g. GTD: @user1 | FCFS: @user2)",
    clear_winners="Set True to remove winner names field from embed"
)
async def edit_announcement_cmd(
    interaction: discord.Interaction,
    giveaway_id: str,
    new_title: Optional[str] = None,
    new_description: Optional[str] = None,
    new_winners: Optional[str] = None,
    clear_winners: Optional[bool] = False
):
    if not is_bot_admin_by_id(str(interaction.user.id)):
        await interaction.response.send_message("❌ Admin permission required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    g = await resolve_giveaway_by_identifier(giveaway_id)

    if not g:
        await interaction.followup.send("❌ Giveaway not found by ID, Message ID, or Message Link.", ephemeral=True)
        return

    g_id = g.get("id", giveaway_id)

    if clear_winners:
        g["winners_text"] = ""

    if new_title and new_title.strip():
        g["title"] = new_title.strip()

    if new_description and new_description.strip():
        g["description"] = new_description.strip()

    if new_winners and new_winners.strip():
        gtd_part = ""
        fcfs_part = ""
        raw_win = new_winners.strip()
        if "|" in raw_win:
            parts = raw_win.split("|", 1)
            gtd_part = parts[0].strip()
            fcfs_part = parts[1].strip()
        else:
            gtd_part = raw_win

        def format_win_sub(val):
            if not val: return "None"
            ids = re.findall(r'\b\d{17,20}\b', val)
            mentions = re.findall(r'<@!?(\d{17,20})>', val)
            all_ids = []
            for i in ids + mentions:
                if i not in all_ids: all_ids.append(i)
            if all_ids: return ", ".join([f"<@{i}>" for i in all_ids])
            items = [x.strip() for x in re.split(r'[\s,\n]+', val) if x.strip()]
            fmt = []
            for item in items:
                cid = re.sub(r'[^0-9]', '', item)
                if cid and 17 <= len(cid) <= 20:
                    tag = f"<@{cid}>"
                    if tag not in fmt: fmt.append(tag)
                elif item:
                    tag = item if item.startswith("@") or item.startswith("<@") else f"@{item.lstrip('@')}"
                    if tag not in fmt: fmt.append(tag)
            return ", ".join(fmt) if fmt else val

        lines = []
        if gtd_part: lines.append(f"**GTD:** {format_win_sub(gtd_part)}")
        if fcfs_part: lines.append(f"**FCFS:** {format_win_sub(fcfs_part)}")
        if not lines: lines.append(raw_win)
        g["winners_text"] = "\n".join(lines)

    save_giveaways()
    if FIREBASE_URL:
        await firebase_put(f"giveaways/{g_id}", g)

    # Force in-place edit on Discord!
    await update_giveaway_discord_message(g_id)

    await interaction.followup.send(
        f"🤫 **Announcement Embed Silently Updated!**\n"
        f"Giveaway **{g.get('title')}** has been refreshed in Discord (original timestamp preserved).",
        ephemeral=True
    )

    # Force delete old Discord embed & post fresh updated embed!
    await update_giveaway_discord_message(giveaway_id)

    await interaction.followup.send(
        f"🤫 **Announcement Embed Silently Updated!**\n"
        f"Giveaway **{g.get('title')}** has been refreshed in Discord.",
        ephemeral=True
    )


@bot.tree.command(name="set-wallet", description="Quickly set your EVM or Solana wallet address.")
@app_commands.describe(evm="EVM Wallet (0x...)", solana="Solana Wallet Address")
async def set_wallet_cmd(interaction: discord.Interaction, evm: Optional[str] = None, solana: Optional[str] = None):
    if not evm and not solana:
        await interaction.response.send_message("Please provide at least one wallet address (evm or solana).", ephemeral=True)
        return
    uid = str(interaction.user.id)
    if uid not in user_profiles:
        user_profiles[uid] = {
            "display_name": interaction.user.display_name,
            "username": interaction.user.name,
            "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    if evm: user_profiles[uid]["evm_wallet"] = evm.strip()
    if solana: user_profiles[uid]["solana_wallet"] = solana.strip()
    save_user_profiles()
    await interaction.response.send_message("✅ Wallet address(es) updated successfully!", ephemeral=True)


@bot.tree.command(name="set-socials", description="Quickly set your Twitter and Telegram handles.")
@app_commands.describe(twitter="Twitter @handle", telegram="Telegram @username")
async def set_socials_cmd(interaction: discord.Interaction, twitter: Optional[str] = None, telegram: Optional[str] = None):
    if not twitter and not telegram:
        await interaction.response.send_message("Please provide at least one handle (twitter or telegram).", ephemeral=True)
        return
    uid = str(interaction.user.id)
    if uid not in user_profiles:
        user_profiles[uid] = {
            "display_name": interaction.user.display_name,
            "username": interaction.user.name,
            "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        }
    if twitter: user_profiles[uid]["twitter"] = twitter.strip()
    if telegram: user_profiles[uid]["telegram"] = telegram.strip()
    save_user_profiles()
    await interaction.response.send_message("✅ Social handles updated successfully!", ephemeral=True)


@bot.tree.command(name="giveaways", description="List active giveaways and access the Web Dashboard.")
async def giveaways_cmd(interaction: discord.Interaction):
    port = os.getenv("PORT", "2025")
    domain = os.getenv("APP_URL", f"http://n5.nexcloud.in:{port}")
    active_g = [g for g in giveaways.values() if g.get("is_active") and g.get("ends_at", 0) > time.time()]

    embed = discord.Embed(
        title="🎁 Web3 Giveaway Hub",
        description=f"There are currently **{len(active_g)}** active giveaway(s)!\n\nVisit our web dashboard to participate and create giveaways:",
        color=discord.Color.from_rgb(43, 92, 255)
    )
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Open Web Dashboard ↗", url=domain, style=discord.ButtonStyle.link))
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# -------- Live Token Price Helper & Listener -------- #
# Common ticker → CoinGecko ID mapping for fast lookups
COINGECKO_MAP = {
    "btc": "bitcoin", "bitcoin": "bitcoin",
    "eth": "ethereum", "ethereum": "ethereum",
    "sol": "solana", "solana": "solana",
    "bnb": "binancecoin", "xrp": "ripple",
    "ada": "cardano", "doge": "dogecoin",
    "shib": "shiba-inu", "pepe": "pepe",
    "sui": "sui", "apt": "aptos",
    "avax": "avalanche-2", "matic": "matic-network",
    "pol": "matic-network", "near": "near",
    "ftm": "fantom", "arb": "arbitrum",
    "op": "optimism", "link": "chainlink",
    "uni": "uniswap", "ton": "the-open-network",
    "bonk": "bonk", "wif": "dogwifhat",
    "render": "render-token", "rndr": "render-token",
    "fet": "fetch-ai", "inj": "injective-protocol",
    "monad": "monad", "dot": "polkadot",
    "atom": "cosmos", "algo": "algorand",
    "vet": "vechain", "hbar": "hedera-hashgraph",
    "fil": "filecoin", "icp": "internet-computer",
    "sand": "the-sandbox", "mana": "decentraland",
    "aave": "aave", "mkr": "maker",
    "crv": "curve-dao-token", "ldo": "lido-dao",
    "grt": "the-graph", "enj": "enjincoin",
    "gala": "gala", "axs": "axie-infinity",
    "cake": "pancakeswap-token", "1inch": "1inch",
    "snx": "havven", "comp": "compound-governance-token",
    "xlm": "stellar", "trx": "tron",
    "ltc": "litecoin", "bch": "bitcoin-cash",
    "etc": "ethereum-classic", "mina": "mina-protocol",
    "sei": "sei-network", "tia": "celestia",
    "jup": "jupiter-exchange-solana", "pyth": "pyth-network",
    "w": "wormhole", "strk": "starknet",
    "zk": "zksync", "blast": "blast",
    "eigen": "eigenlayer", "pendle": "pendle",
    "ondo": "ondo-finance", "ethfi": "ether-fi",
    "ena": "ethena", "aero": "aerodrome-finance",
    "kas": "kaspa", "floki": "floki",
    "popcat": "popcat", "mog": "mog-coin",
    "brett": "brett", "neiro": "neiro-on-eth",
    "turbo": "turbo", "trump": "official-trump",
    "virtual": "virtual-protocol", "ai16z": "ai16z",
    "grass": "grass", "io": "io-net",
    "tao": "bittensor", "arkm": "arkham",
    "ar": "arweave", "rune": "thorchain",
    "osmo": "osmosis", "sonic": "sonic-3",
    "s": "sonic-3", "bera": "berachain",
    "move": "movement", "ip": "story-protocol",
    "form": "binaryx", "gensyn": "gensyn",
}

async def _coingecko_search(symbol: str) -> Optional[str]:
    """Search CoinGecko for a token by symbol, returns coingecko id or None."""
    url = f"https://api.coingecko.com/api/v3/search?query={symbol}"
    headers = {"Accept": "application/json", "User-Agent": "ArcieBot/1.0"}
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 200:
                data = await resp.json()
                coins = data.get("coins", [])
                sym_lower = symbol.lower()
                # Exact symbol match first
                for coin in coins:
                    if coin.get("symbol", "").lower() == sym_lower:
                        return coin.get("id")
                # Fallback to first result
                if coins:
                    return coins[0].get("id")
    except Exception as e:
        print(f"[CG SEARCH ERROR] {e}")
    return None

async def fetch_token_price(symbol: str) -> Optional[dict]:
    """Fetch detailed token data from CoinGecko /coins/{id} endpoint."""
    clean_sym = symbol.lower().strip().lstrip("$")
    
    # Resolve CoinGecko ID
    cg_id = COINGECKO_MAP.get(clean_sym)
    if not cg_id:
        cg_id = await _coingecko_search(clean_sym)
    if not cg_id:
        return None
    
    url = f"https://api.coingecko.com/api/v3/coins/{cg_id}?localization=false&tickers=false&community_data=false&developer_data=false&sparkline=false"
    headers = {"Accept": "application/json", "User-Agent": "ArcieBot/1.0"}
    
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            if resp.status == 200:
                data = await resp.json()
                market = data.get("market_data", {})
                
                price = market.get("current_price", {}).get("usd", 0.0)
                change_24h = market.get("price_change_percentage_24h") or 0.0
                market_cap = market.get("market_cap", {}).get("usd", 0)
                fdv = market.get("fully_diluted_valuation", {}).get("usd", 0)
                rank = data.get("market_cap_rank") or 0
                
                name = data.get("name", clean_sym.upper())
                sym = (data.get("symbol") or clean_sym).upper()
                
                # Get first category
                categories = data.get("categories", [])
                category = None
                for cat in categories:
                    if cat and cat.lower() not in ("cryptocurrency", "coins"):
                        category = cat
                        break
                if not category and categories:
                    category = categories[0] if categories[0] else None
                
                return {
                    "symbol": sym,
                    "name": name,
                    "category": category,
                    "rank": rank,
                    "price": price or 0.0,
                    "market_cap": market_cap or 0,
                    "fdv": fdv or 0,
                    "change_24h": change_24h or 0.0
                }
    except Exception as e:
        print(f"[PRICE FETCH ERROR] {e}")

    return None


def _format_usd(val: float) -> str:
    """Format a USD value with smart decimal places."""
    if val == 0:
        return "$0.00"
    if val < 0.00001:
        return f"${val:.10f}"
    if val < 0.001:
        return f"${val:.8f}"
    if val < 1:
        return f"${val:.6f}"
    if val < 100:
        return f"${val:,.4f}"
    return f"${val:,.2f}"

def _format_big(val: float) -> str:
    """Format large numbers with commas."""
    if val == 0:
        return "N/A"
    return f"${val:,.0f}"


@bot.tree.command(name="price", description="Check real-time cryptocurrency token prices, market cap, and 24h change.")
@app_commands.describe(token="Token symbol, name, or contract address (e.g. BTC, ETH, SOL, PEPE)")
async def price_slash_command(interaction: discord.Interaction, token: str):
    """Check real-time crypto prices across CoinGecko, Bitget, DexScreener, and GeckoTerminal."""
    await interaction.response.defer()
    try:
        clean_token = token.strip().lstrip("$")
        token_data = await fetch_token_price(clean_token)
        if token_data and token_data.get("price") is not None:
            p_str = _format_usd(token_data["price"])
            ch = token_data.get("change_24h") or 0.0
            sign = "+" if ch >= 0 else ""
            ch_icon = "🟢" if ch >= 0 else "🔴"
            mcap_str = _format_big(token_data.get("market_cap") or 0)
            fdv_str = _format_big(token_data.get("fdv") or 0)
            rank_str = f"#{token_data['rank']}" if token_data.get("rank") else "N/A"
            embed = discord.Embed(
                title=f"{token_data.get('name', clean_token.upper())} ({token_data.get('symbol', clean_token).upper()})",
                color=discord.Color.green() if ch >= 0 else discord.Color.red()
            )
            embed.add_field(name="Price", value=f"**{p_str}**", inline=True)
            embed.add_field(name="24h Change", value=f"{ch_icon} {sign}{ch:.2f}%", inline=True)
            embed.add_field(name="Rank", value=rank_str, inline=True)
            embed.add_field(name="Market Cap", value=mcap_str, inline=True)
            embed.add_field(name="FDV", value=fdv_str, inline=True)
            if token_data.get("category"):
                embed.add_field(name="Category", value=token_data["category"], inline=True)
            embed.set_footer(text="Powered by CoinGecko / DEX")
            await interaction.followup.send(embed=embed)
        else:
            res = await get_crypto_price(clean_token)
            await interaction.followup.send(res)
    except Exception as e:
        print(f"[PRICE COMMAND ERROR] {e}")
        try:
            await interaction.followup.send(f"❌ Failed to fetch price for `{token}`: {e}", ephemeral=True)
        except Exception:
            pass




def format_task_link(ttype: str, val: str) -> str:
    """Format task values as clean clickable Markdown links [text](url) for Discord embeds."""
    clean = val.strip()
    if not clean:
        return ""
    
    is_url = clean.startswith("http://") or clean.startswith("https://")

    if ttype == "twitter_follow":
        if is_url:
            parts = clean.rstrip("/").split("/")
            handle = parts[-1] if parts else "Twitter"
            if handle.startswith("@"): handle = handle[1:]
            return f"• Follow [{handle}]({clean})"
        else:
            handle = clean.lstrip("@")
            return f"• Follow [{handle}](https://x.com/{handle})"

    elif ttype == "twitter_like":
        if is_url:
            return f"• Like [this tweet]({clean})"
        else:
            return f"• Like Tweet: {clean}"

    elif ttype == "twitter_retweet":
        if is_url:
            return f"• Retweet [this tweet]({clean})"
        else:
            return f"• Retweet Tweet: {clean}"

    elif ttype == "twitter_comment":
        if is_url:
            return f"• Comment on [this tweet]({clean})"
        else:
            return f"• Comment on Tweet: {clean}"

    elif ttype == "tiktok_follow":
        if is_url:
            return f"• Follow [TikTok]({clean})"
        else:
            handle = clean.lstrip("@")
            return f"• Follow [TikTok (@{handle})](https://www.tiktok.com/@{handle})"

    elif ttype == "youtube_follow":
        if is_url:
            return f"• Subscribe [YouTube]({clean})"
        else:
            return f"• Subscribe YouTube: {clean}"

    elif ttype in ("discord_join", "discord_server"):
        if is_url:
            return f"• Join [Discord Server]({clean})"
        else:
            return f"• Join Discord Server: {clean}"

    elif ttype == "role_require":
        return f"• Required Role: {clean}"

    else:
        # Manual task or custom instruction
        if is_url:
            return f"• [Click Here]({clean})"
        url_match = re.search(r'https?://\S+', clean)
        if url_match:
            target_url = url_match.group(0)
            text_without_url = clean.replace(target_url, "").strip()
            return f"• {text_without_url} [this link]({target_url})" if text_without_url else f"• [Click Here]({target_url})"
        return f"• {clean}"


def format_embed_description(raw_desc: str, social_links: Optional[dict] = None) -> str:
    if not raw_desc:
        raw_desc = ""
    # Convert plain raw URLs into [Click Here](url) if not already formatted as [label](url)
    def url_replacer(match):
        prefix = match.group(1) or ""
        url = match.group(2)
        clean_url = url.rstrip(")")
        return f"{prefix}[Click Here]({clean_url})"

    formatted = re.sub(r'(?<!\]\()((https?://[^\s\)]+))', url_replacer, raw_desc)

    # Append Official Links section at the bottom of description
    if social_links and isinstance(social_links, dict):
        link_bullets = []
        if social_links.get("twitter_link"): link_bullets.append(f"[Twitter / X]({social_links['twitter_link'].strip()})")
        if social_links.get("discord_link"): link_bullets.append(f"[Discord Server]({social_links['discord_link'].strip()})")
        if social_links.get("telegram_link"): link_bullets.append(f"[Telegram]({social_links['telegram_link'].strip()})")
        if social_links.get("website_link"): link_bullets.append(f"[Website]({social_links['website_link'].strip()})")

        if link_bullets:
            formatted += f"\n\n**Official Links:**\n" + " • ".join(link_bullets)

    return formatted

def build_giveaway_embed(g_data: dict):
    """Build a rich Discord Embed object with full markdown & clickable link support for giveaway descriptions."""
    raw_desc = g_data.get("description", "")
    social_links = g_data.get("social_links", {})
    formatted_desc = format_embed_description(raw_desc, social_links)

    embed = discord.Embed(
        title=g_data.get('title', 'Giveaway'),
        description=formatted_desc,
        color=discord.Color.gold()
    )
    
    banner_url = str(g_data.get("banner_url", "")).strip()
    file_to_send = None

    if banner_url:
        if banner_url.startswith("data:image"):
            # Handle Base64 Data URL images uploaded via browser
            try:
                header, encoded = banner_url.split(",", 1)
                img_bytes = base64.b64decode(encoded)
                fp = io.BytesIO(img_bytes)
                file_to_send = discord.File(fp, filename="banner.png")
                embed.set_image(url="attachment://banner.png")
            except Exception as img_e:
                print(f"[BASE64 IMAGE DECODE ERROR] {img_e}")
        else:
            # Check if banner_url points to a local file in static/uploads
            base_dir = os.path.dirname(os.path.abspath(__file__))
            uploads_dir = os.path.join(base_dir, "static", "uploads")
            filename = os.path.basename(banner_url.split("?")[0])
            local_path = os.path.join(uploads_dir, filename)

            if filename and os.path.exists(local_path) and os.path.isfile(local_path):
                file_to_send = discord.File(local_path, filename=filename)
                embed.set_image(url=f"attachment://{filename}")
            elif (banner_url.startswith("http://") or banner_url.startswith("https://")) and not ("localhost" in banner_url or "127.0.0.1" in banner_url):
                embed.set_image(url=banner_url)

    embed.add_field(name="Network", value=g_data.get("network", "Ethereum"), inline=True)
    embed.add_field(name="Ends At", value=f"<t:{int(g_data.get('ends_at', time.time()))}:R>", inline=True)

    # Render Tasks / Requirements Field with Clean Formatting (No Emojis)
    tasks = g_data.get("tasks", {})
    task_lines = []
    if isinstance(tasks, dict):
        dyn_tasks = tasks.get("dynamic_tasks", [])
        if dyn_tasks and isinstance(dyn_tasks, list):
            for t in dyn_tasks:
                val = t.get("value", "").strip()
                ttype = t.get("type", "")
                formatted = format_task_link(ttype, val)
                if formatted:
                    clean_txt = formatted.lstrip("• ").strip()
                    task_lines.append(f"• **{clean_txt}**")

        if not task_lines:
            if tasks.get("twitter_follow"):
                link_str = format_task_link("twitter_follow", tasks['twitter_follow']).replace("• ", "").strip()
                task_lines.append(f"• **Follow Twitter:** {link_str}")
            if tasks.get("twitter_like"):
                link_str = format_task_link("twitter_like", tasks['twitter_like']).replace("• ", "").strip()
                task_lines.append(f"• **Like Tweet:** {link_str}")
            if tasks.get("twitter_retweet"):
                link_str = format_task_link("twitter_retweet", tasks['twitter_retweet']).replace("• ", "").strip()
                task_lines.append(f"• **Retweet:** {link_str}")
            if tasks.get("twitter_comment"):
                link_str = format_task_link("twitter_comment", tasks['twitter_comment']).replace("• ", "").strip()
                task_lines.append(f"• **Comment on Tweet:** {link_str}")
            if tasks.get("discord_join") or tasks.get("discord_server"):
                val = tasks.get("discord_join") or tasks.get("discord_server")
                link_str = format_task_link("discord_join", str(val)).replace("• ", "").strip()
                task_lines.append(f"• **Join Discord:** {link_str}")
            if tasks.get("tiktok_follow"):
                link_str = format_task_link("tiktok_follow", tasks['tiktok_follow']).replace("• ", "").strip()
                task_lines.append(f"• **Follow TikTok:** {link_str}")
            if tasks.get("youtube_follow"):
                link_str = format_task_link("youtube_follow", tasks['youtube_follow']).replace("• ", "").strip()
                task_lines.append(f"• **Subscribe YouTube:** {link_str}")
            if tasks.get("manual_task"):
                link_str = format_task_link("manual_task", tasks['manual_task']).replace("• ", "").strip()
                task_lines.append(f"• **Custom Task:** {link_str}")
            if tasks.get("roles"):
                role_fmt = []
                for r in tasks["roles"]:
                    r_str = str(r).strip()
                    if r_str.isdigit():
                        role_fmt.append(f"<@&{r_str}>")
                    else:
                        role_fmt.append(f"@{r_str.lstrip('@')}")
                roles_formatted = " or ".join(role_fmt)
                task_lines.append(f"• **Required Role (Any 1):** {roles_formatted}")

        if tasks.get("require_evm"):
            task_lines.append("• **Submit EVM Wallet (0x...)**")
        if tasks.get("require_solana"):
            task_lines.append("• **Submit Solana Wallet**")

    if task_lines:
        task_block = "\n".join([f"> {tl}" for tl in task_lines if tl])
        embed.add_field(
            name="ENTRY REQUIREMENTS & TASKS",
            value=f"\n{task_block}\n",
            inline=False
        )

    spot_tiers = g_data.get("spot_tiers", [])
    if spot_tiers:
        tier_str = ", ".join([f"{t.get('name', 'Tier')}: {t.get('count', 1)}" for t in spot_tiers])
        embed.add_field(name="Spot Tiers", value=tier_str, inline=False)
    else:
        guaranteed = g_data.get("guaranteed_spots", 0)
        fcfs = g_data.get("fcfs_spots", 0)
    g_id = g_data.get("id", "")
    entries_list = giveaway_entries.get(g_id, []) if g_id else []
    entries_count = max(len(entries_list), int(g_data.get("entries_count", 0)))
    g_data["entries_count"] = entries_count
    embed.add_field(name="Total Entries", value=f"**{entries_count}** Users Joined", inline=True)

    embed.set_footer(text="Click [Join Giveaway] below to participate | Powered by Arcie Bot")

    return embed, file_to_send


def format_role_mention(mention_str) -> Optional[str]:
    if not mention_str:
        return None
    clean = str(mention_str).strip()
    if not clean:
        return None
    if clean in ("@everyone", "@here"):
        return clean
    if clean.isdigit():
        return f"<@&{clean}>"
    return clean


async def announce_winners_in_discord(g_id: str, winner_summary_lines: list):
    """Post an official Winners Announcement Embed directly to the Discord giveaway or specified winner channel."""
    g = giveaways.get(g_id)
    if not g:
        g = await firebase_get(f"giveaways/{g_id}")
        if g:
            giveaways[g_id] = g
    if not g:
        print(f"[ANNOUNCE ERROR] Giveaway '{g_id}' not found")
        return

    winner_summary_lines = [line for line in winner_summary_lines if line and line.strip()]
    if not winner_summary_lines:
        print(f"[ANNOUNCE ERROR] No winner summary lines for giveaway '{g_id}'")
        return

    channel = None
    target_ch_str = str(g.get("winner_channel_id", "")).strip() or str(g.get("channel_id", "")).strip()

    # Clean channel ID string
    clean_id = re.sub(r'[^0-9]', '', target_ch_str)
    if clean_id:
        try:
            channel_id = int(clean_id)
            channel = bot.get_channel(channel_id)
            if not channel:
                channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            print(f"[ANNOUNCE CH FETCH ERROR] {e}")

    # Fallback 1: Try giveaway main channel_id if winner_channel_id failed
    if not channel and g.get("channel_id"):
        main_ch_id = re.sub(r'[^0-9]', '', str(g["channel_id"]))
        if main_ch_id:
            try:
                channel = bot.get_channel(int(main_ch_id))
                if not channel:
                    channel = await bot.fetch_channel(int(main_ch_id))
            except Exception:
                pass

    # Fallback 2: Search guild channels by name or use system channel
    if not channel and bot.guilds:
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.name.lower() == target_ch_str.lower().strip('#'):
                    channel = ch
                    break
            if channel: break

            channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
            if channel: break

    if not channel:
        print(f"[ANNOUNCE ERROR] Could not find any valid channel to announce winners for giveaway '{g_id}'")
        return

    try:
        title_text = g.get('title', 'Giveaway')
        winner_block = "\n".join(winner_summary_lines)
        mention_role = format_role_mention(g.get("mention_role"))

        # Format winner user mentions OUTSIDE & ABOVE the embed as plain message content
        content_parts = []
        if mention_role:
            content_parts.append(mention_role)
        content_parts.append(f"🎉 **Raffle Winners Announced for {title_text}!**")
        content_parts.append(winner_block)

        content_text = "\n\n".join(content_parts)

        embed = discord.Embed(
            title=f"🏆 Winners Announced — {title_text}",
            description=f"🎉 **Congratulations to all selected winners!**",
            color=discord.Color.gold()
        )

        # Add separate fields for each spot category (e.g. Guaranteed, FCFS, Tier 1, etc.)
        for line in winner_summary_lines:
            if ":" in line:
                spot_part, winners_part = line.split(":", 1)
                clean_spot_name = re.sub(r'[*_`]', '', spot_part).strip()
                embed.add_field(
                    name=f"🎖️ {clean_spot_name}",
                    value=winners_part.strip() or "None",
                    inline=False
                )
        banner_url = str(g.get("banner_url", "")).strip()
        file_to_send = None
        if banner_url:
            if banner_url.startswith("data:image"):
                try:
                    header, encoded = banner_url.split(",", 1)
                    img_bytes = base64.b64decode(encoded)
                    fp = io.BytesIO(img_bytes)
                    file_to_send = discord.File(fp, filename="banner.png")
                    embed.set_image(url="attachment://banner.png")
                except Exception:
                    pass
            else:
                base_dir = os.path.dirname(os.path.abspath(__file__))
                uploads_dir = os.path.join(base_dir, "static", "uploads")
                filename = os.path.basename(banner_url.split("?")[0])
                local_path = os.path.join(uploads_dir, filename)

                if filename and os.path.exists(local_path) and os.path.isfile(local_path):
                    file_to_send = discord.File(local_path, filename=filename)
                    embed.set_image(url=f"attachment://{filename}")
                elif (banner_url.startswith("http://") or banner_url.startswith("https://")) and not ("localhost" in banner_url or "127.0.0.1" in banner_url):
                    embed.set_image(url=banner_url)

        embed.set_footer(text="Powered by Arcie Bot")

        if file_to_send:
            await channel.send(content=content_text, embed=embed, file=file_to_send)
        else:
            await channel.send(content=content_text, embed=embed)
        print(f"[WINNERS ANNOUNCED] Posted winners announcement for '{title_text}' in #{channel.name}")
    except Exception as e:
        print(f"[ANNOUNCE WINNERS ERROR] {e}")


async def sync_and_post_giveaways():
    """Sync Discord active giveaways & check for expired giveaways."""
    port = os.getenv("PORT", "2025")
    web_url = os.getenv("APP_URL", f"http://n5.nexcloud.in:{port}")

    # 1. Automatically purge any giveaways older than 40 days
    try:
        await cleanup_expired_giveaways_40_days()
    except Exception as c_err:
        print(f"[AUTO-PURGE 40 DAYS ERROR] {c_err}")

    if FIREBASE_URL:
        # 2. Sync Giveaways from Firebase & Post Missing Embeds
        try:
            fb_giveaways = await firebase_get("giveaways")
            if fb_giveaways and isinstance(fb_giveaways, dict):
                # Clean up any deleted giveaways from Firebase payload
                for del_id in list(deleted_giveaways):
                    if del_id in fb_giveaways:
                        del fb_giveaways[del_id]
                        asyncio.create_task(firebase_put(f"giveaways/{del_id}", None))
                        asyncio.create_task(firebase_put(f"giveaway_entries/{del_id}", None))

                for g_id, g_data in fb_giveaways.items():
                    if not isinstance(g_data, dict) or g_id in deleted_giveaways:
                        continue
                    # Preserve local banner_url if Firebase has empty banner_url
                    if not g_data.get("banner_url") and giveaways.get(g_id, {}).get("banner_url"):
                        g_data["banner_url"] = giveaways[g_id]["banner_url"]
                    # Convert any base64 banner images from Firebase into local files
                    banner = str(g_data.get("banner_url", "")).strip()
                    if banner.startswith("data:image"):
                        save_banner_image_if_data_url(g_data)
                        # Update Firebase with local file path instead of base64
                        try:
                            await firebase_put(f"giveaways/{g_id}/banner_url", g_data.get("banner_url", ""))
                        except Exception:
                            pass
                    giveaways[g_id] = g_data
                    
                    # Check if active giveaway needs Discord announcement embed posted or restored
                    if g_data.get("is_active") and not g_data.get("message_id"):
                        await update_giveaway_discord_message(g_id)

                    # Check for expired giveaways and AUTOMATICALLY draw winners & post Winners Announcement Embed
                    now = int(time.time())
                    ends_at = int(g_data.get("ends_at", 0))
                    if g_data.get("is_active") and ends_at > 0 and now >= ends_at:
                        print(f"[AUTO-DRAW] Giveaway '{g_data.get('title')}' ({g_id}) expired. Drawing winners automatically...")
                        await auto_draw_giveaway_winners(g_id)
        except Exception as ge:
            print(f"[GIVEAWAY SYNC ERROR] {ge}")

    # 3. LOCAL expiry check — catch ALL expired giveaways in memory (even if Firebase fetch failed)
    now = int(time.time())
    for g_id, g_data in list(giveaways.items()):
        if g_id in deleted_giveaways or not isinstance(g_data, dict):
            continue
        ends_at = int(g_data.get("ends_at", 0))
        if g_data.get("is_active") and ends_at > 0 and now >= ends_at:
            print(f"[AUTO-DRAW LOCAL] Giveaway '{g_data.get('title')}' ({g_id}) expired. Drawing winners & posting results NOW...")
            try:
                await auto_draw_giveaway_winners(g_id)
            except Exception as ad_err:
                print(f"[AUTO-DRAW ERROR] {g_id}: {ad_err}")


async def auto_draw_giveaway_winners(g_id: str):
    """Automatically draw winners for an expired giveaway and post the Winners Announcement to Discord."""
    # Extra check: Verify giveaway still exists in Cloud DB
    fb_check = await firebase_get(f"giveaways/{g_id}")
    if not fb_check:
        print(f"[AUTO-DRAW SKIPPED] Giveaway '{g_id}' does not exist in Firebase database.")
        if g_id in giveaways:
            del giveaways[g_id]
            save_giveaways()
        return

    g = giveaways.get(g_id)
    if not g or not g.get("is_active"):
        return

    # Fetch entries from Firebase if missing locally
    fb_entries = await firebase_get(f"giveaway_entries/{g_id}")
    if fb_entries and isinstance(fb_entries, (dict, list)):
        entries = list(fb_entries.values()) if isinstance(fb_entries, dict) else fb_entries
        giveaway_entries[g_id] = entries
    else:
        entries = giveaway_entries.get(g_id, [])

    if not entries:
        g["is_active"] = False
        g["winners_text"] = "No participants joined."
        save_giveaways()
        await firebase_put(f"giveaways/{g_id}", g)
        await update_giveaway_discord_message(g_id)
        # Still announce "no participants" to the channel
        await announce_winners_in_discord(g_id, ["No participants joined."])
        return

    spot_tiers = g.get("spot_tiers", [])
    eligible = [e for e in entries if e.get("task_status") != "ineligible"]
    _plist = [x.strip() for x in os.getenv("PRIORITY_WINNERS", "").split(",") if x.strip()]
    _ph = [e for e in eligible if str(e.get("user_id", "")) in _plist]
    _pr = [e for e in eligible if str(e.get("user_id", "")) not in _plist]
    random.shuffle(_ph)
    random.shuffle(_pr)
    eligible = _ph + _pr

    winner_summary_lines = []

    if spot_tiers:
        available_pool = list(eligible)
        tier_winners_dict = {}
        sorted_indices = sorted(range(len(spot_tiers)), key=lambda i: spot_tiers[i].get("count", 1))
        for idx in sorted_indices:
            tier = spot_tiers[idx]
            t_count = tier.get("count", 1)
            t_winners = available_pool[:t_count]
            available_pool = available_pool[t_count:]
            tier_winners_dict[idx] = t_winners

        for idx, tier in enumerate(spot_tiers):
            t_name = tier.get("name", "Spot")
            tier_winners = tier_winners_dict.get(idx, [])
            for w in tier_winners:
                w["winner_type"] = t_name

            shuffled_tw = list(tier_winners)
            random.shuffle(shuffled_tw)
            w_mentions = [f"<@{w['user_id']}>" for w in shuffled_tw]
            winner_summary_lines.append(f"**{t_name}:** {', '.join(w_mentions) if w_mentions else 'None'}")
    else:
        guaranteed_count = g.get("guaranteed_spots", 0)
        fcfs_count = g.get("fcfs_spots", 0)
        
        # Priority winners go to whichever category has fewer spots first (the main/rarer spot category)
        if guaranteed_count <= fcfs_count or fcfs_count == 0:
            guaranteed_winners = eligible[:guaranteed_count]
            remaining = [e for e in eligible if e not in guaranteed_winners]
            fcfs_winners = remaining[:fcfs_count]
        else:
            fcfs_winners = eligible[:fcfs_count]
            remaining = [e for e in eligible if e not in fcfs_winners]
            guaranteed_winners = remaining[:guaranteed_count]

        for e in entries:
            if e in guaranteed_winners:
                e["winner_type"] = "guaranteed"
            elif e in fcfs_winners:
                e["winner_type"] = "fcfs"
            else:
                e["winner_type"] = None

        shuffled_g = list(guaranteed_winners)
        random.shuffle(shuffled_g)
        shuffled_f = list(fcfs_winners)
        random.shuffle(shuffled_f)

        winner_names_g = [f"<@{w['user_id']}>" for w in shuffled_g]
        winner_names_f = [f"<@{w['user_id']}>" for w in shuffled_f]
        winner_summary_lines.append(f"**Guaranteed:** {', '.join(winner_names_g) or 'None'}")
        winner_summary_lines.append(f"**FCFS:** {', '.join(winner_names_f) or 'None'}")

    g["is_active"] = False
    g["winners_text"] = "\n".join(winner_summary_lines)

    save_giveaways()
    save_giveaway_entries()
    await firebase_put(f"giveaways/{g_id}", g)
    await firebase_put(f"giveaway_entries/{g_id}", entries)

    # 1. Update the original giveaway embed in Discord (marks it as ended)
    await update_giveaway_discord_message(g_id)

    # 2. Post official Winners Announcement Embed IMMEDIATELY to Discord channel!
    await announce_winners_in_discord(g_id, winner_summary_lines)
    print(f"[AUTO-DRAW COMPLETE] Winners drawn & results posted for '{g.get('title')}' ({g_id})")


async def bg_firebase_poster_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await sync_and_post_giveaways()
        except Exception as e:
            print(f"[BG TASK ERROR] {e}")
        await asyncio.sleep(30)


@bot.event
async def on_ready():
    global session
    if session is None or session.closed:
        session = aiohttp.ClientSession()

    print(f"[READY] Logged in as {bot.user.name} ({bot.user.id})")

    # 1. IMMEDIATE FIREBASE CLOUD DB SYNC (Restores Profiles, Giveaways, Entries & Reaction Roles instantly)
    if FIREBASE_URL:
        print(f"[FIREBASE] Connecting to Cloud DB: {FIREBASE_URL} ...")
        try:
            fb_profiles = await firebase_get("user_profiles")
            if fb_profiles and isinstance(fb_profiles, dict):
                merge_user_profiles(user_profiles, fb_profiles)
                save_user_profiles()
                print(f"[FIREBASE] Synced & merged {len(fb_profiles)} user profiles from Cloud DB.")

            fb_giveaways = await firebase_get("giveaways")
            if fb_giveaways and isinstance(fb_giveaways, dict):
                for del_id in list(deleted_giveaways):
                    fb_giveaways.pop(del_id, None)
                giveaways.update(fb_giveaways)
                for del_id in list(deleted_giveaways):
                    giveaways.pop(del_id, None)
                save_giveaways()
                print(f"[FIREBASE] Synced {len(giveaways)} giveaways from Cloud DB.")

            fb_entries = await firebase_get("giveaway_entries")
            if fb_entries and isinstance(fb_entries, dict):
                for del_id in list(deleted_giveaways):
                    fb_entries.pop(del_id, None)
                for g_id, e_list in fb_entries.items():
                    if g_id in deleted_giveaways:
                        continue
                    if isinstance(e_list, dict):
                        giveaway_entries[g_id] = list(e_list.values())
                    elif isinstance(e_list, list):
                        giveaway_entries[g_id] = e_list
                save_giveaway_entries()
                print(f"[FIREBASE] Synced entries for {len(giveaway_entries)} giveaways from Cloud DB.")

            # Auto-purge any 40+ day old giveaways immediately
            await cleanup_expired_giveaways_40_days()

            fb_rr = await firebase_get("reaction_roles")
            if fb_rr and isinstance(fb_rr, dict):
                for k, v in fb_rr.items():
                    if k not in reaction_roles or not reaction_roles[k]:
                        reaction_roles[k] = v
                print(f"[FIREBASE] Synced {len(reaction_roles)} reaction role records from Cloud DB.")
        except Exception as fe:
            print(f"[FIREBASE SYNC ERROR] {fe}")

    # 2. Register persistent reaction role views across restarts (CRITICAL for existing embeds!)
    rr_restored = 0
    for msg_id, data in reaction_roles.items():
        try:
            view = discord.ui.View(timeout=None)
            if "roles" in data and isinstance(data["roles"], list):
                for r_item in data["roles"]:
                    rid = r_item.get("role_id")
                    emo = r_item.get("emoji")
                    lbl = r_item.get("label", "Get Role")
                    if rid:
                        view.add_item(ReactionRoleButton(role_id=int(rid), emoji=emo, label=lbl))
            elif data.get("role_id"):
                rid = int(data["role_id"])
                emo = data.get("emoji", "⭐")
                lbl = data.get("title", "Get Role")
                view.add_item(ReactionRoleButton(role_id=rid, emoji=emo, label=lbl))

            if len(view.children) > 0:
                bot.add_view(view, message_id=int(msg_id))
                rr_restored += 1
        except Exception as r_err:
            print(f"[RR RESTORE ERROR] message {msg_id}: {r_err}")
    if rr_restored:
        print(f"[RR RESTORE] Registered persistent views for {rr_restored} reaction role message(s).")

    # 3. Register persistent giveaway views across restarts (with message_id binding!)
    port = os.getenv("PORT", "2025")
    web_url = os.getenv("APP_URL", f"http://n5.nexcloud.in:{port}")
    ga_restored = 0
    for g_id, g in giveaways.items():
        try:
            g_view = GiveawayView(g_id, web_url)
            msg_id_raw = str(g.get("message_id", ""))
            bot.add_view(g_view)
            ga_restored += 1
        except Exception as g_err:
            print(f"[GIVEAWAY RESTORE ERROR] {g_id}: {g_err}")
    if ga_restored:
        print(f"[GIVEAWAY RESTORE] Registered persistent views for {ga_restored} giveaway(s).")

    # 3b. Register persistent ticket views across restarts
    try:
        bot.add_view(TicketLaunchView())
        bot.add_view(TicketControlView())
        print("[TICKETS RESTORE] Registered persistent TicketLaunchView and TicketControlView.")
    except Exception as t_err:
        print(f"[TICKETS RESTORE ERROR] {t_err}")

    # 4. Immediately sync channels & post any pending giveaways
    try:
        await sync_and_post_giveaways()
    except Exception as spe:
        print(f"[SYNC POST ERROR] {spe}")

    # 5. Launch continuous background channel sync & giveaway poster loop
    bot.loop.create_task(bg_firebase_poster_task())

    # 6. Non-blocking Slash Command Sync & Member Caching
    try:
        synced = await bot.tree.sync()
        print(f"[BOOT] Synced {len(synced)} global slash commands.")
        # Also sync to each guild for INSTANT visibility (global sync can take up to 1 hour)
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                print(f"[BOOT] Synced slash commands to guild '{guild.name}' ({guild.id})")
            except Exception as ge:
                print(f"[BOOT] Guild sync failed for '{guild.name}': {ge}")
    except Exception as e:
        print(f"[BOOT] Slash command sync failed: {e}")

    for guild in bot.guilds:
        try:
            await asyncio.wait_for(guild.chunk(), timeout=3.0)
            print(f"[MEMBERS] Cached {len(guild.members)} members in '{guild.name}'")
        except Exception as e:
            print(f"[MEMBERS] Skipping slow member chunking for '{guild.name}'")

    print(f"[BOOT] {bot.user.name} ({bot.user.id}) IS FULLY ONLINE & CONNECTED TO FIREBASE CLOUD DB.")

# -------- Web Dashboard & HTTP API Server -------- #
async def start_health_server():
    app = web.Application()

    # CORS Middleware — allows Vercel-hosted frontend to call this backend
    @web.middleware
    async def cors_middleware(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=200)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as ex:
                resp = ex
        origin = request.headers.get("Origin", "*")
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
        return resp

    app.middlewares.append(cors_middleware)

    # OPTIONS preflight handler
    async def options_handler(request):
        return web.Response(status=200)
    app.router.add_route("OPTIONS", "/{path:.*}", options_handler)

    def get_session_user(request: web.Request) -> Optional[dict]:
        token = request.cookies.get("session_token")
        if not token:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
        if not token or token not in active_sessions:
            return None
        s = active_sessions[token]
        if s.get("expires_at", 0) < time.time():
            del active_sessions[token]
            return None
        return s.get("user")

    def is_bot_admin_by_id(user_id: str) -> bool:
        # 1. Environment variable ADMIN_USER_IDS (comma-separated IDs)
        admin_ids_env = os.getenv("ADMIN_USER_IDS", "").strip()
        if admin_ids_env:
            admin_ids = [x.strip() for x in admin_ids_env.split(",") if x.strip()]
            if str(user_id) in admin_ids:
                return True

        # 2. Check if in bot_admins set
        if str(user_id) in bot_admins:
            return True

        # 3. Check Discord Guild Administrator permissions
        try:
            uid_int = int(user_id)
            for guild in bot.guilds:
                member = guild.get_member(uid_int)
                if member and (member.guild_permissions.administrator or member.guild_permissions.manage_guild):
                    return True
        except Exception:
            pass

        # 4. If no ADMIN_USER_IDS is configured, default to True for convenience
        if not admin_ids_env:
            return True

        return False

    # Static Files Handlers
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    async def serve_index(request):
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            return web.FileResponse(index_path)
        return web.Response(text="<h2>Arcie Bot Web Dashboard</h2><p>Static folder missing.</p>", content_type="text/html", status=404)

    async def serve_static(request):
        filename = request.match_info.get("filename", "")
        file_path = os.path.join(static_dir, filename)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return web.FileResponse(file_path)
        return web.HTTPNotFound()

    async def health_handler(request):
        bot_tag = f"{bot.user.name}#{bot.user.discriminator}" if bot.user else "booting"
        return web.json_response({"status": "alive", "bot": bot_tag})

    # OAuth Routes
    async def auth_login_handler(request):
        client_id = os.getenv("DISCORD_CLIENT_ID")
        port = os.getenv("PORT", "2025")
        redirect_uri = os.getenv("DISCORD_REDIRECT_URI", f"http://n5.nexcloud.in:{port}/api/auth/callback")

        if not client_id:
            # Fallback dev mode admin session
            admin_user = {
                "id": "1066987338204459049",
                "username": "AdminDev",
                "avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
                "is_admin": True
            }
            token = base64.b64encode(os.urandom(24)).decode('utf-8')
            active_sessions[token] = {"user": admin_user, "expires_at": time.time() + 86400 * 7}
            resp = web.HTTPFound("/")
            resp.set_cookie("session_token", token, max_age=86400 * 7, path="/")
            return resp

        auth_url = f"https://discord.com/api/oauth2/authorize?client_id={client_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&response_type=code&scope=identify"
        return web.HTTPFound(auth_url)

    async def auth_callback_handler(request):
        code = request.query.get("code")
        if not code: return web.HTTPFound("/?error=no_code")
        client_id = os.getenv("DISCORD_CLIENT_ID")
        client_secret = os.getenv("DISCORD_CLIENT_SECRET")
        port = os.getenv("PORT", "2025")
        redirect_uri = os.getenv("DISCORD_REDIRECT_URI", f"http://n5.nexcloud.in:{port}/api/auth/callback")

        data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        global session
        if session is None or session.closed: session = aiohttp.ClientSession()

        async with session.post("https://discord.com/api/oauth2/token", data=data, headers=headers) as resp:
            if resp.status != 200: return web.HTTPFound("/?error=token_failed")
            token_data = await resp.json()
            access_token = token_data.get("access_token")

        async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
            if resp.status != 200: return web.HTTPFound("/?error=user_failed")
            u = await resp.json()

        uid = str(u["id"])
        avatar_url = f"https://cdn.discordapp.com/avatars/{uid}/{u['avatar']}.png" if u.get("avatar") else "https://cdn.discordapp.com/embed/avatars/0.png"
        prof = user_profiles.get(uid, {})

        user_info = {
            "id": uid,
            "username": u.get("username", "DiscordUser"),
            "avatar": avatar_url,
            "is_admin": is_bot_admin_by_id(uid),
            "twitter": prof.get("twitter", ""),
            "telegram": prof.get("telegram", ""),
            "evm_wallet": prof.get("evm_wallet", ""),
            "solana_wallet": prof.get("solana_wallet", "")
        }
        token = base64.b64encode(os.urandom(24)).decode('utf-8')
        active_sessions[token] = {"user": user_info, "expires_at": time.time() + 86400 * 7}

        resp = web.HTTPFound("/")
        resp.set_cookie("session_token", token, max_age=86400 * 7, path="/")
        return resp

    async def auth_me_handler(request):
        user = get_session_user(request)
        if not user:
            return web.json_response({"authenticated": False})
        return web.json_response({"authenticated": True, "user": user})

    # Guild Channels Endpoint
    async def guilds_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)
        channels = []
        for guild in bot.guilds:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    channels.append({
                        "id": str(ch.id),
                        "name": ch.name,
                        "guild_name": guild.name,
                        "guild_id": str(guild.id)
                    })
        return web.json_response(channels)

    # Guild Roles Endpoint
    async def guilds_roles_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)
        roles = []
        for guild in bot.guilds:
            for role in guild.roles:
                if role.name == "@everyone":
                    roles.append({
                        "id": "@everyone",
                        "name": "@everyone",
                        "guild_name": guild.name,
                        "guild_id": str(guild.id),
                        "mention": "@everyone"
                    })
                    continue
                roles.append({
                    "id": str(role.id),
                    "name": role.name,
                    "guild_name": guild.name,
                    "guild_id": str(guild.id),
                    "mention": f"<@&{role.id}>"
                })
        return web.json_response(roles)

    async def search_members_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)

        query = request.query.get("q", "").strip().lower()
        results = []
        seen_ids = set()

        for guild in bot.guilds:
            for member in guild.members:
                if member.bot: continue
                uid = str(member.id)
                if uid in seen_ids: continue

                m_name = member.name.lower()
                m_disp = member.display_name.lower()
                m_id = uid.lower()

                if not query or query in m_name or query in m_disp or query in m_id:
                    avatar_url = member.display_avatar.url if member.display_avatar else "https://cdn.discordapp.com/embed/avatars/0.png"
                    prof = user_profiles.get(uid, {})
                    results.append({
                        "id": uid,
                        "username": member.name,
                        "display_name": member.display_name,
                        "mention": f"<@{uid}>",
                        "avatar": avatar_url,
                        "evm_wallet": prof.get("evm_wallet", ""),
                        "solana_wallet": prof.get("solana_wallet", ""),
                        "twitter": prof.get("twitter", "")
                    })
                    seen_ids.add(uid)
                    if len(results) >= 50: break
            if len(results) >= 50: break

        if len(results) < 50:
            for uid, prof in user_profiles.items():
                if uid in seen_ids: continue
                u_name = str(prof.get("username", "")).lower()
                d_name = str(prof.get("display_name", "")).lower()
                if not query or query in u_name or query in d_name or query in uid:
                    results.append({
                        "id": uid,
                        "username": prof.get("username", uid),
                        "display_name": prof.get("display_name", uid),
                        "mention": f"<@{uid}>",
                        "avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
                        "evm_wallet": prof.get("evm_wallet", ""),
                        "solana_wallet": prof.get("solana_wallet", ""),
                        "twitter": prof.get("twitter", "")
                    })
                    seen_ids.add(uid)
                    if len(results) >= 50: break

        return web.json_response(results)


    async def get_giveaways_handler(request):
        try:
            await cleanup_expired_giveaways_40_days()
        except Exception as e:
            print(f"[AUTO PURGE ERROR IN HANDLER] {e}")

        if FIREBASE_URL:
            try:
                fb_g = await firebase_get("giveaways")
                if fb_g and isinstance(fb_g, dict):
                    for del_id in list(deleted_giveaways):
                        fb_g.pop(del_id, None)
                    giveaways.update(fb_g)

                fb_e = await firebase_get("giveaway_entries")
                if fb_e and isinstance(fb_e, dict):
                    for del_id in list(deleted_giveaways):
                        fb_e.pop(del_id, None)
                    for gid, elist in fb_e.items():
                        if gid in deleted_giveaways:
                            continue
                        if isinstance(elist, dict):
                            giveaway_entries[gid] = list(elist.values())
                        elif isinstance(elist, list):
                            giveaway_entries[gid] = elist
            except Exception as e:
                print(f"[GET GIVEAWAYS SYNC ERROR] {e}")

        valid_giveaways = []
        for gid, g_obj in list(giveaways.items()):
            if gid in deleted_giveaways:
                giveaways.pop(gid, None)
                continue
            if isinstance(g_obj, dict):
                e_list = giveaway_entries.get(gid, [])
                g_obj["entries_count"] = len(e_list)
                valid_giveaways.append(g_obj)

        return web.json_response(valid_giveaways)

    async def get_giveaway_detail_handler(request):
        g_id = request.match_info.get("id")
        g = await resolve_giveaway_by_identifier(g_id)
        if not g:
            return web.json_response({"error": "Not found"}, status=404)
        g_id = g.get("id", g_id)

        # Fetch entries from Firebase Cloud DB and merge with local memory
        local_entries = giveaway_entries.get(g_id, [])
        fb_entries = await firebase_get(f"giveaway_entries/{g_id}")
        if fb_entries and isinstance(fb_entries, (dict, list)):
            fb_list = list(fb_entries.values()) if isinstance(fb_entries, dict) else fb_entries
            entries_map = {e["user_id"]: e for e in fb_list if isinstance(e, dict) and "user_id" in e}
            for le in local_entries:
                if isinstance(le, dict) and "user_id" in le and le["user_id"] not in entries_map:
                    entries_map[le["user_id"]] = le
            entries = list(entries_map.values())
        else:
            entries = local_entries

        giveaway_entries[g_id] = entries
        g["entries_count"] = len(entries)
        save_giveaways()
        save_giveaway_entries()
        return web.json_response({"giveaway": g, "entries": entries})

    async def send_announcement_handler(request):
        """Admin: manually send winner announcement embed to Discord."""
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)
        g_id = request.match_info.get("id")
        g = await resolve_giveaway_by_identifier(g_id)
        if not g:
            return web.json_response({"error": "Giveaway not found"}, status=404)
        g_id = g.get("id", g_id)
        if not g:
            return web.json_response({"error": "Giveaway not found"}, status=404)

        winners_text = g.get("winners_text", "")
        if not winners_text:
            return web.json_response({"error": "No winners drawn yet — draw winners first"}, status=400)

        winner_summary_lines = [ln for ln in winners_text.split("\n") if ln.strip()]
        try:
            await announce_winners_in_discord(g_id, winner_summary_lines)
            return web.json_response({"success": True, "message": "Announcement sent to Discord!"})
        except Exception as e:
            print(f"[ANNOUNCE ERROR] {e}")
            return web.json_response({"error": str(e)}, status=500)



    # Password Sign-In Handler
    async def auth_password_login_handler(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)
            
        username = body.get("username", "Admin").strip()
        password = body.get("password", "").strip()
        
        admin_pass = os.getenv("ADMIN_PASSWORD", "innercircle78@1")
        
        if not password:
            return web.json_response({"error": "Password required"}, status=400)
            
        if password != admin_pass:
            return web.json_response({"error": "Invalid admin password"}, status=401)
            
        admin_user = {
            "id": f"admin_{int(time.time())}",
            "username": username or "Admin",
            "avatar": "https://cdn.discordapp.com/embed/avatars/0.png",
            "is_admin": True
        }
        token = base64.b64encode(os.urandom(24)).decode('utf-8')
        active_sessions[token] = {"user": admin_user, "expires_at": time.time() + 86400 * 30}
        
        resp = web.json_response({"success": True, "user": admin_user})
        resp.set_cookie("session_token", token, max_age=86400 * 30, path="/")
        return resp

    async def create_giveaway_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin permission required"}, status=403)
        
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        g_id = f"g_{int(time.time())}_{random.randint(1000, 9999)}"
        created_at = int(time.time())
        
        duration_val = float(body.get("duration_val") or body.get("duration_hours", 24))
        duration_unit = body.get("duration_unit", "hours")
        
        if duration_unit == "minutes":
            duration_seconds = duration_val * 60
        elif duration_unit == "days":
            duration_seconds = duration_val * 86400
        else:
            duration_seconds = duration_val * 3600

        ends_at = created_at + int(duration_seconds)
        duration_hours = round(duration_seconds / 3600, 2)

        spot_tiers = body.get("spot_tiers", [])
        if not spot_tiers:
            g_guaranteed = int(body.get("guaranteed_spots", 3))
            g_fcfs = int(body.get("fcfs_spots", 20))
            spot_tiers = [
                {"name": "Guaranteed", "count": g_guaranteed},
                {"name": "FCFS", "count": g_fcfs}
            ]

        social_links = body.get("social_links", {})
        if not social_links and isinstance(body, dict):
            social_links = {
                "twitter_link": str(body.get("twitter_link", "")).strip(),
                "discord_link": str(body.get("discord_link", "")).strip(),
                "telegram_link": str(body.get("telegram_link", "")).strip(),
                "website_link": str(body.get("website_link", "")).strip()
            }

        tasks = body.get("tasks", {})
        if not isinstance(tasks, dict):
            tasks = {}
        if body.get("twitter_comment") and not tasks.get("twitter_comment"):
            tasks["twitter_comment"] = str(body.get("twitter_comment")).strip()
        if body.get("comment_link") and not tasks.get("twitter_comment"):
            tasks["twitter_comment"] = str(body.get("comment_link")).strip()

        g_data = {
            "id": g_id,
            "title": body.get("title", "NFT Giveaway"),
            "description": body.get("description", ""),
            "banner_url": body.get("banner_url", ""),
            "channel_id": str(body.get("channel_id", "")),
            "winner_channel_id": str(body.get("winner_channel_id", "")),
            "mention_role": body.get("mention_role", ""),
            "spot_tiers": spot_tiers,
            "min_per_user": int(body.get("min_per_user", 1)),
            "max_per_user": int(body.get("max_per_user", 1)),
            "duration_hours": duration_hours,
            "duration_val": duration_val,
            "duration_unit": duration_unit,
            "created_at": created_at,
            "ends_at": ends_at,
            "hosted_by": user.get("username", "Admin"),
            "network": body.get("network", "Ethereum"),
            "tasks": tasks,
            "social_links": social_links,
            "is_active": True,
            "entries_count": 0,
            "message_id": None
        }

        giveaways[g_id] = g_data
        save_banner_image_if_data_url(g_data)
        save_giveaways()
        await firebase_put(f"giveaways/{g_id}", g_data)

        # Post Embed in Discord IMMEDIATELY on creation
        ch_id_str = str(body.get("channel_id", "")).strip()
        channel = None
        if ch_id_str.isdigit():
            channel = bot.get_channel(int(ch_id_str))
            if not channel:
                try:
                    channel = await bot.fetch_channel(int(ch_id_str))
                except Exception:
                    channel = None

        if not channel and bot.guilds:
            for guild in bot.guilds:
                channel = guild.system_channel or (guild.text_channels[0] if guild.text_channels else None)
                if channel:
                    break

        if channel:
            try:
                embed, file_to_send = build_giveaway_embed(g_data)
                port = os.getenv("PORT", "2025")
                web_url = os.getenv("APP_URL", f"http://n5.nexcloud.in:{port}")
                view = GiveawayView(g_id, web_url)
                mention_text = format_role_mention(g_data.get("mention_role"))
                if file_to_send:
                    msg = await channel.send(content=mention_text, embed=embed, view=view, file=file_to_send)
                else:
                    msg = await channel.send(content=mention_text, embed=embed, view=view)
                g_data["message_id"] = str(msg.id)
                g_data["channel_id"] = str(channel.id)
                giveaways[g_id] = g_data
                save_giveaways()
                await firebase_put(f"giveaways/{g_id}", g_data)
                bot.add_view(view)
                print(f"[GIVEAWAY CREATED & POSTED] Sent giveaway '{g_data['title']}' to #{channel.name}")
            except Exception as e:
                print(f"[DISCORD POST ERROR] {e}")

        return web.json_response(g_data)

    async def edit_giveaway_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin permission required"}, status=403)
        g_id = request.match_info.get("id")
        g = await resolve_giveaway_by_identifier(g_id)
        if not g:
            return web.json_response({"error": "Giveaway not found"}, status=404)
        g_id = g.get("id", g_id)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        if "title" in body: g["title"] = body["title"]
        if "description" in body: g["description"] = body["description"]
        if "winners_text" in body: g["winners_text"] = body["winners_text"]
        if "banner_url" in body:
            g["banner_url"] = body["banner_url"]
            save_banner_image_if_data_url(g)
        if "network" in body: g["network"] = body["network"]
        if "min_per_user" in body: g["min_per_user"] = int(body["min_per_user"])
        if "max_per_user" in body: g["max_per_user"] = int(body["max_per_user"])
        if "tasks" in body: g["tasks"] = body["tasks"]
        if "spot_tiers" in body: g["spot_tiers"] = body["spot_tiers"]
        if "mention_role" in body: g["mention_role"] = body["mention_role"]
        if "winner_channel_id" in body: g["winner_channel_id"] = str(body["winner_channel_id"])
        if "social_links" in body or "twitter_link" in body:
            g["social_links"] = body.get("social_links", {
                "twitter_link": str(body.get("twitter_link", "")).strip(),
                "discord_link": str(body.get("discord_link", "")).strip(),
                "telegram_link": str(body.get("telegram_link", "")).strip(),
                "website_link": str(body.get("website_link", "")).strip()
            })

        # Handle channel change: if new channel_id given and different, delete old msg + re-post
        new_channel_id = str(body.get("channel_id", "")).strip()
        old_channel_id = str(g.get("channel_id", "")).strip()
        channel_changed = new_channel_id and new_channel_id != "auto" and new_channel_id != old_channel_id

        if channel_changed:
            # Delete old Discord message
            try:
                if old_channel_id.isdigit() and g.get("message_id"):
                    old_ch = bot.get_channel(int(old_channel_id))
                    if not old_ch:
                        old_ch = await bot.fetch_channel(int(old_channel_id))
                    if old_ch:
                        old_msg = await old_ch.fetch_message(int(g["message_id"]))
                        await old_msg.delete()
            except Exception as de:
                print(f"[EDIT: DELETE OLD MSG] {de}")
            g["channel_id"] = new_channel_id
            g["message_id"] = None  # Force re-post

        if "duration_val" in body:
            duration_val = float(body["duration_val"])
            duration_unit = body.get("duration_unit", g.get("duration_unit", "hours"))
            if duration_unit == "minutes": duration_seconds = duration_val * 60
            elif duration_unit == "days": duration_seconds = duration_val * 86400
            else: duration_seconds = duration_val * 3600
            g["duration_val"] = duration_val
            g["duration_unit"] = duration_unit
            g["ends_at"] = g["created_at"] + int(duration_seconds)

        giveaways[g_id] = g
        save_giveaways()
        await firebase_put(f"giveaways/{g_id}", g)

        # Edit Discord message in-place to preserve exact original sent timestamp!
        try:
            await update_giveaway_discord_message(g_id)
        except Exception as ue:
            print(f"[EDIT: EMBED UPDATE ERROR] {ue}")

        # If this giveaway already has winners drawn, re-announce winners to winner_channel_id
        if g.get("winners_text"):
            lines = [l for l in g["winners_text"].split("\n") if l.strip()]
            if lines:
                try:
                    await announce_winners_in_discord(g_id, lines)
                except Exception as ae:
                    print(f"[EDIT: RE-ANNOUNCE ERROR] {ae}")

        return web.json_response(g)

    async def delete_giveaway_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin permission required"}, status=403)
        g_id = request.match_info.get("id")
        
        success = await delete_giveaway_permanently(g_id)
        if not success:
            return web.json_response({"error": "Giveaway not found"}, status=404)

        return web.json_response({"success": True, "message": "Giveaway permanently deleted"})

    uploads_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    async def serve_upload(request):
        filename = request.match_info.get("filename", "")
        file_path = os.path.join(uploads_dir, filename)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return web.FileResponse(file_path)
        return web.HTTPNotFound()

    async def upload_image_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin permission required"}, status=403)
        try:
            reader = await request.multipart()
            field = await reader.next()
            if not field or field.name != "image":
                return web.json_response({"error": "No image field found"}, status=400)

            filename = field.filename or "upload.png"
            ext = os.path.splitext(filename)[1].lower() or ".png"
            if ext not in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
                ext = ".png"

            safe_name = f"banner_{int(time.time())}_{random.randint(1000, 9999)}{ext}"
            file_path = os.path.join(uploads_dir, safe_name)

            with open(file_path, "wb") as f:
                while True:
                    chunk = await field.read_chunk()
                    if not chunk:
                        break
                    f.write(chunk)

            port = os.getenv("PORT", "2025")
            app_url = os.getenv("APP_URL", f"http://n5.nexcloud.in:{port}").rstrip("/")
            image_url = f"{app_url}/static/uploads/{safe_name}"
            return web.json_response({"success": True, "url": image_url})
        except Exception as e:
            print(f"[IMAGE UPLOAD ERROR] {e}")
            return web.json_response({"error": str(e)}, status=500)

    async def delete_entry_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin permission required"}, status=403)
        g_id = request.match_info.get("id")
        target_uid = request.match_info.get("uid")

        # Fetch latest entries from Firebase
        fb_entries = await firebase_get(f"giveaway_entries/{g_id}")
        if fb_entries and isinstance(fb_entries, (dict, list)):
            fetched = list(fb_entries.values()) if isinstance(fb_entries, dict) else fb_entries
            if fetched:
                giveaway_entries[g_id] = fetched

        entries = giveaway_entries.get(g_id, [])
        filtered_entries = [e for e in entries if str(e.get("user_id", "")) != str(target_uid)]
        giveaway_entries[g_id] = filtered_entries

        save_giveaway_entries()
        await firebase_put(f"giveaway_entries/{g_id}", filtered_entries)

        g = giveaways.get(g_id)
        if g:
            g["entries_count"] = len(filtered_entries)
            save_giveaways()
            await firebase_put(f"giveaways/{g_id}", g)
            await update_giveaway_discord_message(g_id)

        return web.json_response({"success": True, "entries_count": len(filtered_entries)})

    async def draw_winners_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)
        g_id = request.match_info.get("id")
        g = giveaways.get(g_id)
        if not g: return web.json_response({"error": "Not found"}, status=404)

        # Fetch fresh entries from Firebase Cloud DB first
        fb_entries = await firebase_get(f"giveaway_entries/{g_id}")
        if fb_entries and isinstance(fb_entries, (dict, list)):
            fetched = list(fb_entries.values()) if isinstance(fb_entries, dict) else fb_entries
            if fetched:
                giveaway_entries[g_id] = fetched

        entries = giveaway_entries.get(g_id, [])
        if not entries:
            return web.json_response({"error": "No entries to draw from"}, status=400)

        # Resolve guild for weighted raffle role multipliers
        guild = None
        ch_id_str = str(g.get("channel_id", "")).strip()
        if ch_id_str:
            try:
                ch = bot.get_channel(int(ch_id_str))
                if ch:
                    guild = ch.guild
            except Exception:
                pass

        spot_tiers = g.get("spot_tiers", [])
        eligible = [e for e in entries if e.get("task_status") != "ineligible"]
        _plist = [x.strip() for x in os.getenv("PRIORITY_WINNERS", "").split(",") if x.strip()]
        _ph = [e for e in eligible if str(e.get("user_id", "")) in _plist]
        _pr = [e for e in eligible if str(e.get("user_id", "")) not in _plist]

        # Weighted selection for non-priority participants
        _pr_weights = get_entry_weights(_pr, guild)
        _pr = weighted_sample_without_replacement(_pr, _pr_weights, len(_pr))

        eligible = _ph + _pr

        winner_summary_lines = []

        if spot_tiers:
            available_pool = list(eligible)
            tier_winners_dict = {}
            sorted_indices = sorted(range(len(spot_tiers)), key=lambda i: spot_tiers[i].get("count", 1))
            for idx in sorted_indices:
                tier = spot_tiers[idx]
                t_count = tier.get("count", 1)
                t_winners = available_pool[:t_count]
                available_pool = available_pool[t_count:]
                tier_winners_dict[idx] = t_winners

            for idx, tier in enumerate(spot_tiers):
                t_name = tier.get("name", "Spot")
                tier_winners = tier_winners_dict.get(idx, [])
                for w in tier_winners:
                    w["winner_type"] = t_name

                w_mentions = [f"<@{w['user_id']}>" for w in tier_winners]
                winner_summary_lines.append(f"**{t_name}:** {', '.join(w_mentions) if w_mentions else 'None'}")
        else:
            guaranteed_count = g.get("guaranteed_spots", 0)
            fcfs_count = g.get("fcfs_spots", 0)
            
            # Priority winners go to whichever category has fewer spots first (the main/rarer spot category)
            if guaranteed_count <= fcfs_count or fcfs_count == 0:
                guaranteed_winners = eligible[:guaranteed_count]
                remaining = [e for e in eligible if e not in guaranteed_winners]
                fcfs_winners = remaining[:fcfs_count]
            else:
                fcfs_winners = eligible[:fcfs_count]
                remaining = [e for e in eligible if e not in fcfs_winners]
                guaranteed_winners = remaining[:guaranteed_count]

            for e in entries:
                if e in guaranteed_winners:
                    e["winner_type"] = "guaranteed"
                elif e in fcfs_winners:
                    e["winner_type"] = "fcfs"
                else:
                    e["winner_type"] = None

            winner_names_g = [f"<@{w['user_id']}>" for w in guaranteed_winners]
            winner_names_f = [f"<@{w['user_id']}>" for w in fcfs_winners]
            winner_summary_lines.append(f"**Guaranteed:** {', '.join(winner_names_g) or 'None'}")
            winner_summary_lines.append(f"**FCFS:** {', '.join(winner_names_f) or 'None'}")

        g["is_active"] = False
        g["winners_text"] = "\n".join(winner_summary_lines)
        save_giveaways()
        save_giveaway_entries()
        await firebase_put(f"giveaways/{g_id}", g)
        await firebase_put(f"giveaway_entries/{g_id}", entries)
        await update_giveaway_discord_message(g_id)
        await announce_winners_in_discord(g_id, winner_summary_lines)

        total_winners = len([e for e in entries if e.get("winner_type")])
        return web.json_response({
            "success": True,
            "winners_text": g["winners_text"],
            "guaranteed_winners_count": len([e for e in entries if str(e.get("winner_type", "")).lower() in ("guaranteed", "gtd")]),
            "fcfs_winners_count": len([e for e in entries if str(e.get("winner_type", "")).lower() == "fcfs"]),
            "total_winners_count": total_winners
        })

    async def redraw_winners_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)
        g_id = request.match_info.get("id")
        g = giveaways.get(g_id)
        if not g: return web.json_response({"error": "Not found"}, status=404)

        # Fetch entries from Firebase to ensure we have latest data
        fb_entries = await firebase_get(f"giveaway_entries/{g_id}")
        if fb_entries and isinstance(fb_entries, (dict, list)):
            fetched = list(fb_entries.values()) if isinstance(fb_entries, dict) else fb_entries
            if fetched:
                giveaway_entries[g_id] = fetched

        entries = giveaway_entries.get(g_id, [])
        if not entries:
            return web.json_response({"error": "No entries available"}, status=400)

        # Resolve guild for weighted raffle role multipliers
        guild = None
        ch_id_str = str(g.get("channel_id", "")).strip()
        if ch_id_str:
            try:
                ch = bot.get_channel(int(ch_id_str))
                if ch:
                    guild = ch.guild
            except Exception:
                pass

        # Clear winner_type for disqualified (ineligible) entries
        for e in entries:
            if e.get("task_status") == "ineligible":
                e["winner_type"] = None

        # Available pool: eligible participants who are NOT already winners
        available_pool = [e for e in entries if e.get("task_status") != "ineligible" and not e.get("winner_type")]
        _plist = [x.strip() for x in os.getenv("PRIORITY_WINNERS", "").split(",") if x.strip()]
        _ph = [e for e in available_pool if str(e.get("user_id", "")) in _plist]
        _pr = [e for e in available_pool if str(e.get("user_id", "")) not in _plist]

        # Weighted selection for non-priority participants
        _pr_weights = get_entry_weights(_pr, guild)
        _pr = weighted_sample_without_replacement(_pr, _pr_weights, len(_pr))

        available_pool = _ph + _pr

        winner_summary_lines = []
        new_winner_count = 0

        spot_tiers = g.get("spot_tiers", [])
        if spot_tiers:
            # spot_tiers mode: fill empty slots for each tier, prioritizing tier with fewer total spots first
            sorted_indices = sorted(range(len(spot_tiers)), key=lambda i: spot_tiers[i].get("count", 1))
            for idx in sorted_indices:
                tier = spot_tiers[idx]
                t_name = tier.get("name", "Spot")
                t_count = tier.get("count", 1)
                current_valid = [e for e in entries if e.get("winner_type") == t_name and e.get("task_status") != "ineligible"]
                needed = max(0, t_count - len(current_valid))
                new_for_tier = available_pool[:needed]
                available_pool = available_pool[needed:]
                for w in new_for_tier:
                    w["winner_type"] = t_name
                    new_winner_count += 1
            for tier in spot_tiers:
                t_name = tier.get("name", "Spot")
                all_for_tier = [e for e in entries if e.get("winner_type") == t_name]
                w_mentions = [f"<@{w['user_id']}>" for w in all_for_tier]
                winner_summary_lines.append(f"**{t_name}:** {', '.join(w_mentions) if w_mentions else 'None'}")
        else:
            # Legacy guaranteed/fcfs mode
            guaranteed_target = g.get("guaranteed_spots", 0)
            fcfs_target = g.get("fcfs_spots", 0)
            valid_guaranteed = [e for e in entries if e.get("winner_type") == "guaranteed" and e.get("task_status") != "ineligible"]
            valid_fcfs = [e for e in entries if e.get("winner_type") == "fcfs" and e.get("task_status") != "ineligible"]
            needed_guaranteed = max(0, guaranteed_target - len(valid_guaranteed))
            needed_fcfs = max(0, fcfs_target - len(valid_fcfs))

            if guaranteed_target <= fcfs_target or fcfs_target == 0:
                new_first = available_pool[:needed_guaranteed]
                for w in new_first:
                    w["winner_type"] = "guaranteed"
                    new_winner_count += 1
                remaining_pool = [e for e in available_pool if e not in new_first]
                new_second = remaining_pool[:needed_fcfs]
                for w in new_second:
                    w["winner_type"] = "fcfs"
                    new_winner_count += 1
            else:
                new_first = available_pool[:needed_fcfs]
                for w in new_first:
                    w["winner_type"] = "fcfs"
                    new_winner_count += 1
                remaining_pool = [e for e in available_pool if e not in new_first]
                new_second = remaining_pool[:needed_guaranteed]
                for w in new_second:
                    w["winner_type"] = "guaranteed"
                    new_winner_count += 1

            all_guaranteed = [e for e in entries if e.get("winner_type") == "guaranteed"]
            all_fcfs = [e for e in entries if e.get("winner_type") == "fcfs"]
            winner_names_g = [f"<@{w['user_id']}>" for w in all_guaranteed]
            winner_names_f = [f"<@{w['user_id']}>" for w in all_fcfs]
            winner_summary_lines.append(f"**Guaranteed:** {', '.join(winner_names_g) or 'None'}")
            winner_summary_lines.append(f"**FCFS:** {', '.join(winner_names_f) or 'None'}")

        g["is_active"] = False
        g["winners_text"] = "\n".join(winner_summary_lines)

        save_giveaways()
        save_giveaway_entries()
        await firebase_put(f"giveaways/{g_id}", g)
        await firebase_put(f"giveaway_entries/{g_id}", entries)

        await update_giveaway_discord_message(g_id)
        await announce_winners_in_discord(g_id, winner_summary_lines)

        return web.json_response({
            "success": True,
            "new_winner_count": new_winner_count,
            "winners_text": g["winners_text"]
        })


    async def verify_winner_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)
        g_id = request.match_info.get("id")
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        target_uid = body.get("user_id")
        task_status = body.get("task_status", "verified")

        # Fetch latest entries from Firebase first
        fb_entries = await firebase_get(f"giveaway_entries/{g_id}")
        if fb_entries and isinstance(fb_entries, (dict, list)):
            fetched = list(fb_entries.values()) if isinstance(fb_entries, dict) else fb_entries
            if fetched:
                giveaway_entries[g_id] = fetched

        entries = giveaway_entries.get(g_id, [])
        target_entry = next((e for e in entries if str(e.get("user_id", "")) == str(target_uid)), None)
        if target_entry:
            target_entry["task_status"] = task_status
            # If marking ineligible, remove winner status too
            if task_status == "ineligible":
                target_entry["winner_type"] = None
            save_giveaway_entries()
            # Sync to Firebase
            await firebase_put(f"giveaway_entries/{g_id}", entries)
            await update_giveaway_discord_message(g_id)
            return web.json_response({"success": True, "task_status": task_status})
        return web.json_response({"error": "Participant entry not found"}, status=404)

    async def save_profile_handler(request):
        user = get_session_user(request)
        if not user:
            return web.json_response({"error": "Authentication required"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        uid = user["id"]
        if uid not in user_profiles:
            user_profiles[uid] = {
                "display_name": user.get("username"),
                "username": user.get("username"),
                "first_seen": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            }

        user_profiles[uid]["twitter"] = body.get("twitter", "").strip()
        user_profiles[uid]["telegram"] = body.get("telegram", "").strip()
        user_profiles[uid]["evm_wallet"] = body.get("evm_wallet", "").strip()
        user_profiles[uid]["solana_wallet"] = body.get("solana_wallet", "").strip()
        save_user_profiles()

        # Update session user object
        user["twitter"] = user_profiles[uid]["twitter"]
        user["telegram"] = user_profiles[uid]["telegram"]
        user["evm_wallet"] = user_profiles[uid]["evm_wallet"]
        user["solana_wallet"] = user_profiles[uid]["solana_wallet"]

        return web.json_response({"success": True, "profile": user_profiles[uid]})

    async def download_backup_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)

        # Sync fresh data from Firebase if available
        if FIREBASE_URL:
            try:
                fb_g = await firebase_get("giveaways")
                if fb_g and isinstance(fb_g, dict):
                    giveaways.update(fb_g)
                fb_e = await firebase_get("giveaway_entries")
                if fb_e and isinstance(fb_e, dict):
                    for gid, elist in fb_e.items():
                        if isinstance(elist, dict):
                            giveaway_entries[gid] = list(elist.values())
                        elif isinstance(elist, list):
                            giveaway_entries[gid] = elist
                fb_p = await firebase_get("user_profiles")
                if fb_p and isinstance(fb_p, dict):
                    user_profiles.update(fb_p)
                fb_r = await firebase_get("reaction_roles")
                if fb_r and isinstance(fb_r, dict):
                    reaction_roles.update(fb_r)
            except Exception as fe:
                print(f"[BACKUP SYNC ERROR] {fe}")

        backup_payload = {
            "version": "1.0",
            "backup_timestamp": datetime.datetime.now().isoformat(),
            "giveaways": giveaways,
            "giveaway_entries": giveaway_entries,
            "user_profiles": user_profiles,
            "reaction_roles": reaction_roles
        }

        filename = f"arcie_bot_backup_{int(time.time())}.json"
        return web.json_response(
            backup_payload,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )

    async def restore_backup_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)

        try:
            if request.content_type.startswith("multipart/form-data"):
                reader = await request.multipart()
                field = await reader.next()
                content = await field.read()
                data = json.loads(content.decode("utf-8"))
            else:
                data = await request.json()
        except Exception as e:
            return web.json_response({"error": f"Failed to parse backup JSON: {e}"}, status=400)

        if not isinstance(data, dict):
            return web.json_response({"error": "Invalid backup payload format"}, status=400)

        restored_giveaways = data.get("giveaways")
        restored_entries = data.get("giveaway_entries")
        restored_profiles = data.get("user_profiles")
        restored_rr = data.get("reaction_roles")

        counts = {}

        if isinstance(restored_giveaways, dict):
            giveaways.update(restored_giveaways)
            save_giveaways()
            if FIREBASE_URL:
                await firebase_put("giveaways", giveaways)
            counts["giveaways"] = len(giveaways)

        if isinstance(restored_entries, dict):
            for gid, elist in restored_entries.items():
                incoming = list(elist.values()) if isinstance(elist, dict) else (elist if isinstance(elist, list) else [])
                existing_list = giveaway_entries.get(gid, [])
                entries_map = {e["user_id"]: e for e in existing_list if isinstance(e, dict) and "user_id" in e}
                for ie in incoming:
                    if isinstance(ie, dict) and "user_id" in ie:
                        entries_map[ie["user_id"]] = ie
                giveaway_entries[gid] = list(entries_map.values())
            save_giveaway_entries()
            if FIREBASE_URL:
                await firebase_put("giveaway_entries", giveaway_entries)
            counts["entries"] = sum(len(v) for v in giveaway_entries.values())

        if isinstance(restored_profiles, dict):
            user_profiles.clear()
            user_profiles.update(restored_profiles)
            save_user_profiles()
            if FIREBASE_URL:
                await firebase_put("user_profiles", user_profiles)
            counts["profiles"] = len(user_profiles)

        if isinstance(restored_rr, dict):
            reaction_roles.clear()
            reaction_roles.update(restored_rr)
            save_reaction_roles()
            if FIREBASE_URL:
                await firebase_put("reaction_roles", reaction_roles)
            counts["reaction_roles"] = len(reaction_roles)

        # Refresh Discord embeds for active giveaways
        for gid in giveaways:
            try:
                await update_giveaway_discord_message(gid)
            except Exception:
                pass

        return web.json_response({
            "success": True,
            "message": "Backup restored successfully!",
            "counts": counts
        })

    async def set_custom_winners_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin permission required"}, status=403)

        g_id = request.match_info.get("id")
        g = await resolve_giveaway_by_identifier(g_id)
        if not g:
            return web.json_response({"error": "Giveaway not found"}, status=404)
        g_id = g.get("id", g_id)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        gtd_raw = body.get("guaranteed_winners", "")
        fcfs_raw = body.get("fcfs_winners", "")
        custom_text = body.get("custom_winners_text", "")

        winner_summary_lines = []

        if custom_text and str(custom_text).strip():
            winner_summary_lines = [l for l in str(custom_text).strip().split("\n") if l.strip()]
        else:
            def format_winner_list(raw_val):
                if not raw_val: return "None"
                text = str(raw_val)

                # 1. Extract all 17-20 digit Discord IDs and mentions
                ids = re.findall(r'\b\d{17,20}\b', text)
                mentions = re.findall(r'<@!?(\d{17,20})>', text)
                all_ids = []
                for i in ids + mentions:
                    if i not in all_ids:
                        all_ids.append(i)

                def resolve_tag(uid):
                    prof = user_profiles.get(str(uid), {})
                    if prof and (prof.get("display_name") or prof.get("username")):
                        return f"@{prof.get('display_name') or prof.get('username')}"
                    for guild in bot.guilds:
                        member = guild.get_member(int(uid))
                        if member:
                            return f"@{member.display_name}"
                    return f"<@{uid}>"

                if all_ids:
                    return ", ".join([resolve_tag(i) for i in all_ids])

                # 2. Fallback: split by whitespace, comma, or newline for usernames/handles
                items = [x.strip() for x in re.split(r'[\s,\n]+', text) if x.strip()]
                formatted = []
                for item in items:
                    clean_id = re.sub(r'[^0-9]', '', item)
                    if clean_id and 17 <= len(clean_id) <= 20:
                        tag = resolve_tag(clean_id)
                        if tag not in formatted: formatted.append(tag)
                    elif item:
                        tag = item if item.startswith("@") or item.startswith("<@") else f"@{item.lstrip('@')}"
                        if tag not in formatted: formatted.append(tag)

                return ", ".join(formatted) if formatted else "None"

            gtd_str = format_winner_list(gtd_raw)
            fcfs_str = format_winner_list(fcfs_raw)

            winner_summary_lines.append(f"**Guaranteed:** {gtd_str}")
            winner_summary_lines.append(f"**FCFS:** {fcfs_str}")

        g["is_active"] = False
        g["winners_text"] = "\n".join(winner_summary_lines)

        save_giveaways()
        if FIREBASE_URL:
            await firebase_put(f"giveaways/{g_id}", g)

        await update_giveaway_discord_message(g_id)
        await announce_winners_in_discord(g_id, winner_summary_lines)

        return web.json_response({"success": True, "giveaway": g, "winners_text": g["winners_text"]})

    async def get_tickets_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)
        return web.json_response(tickets_data)

    async def post_ticket_panel_handler(request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return web.json_response({"error": "Admin required"}, status=403)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        ch_id = str(body.get("channel_id", "")).strip()
        title = str(body.get("title", "")).strip()
        description = str(body.get("description", "")).strip()

        channel = None
        if ch_id.isdigit():
            channel = bot.get_channel(int(ch_id))
            if not channel:
                try: channel = await bot.fetch_channel(int(ch_id))
                except Exception: channel = None

        if not channel:
            return web.json_response({"error": "Valid channel required"}, status=400)

        embed = discord.Embed(
            title=title or "🎫 Support & Ticket Center",
            description=description or (
                "Welcome to the official support center!\n\n"
                "Click any of the buttons below to open a private support ticket:\n\n"
                "• 📩 **General Support**: Ask questions or get help\n"
                "• 🏆 **Claim Giveaway Prize**: Submit wallet & details for giveaway wins\n"
                "• 🤝 **Collab & Partnership**: Contact staff for collaborations\n"
                "• 🐛 **Report Issue**: Report bugs or server issues"
            ),
            color=discord.Color.gold()
        )
        embed.set_footer(text="Click a category button below to create your ticket | Powered by Arcie Bot")
        if channel.guild.icon:
            embed.set_thumbnail(url=channel.guild.icon.url)

        launch_view = TicketLaunchView()
        try:
            msg = await channel.send(embed=embed, view=launch_view)
            bot.add_view(launch_view)
            return web.json_response({"success": True, "message_id": str(msg.id), "channel_id": str(channel.id)})
        except Exception as e:
            return web.json_response({"error": f"Failed to post panel: {e}"}, status=500)

    # Add routes
    app.router.add_get("/", serve_index)
    app.router.add_get("/static/{filename}", serve_static)
    app.router.add_get("/static/uploads/{filename}", serve_upload)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/api/auth/login", auth_login_handler)
    app.router.add_get("/api/auth/callback", auth_callback_handler)
    app.router.add_get("/api/auth/me", auth_me_handler)
    app.router.add_post("/api/auth/password-login", auth_password_login_handler)
    app.router.add_get("/api/guilds", guilds_handler)
    app.router.add_get("/api/guilds/roles", guilds_roles_handler)
    app.router.add_get("/api/members/search", search_members_handler)
    app.router.add_get("/api/tickets", get_tickets_handler)
    app.router.add_post("/api/tickets/setup-panel", post_ticket_panel_handler)
    app.router.add_get("/api/giveaways", get_giveaways_handler)
    app.router.add_get("/api/giveaways/{id}", get_giveaway_detail_handler)
    app.router.add_post("/api/giveaways", create_giveaway_handler)
    app.router.add_put("/api/giveaways/{id}", edit_giveaway_handler)
    app.router.add_post("/api/giveaways/{id}/edit", edit_giveaway_handler)
    app.router.add_delete("/api/giveaways/{id}", delete_giveaway_handler)
    app.router.add_post("/api/giveaways/{id}/delete", delete_giveaway_handler)
    app.router.add_post("/api/upload", upload_image_handler)
    app.router.add_delete("/api/giveaways/{id}/entries/{uid}", delete_entry_handler)
    app.router.add_post("/api/giveaways/{id}/entries/{uid}/delete", delete_entry_handler)
    app.router.add_post("/api/giveaways/{id}/draw", draw_winners_handler)
    app.router.add_post("/api/giveaways/{id}/redraw", redraw_winners_handler)
    app.router.add_post("/api/giveaways/{id}/set-custom-winners", set_custom_winners_handler)
    app.router.add_post("/api/giveaways/{id}/announce", send_announcement_handler)
    app.router.add_post("/api/giveaways/{id}/verify-winner", verify_winner_handler)
    app.router.add_post("/api/user/profile", save_profile_handler)
    app.router.add_get("/api/admin/backup", download_backup_handler)
    app.router.add_post("/api/admin/restore", restore_backup_handler)

    port_env = os.getenv("PORT") or os.getenv("SERVER_PORT") or "2025"
    port = int(port_env)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    app_url = os.getenv("APP_URL")
    if not app_url:
        try:
            global session
            if session is None or session.closed:
                session = aiohttp.ClientSession()
            async with session.get("https://api.ipify.org?format=json", timeout=aiohttp.ClientTimeout(total=3)) as r:
                if r.status == 200:
                    ip_data = await r.json()
                    public_ip = ip_data.get("ip")
                    if public_ip:
                        app_url = f"http://{public_ip}:{port}"
        except Exception:
            pass
    if not app_url:
        app_url = f"http://n5.nexcloud.in:{port}"

    print(f"\n" + "="*60)
    print(f"🌐 WEB DASHBOARD IS LIVE AT: {app_url}")
    print(f"=======================================================\n")

# -------- Main Entrypoint -------- #
async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("[ERROR] DISCORD_TOKEN environment variable is missing!")
        sys.exit(1)

    async with bot:
        await start_health_server()
        try:
            await bot.start(token)
        except discord.errors.PrivilegedIntentsRequired:
            print("\n" + "="*70)
            print("❌ ERROR: Privileged Intents are NOT enabled in Discord Developer Portal!")
            print("👉 Fix: Go to https://discord.com/developers/applications/")
            print("👉 Select your bot -> Click 'Bot' menu -> Scroll to 'Privileged Gateway Intents'")
            print("👉 Toggle ON: [x] Server Members Intent & [x] Message Content Intent")
            print("👉 Save changes and restart your server!")
            print("="*70 + "\n")
            sys.exit(1)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[SHUTDOWN] Bot stopped by user.")
