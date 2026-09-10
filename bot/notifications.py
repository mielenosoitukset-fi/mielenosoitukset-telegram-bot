from __future__ import annotations

from urllib.parse import quote


def _best_identifier(demo: dict) -> str:
    return demo.get("slug") or str(demo.get("running_number") or demo.get("_id", ""))


def demo_url(demo: dict) -> str:
    return f"https://mielenosoitukset.fi/demonstration/{quote(_best_identifier(demo))}"


def _html_escape(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_MONTHS_FI = ["", "tammikuuta", "helmikuuta", "maaliskuuta", "huhtikuuta", "toukokuuta", "kesäkuuta",
              "heinäkuuta", "elokuuta", "syyskuuta", "lokakuuta", "marraskuuta", "joulukuuta"]


def format_demo(demo: dict) -> str:
    title = demo.get("title") or "Tapahtuma"
    date = demo.get("date") or ""
    start_time = demo.get("start_time") or ""
    end_time = demo.get("end_time") or ""
    city = demo.get("city") or ""
    address = demo.get("address") or ""
    cancelled = bool(demo.get("cancelled"))

    date_str = ""
    if date:
        try:
            y, m, d = (int(x) for x in date.split("-"))
            date_str = f"{d}. {_MONTHS_FI[m]} {y}"
        except (ValueError, KeyError):
            date_str = date

    time_str = start_time
    if time_str and end_time:
        time_str = f"{start_time}–{end_time}"

    url = demo_url(demo)
    esc = _html_escape
    link = f'<a href="{url}">{esc(title)}</a>'

    lines = []
    if cancelled:
        lines.append("❌ PERUTTU:")
    lines.append(f"<b>{link}</b>")
    lines.append("")
    if date_str:
        lines.append(f"📅 {date_str}")
    if time_str:
        lines.append(f"🕒 {time_str}")
    lines.append(f"📍 {esc(city)}")
    if address:
        lines.append(esc(address))
    return "\n".join(lines)


_MONTHS_SHORT_FI = ["", "tammi", "helmi", "maalisk", "huhti", "touko", "kesä",
                     "heinä", "elo", "syys", "loka", "marras", "joulu"]


def format_demo_compact(demo: dict) -> str:
    title = demo.get("title") or "Tapahtuma"
    date = demo.get("date") or ""
    start_time = (demo.get("start_time") or "")[:5]
    city = demo.get("city") or ""
    cancelled = "❌ " if demo.get("cancelled") else ""

    date_str = ""
    if date:
        try:
            y, m, d = (int(x) for x in date.split("-"))
            date_str = f"{d}.{m}."
        except (ValueError, KeyError):
            date_str = date

    time_part = f" {start_time}" if start_time else ""
    url = demo_url(demo)
    esc = _html_escape
    link = f'<a href="{url}">{esc(title)}</a>'
    return f"{cancelled}{link} — {date_str}{time_part} · {esc(city)}"
