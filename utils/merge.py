"""Scalanie wyników z równoległych zadań (jedna sieć = jedno zadanie) w jeden raport.

  python -m utils.merge <folder_z_wynikami> <folder_output>
W <folder_z_wynikami>/<sieć>/ są: raport *.xlsx i url_map.json z danej sieci.
"""
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from utils.excel_handler import write_report


GOOD = {"OK", "NIE_ZNALEZIONO"}


def keep_previous(rows: list[dict]) -> list[dict]:
    """Produkty, których tej nocy nie udało się odczytać, dostają ostatni dobry wynik (z jego datą)."""
    import io
    import os

    from utils.publish import decrypt_bytes
    prev_file = Path("docs/raport.enc")
    if not prev_file.exists() or not os.environ.get("DATA_PASSWORD"):
        return rows
    try:
        prev = pd.read_excel(io.BytesIO(decrypt_bytes(prev_file.read_bytes(), os.environ["DATA_PASSWORD"])),
                             sheet_name="Stany", dtype={"EAN": str})
    except Exception as e:
        print("Poprzedni raport nieczytelny:", e)
        return rows
    prev = prev.astype(object).where(prev.notna(), None)
    good_now = {(r.get("Market"), str(r.get("EAN"))) for r in rows if r.get("Status") in GOOD}
    seen_now = {(r.get("Market"), str(r.get("EAN"))) for r in rows}
    markets_now = {r.get("Market") for r in rows}
    kept = []
    for r in prev.to_dict("records"):
        key = (r.get("Market"), str(r.get("EAN")))
        if r.get("Status") in GOOD and key not in good_now and (key in seen_now or r.get("Market") not in markets_now):
            r["Uwagi_Programu"] = f"stan z poprzedniego odczytu ({str(r.get('Data_Odczytu'))[:10]})"
            kept.append(r)
    restored = {(r.get("Market"), str(r.get("EAN"))) for r in kept}
    rows = [r for r in rows if (r.get("Market"), str(r.get("EAN"))) not in restored] + kept
    print(f"Przywrócono z poprzedniego odczytu: {len(restored)} produktów")
    return rows


def main(src: Path, out: Path):
    rows, url_map = [], {}
    for d in sorted(p for p in src.iterdir() if p.is_dir()):
        key = d.name.replace("wynik-", "")
        reports = sorted(f for f in d.rglob("stany_magazynowe_2*.xlsx"))
        if reports:
            df = pd.read_excel(reports[-1], sheet_name="Stany", dtype={"EAN": str})
            df = df.astype(object).where(df.notna(), None)
            rows += df.to_dict("records")
            print(f"{key}: {len(df)} wierszy z {reports[-1].name}")
        else:
            print(f"{key}: BRAK raportu")
        um = next(d.rglob("url_map.json"), None)
        if um:
            part = json.loads(um.read_text(encoding="utf-8"))
            if key in part:
                url_map[key] = part[key]
    rows = keep_previous(rows)
    if not rows:
        raise SystemExit("Żadne zadanie nie zwróciło wyników.")
    write_report(rows, out, date.today())
    Path("data").mkdir(exist_ok=True)
    old = Path("dane/url_map.json")
    merged = json.loads(old.read_text(encoding="utf-8")) if old.exists() else {}
    merged.update(url_map)
    Path("data/url_map.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
