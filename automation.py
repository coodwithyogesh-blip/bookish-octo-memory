from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import re


def parse_schedule(value, timezone_name="Asia/Kolkata", now=None):
    """Parse a local YYYY-MM-DD HH:MM schedule and return an aware UTC datetime."""
    try:
        local = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M")
        zone = ZoneInfo(timezone_name)
    except (ValueError, KeyError) as exc:
        raise ValueError("Time format YYYY-MM-DD HH:MM hona chahiye; timezone valid hona chahiye.") from exc
    scheduled = local.replace(tzinfo=zone)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if scheduled.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
        raise ValueError("Schedule future date/time ka hona chahiye.")
    return scheduled.astimezone(timezone.utc)


def comment_is_suitable(text):
    """Conservative first-pass filter: skip abuse, links, personal data and risky topics."""
    value = (text or "").strip()
    if not value or len(value) > 1000:
        return False
    if re.search(r"https?://|www\.|\b\w+@\w+\.\w+\b|\+?\d[\d\s-]{8,}\d", value, re.I):
        return False
    blocked = (
        "suicide", "self harm", "kill myself", "medical advice", "diagnose",
        "investment advice", "bank account", "password", "otp", "address",
        "hate", "terrorist",
    )
    lowered = value.lower()
    return not any(term in lowered for term in blocked)
