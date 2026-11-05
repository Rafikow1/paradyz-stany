"""Scalanie wyników z równoległych zadań (jedna sieć = jedno zadanie) w jeden raport.

  python -m utils.merge <folder_z_wynikami> <folder_output>
W <folder_z_wynikami>/<sieć>/ są: raport *.xlsx i url_map.json z danej sieci.
"""
import json
import sys
from datetime import date, timedelta
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
    # "nie znaleziono" dziś, a wcześniej był odczyt – to raczej kaprys wyszukiwarki, zostawiamy stary stan
    good_now = {(r.get("Market"), str(r.get("EAN"))) for r in rows if r.get("Status") == "OK"}
    # Podejrzany odczyt: dziś 0 we WSZYSTKICH sklepach, a niedawno (≤2 dni) towar był w wielu sklepach.
    # Tyle towaru nie znika w jedną noc – to chwilowy błąd strony sieci, więc pokazujemy poprzedni stan.
    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    now_q, prev_q = {}, {}
    for r in rows:
        if r.get("Status") == "OK" and num(r.get("Stan_Magazynowy")) is not None:
            now_q.setdefault((r.get("Market"), str(r.get("EAN"))), []).append(num(r.get("Stan_Magazynowy")))
    recent = (date.today() - timedelta(days=2)).isoformat()
    for r in prev.to_dict("records"):
        if r.get("Status") == "OK" and num(r.get("Stan_Magazynowy")) is not None \
                and str(r.get("Data_Odczytu") or "")[:10] >= recent:
            prev_q.setdefault((r.get("Market"), str(r.get("EAN"))), []).append(num(r.get("Stan_Magazynowy")))
    suspicious = set()
    for k, qs in now_q.items():
        p = prev_q.get(k, [])
        if len(qs) >= 5 and not any(qs) and sum(1 for q in p if q > 0) >= 5 and sum(p) >= 50:
            suspicious.add(k)
    if suspicious:
        print(f"Podejrzane odczyty (0 wszędzie, wcześniej towar): {len(suspicious)} produktów -> poprzedni stan")
        good_now -= suspicious
    seen_now = {(r.get("Market"), str(r.get("EAN"))) for r in rows}
    markets_now = {r.get("Market") for r in rows}
    kept = []
    for r in prev.to_dict("records"):
        key = (r.get("Market"), str(r.get("EAN")))
        fresh = str(r.get("Data_Odczytu") or "")[:10] >= (date.today() - timedelta(days=14)).isoformat()
        if r.get("Status") == "OK" and key not in good_now and fresh:   # stan starszy niż 14 dni już nie wraca
            r["Uwagi_Programu"] = f"stan z poprzedniego odczytu ({str(r.get('Data_Odczytu'))[:10]})" + (
                " – dziś sieć pokazała 0 we wszystkich sklepach (podejrzany odczyt)" if key in suspicious else "")
            kept.append(r)
    restored = {(r.get("Market"), str(r.get("EAN"))) for r in kept}
    rows = [r for r in rows if (r.get("Market"), str(r.get("EAN"))) not in restored] + kept
    print(f"Przywrócono z poprzedniego odczytu: {len(restored)} produktów")
    return rows


def main(src: Path, out: Path):
    rows, url_map = [], {}
    # najpierw pierwsza runda (wynik-*), potem druga (wynik2-*) – druga nadpisuje produkty, które odczytała
    dirs = sorted((p for p in src.iterdir() if p.is_dir()), key=lambda p: (p.name.startswith("wynik2-"), p.name))
    for d in dirs:
        key = d.name.split("-")[1]
        reports = sorted(f for f in d.rglob("stany_magazynowe_2*.xlsx"))
        if reports:
            df = pd.read_excel(reports[-1], sheet_name="Stany", dtype={"EAN": str})
            df = df.astype(object).where(df.notna(), None)
            new = df.to_dict("records")
            if d.name.startswith("wynik2-"):
                fixed = {(r.get("Market"), str(r.get("EAN"))) for r in new if r.get("Status") == "OK"}
                rows = [r for r in rows if (r.get("Market"), str(r.get("EAN"))) not in fixed]
                new = [r for r in new if (r.get("Market"), str(r.get("EAN"))) in fixed]
                print(f"{d.name}: druga runda poprawiła {len(fixed)} produktów")
            rows += new
            print(f"{d.name}: {len(new)} wierszy z {reports[-1].name}")
        else:
            print(f"{key}: BRAK raportu")
        um = next(d.rglob("url_map.json"), None)
        if um:
            part = json.loads(um.read_text(encoding="utf-8")).get(key, {})
            dst = url_map.setdefault(key, {})
            for ean, e in part.items():   # kilka części tej samej sieci – wygrywa najnowszy wpis
                if ean not in dst or str(e.get("updated_at", "")) > str(dst[ean].get("updated_at", "")):
                    dst[ean] = e
    rows = keep_previous(rows)
    if not rows:
        raise SystemExit("Żadne zadanie nie zwróciło wyników.")
    write_report(rows, out, date.today())
    Path("data").mkdir(exist_ok=True)
    old = Path("dane/url_map.json")
    merged = json.loads(old.read_text(encoding="utf-8")) if old.exists() else {}
    for k, v in url_map.items():
        merged.setdefault(k, {}).update(v)
    Path("data/url_map.json").write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
