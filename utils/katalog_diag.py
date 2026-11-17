"""Rozpoznanie list kategorii płytek w sieciach (tylko struktura stron, bez danych firmowych)."""
import asyncio
import re
import sys

from scrapers import SCRAPERS
from utils.common import UrlCache, load_config, setup_logging

KW = re.compile(r"p[lł]ytk|glazur|gres|terakot|mozaik|klinkier", re.I)


async def main(key):
    cfg = load_config()
    setup_logging()
    mc = cfg["markets"][key]
    async with SCRAPERS[key](cfg["general"], mc, UrlCache()) as s:
        api = []
        s.page.on("response", lambda r: api.append(r.url) if "json" in (r.headers.get("content-type") or "") else None)
        await s.goto(mc["base_url"])
        await asyncio.sleep(4)
        links = await s.page.eval_on_selector_all("a[href]", "els => els.map(e => [e.href, (e.innerText||'').trim().slice(0,60)])")
        tile = {}
        for h, t in links:
            if KW.search(h) or KW.search(t):
                tile.setdefault(h.split("?")[0], t)
        print(f"\n##### {key}: linki z płytkami ({len(tile)})")
        for h, t in list(tile.items())[:60]:
            print("  ", h, "|", t)
        extra = sys.argv[2:] or []
        cands = extra or [h for h in tile if re.search(r"p[lł]ytk", h, re.I)][:3]
        for url in cands[:4]:
            api.clear()
            print(f"\n##### {key}: KATEGORIA {url}")
            try:
                await s.goto(url)
            except Exception as e:
                print("BŁĄD", e)
                continue
            await asyncio.sleep(5)
            txt = re.sub(r"\s+", " ", await s.page.locator("body").inner_text())
            for m in re.finditer(r"(\d[\d  .]*)\s*(produkt|wynik|artyku)", txt, re.I):
                print("   LICZBA:", txt[max(0, m.start() - 40): m.end() + 20])
            prod = await s.page.eval_on_selector_all("a[href]", "els => [...new Set(els.map(e => e.href))]")
            pl = [p for p in prod if re.search(r"/produkty/|/p/|_CAPL|\.prd", p)]
            print("   linków do produktów:", len(pl), pl[:5])
            ld = await s.page.locator("script[type='application/ld+json']").all_text_contents()
            print("   JSON-LD typy:", re.findall(r'"@type"\s*:\s*"(\w+)"', " ".join(ld))[:15])
            print("   filtry/producent:", [txt[m.start()-20:m.end()+120] for m in re.finditer(r"Producent|Marka", txt)][:3])
            nxt = [p for p in prod if re.search(r"page=|strona|p=2|/2/", p)][:5]
            print("   paginacja:", nxt)
            sub = sorted({p.split("?")[0].split("#")[0] for p in prod if re.search(r"plytk|glazur|gres|/c/|\.cat", p, re.I) and "/p/" not in p and ".prd" not in p and not p.endswith(".html")})
            print("   PODKATEGORIE:", len(sub))
            for x in sub[:80]:
                print("      ", x)
            print("   JSON API:", [u for u in api if not re.search(r"google|facebook|analytics|doubleclick|cookie|consent", u)][:25])


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
