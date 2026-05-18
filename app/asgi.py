import base64
import json
import os
import asyncio
import aiohttp
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

BOT_TOKEN  = os.environ.get("BOT_TOKEN")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = os.environ.get("GITHUB_REPO")
GITHUB_FILE  = os.environ.get("GITHUB_FILE", "epochradar.json")

OWNER_IDS       = "1837260280"
TARGET_THREAD_ID = 3

bot = Bot(token=BOT_TOKEN)

IST  = timezone(timedelta(hours=5, minutes=30))
CEST = timezone(timedelta(hours=2))
UTC  = timezone.utc

BLOCKS_PER_EPOCH    = 262_000
AVG_BLOCK_TIME = 0.33               # fallback only — never stored

TIER_1_END = BLOCKS_PER_EPOCH // 3
TIER_2_END = (BLOCKS_PER_EPOCH * 2) // 3

OWNER_LIST = [i.strip() for i in OWNER_IDS.split(",") if i.strip()]

# Bot user-IDs allowed to trigger /status (your auto-scheduler bot)
TRUSTED_BOT_IDS = {"8589208931"}

GRAPHQL_URL_PRIMARY  = "https://mainnet.ackinacki.org/graphql"
GRAPHQL_URL_FALLBACK = "https://mainnet-cf.ackinacki.org/graphql"

GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"

DEFAULT_ENDPOINTS    = "mainnet.ackinacki.org,mainnet-cf.ackinacki.org"
DEFAULT_SAMPLE_BLOCKS = 150

# Callback-data constants
CB_UPDATE_DASHBOARD = "update_dashboard"
CB_REFRESH_BLOCKS   = "refresh_blocks"

# ============================================================
# GitHub storage
# ============================================================

def gh_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


def _sanitize_store(store):
    if not isinstance(store, dict):
        store = {}

    hist = store.get("history", [])
    if not isinstance(hist, list):
        hist = []

    cleaned, seen = [], set()
    for item in hist:
        if not isinstance(item, dict):
            continue
        en = normalize_uint(item.get("epoch_no"))
        if en < 1 or en in seen:
            continue
        seen.add(en)
        cleaned.append(item)

    cleaned.sort(key=lambda x: normalize_uint(x.get("epoch_no")))
    store["history"] = cleaned

    if not isinstance(store.get("chat_pins"), dict):
        store["chat_pins"] = {}

    return store


def load_data():
    try:
        req = Request(GITHUB_API)
        for k, v in gh_headers().items():
            req.add_header(k, v)
        res  = urlopen(req).read()
        data = json.loads(res)
        content = base64.b64decode(data["content"]).decode()
        store   = json.loads(content) if content.strip() else {}
        return _sanitize_store(store), data["sha"]
    except Exception:
        return {"history": [], "chat_pins": {}}, None


def save_data(store, sha):
    store = _sanitize_store(store)
    body  = {
        "message": "update",
        "content": base64.b64encode(json.dumps(store, indent=2).encode()).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    req = Request(GITHUB_API, data=json.dumps(body).encode(), method="PUT")
    for k, v in gh_headers().items():
        req.add_header(k, v)
    req.add_header("Content-Type", "application/json")
    urlopen(req)


async def load_data_async():
    return await asyncio.to_thread(load_data)

async def save_data_async(store, sha):
    return await asyncio.to_thread(save_data, store, sha)


# per-chat pin helpers

def get_chat_pins(store, chat):
    pins = store.setdefault("chat_pins", {})
    if not isinstance(pins.get(chat), dict):
        pins[chat] = {"pin_msg_id": None, "dashboard_msg_id": None}
    return pins[chat]


def set_chat_pins(store, chat, *, pin_msg_id=None, dashboard_msg_id=None):
    pins = get_chat_pins(store, chat)
    if pin_msg_id       is not None: pins["pin_msg_id"]       = pin_msg_id
    if dashboard_msg_id is not None: pins["dashboard_msg_id"] = dashboard_msg_id

# ============================================================
# Telegram helpers
# ============================================================

def _update_button():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔁 Update", callback_data=CB_UPDATE_DASHBOARD)]]
    )

def _refresh_button():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Refresh", callback_data=CB_REFRESH_BLOCKS)]]
    )


async def send_text(chat_id, text, forum=False, reply_markup=None, parse_mode="HTML"):
    kw = {"parse_mode": parse_mode}
    if reply_markup: kw["reply_markup"] = reply_markup
    if forum:        kw["message_thread_id"] = TARGET_THREAD_ID
    return await bot.send_message(int(chat_id), text, **kw)


async def send_chunked(chat_id, text, forum=False, parse_mode="HTML"):
    if len(text) <= 3900:
        return [await send_text(chat_id, text, forum=forum, parse_mode=parse_mode)]

    chunks, current = [], ""
    for paragraph in text.split("\n\n"):
        piece = paragraph.strip()
        if not piece:
            continue
        piece += "\n\n"
        if len(current) + len(piece) > 3900 and current:
            chunks.append(current.rstrip())
            current = piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.rstrip())

    return [await send_text(chat_id, chunk, forum=forum, parse_mode=parse_mode) for chunk in chunks]


def owner_only(user_id):
    return str(user_id) in OWNER_LIST

# ============================================================
# Configuration / GraphQL helpers
# ============================================================

def env_int(key, fallback):
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return fallback
    try:    return int(raw)
    except: return fallback


def normalize_endpoint(ep):
    t = str(ep or "").strip().rstrip("/")
    if not t: return None
    return t if t.startswith("http") else f"https://{t}"


def build_graphql_urls():
    raw  = os.environ.get("ENDPOINTS", DEFAULT_ENDPOINTS)
    urls, seen = [], set()
    for ep in raw.split(","):
        base = normalize_endpoint(ep)
        if not base: continue
        url = f"{base}/graphql"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls or [GRAPHQL_URL_PRIMARY, GRAPHQL_URL_FALLBACK]


