"""Supabase Storage adapter for files that must survive Render Free restarts."""
import os
from pathlib import Path
from urllib.parse import quote, unquote

import requests


def configured():
    return bool(os.getenv("SUPABASE_URL", "").strip() and
                os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip())


def _settings():
    base = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    bucket = os.getenv("SUPABASE_STORAGE_BUCKET", "youtube-media").strip()
    if not base or not key:
        raise RuntimeError("Supabase Storage not configured: set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY.")
    return base, key, bucket


def _headers(key, content_type="application/octet-stream"):
    return {"Authorization": f"Bearer {key}", "apikey": key,
            "Content-Type": content_type, "x-upsert": "true"}


def upload_file(local_path, object_key):
    base, key, bucket = _settings()
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("Supabase Free Storage file limit is 50 MB. This file is too large.")
    safe_key = "/".join(quote(part, safe="") for part in object_key.strip("/").split("/"))
    url = f"{base}/storage/v1/object/{quote(bucket, safe='')}/{safe_key}"
    content_type = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else (
        "image/png" if path.suffix.lower() == ".png" else "video/mp4"
    )
    with path.open("rb") as stream:
        response = requests.post(url, headers=_headers(key, content_type), data=stream, timeout=(30, 180))
    response.raise_for_status()
    return f"supabase://{bucket}/{object_key.strip('/')}"


def is_remote(value):
    return str(value or "").startswith("supabase://")


def download_file(storage_uri, destination):
    base, key, default_bucket = _settings()
    if not is_remote(storage_uri):
        path = Path(storage_uri)
        if not path.is_file():
            raise FileNotFoundError(path)
        return str(path)
    rest = storage_uri[len("supabase://"):]
    bucket, _, object_key = rest.partition("/")
    if not bucket or not object_key:
        raise ValueError("Invalid Supabase storage URI")
    safe_key = "/".join(quote(unquote(part), safe="") for part in object_key.split("/"))
    url = f"{base}/storage/v1/object/{quote(bucket, safe='')}/{safe_key}"
    response = requests.get(url, headers={"Authorization": f"Bearer {key}", "apikey": key},
                            stream=True, timeout=(30, 180))
    response.raise_for_status()
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as output:
        for chunk in response.iter_content(1024 * 1024):
            if chunk:
                output.write(chunk)
    return str(target)


def materialize(storage_uri, cache_path):
    target = Path(cache_path)
    if is_remote(storage_uri):
        if not target.is_file():
            download_file(storage_uri, target)
        return str(target)
    return download_file(storage_uri, target)


def persist(local_path, object_key):
    if not configured():
        raise RuntimeError("Supabase Storage not configured. Configure Supabase before uploading media.")
    return upload_file(local_path, object_key)
