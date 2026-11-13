"""Diagnoza cen: co dokładnie pokazuje strona produktu (cena główna, wszystkie ceny za m², opakowanie).

python -m utils.diag_ceny leroy 3276007824339 3276007612868 ...
Wypisuje tylko publiczne dane ze stron sklepów (ceny), bez stanów firmowych.
"""
import asyncio
import json
import re
import sys

from scrapers import SCRAPERS
from utils.common import UrlCache, load_config, setup_logging


async def main(key, eans):
    cfg = load_config()
    cache = UrlCache()
    log = setup_logging()
    async with SCRAPERS[key](cfg["general"], cfg["markets"][key], cache) as s:
        for ean in eans:
            e = cache.get(key, ean) or {}
            url = e.get("url") or (await s.direct_url(ean) if hasattr(s, "direct_url") else None)
            print("\n" + "=" * 100 + f"\n{key} {ean} {e.get('name')}\n{url}")
            if not url:
                continue
            try:
                await s.goto(url)
                info = await s.read_product()
            except Exception as ex:
                print("BŁĄD", ex)
                continue
            offers = (info.raw_jsonld or {}).get("offers")
            print("JSON-LD price:", info.price, "| offers:", json.dumps(offers, ensure_ascii=False)[:400])
            print("WYNIK: cena opak.", info.price, "| cena m2", info.price_m2, "| m2 w opak.", info.m2_per_pack)
            t = re.sub(r"(\d) ?, ?(\d)", r"\1,\2", re.sub(r"\s+", " ", info.page_text or ""))
            ps = f"{info.price:.2f}".replace(".", ",") if info.price else "@@"
            for m in list(re.finditer(re.escape(ps), t))[:6]:
                print("   ...", t[max(0, m.start() - 150): m.end() + 80])
            for m in list(re.finditer(r"opak", t, re.I))[:0]:
                print("   OPAK:", t[max(0, m.start() - 50): m.end() + 60])
            await asyncio.sleep(8)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
