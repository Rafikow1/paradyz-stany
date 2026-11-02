"""Monitor stanów magazynowych Ceramika Paradyż w Leroy Merlin, Castoramie i OBI.

Przykłady:
    python main.py --input input.xlsx                 # pełny cykl (Discovery + stany)
    python main.py --input input.xlsx --tylko-discovery
    python main.py --input input.xlsx --limit 5 --snoop --rynki leroy   # test + podsłuch API
    python main.py --input input.xlsx --wszystkie-markety              # każdy EAN w każdej sieci
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import random
import time
from datetime import date

from scrapers import SCRAPERS
from scrapers.base import ScraperError, StockResult
from utils.common import UrlCache, load_config, setup_logging
from utils.excel_handler import load_products, write_report


def parse_args():
    p = argparse.ArgumentParser(description="Scraper stanów magazynowych Ceramika Paradyż")
    p.add_argument("--input", default="input.xlsx", help="plik xlsx/csv z kolumnami EAN, Indeks_Producenta, Nazwa_Produktu")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--rynki", nargs="*", choices=list(SCRAPERS), help="ogranicz do wybranych sieci")
    p.add_argument("--wszystkie-markety", action="store_true",
                   help="sprawdzaj każdy EAN we wszystkich sieciach (domyślnie: tylko w sieci z kolumny Market)")
    p.add_argument("--tylko-discovery", action="store_true", help="tylko Etap 1 – mapowanie URL-i")
    p.add_argument("--od-nowa", action="store_true", help="wyszukaj URL-e ponownie, ignorując cache")
    p.add_argument("--limit", type=int, default=0, help="maks. liczba produktów na sieć (test)")
    p.add_argument("--snoop", action="store_true", help="zapisuj odpowiedzi XHR/Fetch do data/snoop/")
    p.add_argument("--headless", action="store_true", help="ukryj okno przeglądarki")
    p.add_argument("--limit-czasu", type=int, default=0,
                   help="po ilu minutach przestać zaczynać nowe produkty (zapisuje to, co zdążył)")
    p.add_argument("--dokoncz", action="store_true",
                   help="ponów tylko produkty, które w ostatnim raporcie się nie udały, i scal wyniki")
    p.add_argument("--pakuj", action="store_true", help="spakuj raport + dzienniki do wyniki_do_wyslania.zip")
    return p.parse_args()


def signal(product: dict, r: StockResult, sig: dict, stock_m2: float | None = None) -> str:
    """Ostrzeżenie dla przedstawiciela: porównanie stanu w sklepie ze stanem Paradyża."""
    if r.status != "OK":
        return ""
    try:
        ours = float(product.get("Stan_Paradyz"))
    except (TypeError, ValueError):
        ours = None
    have = ours is not None and ours >= sig.get("paradyz_min_m2", 100)
    jm = product.get("JM") or ""
    ours_txt = f"Paradyż ma {ours:,.0f} {jm}".replace(",", " ") if ours is not None else ""
    if r.stock == "Niedostępny":
        return f"Brak online – {ours_txt}" if have else "Brak online"
    if isinstance(r.stock, (int, float)):
        if r.stock == 0:
            return f"Brak w sklepie – {ours_txt}" if have else "Brak w sklepie i mało u Paradyża"
        low = (stock_m2 < sig.get("low_store_m2", 15)) if stock_m2 is not None \
            else (r.stock < sig.get("low_store_packs", 5))
        if low:
            return f"Niski stan w sklepie – {ours_txt}" if have else "Niski stan w sklepie"
    return ""


def make_row(product: dict, market_name: str, r: StockResult, sig: dict | None = None) -> dict:
    stock_m2 = (round(r.stock * r.m2_per_pack, 2)
                if isinstance(r.stock, (int, float)) and r.m2_per_pack else None)
    return {
        "EAN": product["EAN"],
        "Indeks_Producenta": product["Indeks_Producenta"],
        "Nazwa_Produktu": product["Nazwa_Produktu"],
        "Market": market_name,
        "ID_Sklepu_Fizycznego / Nazwa_Miasta": r.location,
        "Stan_Magazynowy": r.stock,
        "Stan_Sklep_m2": stock_m2,
        "Sygnal": signal(product, r, sig or {}, stock_m2),
        "Stan_Paradyz": product.get("Stan_Paradyz"),
        "JM": product.get("JM"),
        "Data_Stanu_Paradyz": product.get("Data_Stanu_Paradyz"),
        "Uwagi_Paradyz": product.get("Uwagi"),
        "Cena_za_m2": r.price_m2,
        "Cena_za_opakowanie": r.price,
        "m2_w_opakowaniu": r.m2_per_pack,
        "Link_Do_Oferty": r.url,
        "Data_Odczytu": r.read_at,
        "Status": r.status,
        "Uwagi_Programu": r.note,
    }


DEADLINE: float | None = None


def checkpoint(rows: list[dict], cfg: dict, log):
    """Zapis częściowego raportu – nagłe przerwanie nie kasuje kilku godzin pracy."""
    try:
        write_report(list(rows), cfg["general"].get("output_dir", "output"), date.today())
    except Exception as e:
        log.warning("Zapis częściowy nie powiódł się: %s", e)


async def run_market(key: str, products: list[dict], cfg: dict, cache: UrlCache, args, rows: list[dict], log):
    mcfg = cfg["markets"][key]
    sig = cfg.get("signals", {})
    name = mcfg["name"]
    log.info("=== %s: %d produktów ===", name, len(products))
    try:
        async with SCRAPERS[key](cfg["general"], mcfg, cache) as scraper:
            for i, product in enumerate(products, 1):
                ean = product["EAN"]
                if DEADLINE and time.monotonic() > DEADLINE:
                    for rest in products[i - 1:]:
                        rows.append(make_row(rest, name, StockResult("—", status="NIE_SPRAWDZONO",
                                                                     note="zabrakło czasu – sprawdzony zostanie później")))
                    scraper.log.warning("Koniec czasu – pominięto %d produktów", len(products) - i + 1)
                    break
                if i % 20 == 0:
                    checkpoint(rows, cfg, log)
                pause = mcfg.get("product_pause")
                if pause and i > 1:
                    await asyncio.sleep(random.uniform(*pause))
                try:
                    if args.tylko_discovery:
                        url = await scraper.discover(product, force=args.od_nowa)
                        rows.append(make_row(product, name, StockResult("—", None, None, url, note="discovery")))
                    else:
                        for r in await scraper.check(product, force_discover=args.od_nowa):
                            rows.append(make_row(product, name, r, sig))
                    scraper.log.info("[%d/%d] %s OK", i, len(products), ean)
                except ScraperError as e:
                    scraper.log.warning("[%d/%d] %s %s: %s", i, len(products), ean, e.status, e)
                    rows.append(make_row(product, name, StockResult("—", status=e.status, note=str(e),
                                                                    url=(cache.get(key, ean) or {}).get("url", ""))))
                except Exception as e:  # nieprzewidziany błąd – nie przerywamy całego cyklu
                    scraper.log.exception("[%d/%d] %s nieoczekiwany błąd", i, len(products), ean)
                    rows.append(make_row(product, name, StockResult("—", status="BŁĄD", note=repr(e)[:300])))
    except Exception as e:
        log.exception("Sieć %s przerwana: %s", name, e)
        rows.append({"Market": name, "Status": "BŁĄD_SIECI", "Uwagi_Programu": f"Przerwano: {e!r}"[:300]})


async def main_async(args, log):
    global DEADLINE
    if args.limit_czasu:
        DEADLINE = time.monotonic() + args.limit_czasu * 60
    cfg = load_config(args.config)
    if args.snoop:
        cfg["general"]["snoop"] = True
    if args.headless:
        cfg["general"]["headless"] = True

    products = load_products(args.input)
    cache = UrlCache()
    markets = args.rynki or [k for k, m in cfg["markets"].items() if m.get("enabled", True)]

    # kolejka zadań per sieć
    queues: dict[str, list[dict]] = {k: [] for k in markets}
    for p in products:
        targets = markets if (args.wszystkie_markety or not p["Market"]) else [p["Market"]]
        for k in targets:
            if k in queues and not any(x["EAN"] == p["EAN"] for x in queues[k]):
                queues[k].append(p)
    # codzienne przesunięcie kolejki – przy braku czasu nie pomijamy zawsze tych samych produktów
    if not args.limit:
        for k, q in queues.items():
            if q:
                off = (date.today().toordinal() * 37) % len(q)
                queues[k] = q[off:] + q[:off]
    if args.limit:
        queues = {k: v[: args.limit] for k, v in queues.items()}

    old_rows: list[dict] = []
    if args.dokoncz:
        old_rows, todo = load_failed(cfg)
        queues = {k: [p for p in q if (cfg["markets"][k]["name"], p["EAN"]) in todo] for k, q in queues.items()}
        log.info("DOKOŃCZ: do ponowienia %s", {k: len(v) for k, v in queues.items()})

    rows: list[dict] = []
    try:
        # sieci równolegle (każda we własnej przeglądarce), produkty w ramach sieci – po kolei
        await asyncio.gather(*(run_market(k, q, cfg, cache, args, rows, log) for k, q in queues.items() if q))
    finally:
        # raport powstaje nawet po Ctrl+C lub awarii
        path = None
        if old_rows:
            redone = {(r.get("Market"), str(r.get("EAN"))) for r in rows if r.get("Status") not in FAILED}
            rows = [r for r in old_rows if (r.get("Market"), str(r.get("EAN"))) not in redone] + rows
        if rows:
            path = write_report(rows, cfg["general"].get("output_dir", "output"), date.today())
            log.info("Gotowe: %s", path)
        else:
            log.warning("Brak wyników do zapisania.")
        if args.pakuj:
            pack_for_claude(path, log)


FAILED = {"ZABLOKOWANY", "ZMIANA_UKŁADU_STRONY", "BŁĄD", "BŁĄD_SIECI", "NIE_SPRAWDZONO"}


def load_failed(cfg) -> tuple[list[dict], set]:
    """Wczytuje ostatni raport; zwraca jego wiersze i zbiór (Market, EAN) do ponowienia."""
    import glob
    import os

    import pandas as pd
    out_dir = cfg["general"].get("output_dir", "output")
    files = sorted(f for f in glob.glob(os.path.join(out_dir, "stany_magazynowe_*.xlsx")) if "AKTUALNE" not in f)
    if not files:
        raise SystemExit("Brak poprzedniego raportu w folderze output – najpierw uruchom pełny odczyt.")
    df = pd.read_excel(files[-1], sheet_name="Stany", dtype={"EAN": str})
    df = df.astype(object).where(df.notna(), None)
    rows = df.to_dict("records")
    todo = {(r["Market"], str(r["EAN"])) for r in rows if r.get("Status") in FAILED}
    return rows, todo


def pack_for_claude(report, log):
    """Jedna paczka do wysłania Claude: raport + dzienniki + zrzuty z problemów."""
    import zipfile
    from utils.common import DATA_DIR, LOG_DIR, ROOT
    out = ROOT / "wyniki_do_wyslania.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if report:
            z.write(report, f"raport/{report.name}")
        for f in LOG_DIR.glob("*.log"):
            z.write(f, f"logi/{f.name}")
        for f in (DATA_DIR / "debug").rglob("*"):
            if f.is_file():
                z.write(f, f"debug/{f.relative_to(DATA_DIR / 'debug')}")
    log.info("Paczka dla Claude: %s", out)


def main():
    args = parse_args()
    log = setup_logging()
    try:
        asyncio.run(main_async(args, log))
    except KeyboardInterrupt:
        log.warning("Przerwano przez użytkownika.")
        sys.exit(1)


if __name__ == "__main__":
    main()