def normalize_uint(value):
    if value is None: return 0
    if isinstance(value, int):   return value
    if isinstance(value, float): return int(value)
    if isinstance(value, str) and value.strip():
        try:    return int(value)
        except: return 0
    return 0


async def graphql_fetch(query, retries_per_endpoint=2):
    urls, last_err = build_graphql_urls(), None
    for url in urls:
        for i in range(retries_per_endpoint):
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession() as s:
                    async with s.post(url, json={"query": query}, timeout=timeout) as r:
                        if not r.ok:
                            raise RuntimeError(f"HTTP {r.status} @ {url}")
                        return {"url": url, "json": await r.json()}
            except Exception as e:
                last_err = e
                if i < retries_per_endpoint - 1:
                    await asyncio.sleep(1)
    raise last_err if last_err else RuntimeError("GraphQL failed on all endpoints")


async def get_live_block_snapshot():
    n = max(3, env_int("BLOCK_SAMPLE_BLOCKS", DEFAULT_SAMPLE_BLOCKS))
    query = f"""
    query {{
        blockchain {{
            blocks(last: {n}) {{
                edges {{ node {{ seq_no gen_utime }} }}
            }}
        }}
    }}
    """
    result = await graphql_fetch(query)
    edges  = (
        result.get("json", {}).get("data", {})
        .get("blockchain", {}).get("blocks", {}).get("edges", [])
    )

    parsed = []
    for edge in edges:
        node      = edge.get("node", {}) if isinstance(edge, dict) else {}
        seq_no    = normalize_uint(node.get("seq_no"))
        gen_utime = normalize_uint(node.get("gen_utime"))
        if seq_no > 0:
            parsed.append({"seq_no": seq_no, "gen_utime": gen_utime})

    parsed.sort(key=lambda x: x["seq_no"])
    if not parsed:
        raise RuntimeError("No block height available")

    first, last = parsed[0], parsed[-1]
    sample_block_sec = None
    if (len(parsed) >= 2
            and last["seq_no"]    > first["seq_no"]
            and last["gen_utime"] >= first["gen_utime"]):
        ds = last["seq_no"]    - first["seq_no"]
        dt = last["gen_utime"] - first["gen_utime"]
        if ds > 0 and dt >= 0:
            sample_block_sec = dt / ds

    return {
        "sourceUrl":        result["url"],
        "currentHeight":    last["seq_no"],
        "currentTimestamp": last["gen_utime"],
        "sampleBlockSec":   sample_block_sec,
        "sampleBlocks":     len(parsed),
    }


THREAD_ID_MAIN = "00000000000000000000000000000000000000000000000000000000000000000000"


async def fetch_block_hash_by_height(block_height: int) -> str | None:
    """
    Use the blockByHeight query (as used by acki.live explorer) to resolve
    a seq_no → hash.  Returns the hash string or None on failure.
    """
    query = """
    query NextBlockByHeight($threadId: String!, $height: Int!) {
        blockchain {
            blockByHeight(thread_id: $threadId, height: $height) {
                id
                hash
            }
        }
    }
    """
    variables = {"threadId": THREAD_ID_MAIN, "height": block_height}
    try:
        result = await graphql_fetch_vars(query, variables)
        block_by_height = (
            result.get("json", {}).get("data", {})
                  .get("blockchain", {}).get("blockByHeight") or {}
        )
        return block_by_height.get("hash") or block_by_height.get("id") or None
    except Exception as e:
        print(f"fetch_block_hash_by_height({block_height}): {e}")
        return None


async def fetch_block_detail_by_hash(block_hash: str) -> dict | None:
    """
    Fetch full block detail by hash (same query used by acki.live explorer).
    Returns the block dict containing gen_utime, gen_utime_string, seq_no etc.
    """
    query = """
    query GetBlock($hash: String!) {
        blockchain {
            block(hash: $hash) {
                id
                hash
                seq_no
                height
                gen_utime
                gen_utime_string
                tr_count
                workchain_id
                shard
                thread_id
            }
        }
    }
    """
    try:
        result = await graphql_fetch_vars(query, {"hash": block_hash})
        return (
            result.get("json", {}).get("data", {})
                  .get("blockchain", {}).get("block") or None
        )
    except Exception as e:
        print(f"fetch_block_detail_by_hash({block_hash[:12]}...): {e}")
        return None


EXPLORER_BASE = "https://dev.acki.live/blocks"


def parse_chain_order_timestamp(chain_order: str) -> int | None:
    """
    Decode the Unix timestamp embedded in a chain_order string.

    Official Acki Nacki docs format:
        <len-1><timestamp_hex><len-1><placeholder_hex><len-1><thread_id_hex><len-1><height_hex>

    The prefix digit is EXACTLY 1 character and represents (hex_length - 1).

    Verified example from docs:
        "7698320d000670...061d4b1c0"
         ^ = "7"  →  field is 8 hex chars
           "698320d0" = 0x698320d0 = 1770201296  ✓

    Returns Unix timestamp (int) or None if parsing fails.
    """
    try:
        s = chain_order.strip()
        if len(s) < 2:
            return None
        # The prefix is always exactly 1 decimal digit
        field_len = int(s[0]) + 1   # e.g. "7" → 8 hex chars
        ts_hex    = s[1 : 1 + field_len]
        return int(ts_hex, 16)
    except Exception as e:
        print(f"parse_chain_order_timestamp failed on '{chain_order[:20]}...': {e}")
        return None


