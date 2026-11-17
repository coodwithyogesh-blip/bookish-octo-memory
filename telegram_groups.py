def parse_telegram_id(value):
    """Normalize a Telegram numeric user or group ID."""
    value = str(value or "").strip()
    if not value or not value.lstrip("-").isdigit():
        raise ValueError("Telegram ID must be numeric")
    return value


def is_authorized_user(user_id, owner_id, team_ids):
    """Only the configured owner and explicitly approved team IDs may act."""
    user_id = str(user_id or "").strip()
    owner_id = str(owner_id or "").strip()
    allowed = {str(item).strip() for item in (team_ids or [])}
    return bool(user_id) and (user_id == owner_id or user_id in allowed)


def is_allowed_group(chat_id, configured_group_id):
    """Require an exact match with the one group bound by the owner."""
    configured_group_id = str(configured_group_id or "").strip()
    return bool(configured_group_id) and str(chat_id or "").strip() == configured_group_id
