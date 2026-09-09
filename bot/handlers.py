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
from .api_client import fetch_demo_detail, fetch_upcoming_demos, search_organizations
from .notifications import format_demo_compact
from .database import (
    add_subscription,
    add_admin,
    get_admins,
    is_bootstrapped,
    get_subscriptions,
    remove_subscription,
    link_group,
    get_linked_groups,
)

logger = logging.getLogger(__name__)

# Cached catalogs
CITIES: list[str] = []
ORGS: list[dict] = []
CHAINS: list[dict] = []

# Pending org-search state per chat
_org_search_pending: set[int] = set()

# Pending group pairing: code -> {group_chat_id, group_title, created_at}
_pending_pairing: dict[str, dict] = {}

PAIRING_CODE_TTL = 600  # 10 minutes


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

async def build_catalog(max_days_till: int = 60) -> None:
    global CITIES, ORGS, CHAINS
    try:
        demos = await fetch_upcoming_demos(max_days_till=max_days_till, per_page=100)
        city_set = {d.get("city") for d in demos if d.get("city")}
        if city_set:
            CITIES = sorted(city_set)

        chain_set: dict[str, str] = {}
        org_map: dict[str, str] = {}

        for d in demos[:30]:
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
            for org in detail.get("organizers") or []:
                oid = org.get("organization_id") or org.get("id")
                name = org.get("name")
                if oid and name:
                    org_map.setdefault(str(oid), name)

        CHAINS = [{"id": pid, "title": title} for pid, title in chain_set.items()]
        ORGS = [{"id": oid, "name": name} for oid, name in org_map.items()]
    except Exception:
        logger.exception("Failed to build catalog")


# ── Menu builders ──────────────────────────────────────────────────

async def _main_menu(is_group: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📋 Tulevat", callback_data="list:0")],
        [InlineKeyboardButton("🏙️ Kaupungit", callback_data="cities"),
         InlineKeyboardButton("🏢 Järjestöt", callback_data="orgs")],
        [InlineKeyboardButton("🔁 Ketjut", callback_data="chains"),
         InlineKeyboardButton("ℹ️ Ohjeet", callback_data="help")],
    ]
    if is_group:
        rows.insert(0, [InlineKeyboardButton("⚙️ Ryhmäasetukset (DM)", callback_data="group_settings")])
    return InlineKeyboardMarkup(rows)


async def _overview(chat_id: int, chat_title: str, is_group: bool = False) -> str:
    subs = await get_subscriptions(chat_id)
    if not subs:
        if is_group:
            return (
                f"*{chat_title}*\n\n"
                "Tilaa mielenosoituksia kaupungin, järjestön tai ketjun mukaan.\n"
                "Valitse alta tai käytä komentoja:\n"
                "  /tanaan – tänään\n"
                "  /viikolla – tällä viikolla\n"
                "  /listaa – kaikki tulevat\n"
                "  /tilaa – hallitse tilauksia\n\n"
                "Ylläpitäjä voi hallita asetuksia DM:stä komennolla /liita."
            )
        return (
            f"*{chat_title}*\n\n"
            "Tilaa mielenosoituksia kaupungin, järjestön tai ketjun mukaan.\n\n"
            "Valitse alta:\n"
            "🏙️ *Kaupungit* – kaikki mielenosoitukset kaupungissa\n"
            "🏢 *Järjestöt* – tietyn järjestön järjestämät\n"
            "🔁 *Ketjut* – toistuvat mielenosoitusketjut\n\n"
            "Tai komentoilla: /tanaan, /viikolla, /listaa, /tilaa"
        )

    lines = [f"*{chat_title} – tilauksesi:*\n"]
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


async def _city_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "city")}
    buttons = [
        [InlineKeyboardButton(f"{'✅' if c in subs else '➕'} {c}", callback_data=f"city:{c}")]
        for c in CITIES
    ]
    buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")])
    return InlineKeyboardMarkup(buttons)


async def _org_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "org")}
    buttons = []
    for org in ORGS[:30]:
        mark = "✅" if org["id"] in subs else "➕"
        buttons.append([
            InlineKeyboardButton(f"{mark} {org['name']}",
                                 callback_data=f"org:{org['id']}:{org['name']}")
        ])
    buttons.append([InlineKeyboardButton("🔍 Etsi järjestöä", callback_data="search_org")])
    buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")])
    return InlineKeyboardMarkup(buttons)


