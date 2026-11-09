"""Wczytywanie listy produktów (xlsx/csv) i generowanie raportu stany_magazynowe_YYYY-MM-DD.xlsx."""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from utils.common import only_digits

log = logging.getLogger("excel")

# Nazwy kolumn akceptowane na wejściu (porównanie bez polskich znaków, wielkości liter i spacji)
COLUMN_ALIASES = {
    "EAN": ["ean", "eankodkreskowy", "kodkreskowy", "gtin"],
    "Indeks_Producenta": ["indeksproducenta", "indeksproducentakodmarketu", "indeks", "kodproducenta"],
    "Nazwa_Produktu": ["nazwaproduktu", "nazwa", "produkt"],
    "Market": ["market", "siec", "sklep"],
    "JM": ["jm", "jednostka"],
    "Stan_Paradyz": ["stanmagazynowy", "stan", "stanparadyz"],
    "Uwagi": ["uwagistatus", "uwagi", "status"],
    "Data_Stanu_Paradyz": ["dataraportu", "data", "datastanu"],
}

MARKET_KEYS = {"leroy merlin": "leroy", "leroy": "leroy", "castorama": "castorama", "obi": "obi"}

REPORT_COLUMNS = [
    "EAN", "Indeks_Producenta", "Nazwa_Produktu", "Market", "Region", "Gama", "ID_Sklepu_Fizycznego / Nazwa_Miasta",
    "Stan_Magazynowy", "Stan_Sklep_m2", "Sygnal",
    "Stan_Paradyz", "JM", "Data_Stanu_Paradyz", "Uwagi_Paradyz",
    "Cena_za_m2", "Cena_za_opakowanie", "m2_w_opakowaniu",
    "Link_Do_Oferty", "Data_Odczytu", "Status", "Uwagi_Programu",
]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _pick_sheet(path: Path) -> str | int:
    sheets = pd.ExcelFile(path).sheet_names
    for name in sheets:
        if "zbiorcz" in _norm(name) or "input" in _norm(name):
            return name
    return sheets[0]


