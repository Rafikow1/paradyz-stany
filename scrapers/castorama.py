"""Castorama – stany we wszystkich sklepach.

Odkryte w diagnozie (30.10.2026):
  lista sklepów:  GET https://api.kingfisher.com/v1/mobile/stores/CAPL?nearLatLong=LAT,LON&page[size]=N
                  (wymaga nagłówków, które strona wysyła sama – przechwytujemy je)
  stan w sklepie: GET /castopl-browse-mfe/api/fulfilment-options?compositeOfferId={EAN}&storeId={id}&postalCode=..
                  -> data[0].attributes.inStore.quantity  (opakowania)

Uwaga: produkty marki własnej Kingfisher (EAN 5063022…/5036581…, np. DOBRA, MARJO, RAVA, TIXXIS)
są sprzedawane pod nazwami Castoramy – szukamy po EAN.
"""
import asyncio
from urllib.parse import quote

from playwright.async_api import Error as PWError
from playwright.async_api import Request
from playwright.async_api import TimeoutError as PWTimeout

from scrapers.base import BaseScraper, ScraperError, StockResult

STORES_API = "https://api.kingfisher.com/v1/mobile/stores/CAPL?nearLatLong={lat}%2C{lon}&page%5Bsize%5D={size}"
FULFIL_API = ("https://www.castorama.pl/castopl-browse-mfe/api/fulfilment-options"
              "?compositeOfferId={ean}&storeId={ids}&postalCode={pc}")
# punkty, z których szukamy sklepów (centrum + 4 regiony), żeby objąć całą Polskę
SEARCH_POINTS = [(52.07, 19.48), (54.35, 18.65), (50.06, 19.94), (52.23, 21.01), (51.11, 17.03), (53.43, 14.55)]
SKIP_HEADERS = {"host", "content-length", "user-agent", "cookie", "referer", "origin", "accept-encoding",
                "connection", "accept-language"}


