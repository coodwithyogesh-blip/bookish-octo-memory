import re


def parse_reply_command(text):
    """Parse /reply COMMENT_ID followed by the creator's exact reply text."""
    match = re.match(r"^/reply(?:@\\w+)?\\s+(\\S+)\\s+(.+)$", (text or "").strip(), re.S)
    if not match:
        raise ValueError("Use: /reply COMMENT_ID your reply text")
    comment_id, reply = match.group(1).strip(), match.group(2).strip()
    if not comment_id or not reply or len(reply) > 10000:
        raise ValueError("Comment ID aur 1-10000 character reply required hai.")
    return comment_id, reply


def parse_comment_action(value):
    match = re.fullmatch(r"comment:(approve|skip):([A-Za-z0-9_-]+)", str(value or ""))
    return (match.group(1), match.group(2)) if match else None


def comment_notification(comment_id, video_title, author, comment, suggestion):
    suggestion_text = suggestion or "AI suggestion unavailable — use /reply COMMENT_ID your reply"
    message = (
        f"💬 New YouTube comment · {video_title or 'Video'}\\n"
        f"From: {author or 'Viewer'}\\n"
        f"Comment: {comment}\\n\\n"
        f"AI reply suggestion: {suggestion_text}\\n\\n"
        f"Review ID: {comment_id}\\n"
        f"Approve with /approve {comment_id}, skip with /skip {comment_id}, "
        f"or send /reply {comment_id} your text."
    )
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Approve AI reply", "callback_data": f"comment:approve:{comment_id}"},
        {"text": "⏭ Skip", "callback_data": f"comment:skip:{comment_id}"},
    ]]}
    return message, keyboard
