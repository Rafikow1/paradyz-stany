"""Historia stanów i szacunek sprzedaży / dostaw.

Każdy odczyt dopisuje stany sklepów do zaszyfrowanej historii (historia.enc). Zapisujemy tylko
ZMIANY stanu (sklep, produkt, czas, nowy stan), więc plik rośnie powoli.

Szacunek:
  spadek stanu między odczytami  -> sprzedaż (opakowania)
  wzrost stanu między odczytami  -> dostawa
To szacunek: jeśli między dwoma odczytami przyszła dostawa i była sprzedaż, widać tylko różnicę.
Zwroty i korekty inwentaryzacyjne też wyglądają jak dostawa/sprzedaż.

Użycie:
  python -m utils.historia update <historia.enc> <folder_z_raportami_xlsx> [<sprzedaz.enc>]
  python -m utils.historia rebuild <historia.enc> <sprzedaz.enc>
"""
from __future__ import annotations

import gzip
import json
import os
import sys
from bisect import insort
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

GLITCH_HOURS = 36      # stan spada i wraca DOKŁADNIE do poprzedniej wartości w tym czasie = chwilowy błąd sieci
DAILY_DAYS = 400       # dzienne sumy trzymamy ok. roku (do filtrów "ostatnie 7/30 dni" i własnych zakresów)
SKIP_NOTES = ("poprzedniego odczytu", "0 we wszystkich sklepach")


def _pw() -> str:
    return os.environ["DATA_PASSWORD"]


def load(path: Path) -> dict:
    if not path.exists():
        return {"v": 1, "series": {}, "meta": {}, "reads": []}
    from utils.publish import decrypt_bytes
    return json.loads(gzip.decompress(decrypt_bytes(path.read_bytes(), _pw())))   # błąd = przerwij, nie nadpisuj


def save(h: dict, path: Path) -> None:
    from utils.publish import encrypt_bytes
    data = gzip.compress(json.dumps(h, ensure_ascii=False, separators=(",", ":")).encode())
    path.write_bytes(encrypt_bytes(data, _pw()))


def _store_key(label: str) -> str:
    return str(label).replace(" (HUB)", "").strip()


def _rows(report: Path):
    import pandas as pd
    try:
        df = pd.read_excel(report, sheet_name="Stany", dtype={"EAN": str})
    except Exception as e:   # uszkodzony / niepełny plik – pomijamy
        print(f"Pominięto {report.name}: {e}")
        return []
    df = df[df["Status"] == "OK"]
    out = []
    for r in df.to_dict("records"):
        q, ts = r.get("Stan_Magazynowy"), str(r.get("Data_Odczytu") or "")[:16]
        store = r.get("ID_Sklepu_Fizycznego / Nazwa_Miasta")
        try:
            q = float(q)
        except (TypeError, ValueError):
            continue
        if q != q or len(ts) < 16 or not store or store in ("Sklep internetowy", "—"):
            continue
        note = str(r.get("Uwagi_Programu") or "")
        if any(s in note for s in SKIP_NOTES):
            continue
        pk = r.get("m2_w_opakowaniu")
        out.append({"m": r.get("Market"), "e": str(r.get("EAN")).strip(), "s": str(store), "q": int(q) if q.is_integer() else q,
                    "t": ts, "n": r.get("Nazwa_Produktu"), "pk": pk if isinstance(pk, (int, float)) and pk == pk else None})
    return out


