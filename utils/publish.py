"""Publikacja wyników na stronę dla przedstawicieli + szyfrowanie.

Repozytorium jest publiczne (darmowe minuty GitHub i darmowa strona), więc WSZYSTKIE dane firmowe
są szyfrowane hasłem (AES-256-GCM, klucz z PBKDF2-SHA256). Strona odszyfrowuje je dopiero
w przeglądarce, po wpisaniu hasła. Bez hasła pliki to losowe bajty.

Użycie:
  python -m utils.publish encrypt  <plik_wejsciowy> <plik.enc>      # hasło z env DATA_PASSWORD
  python -m utils.publish decrypt  <plik.enc> <plik_wyjsciowy>
  python -m utils.publish site     <raport.xlsx> <folder_docs>
"""
from __future__ import annotations

import base64
import gzip
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ITER = 250_000


def _key(password: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITER).derive(password.encode())


def encrypt_bytes(data: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(password, salt)).encrypt(iv, data, None)
    b = lambda x: base64.b64encode(x).decode()
    return json.dumps({"v": 1, "iter": ITER, "salt": b(salt), "iv": b(iv), "ct": b(ct)}).encode()


def decrypt_bytes(blob: bytes, password: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    d = json.loads(blob)
    u = base64.b64decode
    return AESGCM(_key(password, u(d["salt"]))).decrypt(u(d["iv"]), u(d["ct"]), None)


def _password() -> str:
    pw = os.environ.get("DATA_PASSWORD", "")
    if len(pw) < 8:
        raise SystemExit("Brak hasła: ustaw zmienną DATA_PASSWORD (min. 8 znaków).")
    return pw


SITE_COLS = [
    ("Market", "m"), ("ID_Sklepu_Fizycznego / Nazwa_Miasta", "s"), ("EAN", "e"), ("Indeks_Producenta", "i"),
    ("Nazwa_Produktu", "n"), ("Stan_Magazynowy", "q"), ("Stan_Sklep_m2", "qm"), ("Sygnal", "sg"),
    ("Stan_Paradyz", "p"), ("JM", "j"), ("Data_Stanu_Paradyz", "dp"), ("m2_w_opakowaniu", "pk"), ("Cena_za_m2", "c"), ("Link_Do_Oferty", "u"), ("Status", "st"), ("Data_Odczytu", "d"),
]


def build_site(report: Path, docs: Path, password: str) -> None:
    import pandas as pd
    df = pd.read_excel(report, sheet_name="Stany", dtype={"EAN": str})
    df = df[df["Status"].isin(["OK", "NIE_ZNALEZIONO"]) | df["Status"].isna()]
    rows = []
    for r in df.to_dict("records"):
        row = {}
        for col, k in SITE_COLS:
            v = r.get(col)
            if v is None or (isinstance(v, float) and v != v):
                continue
            if isinstance(v, float) and v.is_integer():
                v = int(v)
            row[k] = v
        rows.append(row)
    reads = df.get("Data_Odczytu")
    generated = str(reads.dropna().max()) if reads is not None and reads.notna().any() else datetime.now().strftime("%Y-%m-%d %H:%M")
    payload = {"generated": generated[:16], "source": report.name, "rows": rows}
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "dane.enc").write_bytes(
        encrypt_bytes(gzip.compress(json.dumps(payload, ensure_ascii=False).encode()), password))
    (docs / "raport.enc").write_bytes(encrypt_bytes(report.read_bytes(), password))
    (docs / "stan.json").write_text(json.dumps({"generated": payload["generated"], "rows": len(rows)}),
                                    encoding="utf-8")   # jawne: tylko data i liczba wierszy
    print(f"Strona: {len(rows)} wierszy -> {docs}")


if __name__ == "__main__":
    cmd, a, b = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    if cmd == "encrypt":
        b.write_bytes(encrypt_bytes(a.read_bytes(), _password()))
    elif cmd == "decrypt":
        b.write_bytes(decrypt_bytes(a.read_bytes(), _password()))
    elif cmd == "site":
        build_site(a, b, _password())
    else:
        raise SystemExit(__doc__)
