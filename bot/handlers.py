from __future__ import annotations

import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import token_manager
from .api_client import (
    fetch_demo_detail,
    fetch_all_upcoming_demos,
    fetch_all_organizations,
    search_organizations,
)
from .cities import CITIES
from .notifications import format_demo_compact
from .database import (
    add_subscription,
    add_admin,
    get_admins,
    is_bootstrapped,
    get_subscriptions,
    remove_subscription,
    link_entity,
    get_linked_entities,
)

logger = logging.getLogger(__name__)

# Cached catalogs (refreshed by the poller on a schedule)
ORGS: list[dict] = []       # {id, name}
CHAINS: list[dict] = []     # {id, title}

# Pending search state per chat
_org_search_pending: set[int] = set()
_city_search_pending: set[int] = set()
_city_search_term: dict[int, str] = {}
_entity_city_search_pending: dict[int, int] = {}  # user chat -> entity gid
_entity_city_search_term: dict[int, str] = {}

CITY_PAGE_SIZE = 20
CITY_SEARCH_MAX = 25

# Pending entity pairing: code -> {entity_chat_id, entity_title, entity_type, created_at}
_pending_pairing: dict[str, dict] = {}

PAIRING_CODE_TTL = 600  # 10 minutes

# Default CommandHandler filter only matches UpdateType.MESSAGES, which excludes
# channel_posts. Commands must work in channels too, so match both.
_COMMAND_FILTER = filters.UpdateType.CHANNEL_POST | filters.UpdateType.MESSAGES


# ── Helpers ────────────────────────────────────────────────────────