class CastoramaScraper(BaseScraper):
    key = "castorama"
    _stores: dict[str, dict] | None = None
    _store_headers: dict | None = None
    _multi: bool | None = None

    async def direct_url(self, ean: str) -> str | None:
        # karty Castoramy kończą się na /{EAN}_CAPL.prd – próbujemy wejść wprost, z weryfikacją EAN
        return f"https://www.castorama.pl/p/{ean}_CAPL.prd"

    async def __aenter__(self):
        await super().__aenter__()
        self.page.on("request", self._sniff_headers)
        return self

    def _sniff_headers(self, req: Request):
        if "api.kingfisher.com/v1/mobile/stores" in req.url and self._store_headers is None:
            self._store_headers = {k: v for k, v in req.headers.items()
                                   if k.lower() not in SKIP_HEADERS and not k.lower().startswith("sec-")}

    async def _trigger_store_search(self):
        """Wpisuje kod pocztowy na stronie, żeby strona sama zapytała o listę sklepów."""
        loc = self.page.locator("input[placeholder*='kod pocztowy']")
        for i in range(await loc.count()):
            el = loc.nth(i)
            try:
                if await el.is_visible(timeout=1000):
                    await el.click(timeout=3000)
                    await el.fill("")
                    await el.type("00-001", delay=90)
                    await el.press("Enter")
                    await asyncio.sleep(4)
                    return
            except (PWTimeout, PWError):
                continue

    async def load_stores(self) -> dict[str, dict]:
        if self._stores is not None:
            return self._stores
        if self._store_headers is None:
            await self._trigger_store_search()
        stores: dict[str, dict] = {}
        for lat, lon in SEARCH_POINTS:
            try:
                data = await self.browser_fetch_json(STORES_API.format(lat=lat, lon=lon, size=100),
                                                     headers=self._store_headers)
            except ScraperError as e:
                self.log.warning("Castorama: lista sklepów niedostępna (%s)", e)
                continue
            for d in data.get("data", []):
                st = d.get("attributes", {}).get("store", {})
                pc = _find_postcode(st) or "00-001"
                stores[str(d.get("id"))] = {"name": st.get("name", d.get("id")), "pc": pc}
        # zapasowo: sklepy, które strona sama pobrała (np. najbliższe 10)
        if not stores:
            for c in self._captured:
                if "mobile/stores/CAPL" in c["url"] and isinstance(c["json"], dict):
                    for d in c["json"].get("data", []):
                        st = d.get("attributes", {}).get("store", {})
                        stores[str(d.get("id"))] = {"name": st.get("name", d.get("id")),
                                                    "pc": _find_postcode(st) or "00-001"}
            if stores:
                self.log.warning("Castorama: używam tylko %d sklepów załadowanych przez stronę", len(stores))
        # zapasowo: sklepy z config.yaml
        for s in self.m.get("stores", []) or []:
            if s.get("id"):
                stores.setdefault(str(s["id"]), {"name": s.get("name", s["id"]), "pc": s.get("postal_code", "00-001")})
        if stores:
            self.log.info("Castorama: %d sklepów", len(stores))
        else:
            self.log.warning("Castorama: nie udało się pobrać listy sklepów (nagłówki: %s)",
                             "przechwycone" if self._store_headers else "brak")
            await self.debug_dump("lista_sklepow")
        self._stores = stores
        return stores

    async def _fulfil(self, ean: str, ids: list[str], pc: str) -> dict[str, tuple]:
        data = await self.browser_fetch_json(FULFIL_API.format(ean=ean, ids=quote(",".join(ids)), pc=pc))
        out = {}
        for row in data.get("data", []):
            sid = str(row.get("relationships", {}).get("store", {}).get("data", {}).get("id", ""))
            inst = row.get("attributes", {}).get("inStore", {}) or {}
            out[sid] = (inst.get("quantity"), inst.get("availability"))
        return out

    async def all_stores_stock(self, product, info, url):
        stores = await self.load_stores()
        if not stores:
            return []
        ean = product["EAN"]
        ids = list(stores)

        # jednorazowy test: czy API przyjmuje kilka sklepów naraz?
        if self._multi is None and len(ids) >= 2:
            try:
                self._multi = len(await self._fulfil(ean, ids[:2], stores[ids[0]]["pc"])) >= 2
            except ScraperError:
                self._multi = False
            self.log.info("Castorama: zapytanie o kilka sklepów naraz: %s", "TAK" if self._multi else "NIE")

        results: dict[str, tuple] = {}
        if self._multi:
            for i in range(0, len(ids), 10):
                results.update(await self._fulfil(ean, ids[i:i + 10], stores[ids[i]]["pc"]))
        else:
            par = self.g.get("castorama_parallel", 6)
            for i in range(0, len(ids), par):
                part = ids[i:i + par]
                urls = [FULFIL_API.format(ean=ean, ids=quote(sid), pc=stores[sid]["pc"]) for sid in part]
                for sid, data in zip(part, await self.browser_fetch_json_many(urls)):
                    if not data:
                        results[sid] = (None, "błąd zapytania")
                        continue
                    for row in data.get("data", []):
                        inst = row.get("attributes", {}).get("inStore", {}) or {}
                        results[sid] = (inst.get("quantity"), inst.get("availability"))

        out = []
        for sid in ids:
            q, av = results.get(sid, (None, None))
            label = f"{sid} / {stores[sid]['name']}"
            if q is None and av == "NotStockedInStore":
                out.append(StockResult(label, "Poza asortymentem", info.price, url,
                                       note="sklep nie ma tego produktu w ofercie"))
            else:
                out.append(StockResult(label, q, info.price, url,
                                       status="OK" if q is not None else "BRAK_DANYCH",
                                       note="" if q is not None else (av or "")))
        return out


def _find_postcode(obj) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("postalcode", "postcode", "zip", "zipcode") and isinstance(v, str):
                return v
            r = _find_postcode(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_postcode(v)
            if r:
                return r
    return None
