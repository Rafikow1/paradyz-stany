"""Baza płytek w sieciach (wszyscy producenci) – zbierana z list kategorii, bez wchodzenia w każdy produkt.

python -m utils.katalog <siec> <wynik.json> <url_listy> [<url_listy> ...]
Zapisuje: url, nazwa, marka, cena, EAN/sku, pozycja na liście (kolejność domyślna sieci).
"""
import asyncio
import json
import random
import re
import sys
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from scrapers import SCRAPERS
from utils.common import UrlCache, load_config, setup_logging

def pid(url: str, sku=None) -> str | None:
    """Stały identyfikator produktu w sieci (LM: nr ref., OBI: nr art., Castorama: EAN)."""
    for rx in (r"-(\d{6,})\.html", r"/p/(\d+)", r"/(\d{8,14})_CAPL"):
        m = re.search(rx, url or "")
        if m:
            return m.group(1)
    return str(sku) if sku else None


PROD_RE = re.compile(r"/p/\d+|_CAPL\.prd|/produkty/[^?#]*-\d{6,}\.html")


def _with_page(url: str, param: str, n: int) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    q[param] = [str(n)]
    return urlunparse(u._replace(query=urlencode(q, doseq=True)))


def _jsonld_products(blocks):
    out = []

    def walk(o):
        if isinstance(o, dict):
            if o.get("@type") == "Product":
                out.append(o)
                return
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    for b in blocks:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return out


async def read_page(s):
    await asyncio.sleep(2)
    for _ in range(6):   # listy doładowują się przy przewijaniu
        await s.page.mouse.wheel(0, 4000)
        await asyncio.sleep(0.6)
    blocks = await s.page.locator("script[type='application/ld+json']").all_text_contents()
    items = []
    for p in _jsonld_products(blocks):
        off = p.get("offers") or {}
        off = off[0] if isinstance(off, list) and off else off
        br = p.get("brand")
        br = br[0] if isinstance(br, list) and br else br
        items.append({"url": (p.get("url") or (off or {}).get("url") or "").split("?")[0], "n": p.get("name"),
                      "b": br.get("name") if isinstance(br, dict) else br,
                      "c": (off or {}).get("price") or (off or {}).get("lowPrice"),
                      "e": p.get("gtin13") or p.get("gtin") or p.get("sku"), "sku": p.get("sku")})
    for i in items:
        i["id"] = pid(i["url"], i.get("sku"))
    links = await s.page.eval_on_selector_all(
        "a[href]", "els => els.map(e => [e.href.split('?')[0].split('#')[0], (e.innerText||'').trim().slice(0,120)])")
    byid = {i["id"]: i for i in items if i["id"]}
    for h, t in links:
        if not PROD_RE.search(h):
            continue
        k = pid(h)
        if k in byid:
            byid[k]["url"] = byid[k]["url"] or h
        elif k:
            byid[k] = {"url": h, "n": t or None, "id": k}
            items.append(byid[k])
    items = [i for i in items if i.get("id")]
    ld = [i for i in items if "e" in i]          # produkty z danych listy (JSON-LD), bez "polecanych"
    items = ld if len(ld) >= 5 else items
    txt = re.sub(r"\s+", " ", await s.page.locator("body").inner_text())
    m = re.search(r"(\d[\d  ]*)\s*(?:produkt|wynik)", txt, re.I)
    total = int(re.sub(r"\D", "", m.group(1))) if m else None
    return items, total


async def crawl(key, lists, out):
    cfg = load_config()
    setup_logging()
    found = {}
    async with SCRAPERS[key](cfg["general"], cfg["markets"][key], UrlCache()) as s:
        for lst in lists:
            await s.goto(lst)
            items, total = await read_page(s)
            print(f"\n{lst}: na stronie {len(items)}, sieć podaje łącznie {total}")
            pos = 0
            for it in items:
                pos += 1
                found.setdefault(it["id"], {**it, "lista": lst, "poz": pos})
            param, page = {"leroy": "p", "obi": "page", "castorama": "page"}[key], 1
            print(f"   paginacja: {param}")
            empty = 0
            while page < 120:
                page += 1
                await asyncio.sleep(random.uniform(2, 5))
                await s.goto(_with_page(lst, param, page))
                nxt, _ = await read_page(s)
                new = [i for i in nxt if i["id"] not in found]
                for it in nxt:
                    pos += 1
                    found.setdefault(it["id"], {**it, "lista": lst, "poz": pos})
                if not new:
                    empty += 1
                    if empty >= 2:
                        break
                else:
                    empty = 0
            print(f"   zebrano z listy: stron {page}, produktów razem {len(found)}")
    json.dump(list(found.values()), open(out, "w"), ensure_ascii=False)
    brands = {}
    for v in found.values():
        brands[v.get("b") or "(brak marki w danych listy)"] = brands.get(v.get("b") or "(brak marki w danych listy)", 0) + 1
    print(f"\n=== {key}: {len(found)} produktów; z marką: {sum(1 for v in found.values() if v.get('b'))}")
    for b, n in sorted(brands.items(), key=lambda x: -x[1])[:60]:
        print(f"   {n:5d}  {b}")


if __name__ == "__main__":
    asyncio.run(crawl(sys.argv[1], sys.argv[3:], sys.argv[2]))
