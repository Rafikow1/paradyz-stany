"""Przypisanie sklepów do regionów sprzedaży (E1–E7). Mapa jest zaszyfrowana: dane/regiony.json.enc."""
import json
import os
from pathlib import Path

_MAP = None


def load(path: str = "dane/regiony.json.enc") -> dict:
    global _MAP
    if _MAP is None:
        _MAP = {}
        p = Path(path)
        if p.exists() and os.environ.get("DATA_PASSWORD"):
            from utils.publish import decrypt_bytes
            _MAP = json.loads(decrypt_bytes(p.read_bytes(), os.environ["DATA_PASSWORD"]))
    return _MAP


def region(market, store) -> str:
    if not market or not store or store in ("Sklep internetowy", "—"):
        return ""
    m = load().get(market, {})
    store = str(store)
    key = store.split(" / ")[0].strip() if " / " in store else store.strip()
    return m.get(key) or m.get(store.strip()) or "Bez regionu"