async def _chain_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "chain")}
    buttons = []
    for chain in CHAINS[:30]:
        mark = "✅" if chain["id"] in subs else "➕"
        buttons.append([
            InlineKeyboardButton(f"{mark} {chain['title'][:40]}",
                                 callback_data=f"chain:{chain['id']}:{chain['title']}")
        ])
    buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")])
    return InlineKeyboardMarkup(buttons)


# ── Time-filtered listings ─────────────────────────────────────────

def _parse_date(d: dict) -> datetime | None:
    """Extract datetime from a demo dict."""
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
            filtered.append(d)  # no date = keep
            continue
        if today and dt.date() == now.date():
            filtered.append(d)
        elif this_week:
            # Mon–Sun of current week
            start_of_week = now - timedelta(days=now.weekday())
            end_of_week = start_of_week + timedelta(days=7)
            if start_of_week.date() <= dt.date() < end_of_week.date():
                filtered.append(d)
    return filtered


async def _send_filtered_listing(send_fn, demos: list[dict], title: str) -> None:
    if not demos:
        await send_fn(f"Ei mielenosoituksia: {title}.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"*{title}* ({len(demos)} kpl)\n"]
    for d in demos[:30]:
        lines.append(format_demo_compact(d))

    await send_fn(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Päivitä", callback_data=f"refresh:{title}")]
        ]),
    )


# ── Commands ───────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_grp = _is_group(update)
    is_ch = _is_channel(update)

    if is_ch:
        await update.message.reply_text("✅ Mielenosoitukset.fi -botti kanavalla.")
        return

    if is_grp:
        text = (
            f"*{get_chat_title(update)}*\n\n"
            "Tilaa mielenosoituksia kaupungin, järjestön tai ketjun mukaan.\n"
            "Käytä /tilaa hallitaksesi tilauksia.\n\n"
            "Pikakäskyt:\n"
            "  /tanaan – tänään tapahtuvat\n"
            "  /viikolla – tällä viikolla\n"
            "  /listaa – kaikki tulevat\n\n"
            "Ylläpitäjä voi hallita asetuksia DM:stä komennolla /liita."
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
        return

    if not await is_bootstrapped():
        await add_admin(chat_id)
        text = (
            "*Tervetuloa!*\n\n"
            "Olet bottin ensimmäinen käyttäjä ja sait automaattisesti "
            "ylläpitäjän oikeudet.\n\n"
            "Käytä /config asettaaksesi API-tokenin.\n"
            "Sitten voit tilata mielenosoituksia alta."
        )
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                        reply_markup=await _main_menu())
        return

    text = await _overview(chat_id, get_chat_title(update))
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=await _main_menu())


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_grp = _is_group(update)
    text = await _overview(chat_id, get_chat_title(update), is_group=is_grp)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=await _main_menu(is_group=is_grp))