async def fetch_block_timestamp(block_height: int) -> tuple[int | None, str | None, str | None]:
    """
    Returns (unix_timestamp, gen_utime_string, block_hash) for a given block height.

    The epoch boundary blocks are EXACT numbers (epoch_no × 262000).

    Strategy 1: blockByHeight(thread_id, height) → block(hash) → full detail
    Strategy 2: seq_no ±5 → edges { node { seq_no hash chain_order gen_utime } }
                → exact seq_no match → extract ts from chain_order (docs method)
                   OR from gen_utime directly → then block(hash) for gen_utime_string
    Strategy 3: Same with nodes { } schema variant

    chain_order is used as the guaranteed timestamp source per official docs:
        chain_order encodes Unix timestamp directly in its first field.

    Returns (None, None, None) if all strategies fail.
    """

    async def _detail_from_hash(h):
        """Fetch full block detail by hash. Returns (ts, tss, hash)."""
        if not h:
            return None, None, None
        detail = await fetch_block_detail_by_hash(h)
        if detail:
            ts  = normalize_uint(detail.get("gen_utime"))
            tss = detail.get("gen_utime_string") or None
            blk = detail.get("hash") or detail.get("id") or h
            if ts:
                return ts, tss, blk
        return None, None, None

    def _extract_from_node(node):
        """
        Extract (ts, tss, hash) from a block node dict.
        Tries gen_utime first, then falls back to decoding chain_order.
        Returns (ts, None, hash) — tss requires a separate block(hash) call.
        """
        if not isinstance(node, dict):
            return None, None, None
        blk = node.get("hash") or node.get("id") or None
        ts  = normalize_uint(node.get("gen_utime"))
        if not ts:
            co = node.get("chain_order")
            if co:
                ts = parse_chain_order_timestamp(co)
        tss = node.get("gen_utime_string") or None
        return ts, tss, blk

    def _find_exact(items, target, use_edges=False):
        """Find the node dict with seq_no == target."""
        for item in (items or []):
            node = item.get("node", {}) if (use_edges and isinstance(item, dict)) else item
            if isinstance(node, dict) and normalize_uint(node.get("seq_no")) == target:
                return node
        return None

    # ── Strategy 1: blockByHeight → block(hash) ───────────────────────────
    try:
        block_hash = await fetch_block_hash_by_height(block_height)
        if block_hash:
            ts, tss, blk = await _detail_from_hash(block_hash)
            if ts:
                return ts, tss, blk
    except Exception as e:
        print(f"fetch_block_timestamp S1({block_height}): {e}")

    # ── Strategy 2 & 3: seq_no range → exact node → chain_order / gen_utime
    # Tried with both edges and nodes schema shapes.
    # chain_order lets us decode timestamp even if gen_utime is absent.
    # Windows: ±5 first (cheap), then ±50 for older blocks the node may
    # store with slight offset from our expected seq_no.
    for use_edges, shape in [(True,  "edges { node { seq_no hash chain_order gen_utime gen_utime_string } }"),
                              (False, "nodes { seq_no hash chain_order gen_utime gen_utime_string }")]:
        for window in (5, 50):
            q = f"""
            query {{
                blockchain {{
                    blocks(seq_no: {{ start: {block_height - window}, end: {block_height + window} }}) {{
                        {shape}
                    }}
                }}
            }}
            """
            label = f"S{'2' if use_edges else '3'}-{'edges' if use_edges else 'nodes'}-w{window}"
            try:
                r     = await graphql_fetch(q)
                bdata = (r.get("json", {}).get("data", {})
                          .get("blockchain", {}).get("blocks", {}) or {})
                items = bdata.get("edges" if use_edges else "nodes") or []
                node  = _find_exact(items, block_height, use_edges=use_edges)
                if not node:
                    continue

                ts, tss, blk = _extract_from_node(node)

                # Fetch full detail for gen_utime_string if we have hash but no tss
                if blk and not tss:
                    detail = await fetch_block_detail_by_hash(blk)
                    if detail:
                        ts2  = normalize_uint(detail.get("gen_utime"))
                        tss2 = detail.get("gen_utime_string") or None
                        if ts2:
                            ts, tss = ts2, tss2

                if ts:
                    return ts, tss, blk
            except Exception as e:
                print(f"fetch_block_timestamp {label}({block_height}): {e}")

    print(f"fetch_block_timestamp: all strategies failed for block {block_height}")
    return None, None, None


async def graphql_fetch_vars(query: str, variables: dict, retries_per_endpoint=2):
    """Like graphql_fetch but accepts variables dict separately."""
    urls, last_err = build_graphql_urls(), None
    for url in urls:
        for i in range(retries_per_endpoint):
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession() as s:
                    payload = {"query": query, "variables": variables}
                    async with s.post(url, json=payload, timeout=timeout) as r:
                        if not r.ok:
                            raise RuntimeError(f"HTTP {r.status} @ {url}")
                        return {"url": url, "json": await r.json()}
            except Exception as e:
                last_err = e
                if i < retries_per_endpoint - 1:
                    await asyncio.sleep(1)
    raise last_err if last_err else RuntimeError("GraphQL failed on all endpoints")

# ============================================================
# Epoch arithmetic
# ============================================================

def epoch_no_from_block(block_height):
    """epoch = floor(block / 262000)"""
    return block_height // BLOCKS_PER_EPOCH

def epoch_start_block(epoch_no):
    return epoch_no * BLOCKS_PER_EPOCH

def epoch_reset_block(epoch_no):
    return (epoch_no + 1) * BLOCKS_PER_EPOCH

def current_epoch_bounds(current_block):
    en    = epoch_no_from_block(current_block)
    start = epoch_start_block(en)
    reset = epoch_reset_block(en)
    return en, start, reset

# ============================================================
# Formatting
# ============================================================

def format_duration(seconds):
    seconds = max(0, int(round(seconds / 60.0) * 60))
    h, m    = seconds // 3600, (seconds % 3600) // 60
    return f"{h}h {m}m" if h else f"{m}m"


def format_blockchain_time(unix_ts):
    dt_utc = datetime.fromtimestamp(unix_ts, UTC)
    dt_ist = dt_utc.astimezone(IST)
    return (
        f"{dt_ist.strftime('%d/%m/%Y')} | "
        f"{dt_ist.strftime('%I:%M %p')} | "
        f"UTC:{dt_utc.strftime('%H:%M')}"
    )


def reward_tier(in_epoch):
    if in_epoch < TIER_1_END: return "Tier 1 — High Reward  (&lt;6k taps)"
    if in_epoch < TIER_2_END: return "Tier 2 — Medium Reward (&gt;6k taps)"
    return                           "Tier 3 — Low Reward   (&gt;12k taps)"


