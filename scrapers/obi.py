"""OBI – stany we wszystkich sklepach.

Odkryte w diagnozie (30.10.2026):
  lista sklepów:  GET /api/disc/store/locator/country/pl        -> {"stores": [{storeNumber, name, city, isActive}]}
  stany:          GET /api/pdp/v1/stock/{nr_art}?storeIds=056,060,...  -> [{storeId, availableQuantity}]
Strona sama pyta o 10 sklepów naraz – robimy tak samo.
"""
import re

from scrapers.base import BaseScraper, LayoutChanged, ProductInfo, ScraperError, StockResult

STORES_URL = "https://www.obi.pl/api/disc/store/locator/country/pl"
STOCK_URL = "https://www.obi.pl/api/pdp/v1/stock/{article}?storeIds={ids}"
CHUNK = 10


class ObiScraper(BaseScraper):
    key = "obi"
    _stores: list[dict] | None = None

    def product_id_from(self, url: str, info: ProductInfo):
        m = re.search(r"/p/(\d+)", url)
        return m.group(1) if m else super().product_id_from(url, info)

    async def load_stores(self) -> list[dict]:
        if self._stores is None:
            data = await self.browser_fetch_json(STORES_URL)
            stores = [s for s in data.get("stores", []) if s.get("isActive", True) and s.get("storeNumber")]
            if not stores:
                raise LayoutChanged("OBI: pusta lista sklepów")
            self._stores = stores
            self.log.info("OBI: %d sklepów", len(stores))
        return self._stores

    async def all_stores_stock(self, product, info, url):
        article = self.product_id_from(url, info)
        if not article:
            raise LayoutChanged("OBI: brak numeru artykułu w adresie")
        try:
            stores = await self.load_stores()
        except ScraperError as e:
            self.log.warning("OBI: nie udało się pobrać listy sklepów: %s", e)
            return []
        by_nr = {s["storeNumber"]: s for s in stores}
        nrs = list(by_nr)
        qty: dict[str, float] = {}
        for i in range(0, len(nrs), CHUNK):
            part = nrs[i:i + CHUNK]
            data = await self.browser_fetch_json(STOCK_URL.format(article=article, ids=",".join(part)))
            for row in data if isinstance(data, list) else []:
                qty[str(row.get("storeId"))] = row.get("availableQuantity")
        out = []
        for nr in nrs:
            s = by_nr[nr]
            label = f"{nr} / {s.get('name') or s.get('city')}"
            q = qty.get(nr)
            out.append(StockResult(label, q if q is not None else None, info.price, url,
                                   status="OK" if q is not None else "BRAK_DANYCH"))
        return out