def load_products(path: str | Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Nie znaleziono pliku wejściowego: {path}")

    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(path, sheet_name=_pick_sheet(path), dtype=str)
    else:
        try:
            df = pd.read_csv(path, sep=None, engine="python", dtype=str, encoding="utf-8-sig")
        except UnicodeDecodeError:
            df = pd.read_csv(path, sep=None, engine="python", dtype=str, encoding="cp1250")

    # dopasowanie kolumn
    rename = {}
    for col in df.columns:
        n = _norm(col)
        for target, aliases in COLUMN_ALIASES.items():
            if n in aliases and target not in rename.values():
                rename[col] = target
                break
    df = df.rename(columns=rename)

    missing = [c for c in ("EAN", "Nazwa_Produktu") if c not in df.columns]
    if missing:
        raise ValueError(f"Brak wymaganych kolumn {missing}. Znalezione kolumny: {list(df.columns)}")

    products, seen = [], set()
    for _, row in df.iterrows():
        ean = only_digits(row.get("EAN", ""))
        if len(ean) < 8:
            continue
        market_raw = str(row.get("Market") or "").strip()
        market = MARKET_KEYS.get(market_raw.lower()) if market_raw else None
        key = (ean, market)
        if key in seen:
            continue
        seen.add(key)
        products.append({
            "EAN": ean,
            "Indeks_Producenta": str(row.get("Indeks_Producenta") or "").strip(),
            "Nazwa_Produktu": re.sub(r"\s+", " ", str(row.get("Nazwa_Produktu") or "")).strip(),
            "Market": market,  # None = sprawdzaj we wszystkich sieciach
            "JM": str(row.get("JM") or "").strip(),
            "Stan_Paradyz": row.get("Stan_Paradyz"),
            "Data_Stanu_Paradyz": "" if pd.isna(row.get("Data_Stanu_Paradyz")) else str(row.get("Data_Stanu_Paradyz"))[:10],
            "Uwagi": "" if pd.isna(row.get("Uwagi")) else str(row.get("Uwagi")).strip(),
        })
    log.info("Wczytano %d pozycji (EAN x market) z %s", len(products), path.name)
    return products


def write_report(rows: list[dict], output_dir: str | Path, run_date: date | None = None) -> Path:
    run_date = run_date or date.today()
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"stany_magazynowe_{run_date:%Y-%m-%d}.xlsx"

    df = pd.DataFrame(rows).reindex(columns=REPORT_COLUMNS)
    try:
        from utils.regions import region
        df["Region"] = [region(m, st) for m, st in zip(df["Market"], df["ID_Sklepu_Fizycznego / Nazwa_Miasta"])]
    except Exception:
        pass
    hide = pd.Series(False, index=df.index)
    try:
        from utils import gamy
        infos = [gamy.info(m, e, n, st) for m, e, n, st in zip(df["Market"], df["EAN"], df["Nazwa_Produktu"],
                                                               df["ID_Sklepu_Fizycznego / Nazwa_Miasta"])]
        df["Gama"] = [i["gama"] for i in infos]
        df["ID_Sklepu_Fizycznego / Nazwa_Miasta"] = [f"{st} (HUB)" if i["hub"] and "(HUB)" not in str(st) else st
                                                     for st, i in zip(df["ID_Sklepu_Fizycznego / Nazwa_Miasta"], infos)]
        hide = pd.Series([gamy.hidden_by_default(m, e, n, st, q) for m, e, n, st, q in zip(
            df["Market"], df["EAN"], df["Nazwa_Produktu"], df["ID_Sklepu_Fizycznego / Nazwa_Miasta"],
            df["Stan_Magazynowy"])], index=df.index)
    except Exception as e:
        log.warning("Gamy niedostępne: %s", e)
    df["Stan_Paradyz"] = pd.to_numeric(df["Stan_Paradyz"], errors="coerce").round(2)

    summary = (df.assign(n=1).pivot_table(index="Market", columns="Status", values="n",
                                          aggfunc="sum", fill_value=0).reset_index()
               if not df.empty else pd.DataFrame())
    problems = df[df["Status"] != "OK"]
    signals = df[(df["Sygnal"].fillna("") != "") & ~hide]   # bez nieobowiązkowych z zerem

    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="Stany", index=False)
        signals.to_excel(xw, sheet_name="Sygnaly", index=False)
        summary.to_excel(xw, sheet_name="Podsumowanie", index=False)
        problems.to_excel(xw, sheet_name="Problemy", index=False)
        for ws in xw.book.worksheets:
            _style_sheet(ws)
        _add_status_colors(xw.book["Stany"], len(df))

    import shutil
    shutil.copyfile(out, out_dir / "stany_magazynowe_AKTUALNE.xlsx")   # stała nazwa = stały link do udostępnienia
    log.info("Raport zapisany: %s (%d wierszy)", out, len(df))
    return out


def _style_sheet(ws) -> None:
    header_fill = PatternFill("solid", fgColor="1F3A5F")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    if ws.max_row > 1:
        ws.auto_filter.ref = ws.dimensions
    for i, col in enumerate(ws.iter_cols(min_row=1, max_row=min(ws.max_row, 300)), start=1):
        width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 60)


def _add_status_colors(ws, n_rows: int) -> None:
    if n_rows == 0:
        return
    status_col = get_column_letter(REPORT_COLUMNS.index("Status") + 1)
    stock_col = get_column_letter(REPORT_COLUMNS.index("Stan_Magazynowy") + 1)
    rng = f"A2:{get_column_letter(len(REPORT_COLUMNS))}{n_rows + 1}"
    red = PatternFill("solid", fgColor="F8D7DA")
    yellow = PatternFill("solid", fgColor="FFF3CD")
    green = PatternFill("solid", fgColor="D4EDDA")
    sig_col = get_column_letter(REPORT_COLUMNS.index("Sygnal") + 1)
    ws.conditional_formatting.add(rng, FormulaRule(formula=[f'LEFT(${sig_col}2,4)="Brak"'], fill=red))
    ws.conditional_formatting.add(rng, FormulaRule(formula=[f'${stock_col}2="Niedostępny"'], fill=red))
    ws.conditional_formatting.add(rng, FormulaRule(formula=[f'${status_col}2<>"OK"'], fill=yellow))
    ws.conditional_formatting.add(rng, FormulaRule(formula=[f'${stock_col}2="Dostępny"'], fill=green))