def tier_progress(in_epoch):
    """Return percentage completion within the current tier only."""
    if in_epoch < TIER_1_END:
        tier_start, tier_end = 0, TIER_1_END
    elif in_epoch < TIER_2_END:
        tier_start, tier_end = TIER_1_END, TIER_2_END
    else:
        # Tier 3 ends at epoch end (BLOCKS_PER_EPOCH)
        tier_start, tier_end = TIER_2_END, BLOCKS_PER_EPOCH
    pct = min((in_epoch - tier_start) / (tier_end - tier_start) * 100, 100)
    return f"{pct:.1f}%"

# ============================================================
# Text builders
# ============================================================

def build_dashboard_text(snapshot):
    cb      = snapshot["currentHeight"]
    cur_ts  = snapshot.get("currentTimestamp") or int(datetime.now(UTC).timestamp())
    blk_sec = snapshot.get("sampleBlockSec") or AVG_BLOCK_TIME

    en, start, reset = current_epoch_bounds(cb)
    done  = max(0, cb - start)
    left  = max(0, reset - cb)
    pct   = done / BLOCKS_PER_EPOCH * 100

    remaining_sec = left * blk_sec
    elapsed_sec   = done * blk_sec
    reset_dt      = datetime.fromtimestamp(cur_ts, UTC) + timedelta(seconds=remaining_sec)

    reset_ist  = reset_dt.astimezone(IST)
    reset_utc  = reset_dt.astimezone(UTC)
    reset_cest = reset_dt.astimezone(CEST)

    return (
        f"Current Epoch: {en}\n"
        f"⏳ Timer Since Epoch Reset: {format_duration(elapsed_sec)}\n"
        f"⏱️ Time left to reset: {format_duration(remaining_sec)}\n\n"
        f"📊 Block Progress\n"
        f"• Current Block Height: <code>{cb:,}</code>\n"
        f"• Epoch {en} Started at: <code>{start:,}</code>\n"
        f"• Epoch {en} Resets at: <code>{reset:,}</code>\n"
        f"• Blocks Produced Today: <code>{done:,}</code>\n"
        f"• Blocks Left to Reset: <code>{left:,}</code>\n"
        f"• Progress: {pct:.1f}%\n\n"
        f"🔁 Estimated Reset\n"
        f"• IST:  {reset_ist.strftime('%d/%m %I:%M %p')}\n"
        f"• UTC:  {reset_utc.strftime('%d/%m %I:%M %p')}\n"
        f"• CEST: {reset_cest.strftime('%d/%m %I:%M %p')}\n\n"
        f"🏆 Reward Tier\n"
        f"• {reward_tier(done)}\n"
        f"• Tier Progress: {tier_progress(done)}"
    )


def build_pin_text(snapshot):
    cb      = snapshot["currentHeight"]
    cur_ts  = snapshot.get("currentTimestamp") or int(datetime.now(UTC).timestamp())
    blk_sec = snapshot.get("sampleBlockSec") or AVG_BLOCK_TIME

    _, start, reset = current_epoch_bounds(cb)
    left      = max(0, reset - cb)
    remaining = left * blk_sec
    reset_dt  = datetime.fromtimestamp(cur_ts, UTC) + timedelta(seconds=remaining)
    reset_ist = reset_dt.astimezone(IST)

    return (
        f"⏳ Time Left To Reset: {format_duration(remaining)}\n"
        f"📌 Est. reset: {reset_ist.strftime('%d/%m %I:%M %p')} IST"
    )


def build_blocks_text(snapshot):
    cb      = snapshot["currentHeight"]
    cur_ts  = snapshot.get("currentTimestamp") or int(datetime.now(UTC).timestamp())
    dt_utc  = datetime.fromtimestamp(cur_ts, UTC)
    dt_ist  = dt_utc.astimezone(IST)
    dt_cest = dt_utc.astimezone(CEST)
    return (
        f"📦 Live Block Height\n"
        f"• Block: {cb:,}\n"
        f"• IST:  {dt_ist.strftime('%d/%m %I:%M %p')}\n"
        f"• UTC:  {dt_utc.strftime('%d/%m %I:%M %p')}\n"
        f"• CEST: {dt_cest.strftime('%d/%m %I:%M %p')}"
    )

# ============================================================
# Dashboard lifecycle
# ============================================================

async def send_fresh_dashboard(chat, forum, snapshot, store, sha):
    """
    /start: delete any existing pin+dashboard messages, then create fresh ones.
      1. Pinned countdown (plain text, gets pinned)
      2. Dashboard with inline 'Update 🔃' button
    Both IDs saved to GitHub.
    """
    # Delete previous messages if they exist
    pins = get_chat_pins(store, chat)
    for msg_id in [pins.get("pin_msg_id"), pins.get("dashboard_msg_id")]:
        if msg_id:
            try:
                await bot.delete_message(chat_id=int(chat), message_id=int(msg_id))
            except Exception:
                pass  # already deleted or not found — fine

    pin_msg  = await send_text(chat, build_pin_text(snapshot), forum=forum)
    try:
        await bot.pin_chat_message(
            chat_id=int(chat),
            message_id=pin_msg.message_id,
            disable_notification=True,
        )
    except Exception as e:
        print(f"pin_chat_message: {e}")

    dash_msg = await send_text(chat, build_dashboard_text(snapshot),
                               forum=forum, reply_markup=_update_button())

    set_chat_pins(store, chat,
                  pin_msg_id=pin_msg.message_id,
                  dashboard_msg_id=dash_msg.message_id)
    await save_data_async(store, sha)


async def _try_edit(chat_id, msg_id, text, reply_markup=None):
    """
    Returns 'ok' | 'deleted' | 'error'
    'message is not modified' → 'ok'  (never triggers a fallback)
    """
    try:
        kw = {"reply_markup": reply_markup} if reply_markup else {}
        await bot.edit_message_text(
            chat_id=int(chat_id),
            message_id=int(msg_id),
            text=text,
            parse_mode="HTML",
            **kw,
        )
        return "ok"
    except BadRequest as e:
        err = str(e).lower()
        if "message is not modified" in err:
            return "ok"
        if "message to edit not found" in err or "chat not found" in err:
            return "deleted"
        print(f"edit({msg_id}): {e}")
        return "error"
    except Exception as e:
        print(f"edit({msg_id}): {e}")
        return "error"