def _is_group(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type in ("group", "supergroup")


def _is_channel(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "channel"


def _is_private(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.type == "private"


def get_chat_title(update: Update) -> str:
    chat = update.effective_chat
    if not chat:
        return ""
    return chat.title or chat.first_name or chat.username or str(chat.id)


async def _is_admin(chat_id: int) -> bool:
    admins = await get_admins()
    return chat_id in admins


async def _is_chat_admin(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    if chat.type == "private":
        return True
    try:
        member = await chat.get_member(user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def _cleanup_pending_pairing() -> None:
    now = time.time()
    expired = [code for code, info in _pending_pairing.items()
               if now - info["created_at"] > PAIRING_CODE_TTL]
    for code in expired:
        del _pending_pairing[code]


# ── Catalog builders ───────────────────────────────────────────────

async def build_catalog(max_days_till: int = 180) -> None:
    global ORGS, CHAINS
    try:
        demos = await fetch_all_upcoming_demos(max_days_till=max_days_till, per_page=100)

        chain_set: dict[str, str] = {}
        org_map: dict[str, str] = {}

        # Prefer the public organization listing endpoint when it is available
        # (deployed on the live backend). Fall back to scraping demo details.
        for org in await fetch_all_organizations():
            oid = org.get("id") or org.get("organization_id")
            name = org.get("name")
            if oid and name:
                org_map.setdefault(str(oid), name)

        for d in demos[:100]:
            demo_id = d.get("_id") or d.get("id")
            if not demo_id:
                continue
            try:
                detail = await fetch_demo_detail(demo_id)
            except Exception:
                continue
            parent = detail.get("parent")
            if parent:
                chain_set[str(parent)] = detail.get("title") or "Ketju"
            if not org_map:
                for org in detail.get("organizers") or []:
                    oid = org.get("organization_id") or org.get("id")
                    name = org.get("name")
                    if oid and name:
                        org_map.setdefault(str(oid), name)

        CHAINS = sorted(
            [{"id": pid, "title": title} for pid, title in chain_set.items()],
            key=lambda c: c["title"],
        )
        ORGS = sorted(
            [{"id": oid, "name": name} for oid, name in org_map.items()],
            key=lambda o: o["name"],
        )
    except Exception:
        logger.exception("Failed to build catalog")


# ── Menu builders ──────────────────────────────────────────────────

async def _main_menu(is_group_or_channel: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📋 Tulevat", callback_data="list:0")],
        [InlineKeyboardButton("🏙️ Kaupungit", callback_data="cities"),
         InlineKeyboardButton("🏢 Järjestöt", callback_data="orgs")],
        [InlineKeyboardButton("🔁 Ketjut", callback_data="chains"),
         InlineKeyboardButton("ℹ️ Ohjeet", callback_data="help")],
    ]
    if is_group_or_channel:
        rows.insert(0, [InlineKeyboardButton("⚙️ Hallinta (DM)", callback_data="go_hallinta")])
    return InlineKeyboardMarkup(rows)


async def _overview(chat_id: int, chat_title: str, is_entity: bool = False) -> str:
    subs = await get_subscriptions(chat_id)
    if not subs:
        if is_entity:
            return (
                f"<b>{chat_title}</b>\n\n"
                "Tilaa mielenosoituksia kaupungin, järjestön tai ketjun mukaan.\n"
                "Valitse alta tai käytä komentoja:\n"
                "  /tanaan – tänään\n"
                "  /viikolla – tällä viikolla\n"
                "  /listaa – kaikki tulevat\n"
                "  /tilaa – hallitse tilauksia\n\n"
                "Ylläpitäjä voi hallita asetuksia DM:stä komennolla /ryhma."
            )
        return (
            f"<b>{chat_title}</b>\n\n"
            "Tilaa mielenosoituksia kaupungin, järjestön tai ketjun mukaan.\n\n"
            "Valitse alta:\n"
            "🏙️ <b>Kaupungit</b> – kaikki mielenosoitukset kaupungissa\n"
            "🏢 <b>Järjestöt</b> – tietyn järjestön järjestämät\n"
            "🔁 <b>Ketjut</b> – toistuvat mielenosoitusketjut\n\n"
            "Tai komentoilla: /tanaan, /viikolla, /listaa, /tilaa"
        )

    lines = [f"<b>{chat_title} – tilauksesi:</b>\n"]
    city = [s["sub_label"] for s in subs if s["sub_type"] == "city"]
    org = [s["sub_label"] for s in subs if s["sub_type"] == "org"]
    chain = [s["sub_label"] for s in subs if s["sub_type"] == "chain"]
    if city:
        lines.append("🏙️ " + ", ".join(city))
    if org:
        lines.append("🏢 " + ", ".join(org))
    if chain:
        lines.append("🔁 " + ", ".join(chain))
    return "\n".join(lines)


def _city_letters() -> list[str]:
    return sorted({c[0].upper() for c in CITIES})


def _cities_starting(letter: str) -> list[str]:
    return [c for c in CITIES if c[0].upper() == letter]


def _city_matches(term: str) -> list[str]:
    needle = term.casefold()
    return [c for c in CITIES if needle in c.casefold()]


def _city_alphabet_markup(page_cb: str, search_cb: str, back_cb: str) -> InlineKeyboardMarkup:
    letters = _city_letters()
    rows = [
        [InlineKeyboardButton(ch, callback_data=f"{page_cb}:{ch}:0") for ch in letters[i:i + 8]]
        for i in range(0, len(letters), 8)
    ]
    rows.append([InlineKeyboardButton("🔍 Hae kaupunkia", callback_data=search_cb)])
    rows.append([InlineKeyboardButton("◀️ Takaisin", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


async def _city_page_markup(
    chat_id: int,
    page_cb: str,
    tog_cb: str,
    letter: str,
    page: int,
    search_cb: str,
    alphabet_cb: str,
    back_cb: str,
) -> InlineKeyboardMarkup:
    subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "city")}
    city_list = _cities_starting(letter)
    total_pages = max(1, (len(city_list) + CITY_PAGE_SIZE - 1) // CITY_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = city_list[page * CITY_PAGE_SIZE:(page + 1) * CITY_PAGE_SIZE]
    rows = [
        [InlineKeyboardButton(f"{'✅' if c in subs else '➕'} {c}",
                              callback_data=f"{tog_cb}:{letter}:{page}:{c}")]
        for c in chunk
    ]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"{page_cb}:{letter}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"{letter} · {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"{page_cb}:{letter}:{page + 1}"))
    rows.append(nav)
    if len(city_list) > CITY_PAGE_SIZE:
        rows.append([InlineKeyboardButton("🔍 Hae kaupunkia", callback_data=search_cb)])
    rows.append([InlineKeyboardButton("🔁 Kirjaimet", callback_data=alphabet_cb),
                 InlineKeyboardButton("◀️ Takaisin", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


async def _city_search_markup(chat_id: int, term: str, tog_cb: str, back_cb: str) -> InlineKeyboardMarkup:
    subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "city")}
    rows = [
        [InlineKeyboardButton(f"{'✅' if c in subs else '➕'} {c}",
                              callback_data=f"{tog_cb}:{c}")]
        for c in _city_matches(term)[:CITY_SEARCH_MAX]
    ]
    rows.append([InlineKeyboardButton("◀️ Takaisin", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def _cb_cap(label: str, prefix: str) -> str:
    """Build callback_data = prefix + label, truncating label to fit Telegram's 64-byte limit."""
    budget = 64 - len(prefix.encode())
    if budget <= 0:
        return prefix[:64]
    enc = label.encode()
    if len(enc) <= budget:
        return prefix + label
    return prefix + enc[:budget].decode("utf-8", "ignore")


def _org_name(org_id: str, fallback: str = "") -> str:
    for org in ORGS:
        if org["id"] == org_id:
            return org["name"]
    return fallback


def _chain_title(chain_id: str, fallback: str = "") -> str:
    for chain in CHAINS:
        if chain["id"] == chain_id:
            return chain["title"]
    return fallback


async def _org_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "org")}
    buttons = []
    for org in ORGS:
        mark = "✅" if org["id"] in subs else "➕"
        buttons.append([
            InlineKeyboardButton(f"{mark} {org['name']}",
                                 callback_data=_cb_cap(org["name"], f"org:{org['id']}:"))
        ])
    buttons.append([InlineKeyboardButton("🔍 Etsi järjestöä", callback_data="search_org")])
    buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")])
    return InlineKeyboardMarkup(buttons)


async def _chain_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "chain")}
    buttons = []
    for chain in CHAINS:
        mark = "✅" if chain["id"] in subs else "➕"
        buttons.append([
            InlineKeyboardButton(f"{mark} {chain['title'][:50]}",
                                 callback_data=_cb_cap(chain["title"], f"chain:{chain['id']}:"))
        ])
    buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")])
    return InlineKeyboardMarkup(buttons)


# ── Time-filtered listings ─────────────────────────────────────────

def _parse_date(d: dict) -> datetime | None:
    for key in ("start", "date", "start_time"):
        val = d.get(key)
        if val:
            try:
                if isinstance(val, (int, float)):
                    return datetime.fromtimestamp(val, tz=timezone.utc)
                return datetime.fromisoformat(val.replace("Z", "+00:00"))
            except Exception:
                continue
    return None


def _filter_demos(demos: list[dict], *, today: bool = False, this_week: bool = False) -> list[dict]:
    now = datetime.now(timezone.utc)
    filtered = []
    for d in demos:
        dt = _parse_date(d)
        if dt is None:
            filtered.append(d)
            continue
        if today and dt.date() == now.date():
            filtered.append(d)
        elif this_week:
            start_of_week = now - timedelta(days=now.weekday())
            end_of_week = start_of_week + timedelta(days=7)
            if start_of_week.date() <= dt.date() < end_of_week.date():
                filtered.append(d)
    return filtered


async def _send_filtered_listing(send_fn, demos: list[dict], title: str) -> None:
    if not demos:
        await send_fn(f"Ei mielenosoituksia: {title}.", parse_mode=ParseMode.HTML)
        return

    lines = [f"<b>{title}</b> ({len(demos)} kpl)\n"]
    for d in demos[:30]:
        lines.append(format_demo_compact(d))

    await send_fn(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ── Commands ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_grp = _is_group(update)
    is_ch = _is_channel(update)
    is_entity = is_grp or is_ch

    if is_ch:
        await update.effective_message.reply_text("✅ Mielenosoitukset.fi -botti kanavalla.")
        return

    if is_grp:
        text = (
            f"<b>{get_chat_title(update)}</b>\n\n"
            "Tilaa mielenosoituksia kaupungin, järjestön tai ketjun mukaan.\n"
            "Käytä /tilaa hallitaksesi tilauksia.\n\n"
            "Pikakäskyt:\n"
            "  /tanaan – tänään tapahtuvat\n"
            "  /viikolla – tällä viikolla\n"
            "  /listaa – kaikki tulevat\n\n"
            "Ylläpitäjä voi hallita asetuksia DM:stä komennolla /ryhma."
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    if not await is_bootstrapped():
        await add_admin(chat_id)
        text = (
            "<b>Tervetuloa!</b>\n\n"
            "Olet bottin ensimmäinen käyttäjä ja sait automaattisesti "
            "ylläpitäjän oikeudet.\n\n"
            "Käytä /config asettaaksesi API-tokenin.\n"
            "Sitten voit tilata mielenosoituksia alta."
        )
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                        reply_markup=await _main_menu())
        return

    text = await _overview(chat_id, get_chat_title(update))
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=await _main_menu())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_entity = _is_group(update) or _is_channel(update)
    text = await _overview(chat_id, get_chat_title(update), is_entity=is_entity)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML,
                                    reply_markup=await _main_menu(is_group_or_channel=is_entity))


async def tilaa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await menu(update, context)


async def ohjeet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "<b>Ohjeet</b>\n\n"
        "🏙️ <b>Kaupungit</b> – ilmoitus kaikista mielenosoituksista kaupungissa\n"
        "🏢 <b>Järjestöt</b> – ilmoitus tietyn järjestön järjestämistä\n"
        "🔁 <b>Ketjut</b> – ilmoitus toistuvista mielenosoitusketjuista\n\n"
        "<b>Pikakäskyt:</b>\n"
        "  /tanaan – tänään tapahtuvat\n"
        "  /viikolla – tällä viikolla tapahtuvat\n"
        "  /listaa – kaikki tulevat\n"
        "  /tilaa – hallitse tilauksia\n\n"
        "<b>Ryhmiin ja kanaviin:</b>\n"
        "Ylläpitäjä voi liittää DM:nsä komennolla /ryhma."
    )
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)


# ── Quick-filter commands ──────────────────────────────────────────

async def tanaan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not token_manager.is_configured():
        await update.effective_message.reply_text("❌ API-tokenia ei ole asetettu.")
        return
    msg = await update.effective_message.reply_text("Haetaan…")
    try:
        demos = await fetch_all_upcoming_demos(max_days_till=1, per_page=100)
        today = _filter_demos(demos, today=True)
        await msg.delete()
        await _send_filtered_listing(update.effective_message.reply_text, today, "Tänään")
    except Exception:
        logger.exception("Failed")
        await msg.edit_text("❌ Haku epäonnistui.")


async def viikolla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not token_manager.is_configured():
        await update.effective_message.reply_text("❌ API-tokenia ei ole asetettu.")
        return
    msg = await update.effective_message.reply_text("Haetaan…")
    try:
        demos = await fetch_all_upcoming_demos(max_days_till=14, per_page=100)
        this_week = _filter_demos(demos, this_week=True)
        await msg.delete()
        await _send_filtered_listing(update.effective_message.reply_text, this_week, "Tällä viikolla")
    except Exception:
        logger.exception("Failed")
        await msg.edit_text("❌ Haku epäonnistui.")


# ── Group/channel pairing ──────────────────────────────────────────

async def ryhma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a pairing code. Works in groups AND channels."""
    if not (_is_group(update) or _is_channel(update)):
        await update.effective_message.reply_text(
            "Käytä tätä komentoa ryhmässä tai kanavassa, jonka haluat liittää DM:ään.\n"
            "Siirry ryhmään/kanavaan ja lähetä /ryhma siellä."
        )
        return

    # Channel posts can only be authored by channel admins, so we trust them.
    # In groups the effective_user is present and we verify admin status.
    if update.message is not None and not await _is_chat_admin(update):
        await update.effective_message.reply_text("❌ Vain ylläpitäjä voi liittää ryhmän/kanavan.")
        return

    _cleanup_pending_pairing()
    code = secrets.token_urlsafe(4).upper()
    entity_type = "channel" if _is_channel(update) else "group"
    _pending_pairing[code] = {
        "entity_chat_id": update.effective_chat.id,
        "entity_title": get_chat_title(update),
        "entity_type": entity_type,
        "created_at": time.time(),
    }
    await update.effective_message.reply_text(
        f"<b>Liittämislinkki luotu!</b>\n\n"
        f"Koodi: <code>{code}</code>\n\n"
        f"Siirry botin DM-chattiin ja lähetä:\n"
        f"<code>/liita {code}</code>\n\n"
        f"Koodi on voimassa 10 minuuttia.",
        parse_mode=ParseMode.HTML,
    )


async def liita(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Link a group/channel to this DM chat. Must be sent in DM."""
    if not _is_private(update):
        await update.effective_message.reply_text("Käytä tätä komentoa botin DM-chattissa.")
        return

    args = context.args
    if not args:
        await update.effective_message.reply_text(
            "Käytä näin: <code>/liita &lt;koodi&gt;</code>\n\n"
            "Hanki koodi ryhmässä/kanavassa komennolla /ryhma.",
            parse_mode=ParseMode.HTML,
        )
        return

    code = args[0].strip().upper()
    _cleanup_pending_pairing()

    if code not in _pending_pairing:
        await update.effective_message.reply_text(
            "❌ Kelvoton tai vanhentunut koodi.\n"
            "Pyydä ylläpitäjää luomaan uusi koodi komennolla /ryhma ryhmässä/kanavassa."
        )
        return

    info = _pending_pairing.pop(code)
    user_id = update.effective_chat.id
    await link_entity(user_id, info["entity_chat_id"], info["entity_type"], info["entity_title"])

    await update.effective_message.reply_text(
        f"<b>Liitetty!</b>\n\n"
        f"Tyyppi: {info['entity_type']}\n"
        f"Nimi: {info['entity_title']}\n\n"
        f"Nyt voit hallita tilauksia täältä DM:stä.\n"
        f"Käytä /hallinta nähdäksesi liitetyt ryhmät ja kanavat.",
        parse_mode=ParseMode.HTML,
    )


async def hallinta(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all linked groups and channels."""
    if not _is_private(update):
        return

    entities = await get_linked_entities(update.effective_chat.id)
    if not entities:
        await update.effective_message.reply_text(
            "Et ole liittänyt yhtään ryhmää tai kanavaa.\n"
            "Liitä ryhmä/kanava komennolla /ryhma siellä, sitten /liita <koodi> tässä."
        )
        return

    buttons = []
    for ent in entities:
        subs = await get_subscriptions(ent["chat_id"])
        count = len(subs)
        title = subs[0]["chat_title"] if subs else str(ent["chat_id"])
        icon = "📢" if ent["type"] == "channel" else "👥"
        buttons.append([
            InlineKeyboardButton(
                f"{icon} {title} ({count} tilausta)",
                callback_data=f"entity:{ent['chat_id']}"
            )
        ])

    await update.effective_message.reply_text(
        "<b>Liitetyt ryhmät ja kanavat:</b>\nValitse hallittava:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Config ─────────────────────────────────────────────────────────

async def config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_grp = _is_group(update)

    if is_grp:
        if not await _is_chat_admin(update):
            await update.effective_message.reply_text("❌ Vain ryhmän ylläpitäjä voi asettaa tokenia.")
            return
    else:
        if not await _is_admin(chat_id):
            await update.effective_message.reply_text("❌ Sinulla ei ole oikeutta määrittää tokenia.")
            return

    args = context.args
    if not args:
        await update.effective_message.reply_text(
            "Käytä näin:\n\n<code>/config &lt;lyhytaikainen-token&gt;</code>\n\n"
            "Botti vaihtaa tokenin pitkäaikaiseksi (90pv) ja tallentaa sen.",
            parse_mode=ParseMode.HTML,
        )
        return

    token = args[0].strip()
    try:
        result = await token_manager.exchange_for_long_lived(token)
    except token_manager.TokenError as e:
        await update.effective_message.reply_text(f"❌ Tokenin vaihto epäonnistui:\n{e}")
        return

    token_manager.store_token({
        "token": result["token"],
        "expires_at": result.get("expires_at"),
        "source_token": token,
    })
    await update.effective_message.reply_text(
        "✅ Token asetettu! Käytä /paivita päivittääksesi katalogi.",
        parse_mode=ParseMode.HTML,
    )


async def paivita(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_grp = _is_group(update)

    if is_grp:
        if not await _is_chat_admin(update):
            await update.effective_message.reply_text("❌ Vain ryhmän ylläpitäjä voi päivittää katalogia.")
            return
    else:
        if not await _is_admin(chat_id):
            await update.effective_message.reply_text("❌ Sinulla ei ole oikeutta.")
            return

    msg = await update.effective_message.reply_text("Päivitetään katalogia…")
    await build_catalog()
    await msg.edit_text(
        f"✅ Katalogi päivitetty:\n🏙️ {len(CITIES)} kaupunkia · 🏢 {len(ORGS)} järjestöä · 🔁 {len(CHAINS)} ketjua",
        parse_mode=ParseMode.HTML,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subs = await get_subscriptions(chat_id)
    token_state = "✅" if token_manager.is_configured() else "❌"
    lines = [
        f"<b>Botti status</b>",
        f"API token: {token_state}",
        f"Tilaukset: {len(subs)}",
    ]
    if isinstance(subs, list) and subs:
        from collections import Counter
        counts = Counter(s["sub_type"] for s in subs)
        lines.append("  " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── Callbacks ──────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    chat_title = get_chat_title(update)
    is_entity = _is_group(update) or _is_channel(update)

    if data == "noop":
        return

    if data == "back_menu":
        await query.edit_message_text(
            await _overview(chat_id, chat_title, is_entity=is_entity),
            parse_mode=ParseMode.HTML,
            reply_markup=await _main_menu(is_group_or_channel=is_entity),
        )
        return

    if data == "go_hallinta":
        # Redirect to DM
        await query.edit_message_text(
            "Avaa botin DM-chatti ja käytä /hallinta hallitaksesi ryhmiä ja kanavia.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "cities":
        await query.edit_message_text("🏙️ <b>Valitse kaupunki (aakkosittain):</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=_city_alphabet_markup("city_page", "city_search", "back_menu"))
        return
    if data == "orgs":
        await query.edit_message_text("🏢 <b>Valitse järjestö (tai etsi):</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _org_keyboard(chat_id))
        return
    if data == "chains":
        await query.edit_message_text("🔁 <b>Valitse mielenosoitusketju:</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _chain_keyboard(chat_id))
        return
    if data == "help":
        await query.edit_message_text(
            "<b>Ohjeet</b>\n\n"
            "🏙️ <b>Kaupungit</b> – ilmoitus kaikista mielenosoituksista kaupungissa.\n"
            "🏢 <b>Järjestöt</b> – ilmoitus tietyn järjestön järjestämistä.\n"
            "🔁 <b>Ketjut</b> – ilmoitus toistuvista mielenosoitusketjuista.\n\n"
            "Paina ➕ tilataksesi, ✅ poistaaksesi.\n"
            "Etkö löydä järjestöä? Paina <b>Etsi järjestöä</b> ja kirjoita nimi.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")]]),
        )
        return
    if data == "search_org":
        _org_search_pending.add(chat_id)
        await query.edit_message_text(
            "Kirjoita järjestön nimi (vähintään 2 merkkiä).\n"
            "Peruuta kirjoittamalla /peru.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Entity management (linked groups/channels)
    if data.startswith("entity:"):
        gid = int(data.split(":")[1])
        subs = await get_subscriptions(gid)
        title = subs[0]["chat_title"] if subs else str(gid)
        lines = [f"<b>{title} – tilaukset:</b>\n"]
        city = [s["sub_label"] for s in subs if s["sub_type"] == "city"]
        org = [s["sub_label"] for s in subs if s["sub_type"] == "org"]
        chain = [s["sub_label"] for s in subs if s["sub_type"] == "chain"]
        if city:
            lines.append("🏙️ " + ", ".join(city))
        if org:
            lines.append("🏢 " + ", ".join(org))
        if chain:
            lines.append("🔁 " + ", ".join(chain))
        if not subs:
            lines.append("Ei tilauksia.")

        buttons = [
            [InlineKeyboardButton("🏙️ Kaupungit", callback_data=f"ent_city:{gid}")],
            [InlineKeyboardButton("🏢 Järjestöt", callback_data=f"ent_org:{gid}")],
            [InlineKeyboardButton("🔁 Ketjut", callback_data=f"ent_chain:{gid}")],
            [InlineKeyboardButton("◀️ Takaisin", callback_data="back_hallinta")],
        ]
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data == "back_hallinta":
        entities = await get_linked_entities(chat_id)
        buttons = []
        for ent in entities:
            subs = await get_subscriptions(ent["chat_id"])
            count = len(subs)
            title = subs[0]["chat_title"] if subs else str(ent["chat_id"])
            icon = "📢" if ent["type"] == "channel" else "👥"
            buttons.append([
                InlineKeyboardButton(f"{icon} {title} ({count})", callback_data=f"entity:{ent['chat_id']}")
            ])
        await query.edit_message_text(
            "<b>Liitetyt ryhmät ja kanavat:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Entity subscription toggles
    if data.startswith("ent_city_sr:"):
        _, gid, city = data.split(":", 2)
        gid = int(gid)
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "city")}
        if city in subs:
            await remove_subscription(gid, "city", city)
        else:
            await add_subscription(gid, "", "city", city, city)
        term = _entity_city_search_term.get(chat_id, "")
        await query.edit_message_text(f"🔍 <b>Haku: {term}</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _city_search_markup(gid, term, f"ent_city_sr:{gid}", f"ent_city:{gid}"))
        return

    if data.startswith("ent_city_search:"):
        gid = int(data.split(":")[1])
        _entity_city_search_pending[chat_id] = gid
        await query.edit_message_text(
            "Kirjoita kaupungin nimi (vähintään 2 merkkiä).\n"
            "Peruuta kirjoittamalla /peru.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("ent_city_page:"):
        _, gid, letter, page = data.split(":", 3)
        gid = int(gid)
        await query.edit_message_text(f"🏙️ <b>Valitse kaupunki · {letter}</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _city_page_markup(
                                          gid, f"ent_city_page:{gid}", f"ent_city_tog:{gid}", letter,
                                          int(page), f"ent_city_search:{gid}", f"ent_city:{gid}", f"entity:{gid}"))
        return

    if data.startswith("ent_city_tog:"):
        _, gid, letter, page, city = data.split(":", 4)
        gid = int(gid)
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "city")}
        if city in subs:
            await remove_subscription(gid, "city", city)
        else:
            await add_subscription(gid, "", "city", city, city)
        await query.edit_message_text(f"🏙️ <b>Valitse kaupunki · {letter}</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _city_page_markup(
                                          gid, f"ent_city_page:{gid}", f"ent_city_tog:{gid}", letter,
                                          int(page), f"ent_city_search:{gid}", f"ent_city:{gid}", f"entity:{gid}"))
        return

    if data.startswith("ent_city:"):
        gid = int(data.split(":")[1])
        await query.edit_message_text("🏙️ <b>Valitse kaupunki (aakkosittain):</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=_city_alphabet_markup(
                                          f"ent_city_page:{gid}", f"ent_city_search:{gid}", f"entity:{gid}"))
        return

    if data.startswith("ent_org:"):
        gid = int(data.split(":")[1])
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "org")}
        buttons = []
        for org in ORGS:
            mark = "✅" if org["id"] in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {org['name']}",
                                     callback_data=_cb_cap(org["name"], f"ent_org_tog:{gid}:{org['id']}:"))
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"entity:{gid}")])
        await query.edit_message_text("🏢 <b>Valitse järjestö:</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("ent_org_tog:"):
        parts = data.split(":", 3)
        gid, org_id, org_name = int(parts[1]), parts[2], _org_name(parts[2], parts[3])
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "org")}
        if org_id in subs:
            await remove_subscription(gid, "org", org_id)
        else:
            await add_subscription(gid, "", "org", org_id, org_name)
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "org")}
        buttons = []
        for org in ORGS:
            mark = "✅" if org["id"] in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {org['name']}",
                                     callback_data=_cb_cap(org["name"], f"ent_org_tog:{gid}:{org['id']}:"))
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"entity:{gid}")])
        await query.edit_message_text("🏢 <b>Valitse järjestö:</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("ent_chain:"):
        gid = int(data.split(":")[1])
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "chain")}
        buttons = []
        for chain in CHAINS:
            mark = "✅" if chain["id"] in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {chain['title'][:50]}",
                                     callback_data=_cb_cap(chain["title"], f"ent_chain_tog:{gid}:{chain['id']}:"))
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"entity:{gid}")])
        await query.edit_message_text("🔁 <b>Valitse mielenosoitusketju:</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("ent_chain_tog:"):
        parts = data.split(":", 3)
        gid, chain_id = int(parts[1]), parts[2]
        chain_title = _chain_title(parts[2], parts[3])
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "chain")}
        if chain_id in subs:
            await remove_subscription(gid, "chain", chain_id)
        else:
            await add_subscription(gid, "", "chain", chain_id, chain_title)
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "chain")}
        buttons = []
        for chain in CHAINS:
            mark = "✅" if chain["id"] in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {chain['title'][:50]}",
                                     callback_data=_cb_cap(chain["title"], f"ent_chain_tog:{gid}:{chain['id']}:"))
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"entity:{gid}")])
        await query.edit_message_text("🔁 <b>Valitse mielenosoitusketju:</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Direct chat subscription toggles
    if data.startswith("city_sr:"):
        city = data.split(":", 1)[1]
        added = await add_subscription(chat_id, chat_title, "city", city, city)
        if not added:
            await remove_subscription(chat_id, "city", city)
        term = _city_search_term.get(chat_id, "")
        await query.edit_message_text(f"🔍 <b>Haku: {term}</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _city_search_markup(chat_id, term, "city_sr", "cities"))
        return

    if data == "city_search":
        _city_search_pending.add(chat_id)
        _org_search_pending.discard(chat_id)
        await query.edit_message_text(
            "Kirjoita kaupungin nimi (vähintään 2 merkkiä).\n"
            "Peruuta kirjoittamalla /peru.",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("city_page:"):
        _, letter, page = data.split(":", 2)
        await query.edit_message_text(f"🏙️ <b>Valitse kaupunki · {letter}</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _city_page_markup(
                                          chat_id, "city_page", "city_tog", letter,
                                          int(page), "city_search", "cities", "back_menu"))
        return

    if data.startswith("city_tog:"):
        _, letter, page, city = data.split(":", 3)
        added = await add_subscription(chat_id, chat_title, "city", city, city)
        if not added:
            await remove_subscription(chat_id, "city", city)
        await query.edit_message_text(f"🏙️ <b>Valitse kaupunki · {letter}</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _city_page_markup(
                                          chat_id, "city_page", "city_tog", letter,
                                          int(page), "city_search", "cities", "back_menu"))
        return

    if data.startswith("org:"):
        parts = data.split(":", 2)
        org_id, org_name = parts[1], _org_name(parts[1], parts[2])
        added = await add_subscription(chat_id, chat_title, "org", org_id, org_name)
        if not added:
            await remove_subscription(chat_id, "org", org_id)
        await query.edit_message_text("🏢 <b>Valitse järjestö:</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _org_keyboard(chat_id))
        return

    if data.startswith("chain:"):
        parts = data.split(":", 2)
        chain_id, chain_title = parts[1], _chain_title(parts[1], parts[2])
        added = await add_subscription(chat_id, chat_title, "chain", chain_id, chain_title)
        if not added:
            await remove_subscription(chat_id, "chain", chain_id)
        await query.edit_message_text("🔁 <b>Valitse mielenosoitusketju:</b>", parse_mode=ParseMode.HTML,
                                      reply_markup=await _chain_keyboard(chat_id))
        return

    if data.startswith("list:"):
        page = int(data.split(":")[1])
        await query.answer()
        await _send_list_page(
            lambda text, **kw: query.edit_message_text(text, **kw),
            page,
        )
        return


# ── Text search handler ────────────────────────────────────────────

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    query_text = (update.effective_message.text or "").strip()
    if not query_text:
        return

    # Organization search
    if chat_id in _org_search_pending:
        results = await search_organizations(query_text)
        if not results:
            await update.effective_message.reply_text(
                "Ei löytynyt järjestöjä haulla. Yritä toisella nimellä tai /peru.",
                parse_mode=ParseMode.HTML,
            )
            return

        subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "org")}
        buttons = []
        for org in results[:10]:
            oid, oname = org.get("id"), org.get("name")
            mark = "✅" if oid in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {oname}",
                                     callback_data=_cb_cap(oname, f"org:{oid}:"))
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")])
        _org_search_pending.discard(chat_id)
        await update.effective_message.reply_text("Valitse järjestö:", parse_mode=ParseMode.HTML,
                                                  reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Own-chat city search
    if chat_id in _city_search_pending:
        if len(query_text) < 2:
            await update.effective_message.reply_text(
                "Anna vähintään 2 merkkiä.", parse_mode=ParseMode.HTML)
            return
        if not _city_matches(query_text):
            await update.effective_message.reply_text(
                "Ei kaupunkeja haulla. Yritä toisella nimellä tai /peru.",
                parse_mode=ParseMode.HTML,
            )
            return
        _city_search_term[chat_id] = query_text
        await update.effective_message.reply_text(f"🔍 <b>Haku: {query_text}</b>", parse_mode=ParseMode.HTML,
                                                  reply_markup=await _city_search_markup(chat_id, query_text, "city_sr", "cities"))
        return

    # Entity city search (via /hallinta)
    gid = _entity_city_search_pending.get(chat_id)
    if gid is not None:
        if len(query_text) < 2:
            await update.effective_message.reply_text(
                "Anna vähintään 2 merkkiä.", parse_mode=ParseMode.HTML)
            return
        if not _city_matches(query_text):
            await update.effective_message.reply_text(
                "Ei kaupunkeja haulla. Yritä toisella nimellä tai /peru.",
                parse_mode=ParseMode.HTML,
            )
            return
        _entity_city_search_term[chat_id] = query_text
        await update.effective_message.reply_text(f"🔍 <b>Haku: {query_text}</b>", parse_mode=ParseMode.HTML,
                                                  reply_markup=await _city_search_markup(gid, query_text, f"ent_city_sr:{gid}", f"ent_city:{gid}"))
        return


async def peru(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _org_search_pending.discard(chat_id)
    _city_search_pending.discard(chat_id)
    _city_search_term.pop(chat_id, None)
    _entity_city_search_pending.pop(chat_id, None)
    _entity_city_search_term.pop(chat_id, None)
    await update.effective_message.reply_text("Peruutettu.", reply_markup=await _main_menu())


# ── Listing upcoming demos ─────────────────────────────────────────

DEMO_LIST_PAGE_SIZE = 20


async def listaa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_list_page(update.effective_message.reply_text, 0)


async def _send_list_page(send_fn, page: int) -> None:
    if not token_manager.is_configured():
        await send_fn("❌ API-tokenia ei ole asetettu. Käytä /config.", parse_mode=ParseMode.HTML)
        return

    try:
        data = await fetch_all_upcoming_demos(max_days_till=90, per_page=100)
    except Exception:
        logger.exception("Failed to fetch demos for listing")
        await send_fn("❌ Mielenosoituksia ei voitu hakea.", parse_mode=ParseMode.HTML)
        return

    total = len(data)
    if total == 0:
        await send_fn("Ei tulevia mielenosoituksia.", parse_mode=ParseMode.HTML)
        return

    start = page * DEMO_LIST_PAGE_SIZE
    end = start + DEMO_LIST_PAGE_SIZE
    chunk = data[start:end]

    lines = [f"<b>Tulevat mielenosoitukset</b> ({start+1}–{min(end, total)}/{total})\n"]
    for d in chunk:
        lines.append(format_demo_compact(d))

    buttons = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Edelliset", callback_data=f"list:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Seuraavat ▶️", callback_data=f"list:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("🔄 Päivitä", callback_data=f"list:{page}")])

    await send_fn(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


# ── Handler registration ───────────────────────────────────────────

def build_handlers(app) -> None:
    app.add_handler(CommandHandler("start", start, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("menu", menu, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("tilaa", tilaa, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("listaa", listaa, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("tanaan", tanaan, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("viikolla", viikolla, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("ohjeet", ohjeet, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("config", config, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("paivita", paivita, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("status", status, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("peru", peru, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("ryhma", ryhma, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("liita", liita, filters=_COMMAND_FILTER))
    app.add_handler(CommandHandler("hallinta", hallinta, filters=_COMMAND_FILTER))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


async def set_admin_from_env(chat_ids: str) -> None:
    """Seed admins from ADMIN_CHAT_IDS env var (comma-separated)."""
    if chat_ids:
        for cid in chat_ids.split(","):
            cid = cid.strip()
            if cid:
                await add_admin(int(cid))
