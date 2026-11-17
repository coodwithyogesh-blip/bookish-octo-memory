"""Local temporary media paths for Render Free; no external storage service."""
from pathlib import Path

def configured():
    return False

def is_remote(value):
    return False

def persist(local_path, object_key):
    path = Path(local_path)
    if not path.is_file():
        raise FileNotFoundError(path)
    return str(path)

def materialize(storage_uri, cache_path):
    path = Path(storage_uri)
    if not path.is_file():
        raise FileNotFoundError(path)
    return str(path)
