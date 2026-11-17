import json
import re


def parse_json_response(raw):
    text = (raw or "").strip()
    text = re.sub(r"^\s*```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```\s*$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI response did not contain a JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("AI response must be a JSON object")
    return data


def clean_metadata(data):
    title = str(data.get("title", "")).strip()
    description = str(data.get("description", "")).strip()
    if not title:
        raise ValueError("Video title is required")
    if len(title) > 100:
        title = title[:97].rstrip() + "..."
    tags = []
    for item in data.get("tags", []):
        tag = str(item).strip().lstrip("#")
        if tag and tag.lower() not in {x.lower() for x in tags}:
            tags.append(tag[:100])
    hashtags = []
    for item in data.get("hashtags", []):
        tag = "#" + str(item).strip().lstrip("#").replace(" ", "")
        if tag != "#" and tag.lower() not in {x.lower() for x in hashtags}:
            hashtags.append(tag[:60])
    return {
        "title": title,
        "description": description[:5000],
        "tags": tags[:30],
        "hashtags": hashtags[:15],
        "category_id": str(data.get("category_id", "22")),
        "language": str(data.get("language", "hi")),
    }


def build_youtube_snippet(metadata):
    """Build a YouTube API snippet and append generated hashtags once."""
    clean = clean_metadata(metadata)
    description = clean["description"].strip()
    present = {tag.lower() for tag in re.findall(r"#[\w]+", description)}
    missing = [tag for tag in clean["hashtags"] if tag.lower() not in present]
    if missing:
        description = (description + "\n\n" if description else "") + " ".join(missing)
    return {
        "title": clean["title"],
        "description": description[:5000],
        "tags": clean["tags"],
        "categoryId": clean["category_id"],
    }