async def do_dashboard_update(chat, forum, snapshot, store, sha):
    """
    Updates the dashboard for ALL chats that have done /start.
    Triggered by: Update button, /status, !status — from any group or user.
    /start stays per-chat and is never affected here.
    """
    pin_text  = build_pin_text(snapshot)
    dash_text = build_dashboard_text(snapshot)

    all_pins = store.get("chat_pins", {})

    if not all_pins:
        await send_text(chat, "ℹ️ No dashboard found. Send /start to set it up.", forum=forum)
        return

    any_updated = False

    for target_chat, pins in all_pins.items():
        if not isinstance(pins, dict):
            continue
        pin_msg_id       = pins.get("pin_msg_id")
        dashboard_msg_id = pins.get("dashboard_msg_id")

        if not pin_msg_id or not dashboard_msg_id:
            continue

        pin_res  = await _try_edit(target_chat, pin_msg_id, pin_text)
        dash_res = await _try_edit(target_chat, dashboard_msg_id, dash_text,
                                   reply_markup=_update_button())

        if pin_res in ("ok", "error") or dash_res in ("ok", "error"):
            any_updated = True

        if pin_res == "deleted" or dash_res == "deleted":
            # Messages deleted in that chat — clear stored IDs so it doesn't retry
            pins["pin_msg_id"]       = None
            pins["dashboard_msg_id"] = None

    if any_updated:
        await save_data_async(store, sha)

# ============================================================
# Command parser — strips @BotUsername suffix
# ============================================================

def parse_command(text: str) -> str:
    """
    '/start@epoch_helper_bot extra args'  →  '/start'
    '!start'                              →  '/start'
    '/status'                             →  '/status'
    """
    if not text:
        return ""
    first_token = text.strip().split()[0].lower()
    if "@" in first_token:
        first_token = first_token.split("@")[0]
    # Normalise ! prefix to / so all downstream checks work unchanged
    if first_token.startswith("!"):
        first_token = "/" + first_token[1:]
    return first_token

# ============================================================
# Analysis / history helpers
# ============================================================

def find_history_record(store, epoch_no):
    for item in store.get("history", []):
        if normalize_uint(item.get("epoch_no")) == epoch_no:
            return item
    return None