def add_reports(h: dict, reports: list[Path]) -> int:
    rows = [r for p in reports for r in _rows(p)]
    # odczyt, w którym produkt ma 0 we WSZYSTKICH sklepach sieci, a wcześniej miał towar = błąd strony sieci
    batch = defaultdict(list)
    for r in rows:
        batch[(r["m"], r["e"], r["t"][:13])].append(r)
    series, meta = h["series"], h["meta"]
    added, reads = 0, set(h.get("reads", []))
    for (m, e, _), rs in batch.items():
        if len(rs) >= 5 and not any(r["q"] for r in rs):
            had = sum(1 for r in rs if (series.get(f"{m}|{e}|{_store_key(r['s'])}") or [[0, 0]])[-1][1])
            if had >= 5:
                print(f"Pominięto podejrzany odczyt: {m} {e} {rs[0]['t']} (0 we wszystkich sklepach)")
                continue
        for r in rs:
            k = f"{m}|{e}|{_store_key(r['s'])}"
            pts = series.setdefault(k, [])
            if any(p[0] == r["t"] for p in pts):
                continue
            insort(pts, [r["t"], r["q"]])
            added += 1
            mt = meta.setdefault(k, {})
            if r["t"] >= mt.get("t", ""):
                mt.update(t=r["t"], lab=r["s"], n=r["n"])
                if r["pk"]:
                    mt["pk"] = r["pk"]
            reads.add(r["t"][:13])
    for k, pts in series.items():   # tylko zmiany stanu
        if len(pts) > 1:
            series[k] = [p for i, p in enumerate(pts) if i == 0 or p[1] != pts[i - 1][1]]
    h["reads"] = sorted(reads)
    return added


def _clean(pts: list) -> list:
    """Usuwa chwilowe skoki: a -> b -> a w ciągu GLITCH_HOURS (np. sieć na chwilę pokazała 0)."""
    pts = list(pts)
    changed = True
    while changed and len(pts) >= 3:
        changed = False
        for i in range(1, len(pts) - 1):
            a, b, c = pts[i - 1], pts[i], pts[i + 1]
            if a[1] == c[1] and b[1] != a[1]:
                dt = datetime.fromisoformat(c[0]) - datetime.fromisoformat(a[0])
                if dt <= timedelta(hours=GLITCH_HOURS):
                    pts = pts[:i] + pts[i + 2:]
                    changed = True
                    break
    return pts


def sales(h: dict) -> dict:
    """Sumy sprzedaży/dostaw: miesięcznie (cała historia) i dziennie (ostatni rok) – dla strony."""
    from utils.regions import region
    items, idx = [], {}
    months, days = defaultdict(dict), defaultdict(dict)
    first = {}
    cutoff = (datetime.now() - timedelta(days=DAILY_DAYS)).strftime("%Y-%m-%d")
    for k, pts in h["series"].items():
        m, e, s = k.split("|", 2)
        if pts:
            first[m] = min(first.get(m, pts[0][0]), pts[0][0])
        pts = _clean(pts)
        for prev, cur in zip(pts, pts[1:]):
            d = cur[1] - prev[1]
            if not d:
                continue
            if k not in idx:
                mt = h["meta"].get(k, {})
                idx[k] = len(items)
                items.append([m, mt.get("lab", s), e, mt.get("n", ""), region(m, mt.get("lab", s)) or "", mt.get("pk")])
            i, day = idx[k], cur[0][:10]
            for bucket, key in ((months, day[:7]), (days, day if day >= cutoff else None)):
                if key is None:
                    continue
                v = bucket[key].setdefault(i, [0, 0])
                v[0 if d < 0 else 1] += abs(d)
    pack = lambda b: {k: [[i, round(v[0], 2), round(v[1], 2)] for i, v in sorted(rows.items())] for k, rows in sorted(b.items())}
    reads = h.get("reads", [])
    return {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "od": {m: t[:10] for m, t in first.items()},
            "reads": len(reads), "items": items, "months": pack(months), "days": pack(days)}


def save_sales(h: dict, path: Path) -> None:
    from utils.publish import encrypt_bytes
    path.write_bytes(encrypt_bytes(gzip.compress(json.dumps(sales(h), ensure_ascii=False, separators=(",", ":")).encode()), _pw()))


if __name__ == "__main__":
    cmd = sys.argv[1]
    hp = Path(sys.argv[2])
    h = load(hp)
    if cmd == "update":
        reports = sorted(p for p in Path(sys.argv[3]).rglob("stany_magazynowe_2*.xlsx"))
        n = add_reports(h, reports)
        save(h, hp)
        print(f"Historia: +{n} zmian z {len(reports)} raportów; kluczy {len(h['series'])}, odczytów {len(h['reads'])}")
        if len(sys.argv) > 4:
            save_sales(h, Path(sys.argv[4]))
    elif cmd == "rebuild":
        save_sales(h, Path(sys.argv[3]))
    else:
        raise SystemExit(__doc__)
