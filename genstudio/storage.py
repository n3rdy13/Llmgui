"""Config, API keys, model catalog and generation history."""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

APP_NAME = "GenStudio"
APP_DIR = Path(os.environ.get("APPDATA") or (Path.home() / ".config")) / APP_NAME
CONFIG_PATH = APP_DIR / "config.json"
HISTORY_PATH = APP_DIR / "history.json"
USER_MODELS_PATH = APP_DIR / "models.json"
DEFAULT_OUTPUT = Path.home() / "Pictures" / APP_NAME

try:
    import keyring  # Windows Credential Manager on Windows
    keyring.get_keyring()
    HAVE_KEYRING = True
except Exception:  # pragma: no cover
    keyring = None
    HAVE_KEYRING = False


def resource_path(name: str) -> Path:
    """Path to a bundled file, works both from source and from a PyInstaller exe."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    p = base / name
    if not p.exists():
        p = base / "genstudio" / name
    return p


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- config

class Config:
    def __init__(self):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        self.data = _read_json(CONFIG_PATH, {})
        self.data.setdefault("output_dir", str(DEFAULT_OUTPUT))
        self.data.setdefault("appearance", "Dark")
        self.data.setdefault("last_models", {})

    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, k, v):
        self.data[k] = v
        self.save()

    def save(self):
        _write_json(CONFIG_PATH, self.data)

    # API keys: Credential Manager when available, else config file.
    def get_key(self, provider: str) -> str:
        if HAVE_KEYRING:
            try:
                v = keyring.get_password(APP_NAME, provider)
                if v:
                    return v
            except Exception:
                pass
        return self.data.get("keys", {}).get(provider, "")

    def set_key(self, provider: str, key: str) -> str:
        key = key.strip()
        if HAVE_KEYRING:
            try:
                if key:
                    keyring.set_password(APP_NAME, provider, key)
                else:
                    try:
                        keyring.delete_password(APP_NAME, provider)
                    except Exception:
                        pass
                self.data.get("keys", {}).pop(provider, None)
                self.save()
                return "Saved to Windows Credential Manager."
            except Exception:
                pass
        self.data.setdefault("keys", {})[provider] = key
        self.save()
        return "Saved to config file (Credential Manager unavailable)."


# --------------------------------------------------------------------------- models

def load_models() -> list[dict]:
    """User-editable copy lives in %APPDATA%/GenStudio/models.json."""
    if not USER_MODELS_PATH.exists():
        reset_models()
    data = _read_json(USER_MODELS_PATH, None)
    if not data or "models" not in data:
        data = _read_json(resource_path("models.json"), {"models": []})
    return data["models"]


def reset_models() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(resource_path("models.json"), USER_MODELS_PATH)


# --------------------------------------------------------------------------- history

class History:
    def __init__(self):
        self.lock = threading.Lock()
        self.items: list[dict] = _read_json(HISTORY_PATH, [])

    def add(self, entry: dict) -> None:
        with self.lock:
            entry.setdefault("time", time.time())
            self.items.append(entry)
            _write_json(HISTORY_PATH, self.items)

    def remove(self, entry_id: str) -> None:
        with self.lock:
            self.items = [e for e in self.items if e.get("id") != entry_id]
            _write_json(HISTORY_PATH, self.items)

    def total_spend(self) -> float:
        return sum(e.get("est_usd", 0) or 0 for e in self.items if e.get("status") == "done")