def _parse_utime_string(raw: str | None) -> str | None:
    """
    Normalize the gen_utime_string from the API (e.g. '2026-05-09 13:38:08.+0000')
    into a clean UTC string: '2026-05-09 13:38:08 UTC'.
    Returns None if raw is missing/unparseable.
    """
    if not raw:
        return None
    try:
        # strip trailing timezone label — it comes as '.+0000' or ' UTC'
        cleaned = raw.strip().replace(".+0000", "").replace("+0000", "").strip()
        dt = datetime.strptime(cleaned, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return None


def build_analysis_record(epoch_no, start_ts, reset_ts,
                          start_utime_str=None, reset_utime_str=None,
                          start_hash=None, reset_hash=None):
    """
    Build a history record.
    Stores partial records when only one timestamp is available.
    Returns None only when BOTH timestamps are missing.

    Fields include exact UTC times, IST times, duration in seconds,
    and explorer URLs built from the block hashes.
    """
    if not start_ts and not reset_ts:
        return None

    start_fmt    = format_blockchain_time(start_ts) if start_ts else "pending"
    reset_fmt    = format_blockchain_time(reset_ts) if reset_ts else "pending"

    duration_sec = None
    duration_str = "pending"
    if start_ts and reset_ts and reset_ts > start_ts:
        duration_sec = reset_ts - start_ts
        duration_str = format_duration(duration_sec)

    exact_start = _parse_utime_string(start_utime_str) or (
        datetime.fromtimestamp(start_ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        if start_ts else "pending"
    )
    exact_reset = _parse_utime_string(reset_utime_str) or (
        datetime.fromtimestamp(reset_ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        if reset_ts else "pending"
    )

    start_url = f"{EXPLORER_BASE}/{start_hash}" if start_hash else None
    reset_url = f"{EXPLORER_BASE}/{reset_hash}" if reset_hash else None

    return {
        "kind":                   "auto_reset",
        "epoch_no":               epoch_no,
        "start_block":            epoch_start_block(epoch_no),
        "reset_block":            epoch_reset_block(epoch_no),
        "start_timestamp":        start_ts,
        "reset_timestamp":        reset_ts,
        "start_fmt":              start_fmt,
        "reset_fmt":              reset_fmt,
        "exact_start_time":       exact_start,
        "exact_reset_time":       exact_reset,
        "epoch_duration":         duration_str,
        "epoch_duration_seconds": duration_sec,
        "start_hash":             start_hash,
        "reset_hash":             reset_hash,
        "start_url":              start_url,
        "reset_url":              reset_url,
    }


async def build_epoch_record(epoch_no, prefetch_reset=None):
    """
    Build a record for epoch_no.

    prefetch_reset: optional (ts, tss, hash) tuple for the reset block,
    used when the caller already resolved epoch_no+1's start block
    (which is the same block as epoch_no's reset block — no double fetch).
    """
    if epoch_no < 1:
        return None

    if prefetch_reset:
        reset_data = prefetch_reset
        start_data = await fetch_block_timestamp(epoch_start_block(epoch_no))
    else:
        start_data, reset_data = await asyncio.gather(
            fetch_block_timestamp(epoch_start_block(epoch_no)),
            fetch_block_timestamp(epoch_reset_block(epoch_no)),
        )

    start_ts, start_tss, start_hash = start_data
    reset_ts, reset_tss, reset_hash = reset_data

    return build_analysis_record(epoch_no, start_ts, reset_ts,
                                  start_utime_str=start_tss,
                                  reset_utime_str=reset_tss,
                                  start_hash=start_hash,
                                  reset_hash=reset_hash)


ANALYSIS_EPOCH_COUNT = 3   # how many recent completed epochs /analysis fetches


async def ensure_last_n_epochs(store, current_block, n=ANALYSIS_EPOCH_COUNT):
    """
    Fetch (and cache) the last n completed epochs only.
    Already-cached records are reused; missing ones are fetched fresh.

    Optimisation: epoch N's reset_block == epoch N+1's start_block (same number).
    So we fetch each boundary block only once and share across adjacent epochs.
    """
    if not isinstance(store.get("history"), list):
        store["history"] = []

    existing      = {normalize_uint(x.get("epoch_no")) for x in store["history"] if isinstance(x, dict)}
    current_epoch = epoch_no_from_block(current_block)
    last_done     = current_epoch - 1

    if last_done < 1:
        return False

    target_epochs = [en for en in range(last_done, max(0, last_done - n), -1) if en >= 1]
    missing       = [en for en in target_epochs if en not in existing]

    if not missing:
        return False

    # Build a cache of block-height → (ts, tss, hash) so shared boundary blocks
    # (reset of epoch N == start of epoch N+1) are only fetched once.
    block_cache: dict = {}

    async def get_block(height):
        if height not in block_cache:
            block_cache[height] = await fetch_block_timestamp(height)
        return block_cache[height]

    changed = False
    for en in missing:
        start_h = epoch_start_block(en)
        reset_h = epoch_reset_block(en)  # == epoch_start_block(en+1)

        # Reuse epoch en+1's start data as reset data if already cached —
        # they are the same block, and the API only holds ~24h of history.
        next_rec  = find_history_record(store, en + 1)
        if next_rec and next_rec.get("start_timestamp"):
            reset_data = (
                next_rec["start_timestamp"],
                next_rec.get("exact_start_time"),
                next_rec.get("start_hash"),
            )
            start_data = await get_block(start_h)
        else:
            start_data, reset_data = await asyncio.gather(
                get_block(start_h), get_block(reset_h)
            )

        start_ts, start_tss, start_hash = start_data
        reset_ts, reset_tss, reset_hash = reset_data
        rec = build_analysis_record(en, start_ts, reset_ts,
                                     start_utime_str=start_tss,
                                     reset_utime_str=reset_tss,
                                     start_hash=start_hash,
                                     reset_hash=reset_hash)
        if rec:
            store["history"].append(rec)
            changed = True

    store["history"].sort(key=lambda x: normalize_uint(x.get("epoch_no")))
    return changed




async def heal_pending_records(store, sha):
    """
    Scan ALL stored history records that have a missing reset_timestamp.
    For each one, check if epoch N+1's start_timestamp is already cached
    (they are the same block). If yes, fill in reset fields and recalculate
    duration. Saves to GitHub once if anything changed.

    Called on every Update button press and /status so no epoch ever
    stays pending once the next epoch's start is captured.
    """
    hist    = store.get("history", [])
    changed = False

    # Build lookup: epoch_no -> record
    by_epoch = {normalize_uint(r.get("epoch_no")): r for r in hist if isinstance(r, dict)}

    for rec in hist:
        if not isinstance(rec, dict):
            continue
        if rec.get("reset_timestamp"):
            continue  # already filled

        en       = normalize_uint(rec.get("epoch_no"))
        next_rec = by_epoch.get(en + 1)
        if not next_rec or not next_rec.get("start_timestamp"):
            continue  # next epoch not cached yet — nothing to do

        rec["reset_timestamp"]  = next_rec["start_timestamp"]
        rec["reset_fmt"]        = next_rec.get("start_fmt", "pending")
        rec["exact_reset_time"] = next_rec.get("exact_start_time", "pending")
        rec["reset_hash"]       = next_rec.get("start_hash")
        rec["reset_url"]        = next_rec.get("start_url")
        st = rec.get("start_timestamp")
        rt = rec["reset_timestamp"]
        if st and rt and rt > st:
            dur = rt - st
            rec["epoch_duration"]         = format_duration(dur)
            rec["epoch_duration_seconds"] = dur
        changed = True

    if changed and sha:
        try:
            await save_data_async(store, sha)
        except Exception as e:
            print(f"heal_pending_records save: {e}")

    return changed


def build_analysis_report(store, current_block=None, n=ANALYSIS_EPOCH_COUNT):
    """Show only the last n completed epochs, most recent last."""
    hist = store.get("history", [])
    if not hist:
        return None

    # Determine which epoch nos to show
    if current_block is not None:
        last_done = epoch_no_from_block(current_block) - 1
        show_epochs = set(range(max(1, last_done - n + 1), last_done + 1))
        hist = [h for h in hist if normalize_uint(h.get("epoch_no")) in show_epochs]

    if not hist:
        return None

    out = f"📊 Last {n} Completed Epochs\n\n"
    for h in hist:
        dur_sec = h.get("epoch_duration_seconds")
        dur_display = h.get("epoch_duration", "pending")
        if dur_sec and dur_display != "pending":
            h_val, m_val = dur_sec // 3600, (dur_sec % 3600) // 60
            dur_display = f"{h_val}h {m_val}m ({dur_sec:,}s)"

        start_url = h.get("start_url")
        reset_url = h.get("reset_url")
        start_link = f' <a href="{start_url}">Block Info 🔗</a>' if start_url else ""
        reset_link = f' <a href="{reset_url}">Block Info 🔗</a>' if reset_url else ""

        out += (
            f"📅 Epoch {h.get('epoch_no','?')} | Auto Reset\n"
            f"• Start Block: <code>{h.get('start_block',0):,}</code>{start_link}\n"
            f"• Start Time: {h.get('start_fmt','pending')}\n"
            f"• Reset Block: <code>{h.get('reset_block',0):,}</code>{reset_link}\n"
            f"• Reset Time: {h.get('reset_fmt','pending')}\n"
            f"• Epoch Duration: {dur_display}\n\n"
        )
    return out.rstrip()


def build_epoch_report(rec):
    if not rec:
        return None
    dur_sec = rec.get("epoch_duration_seconds")
    dur_display = rec.get("epoch_duration", "pending")
    if dur_sec and dur_display != "pending":
        h_val, m_val = dur_sec // 3600, (dur_sec % 3600) // 60
        dur_display = f"{h_val}h {m_val}m ({dur_sec:,}s exact)"

    start_url = rec.get("start_url")
    reset_url = rec.get("reset_url")
    # Telegram inline hyperlink: [text](url)
    start_link = f' <a href="{start_url}">Block Info 🔗</a>' if start_url else ""
    reset_link = f' <a href="{reset_url}">Block Info 🔗</a>' if reset_url else ""

    return (
        f"📅 Epoch {rec.get('epoch_no','?')} | Auto Reset\n"
        f"• Start Block: <code>{rec.get('start_block',0):,}</code>{start_link}\n"
        f"• Start Time: {rec.get('start_fmt','pending')}\n"
        f"• Reset Block: <code>{rec.get('reset_block',0):,}</code>{reset_link}\n"
        f"• Reset Time: {rec.get('reset_fmt','pending')}\n"
        f"• Epoch Duration: {dur_display}"
    )

# ============================================================
# Loading animation
# ============================================================

async def loading_flow(chat, forum, stages, awaitable, delay=0.45):
    loading_msg = await send_text(chat, stages[0], forum=forum)
    task = asyncio.create_task(awaitable)
    try:
        for stage in stages[1:]:
            await asyncio.sleep(delay)
            try:
                await bot.edit_message_text(
                    chat_id=int(chat),
                    message_id=loading_msg.message_id,
                    text=stage,
                )
            except Exception:
                pass
        return await task
    finally:
        try:
            await bot.delete_message(chat_id=int(chat), message_id=loading_msg.message_id)
        except Exception:
            pass

# ============================================================
# Main handler
# ============================================================

async def handle(update: Update):
    if not update.effective_user or not update.effective_chat:
        return

    user_id   = str(update.effective_user.id)
    chat      = str(update.effective_chat.id)
    forum     = bool(getattr(update.effective_chat, "is_forum", False))
    is_bot_sender = getattr(update.effective_user, "is_bot", False)

    # Allow trusted bots
    if is_bot_sender and user_id not in TRUSTED_BOT_IDS:
        return

    # Check if it's a private DM (not a group/supergroup/channel)
    chat_type     = update.effective_chat.type
    is_private_dm = (chat_type == "private")

    # In DM: only allow owner
    if is_private_dm and user_id not in OWNER_LIST:
        try:
            await bot.send_message(
                int(chat),
                "👋 Sorry for the inconvenience!\n\n"
                "Since this bot runs on a serverless platform, it is not suitable "
                "to handle multiple user requests in DM.\n\n"
                "To avoid this limitation, the bot works only inside the group.\n"
                "Join here 👉 https://t.me/acki_nacki_popit",
                parse_mode="HTML",
            )
        except Exception:
            pass
        return

    # ── Inline button callbacks ──────────────────────────────────────────────
    if update.callback_query:
        cq   = update.callback_query
        data = cq.data or ""

        # 🔃 Update button on dashboard
        if data == CB_UPDATE_DASHBOARD:
            # Answer immediately — Telegram requires this within 3 seconds
            # or it marks the callback as failed and retries.
            try:
                await cq.answer("🔄 Updating…", show_alert=False)
            except Exception:
                pass

            snapshot, (store, sha) = await asyncio.gather(
                get_live_block_snapshot(),
                load_data_async(),
            )
            await do_dashboard_update(chat, forum, snapshot, store, sha)
            asyncio.create_task(heal_pending_records(store, sha))
            return

        # 🔄 Refresh button on /blocks message
        if data == CB_REFRESH_BLOCKS:
            try:
                await cq.answer("🔄 Refreshing…", show_alert=False)
            except Exception:
                pass

            running = _live_state.get(chat, {}).get("running", False)
            snapshot = await get_live_block_snapshot()
            try:
                await bot.edit_message_text(
                    chat_id=int(chat),
                    message_id=cq.message.message_id,
                    text=build_live_text(snapshot),
                    parse_mode="HTML",
                    reply_markup=_live_buttons(running),
                )
            except BadRequest as e:
                if "message is not modified" not in str(e).lower():
                    print(f"refresh_blocks edit: {e}")
            return

        return  # unknown callback

    # ── Text / command messages ──────────────────────────────────────────────
    if not update.message:
        return

    raw_text = (update.message.text or "").strip()
    cmd      = parse_command(raw_text)  # '/start@BotName args' → '/start'

    # /start — always sends two fresh messages + pins the first
    if cmd == "/start":
        snapshot = await loading_flow(
            chat, forum,
            ["📡 Connecting to blockchain…", "🔄 Initialising…", "✅ Ready!"],
            get_live_block_snapshot(),
        )
        store, sha = await load_data_async()
        await send_fresh_dashboard(chat, forum, snapshot, store, sha)
        return

    # /status or !status — updates dashboard for ALL chats
    if cmd == "/status":
        snapshot = await loading_flow(
            chat, forum,
            ["📡 Connecting to blockchain…", "🔍 Fetching live data…", "🔄 Updating dashboard…"],
            get_live_block_snapshot(),
        )
        store, sha = await load_data_async()
        await do_dashboard_update(chat, forum, snapshot, store, sha)
        # Heal any pending reset timestamps using cached next-epoch data
        asyncio.create_task(heal_pending_records(store, sha))
        return

    # /blocks — live block height with 🔄 Refresh inline button
    if cmd == "/blocks":
        snapshot = await loading_flow(
            chat, forum,
            ["📡 Connecting to blockchain…", "🔍 Fetching block height…"],
            get_live_block_snapshot(),
        )
        await send_text(chat, build_blocks_text(snapshot),
                        forum=forum, reply_markup=_refresh_button())
        return

    # /analysis — last 3 completed epochs
    if cmd == "/analysis":

        async def analysis_job():
            s, sh = await load_data_async()
            snap  = await get_live_block_snapshot()
            if await ensure_last_n_epochs(s, snap["currentHeight"]):
                await save_data_async(s, sh)
            return s, snap

        store_snap = await loading_flow(
            chat, forum,
            ["📡 Connecting to blockchain…", "📚 Fetching last 3 epochs…", "📊 Building report…"],
            analysis_job(),
        )
        store, snap = store_snap
        report = build_analysis_report(store, snap["currentHeight"])
        if not report:
            await send_text(chat, "📊 No epoch records yet.", forum=forum)
        else:
            await send_chunked(chat, report, forum=forum)
        return

    # /epoch <no> — single epoch exact report
    if cmd == "/epoch":

        parts = raw_text.split()
        if len(parts) < 2:
            await send_text(chat, "❌ Usage: /epoch 205", forum=forum)
            return
        try:
            epoch_no = int(parts[1])
        except Exception:
            await send_text(chat, "❌ Invalid epoch number.", forum=forum)
            return
        if epoch_no < 1:
            await send_text(chat, "❌ Epoch number must be 1 or higher.", forum=forum)
            return

        async def epoch_job():
            s, sh = await load_data_async()
            rec   = find_history_record(s, epoch_no)

            def _fill_reset_from_next(r, next_r):
                """Fill missing reset fields in r using next epoch's start. Returns True if changed."""
                if not next_r or not next_r.get("start_timestamp"):
                    return False
                r["reset_timestamp"]  = next_r["start_timestamp"]
                r["reset_fmt"]        = next_r.get("start_fmt", "pending")
                r["exact_reset_time"] = next_r.get("exact_start_time", "pending")
                r["reset_hash"]       = next_r.get("start_hash")
                r["reset_url"]        = next_r.get("start_url")
                st = r.get("start_timestamp")
                rt = r["reset_timestamp"]
                if st and rt and rt > st:
                    dur = rt - st
                    r["epoch_duration"]         = format_duration(dur)
                    r["epoch_duration_seconds"] = dur
                return True

            # Always check if next epoch's start can fill a missing reset —
            # even if the record already exists (it may have been stored before
            # epoch N+1 was cached).
            if rec and not rec.get("reset_timestamp"):
                next_rec = find_history_record(s, epoch_no + 1)
                if _fill_reset_from_next(rec, next_rec):
                    s["history"].sort(key=lambda x: normalize_uint(x.get("epoch_no")))
                    await save_data_async(s, sh)
                    return rec

            if rec is None:
                prefetch_reset = None
                next_rec = find_history_record(s, epoch_no + 1)
                if next_rec and next_rec.get("start_timestamp"):
                    prefetch_reset = (
                        next_rec["start_timestamp"],
                        next_rec.get("exact_start_time"),
                        next_rec.get("start_hash"),
                    )
                rec = await build_epoch_record(epoch_no, prefetch_reset=prefetch_reset)
                if rec:
                    s["history"].append(rec)
                    s["history"].sort(key=lambda x: normalize_uint(x.get("epoch_no")))
                    await save_data_async(s, sh)
            return rec

        rec = await loading_flow(
            chat, forum,
            [f"🔎 Loading Epoch {epoch_no}…", "📡 Fetching block timestamps…", "📖 Building report…"],
            epoch_job(),
        )
        if not rec:
            await send_text(
                chat,
                f"⚠️ Epoch {epoch_no} — timestamps unavailable\n\n"
                f"• Start Block: {epoch_start_block(epoch_no):,}\n"
                f"• Reset Block: {epoch_reset_block(epoch_no):,}\n\n"
                f"The node could not resolve these blocks.\n"
                f"This can happen for very old epochs or during node sync.\n"
                f"Try again in a moment or use a more recent epoch number.",
                forum=forum,
            )
            return
        await send_text(chat, build_epoch_report(rec), forum=forum)
        return

    # /help
    if cmd == "/help":
        await send_text(chat, (
            "🧭 Bot Commands\n\n"
            "▶️ /start      — Pinned countdown + dashboard with Update button\n"
            "📊 /status    — Edit dashboard & pin in place\n"
            "📦 /blocks    — Live block height with Refresh button\n"
            "📈 /analysis  — Full epoch history (all epochs)\n"
            "🔎 /epoch 205 — Exact report for a single epoch\n"
            "ℹ️ /help      — Show this help\n\n"
            "💡 All commands work with @BotUsername suffix in groups.\n"
            "   e.g. /start@epoch_helper_bot"
        ), forum=forum)
        return

# ============================================================
# ASGI entry point
# ============================================================

# Deduplication: track recently processed update IDs to ignore Telegram retries
_seen_update_ids: set = set()
_MAX_SEEN = 500  # keep memory bounded


async def app(scope, receive, send):
    if scope["type"] != "http":
        return

    body, more = b"", True
    while more:
        m     = await receive()
        body += m.get("body", b"")
        more  = m.get("more_body", False)

    # ── Respond 200 immediately so Telegram stops retrying ──────────────
    # Telegram has a 3-second webhook timeout. Cold-start + GitHub + GraphQL
    # easily exceeds that, causing duplicate retries. We ack first, process after.
    await send({"type": "http.response.start", "status": 200,
                "headers": [[b"content-type", b"text/plain"]]})
    await send({"type": "http.response.body", "body": b"ok"})

    # ── Process update after responding ─────────────────────────────────
    try:
        data      = json.loads(body.decode())
        update_id = data.get("update_id")

        # Ignore duplicates — Telegram retries deliver the same update_id
        if update_id is not None:
            if update_id in _seen_update_ids:
                return
            _seen_update_ids.add(update_id)
            if len(_seen_update_ids) > _MAX_SEEN:
                # Prune oldest half to keep memory bounded
                to_remove = sorted(_seen_update_ids)[:_MAX_SEEN // 2]
                for uid in to_remove:
                    _seen_update_ids.discard(uid)

        update = Update.de_json(data, bot)
        await handle(update)
    except Exception as e:
        print(f"app handler error: {e}")