async def tilaa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show subscription management menu."""
    await menu(update, context)


async def ohjeet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "*Ohjeet*\n\n"
        "🏙️ *Kaupungit* – ilmoitus kaikista mielenosoituksista kaupungissa\n"
        "🏢 *Järjestöt* – ilmoitus tietyn järjestön järjestämistä\n"
        "🔁 *Ketjut* – ilmoitus toistuvista mielenosoitusketjuista\n\n"
        "*Pikakäskyt:*\n"
        "  /tanaan – tänään tapahtuvat\n"
        "  /viikolla – tällä viikolla tapahtuvat\n"
        "  /listaa – kaikki tulevat\n"
        "  /tilaa – hallitse tilauksia\n\n"
        "*Ryhmille:*\n"
        "Ylläpitäjä voi liittää DM:nsä ryhmään komennolla /liita.\n"
        "Sen jälkeen hallitse ryhmän tilauksia suoraan DM:stä."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


# ── Quick-filter commands ──────────────────────────────────────────

async def tanaan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not token_manager.is_configured():
        await update.message.reply_text("❌ API-tokenia ei ole asetettu.")
        return
    msg = await update.message.reply_text("Haetaan…")
    try:
        demos = await fetch_upcoming_demos(max_days_till=1, per_page=100)
        today = _filter_demos(demos, today=True)
        await msg.delete()
        await _send_filtered_listing(update.message.reply_text, today, "Tänään")
    except Exception:
        logger.exception("Failed")
        await msg.edit_text("❌ Haku epäonnistui.")


async def viikolla(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not token_manager.is_configured():
        await update.message.reply_text("❌ API-tokenia ei ole asetettu.")
        return
    msg = await update.message.reply_text("Haetaan…")
    try:
        demos = await fetch_upcoming_demos(max_days_till=14, per_page=100)
        this_week = _filter_demos(demos, this_week=True)
        await msg.delete()
        await _send_filtered_listing(update.message.reply_text, this_week, "Tällä viikolla")
    except Exception:
        logger.exception("Failed")
        await msg.edit_text("❌ Haku epäonnistui.")


# ── Group pairing ──────────────────────────────────────────────────

async def ryhma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate a pairing code for a group. Must be sent IN the group."""
    if not _is_group(update):
        await update.message.reply_text(
            "Käytä tätä komentoa ryhmässä, jonka haluat liittää DM:ään.\n"
            "Siirry ryhmään ja lähetä /ryhma siellä."
        )
        return

    if not await _is_chat_admin(update):
        await update.message.reply_text("❌ Vain ryhmän ylläpitäjä voi liittää ryhmän.")
        return

    _cleanup_pending_pairing()
    code = secrets.token_urlsafe(4).upper()
    _pending_pairing[code] = {
        "group_chat_id": update.effective_chat.id,
        "group_title": get_chat_title(update),
        "created_at": time.time(),
    }
    await update.message.reply_text(
        f"*Liittämislinkki luotu!*\n\n"
        f"Koodi: `{code}`\n\n"
        f"Siirry botin DM-chattiin ja lähetä:\n"
        f"`/liita {code}`\n\n"
        f"Koodi on voimassa 10 minuuttia.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def liita(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Link a group to this DM chat. Must be sent in DM."""
    if not _is_private(update):
        await update.message.reply_text("Käytä tätä komentoa botin DM-chattissa.")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Käytä näin: `/liita <koodi>`\n\n"
            "Hanki koodi ryhmässä komennolla /ryhma.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    code = args[0].strip().upper()
    _cleanup_pending_pairing()

    if code not in _pending_pairing:
        await update.message.reply_text(
            "❌ Kelvoton tai vanhentunut koodi.\n"
            "Pyydä ryhmän ylläpitäjää luomaan uusi koodi komennolla /ryhma ryhmässä."
        )
        return

    info = _pending_pairing.pop(code)
    user_id = update.effective_chat.id
    await link_group(user_id, info["group_chat_id"], info["group_title"])

    # Also make user an admin for the group
    await add_admin(info["group_chat_id"])

    await update.message.reply_text(
        f"✅ *Ryhmä liitetty!*\n\n"
        f"Ryhmä: {info['group_title']}\n\n"
        f"Nyt voit hallita ryhmän tilauksia täältä DM:stä.\n"
        f"Käytä /ryhma_listaa nähdäksesi ryhmän tilaukset.\n"
        f"Tai käytä /tilaa_valitse valitaksesi ryhmän.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def ryhma_listaa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List subscriptions for linked groups."""
    if not _is_private(update):
        return

    groups = await get_linked_groups(update.effective_chat.id)
    if not groups:
        await update.message.reply_text(
            "Et ole liittänyt yhtään ryhmää.\n"
            "Liitä ryhmä komennolla /liita <koodi> (koodi saadaan ryhmässä /ryhma)."
        )
        return

    buttons = []
    for gid in groups:
        subs = await get_subscriptions(gid)
        count = len(subs)
        title = subs[0]["chat_title"] if subs else str(gid)
        buttons.append([
            InlineKeyboardButton(
                f"{title} ({count} tilausta)",
                callback_data=f"rg:{gid}"
            )
        ])

    await update.message.reply_text(
        "*Ryhmät:*\nValitse hallittava ryhmä:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Config ─────────────────────────────────────────────────────────

async def config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_grp = _is_group(update)

    if is_grp:
        if not await _is_chat_admin(update):
            await update.message.reply_text("❌ Vain ryhmän ylläpitäjä voi asettaa tokenia.")
            return
    else:
        if not await _is_admin(chat_id):
            await update.message.reply_text("❌ Sinulla ei ole oikeutta määrittää tokenia.")
            return

    args = context.args
    if not args:
        await update.message.reply_text(
            "Käytä näin:\n\n`/config <lyhytaikainen-token>`\n\n"
            "Botti vaihtaa tokenin pitkäaikaiseksi (90pv) ja tallentaa sen.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    token = args[0].strip()
    try:
        result = await token_manager.exchange_for_long_lived(token)
    except token_manager.TokenError as e:
        await update.message.reply_text(f"❌ Tokenin vaihto epäonnistui:\n{e}")
        return

    token_manager.store_token({
        "token": result["token"],
        "expires_at": result.get("expires_at"),
        "source_token": token,
    })
    await update.message.reply_text(
        "✅ Token asetettu! Käytä /paivita päivittääksesi katalogi.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def paivita(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    is_grp = _is_group(update)

    if is_grp:
        if not await _is_chat_admin(update):
            await update.message.reply_text("❌ Vain ryhmän ylläpitäjä voi päivittää katalogia.")
            return
    else:
        if not await _is_admin(chat_id):
            await update.message.reply_text("❌ Sinulla ei ole oikeutta.")
            return

    msg = await update.message.reply_text("Päivitetään katalogia…")
    await build_catalog()
    await msg.edit_text(
        f"✅ Katalogi päivitetty:\n🏙️ {len(CITIES)} kaupunkia · 🏢 {len(ORGS)} järjestöä · 🔁 {len(CHAINS)} ketjua",
        parse_mode=ParseMode.MARKDOWN,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    subs = await get_subscriptions(chat_id)
    token_state = "✅" if token_manager.is_configured() else "❌"
    lines = [
        f"*Botti status*",
        f"API token: {token_state}",
        f"Tilaukset: {len(subs)}",
    ]
    if isinstance(subs, list) and subs:
        from collections import Counter
        counts = Counter(s["sub_type"] for s in subs)
        lines.append("  " + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Callbacks ──────────────────────────────────────────────────────

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = query.message.chat_id
    chat_title = get_chat_title(update)
    is_grp = _is_group(update)

    if data == "back_menu":
        await query.edit_message_text(
            await _overview(chat_id, chat_title, is_group=is_grp),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=await _main_menu(is_group=is_grp),
        )
        return

    if data == "cities":
        await query.edit_message_text("🏙️ *Valitse kaupunki:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=await _city_keyboard(chat_id))
        return
    if data == "orgs":
        await query.edit_message_text("🏢 *Valitse järjestö (tai etsi):*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=await _org_keyboard(chat_id))
        return
    if data == "chains":
        await query.edit_message_text("🔁 *Valitse mielenosoitusketju:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=await _chain_keyboard(chat_id))
        return
    if data == "help":
        await query.edit_message_text(
            "*Ohjeet*\n\n"
            "🏙️ *Kaupungit* – ilmoitus kaikista mielenosoituksista kaupungissa.\n"
            "🏢 *Järjestöt* – ilmoitus tietyn järjestön järjestämistä.\n"
            "🔁 *Ketjut* – ilmoitus toistuvista mielenosoitusketjuista.\n\n"
            "Paina ➕ tilataksesi, ✅ poistaaksesi.\n"
            "Etkö löydä järjestöä? Paina *Etsi järjestöä* ja kirjoita nimi.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")]]),
        )
        return
    if data == "search_org":
        _org_search_pending.add(chat_id)
        await query.edit_message_text(
            "Kirjoita järjestön nimi (vähintään 2 merkkiä).\n"
            "Peruuta kirjoittamalla /peru.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if data == "group_settings":
        # In DM, show linked groups
        groups = await get_linked_groups(chat_id)
        if not groups:
            await query.edit_message_text(
                "Et ole liittänyt yhtään ryhmää.\n"
                "Liitä ryhmä komennolla /liita <koodi>.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")]
                ]),
            )
            return
        buttons = []
        for gid in groups:
            subs = await get_subscriptions(gid)
            count = len(subs)
            title = subs[0]["chat_title"] if subs else str(gid)
            buttons.append([
                InlineKeyboardButton(f"{title} ({count})", callback_data=f"rg:{gid}")
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")])
        await query.edit_message_text(
            "*Liitetyt ryhmät:*",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # Linked group management
    if data.startswith("rg:"):
        gid = int(data.split(":")[1])
        subs = await get_subscriptions(gid)
        title = subs[0]["chat_title"] if subs else str(gid)
        lines = [f"*{title} – tilaukset:*\n"]
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

        # Show sub-management buttons prefixed with group id
        buttons = [
            [InlineKeyboardButton("🏙️ Kaupungit", callback_data=f"rg_city:{gid}")],
            [InlineKeyboardButton("🏢 Järjestöt", callback_data=f"rg_org:{gid}")],
            [InlineKeyboardButton("🔁 Ketjut", callback_data=f"rg_chain:{gid}")],
            [InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")],
        ]
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    if data.startswith("rg_city:"):
        gid = int(data.split(":")[1])
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "city")}
        buttons = [
            [InlineKeyboardButton(f"{'✅' if c in subs else '➕'} {c}",
                                  callback_data=f"rg_city_tog:{gid}:{c}")]
            for c in CITIES
        ]
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"rg:{gid}")])
        await query.edit_message_text("🏙️ *Valitse kaupunki:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("rg_city_tog:"):
        parts = data.split(":", 2)
        gid, city = int(parts[1]), parts[2]
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "city")}
        if city in subs:
            await remove_subscription(gid, "city", city)
        else:
            await add_subscription(gid, "", "city", city, city)
        # Rebuild keyboard
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "city")}
        buttons = [
            [InlineKeyboardButton(f"{'✅' if c in subs else '➕'} {c}",
                                  callback_data=f"rg_city_tog:{gid}:{c}")]
            for c in CITIES
        ]
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"rg:{gid}")])
        await query.edit_message_text("🏙️ *Valitse kaupunki:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("rg_org:"):
        gid = int(data.split(":")[1])
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "org")}
        buttons = []
        for org in ORGS[:30]:
            mark = "✅" if org["id"] in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {org['name']}",
                                     callback_data=f"rg_org_tog:{gid}:{org['id']}:{org['name']}")
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"rg:{gid}")])
        await query.edit_message_text("🏢 *Valitse järjestö:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("rg_org_tog:"):
        parts = data.split(":", 3)
        gid, org_id, org_name = int(parts[1]), parts[2], parts[3]
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "org")}
        if org_id in subs:
            await remove_subscription(gid, "org", org_id)
        else:
            await add_subscription(gid, "", "org", org_id, org_name)
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "org")}
        buttons = []
        for org in ORGS[:30]:
            mark = "✅" if org["id"] in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {org['name']}",
                                     callback_data=f"rg_org_tog:{gid}:{org['id']}:{org['name']}")
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"rg:{gid}")])
        await query.edit_message_text("🏢 *Valitse järjestö:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("rg_chain:"):
        gid = int(data.split(":")[1])
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "chain")}
        buttons = []
        for chain in CHAINS[:30]:
            mark = "✅" if chain["id"] in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {chain['title'][:40]}",
                                     callback_data=f"rg_chain_tog:{gid}:{chain['id']}:{chain['title']}")
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"rg:{gid}")])
        await query.edit_message_text("🔁 *Valitse mielenosoitusketju:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    if data.startswith("rg_chain_tog:"):
        parts = data.split(":", 3)
        gid, chain_id, chain_title = int(parts[1]), parts[2], parts[3]
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "chain")}
        if chain_id in subs:
            await remove_subscription(gid, "chain", chain_id)
        else:
            await add_subscription(gid, "", "chain", chain_id, chain_title)
        subs = {s["sub_key"] for s in await get_subscriptions(gid, "chain")}
        buttons = []
        for chain in CHAINS[:30]:
            mark = "✅" if chain["id"] in subs else "➕"
            buttons.append([
                InlineKeyboardButton(f"{mark} {chain['title'][:40]}",
                                     callback_data=f"rg_chain_tog:{gid}:{chain['id']}:{chain['title']}")
            ])
        buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data=f"rg:{gid}")])
        await query.edit_message_text("🔁 *Valitse mielenosoitusketju:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=InlineKeyboardMarkup(buttons))
        return

    # Direct chat subscription toggles
    if data.startswith("city:"):
        city = data.split(":", 1)[1]
        added = await add_subscription(chat_id, chat_title, "city", city, city)
        if not added:
            await remove_subscription(chat_id, "city", city)
        await query.edit_message_text("🏙️ *Valitse kaupunki:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=await _city_keyboard(chat_id))
        return

    if data.startswith("org:"):
        parts = data.split(":", 2)
        org_id, org_name = parts[1], parts[2]
        added = await add_subscription(chat_id, chat_title, "org", org_id, org_name)
        if not added:
            await remove_subscription(chat_id, "org", org_id)
        await query.edit_message_text("🏢 *Valitse järjestö:*", parse_mode=ParseMode.MARKDOWN,
                                      reply_markup=await _org_keyboard(chat_id))
        return

    if data.startswith("chain:"):
        parts = data.split(":", 2)
        chain_id, chain_title = parts[1], parts[2]
        added = await add_subscription(chat_id, chat_title, "chain", chain_id, chain_title)
        if not added:
            await remove_subscription(chat_id, "chain", chain_id)
        await query.edit_message_text("🔁 *Valitse mielenosoitusketju:*", parse_mode=ParseMode.MARKDOWN,
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
    if chat_id not in _org_search_pending:
        return

    query_text = (update.message.text or "").strip()
    if not query_text:
        return

    results = await search_organizations(query_text)
    if not results:
        await update.message.reply_text(
            "Ei löytynyt järjestöjä haulla. Yritä toisella nimellä tai /peru.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    subs = {s["sub_key"] for s in await get_subscriptions(chat_id, "org")}
    buttons = []
    for org in results[:10]:
        mark = "✅" if org.get("id") in subs else "➕"
        buttons.append([
            InlineKeyboardButton(f"{mark} {org.get('name')}",
                                 callback_data=f"org:{org.get('id')}:{org.get('name')}")
        ])
    buttons.append([InlineKeyboardButton("◀️ Takaisin", callback_data="back_menu")])
    _org_search_pending.discard(chat_id)
    await update.message.reply_text("Valitse järjestö:", parse_mode=ParseMode.MARKDOWN,
                                    reply_markup=InlineKeyboardMarkup(buttons))


async def peru(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _org_search_pending.discard(update.effective_chat.id)
    await update.message.reply_text("Peruutettu.", reply_markup=await _main_menu())


# ── Listing upcoming demos ─────────────────────────────────────────

DEMO_LIST_PAGE_SIZE = 20


async def listaa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_list_page(update.message.reply_text, 0)


async def _send_list_page(send_fn, page: int) -> None:
    if not token_manager.is_configured():
        await send_fn("❌ API-tokenia ei ole asetettu. Käytä /config.", parse_mode=ParseMode.MARKDOWN)
        return

    try:
        data = await fetch_upcoming_demos(max_days_till=90, per_page=100)
    except Exception:
        logger.exception("Failed to fetch demos for listing")
        await send_fn("❌ Mielenosoituksia ei voitu hakea.", parse_mode=ParseMode.MARKDOWN)
        return

    total = len(data)
    if total == 0:
        await send_fn("Ei tulevia mielenosoituksia.", parse_mode=ParseMode.MARKDOWN)
        return

    start = page * DEMO_LIST_PAGE_SIZE
    end = start + DEMO_LIST_PAGE_SIZE
    chunk = data[start:end]

    lines = [f"*Tulevat mielenosoitukset* ({start+1}–{min(end, total)}/{total})\n"]
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
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


# ── Handler registration ───────────────────────────────────────────

def build_handlers(app) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu))
    app.add_handler(CommandHandler("tilaa", tilaa))
    app.add_handler(CommandHandler("listaa", listaa))
    app.add_handler(CommandHandler("tanaan", tanaan))
    app.add_handler(CommandHandler("viikolla", viikolla))
    app.add_handler(CommandHandler("ohjeet", ohjeet))
    app.add_handler(CommandHandler("config", config))
    app.add_handler(CommandHandler("paivita", paivita))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("peru", peru))
    app.add_handler(CommandHandler("ryhma", ryhma))
    app.add_handler(CommandHandler("liita", liita))
    app.add_handler(CommandHandler("ryhma_listaa", ryhma_listaa))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))


async def set_admin_from_env(chat_ids: str) -> None:
    """Seed admins from ADMIN_CHAT_IDS env var (comma-separated)."""
    if chat_ids:
        for cid in chat_ids.split(","):
            cid = cid.strip()
            if cid:
                await add_admin(int(cid))
