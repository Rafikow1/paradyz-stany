"""Wspólne narzędzia: konfiguracja, logowanie, cache URL-i, pomocnicze funkcje JSON."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"


# --------------------------------------------------------------------- config
def load_config(path: str | Path = ROOT / "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# -------------------------------------------------------------------- logging
def setup_logging(level: int = logging.INFO) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"scraper_{datetime.now():%Y-%m-%d}.log"
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(ch)
    return logging.getLogger("main")


# ------------------------------------------------------------------ url cache
class UrlCache:
    """Pamięć odnalezionych linków: data/url_map.json  ->  {market: {ean: {...}}}.

    Dzięki temu Etap 1 (Discovery) wykonuje się tylko raz na produkt.
    Plik jest czytelny – można go poprawić ręcznie (np. wkleić właściwy link).
    """

    def __init__(self, path: Path = DATA_DIR / "url_map.json"):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, dict[str, dict]] = {}
        if self.path.exists():
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)

    def get(self, market: str, ean: str) -> dict | None:
        return self.data.get(market, {}).get(ean)

    def set(self, market: str, ean: str, **fields: Any) -> None:
        entry = self.data.setdefault(market, {}).setdefault(ean, {})
        entry.update(fields)
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self.save()

    def drop(self, market: str, ean: str) -> None:
        self.data.get(market, {}).pop(ean, None)
        self.save()

    def should_skip_not_found(self, market: str, ean: str, days: int) -> bool:
        e = self.get(market, ean)
        if not e or e.get("status") != "NOT_FOUND":
            return False
        try:
            return datetime.fromisoformat(e["updated_at"]) > datetime.now() - timedelta(days=days)
        except (KeyError, ValueError):
            return False

    def save(self) -> None:
        # zapis atomowy – przerwanie programu nie uszkodzi pliku
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


# ----------------------------------------------------------------- json utils
def walk(obj: Any) -> Iterable[tuple[str, Any]]:
    """Przechodzi rekurencyjnie po JSON-ie i zwraca pary (klucz, wartość)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from walk(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def find_first_number(obj: Any, keys: list[str]) -> float | None:
    keys_l = [k.lower() for k in keys]
    for wanted in keys_l:  # kolejność kluczy = priorytet
        for k, v in walk(obj):
            if k.lower() == wanted:
                num = to_number(v)
                if num is not None:
                    return num
    return None


def to_number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.replace("\xa0", "").replace(" ", "").replace(",", ".")
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if m:
            return float(m.group())
    return None


def only_digits(s: Any) -> str:
    s = str(s).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return re.sub(r"\D", "", s)
