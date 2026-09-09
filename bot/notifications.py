from __future__ import annotations

from urllib.parse import quote


def demo_url(demo_id: str) -> str:
    return f"https://mielenosoitukset.fi/demonstration/{demo_id}"


_MONTHS_FI = ["", "tammikuuta", "helmikuuta", "maaliskuuta", "huhtikuuta", "toukokuuta", "kesäkuuta",
              "heinäkuuta", "elokuuta", "syyskuuta", "lokakuuta", "marraskuuta", "joulukuuta"]


def format_demo(demo: dict) -> str:
    """Render a friendly Finnish notification message for a demo."""
    title = demo.get("title") or demo.get("title_fi") or "Tapahtuma"
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

    lines = []
    if cancelled:
        lines.append("❌ PERUTTU:")
    lines.append(f"*{_escape(title)}*")
    lines.append("")
    if date_str:
        lines.append(f"📅 {date_str}")
    if time_str:
        lines.append(f"🕒 {time_str}")
    lines.append(f"📍 {_escape(city)}")
    if address:
        lines.append(_escape(address))
    lines.append("")
    lines.append(f"🔗 https://mielenosoitukset.fi/demonstration/{quote(str(demo.get('_id', '')))}")
    return "\n".join(lines)


def _escape(text: str) -> str:
    return (text or "").replace("_", "\\_").replace("*", "\\*").replace("[", "\\[").replace("`", "\\`")


_MONTHS_SHORT_FI = ["", "tammi", "helmi", "maalisk", "huhti", "touko", "kesä",
                     "heinä", "elo", "syys", "loka", "marras", "joulu"]


def format_demo_compact(demo: dict) -> str:
    """One-line compact format for listing."""
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
    return f"{cancelled}*{_escape(title)}* — {date_str}{time_part} · {_escape(city)}"
