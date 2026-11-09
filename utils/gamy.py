"""Gamy produktów (obowiązkowe / nieobowiązkowe), magazyn centralny LM i huby Castoramy.

Plik zaszyfrowany: dane/gamy.json.enc. Zasada na stronie: pozycje nieobowiązkowe
(gama Castoramy powyżej A/A-, towar LM z magazynu centralnego) z zerowym stanem są
domyślnie ukryte – pokazujemy je tylko, gdy sklep (np. hub) faktycznie ma towar.
"""
import json
import os
from pathlib import Path

_G = None


def load(path: str = "dane/gamy.json.enc") -> dict:
    global _G
    if _G is None:
        _G = {}
        p = Path(path)
        if p.exists() and os.environ.get("DATA_PASSWORD"):
            from utils.publish import decrypt_bytes
            _G = json.loads(decrypt_bytes(p.read_bytes(), os.environ["DATA_PASSWORD"]))
    return _G


def info(market, ean, name="", store="") -> dict:
    """{'gama': 'B' | 'Magazyn centralny' | '', 'nieobow': bool, 'hub': bool}"""
    g = load().get(str(market or ""), {})
    ean, name = str(ean or "").strip(), str(name or "").upper()
    out = {"gama": "", "nieobow": False, "hub": False}
    if market == "Leroy Merlin":
        if ean in g.get("magazyn_centralny", []) or any(n in name for n in g.get("magazyn_centralny_nazwy", [])):
            out.update(gama="Magazyn centralny", nieobow=True)
    elif market == "Castorama":
        gm = g.get("gamy", {}).get(ean, "")
        if gm:
            out.update(gama="Gama " + gm, nieobow=gm not in g.get("obowiazkowe_gamy", ["A", "A-"]))
        sid = str(store or "").split(" / ")[0].strip()
        out["hub"] = sid in g.get("huby", [])
    return out


def hidden_by_default(market, ean, name, store, stock) -> bool:
    """Nieobowiązkowa pozycja bez towaru w sklepie – domyślnie nie pokazujemy."""
    if not info(market, ean, name, store)["nieobow"]:
        return False
    try:
        return not float(stock) > 0
    except (TypeError, ValueError):
        return True
