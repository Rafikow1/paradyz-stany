"""Wspólna logika dla wszystkich sieci: przeglądarka, opóźnienia, blokady, Discovery, odczyt stanów.

Pliki leroy.py / castorama.py / obi.py dziedziczą po BaseScraper i nadpisują tylko to,
co jest specyficzne dla danej sieci.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from playwright.async_api import Error as PWError
from playwright.async_api import Page, Response, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

from utils.common import DATA_DIR, UrlCache, find_first_number


# ------------------------------------------------------------------ wyjątki
class ScraperError(Exception):
    status = "BŁĄD"


class ProductNotFound(ScraperError):
    status = "NIE_ZNALEZIONO"


class ProductDiscontinued(ScraperError):
    status = "WYCOFANY"


class LayoutChanged(ScraperError):
    status = "ZMIANA_UKŁADU_STRONY"


class Blocked(ScraperError):
    status = "ZABLOKOWANY"


class BurnedIP(Blocked):
    """Sklep blokuje ten serwer od początku – przerywamy, produkty dostanie druga runda."""


BLOCK_MARKERS = ("just a moment", "access denied", "attention required", "captcha",
                 "pardon our interruption", "request unsuccessful", "are you a robot", "robot")

COOKIE_BUTTONS = (
    "#onetrust-accept-btn-handler",
    "button#didomi-notice-agree-button",
    "button:has-text('Akceptuj wszystkie')",
    "button:has-text('Zaakceptuj wszystkie')",
    "button:has-text('Akceptuję')",
    "button:has-text('Zgadzam się')",
    "button:has-text('Zaakceptuj')",
    "button:has-text('Akceptuj')",
)


@dataclass
class StockResult:
    location: str                 # "Sklep internetowy" albo "ID / Miasto"
    stock: Any = None             # liczba opakowań lub "Dostępny"/"Niedostępny"/"Brak danych"
    price: float | None = None    # cena za opakowanie (jak w danych sklepu)
    url: str = ""
    status: str = "OK"
    note: str = ""
    read_at: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    price_m2: float | None = None
    m2_per_pack: float | None = None


@dataclass
class ProductInfo:
    """To, co udało się odczytać z karty produktu."""
    name: str | None = None
    sku: str | None = None
    gtin: str | None = None
    price: float | None = None
    availability: str | None = None   # np. "InStock" / "OutOfStock"
    raw_jsonld: dict | None = None
    price_m2: float | None = None
    page_text: str = ""

    @property
    def m2_per_pack(self) -> float | None:
        if self.price and self.price_m2 and self.price_m2 > 0:
            v = round(self.price / self.price_m2, 2)
            return v if 0.05 < v < 20 else None
        return None


class BaseScraper:
    key = "base"

    def __init__(self, general: dict, market_cfg: dict, cache: UrlCache):
        self.g = general
        self.m = market_cfg
        self.cache = cache
        self.log = logging.getLogger(self.key)
        self._pw = None
        self.ctx = None
        self.page: Page | None = None
        self._captured: list[dict] = []
        self._capture_tasks: set[asyncio.Task] = set()
        self._cookies_done = False
        self._blocks = 0          # ile blokad z rzędu – kolejne oczekiwania są dłuższe
        self._any_ok = False      # czy ten serwer odczytał już choć jeden produkt

    # ================================================================ browser
    async def __aenter__(self):
        self._pw = await async_playwright().start()
        profile = DATA_DIR / "profiles" / self.key   # trwały profil = ciasteczka zostają między uruchomieniami
        profile.mkdir(parents=True, exist_ok=True)
        opts = dict(
            user_data_dir=str(profile),
            headless=self.g.get("headless", False),
            user_agent=self.g.get("user_agent") or None,
            locale="pl-PL",
            timezone_id="Europe/Warsaw",
            viewport={"width": 1366, "height": 860},
            extra_http_headers={"Accept-Language": self.g.get("accept_language", "pl-PL,pl;q=0.9")},
            proxy=self._pick_proxy(),
            args=["--disable-blink-features=AutomationControlled"],
        )
        channel = self.g.get("browser_channel") or None
        try:
            self.ctx = await self._pw.chromium.launch_persistent_context(channel=channel, **opts)
        except PWError:
            if not channel:
                raise
            self.log.warning("Nie udało się uruchomić '%s' – używam wbudowanego Chromium", channel)
            self.ctx = await self._pw.chromium.launch_persistent_context(**opts)
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()
        self.page.set_default_timeout(self.g.get("page_timeout_ms", 45000))
        self.page.on("response", self._on_response)
        return self

    async def __aexit__(self, *exc):
        try:
            if self.ctx:
                await self.ctx.close()
        finally:
            if self._pw:
                await self._pw.stop()

    def _pick_proxy(self) -> dict | None:
        proxies = self.g.get("proxies") or []
        if not proxies:
            return None
        p = urlparse(random.choice(proxies))
        cfg = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
        if p.username:
            cfg.update(username=p.username, password=p.password or "")
        self.log.info("Proxy: %s", cfg["server"])
        return cfg

    # ============================================================== networking
    async def polite_delay(self):
        await asyncio.sleep(random.uniform(self.g.get("delay_min", 2), self.g.get("delay_max", 5)))

    def _on_response(self, resp: Response):
        ctype = resp.headers.get("content-type", "")
        if resp.request.resource_type in ("xhr", "fetch") and ("json" in ctype or self.g.get("snoop")):
            t = asyncio.ensure_future(self._capture(resp))
            self._capture_tasks.add(t)
            t.add_done_callback(self._capture_tasks.discard)

    async def _capture(self, resp: Response):
        try:
            text = await resp.text()
            try:
                body = json.loads(text)
            except json.JSONDecodeError:
                body = text[:5000]
            self._captured.append({"url": resp.url, "status": resp.status, "method": resp.request.method,
                                   "post_data": resp.request.post_data, "json": body})
        except Exception:
            pass

    async def _flush_captures(self):
        if self._capture_tasks:
            await asyncio.wait(list(self._capture_tasks), timeout=5)

    async def goto(self, url: str) -> int:
        """Otwiera stronę z obsługą blokad i ponowień. Zwraca kod HTTP."""
        retries = self.g.get("max_retries", 2)
        for attempt in range(retries + 1):
            await self.polite_delay()
            try:
                resp = await self.page.goto(url, wait_until="domcontentloaded")
                try:
                    await self.page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass
                status = resp.status if resp else 0
                await self._accept_cookies()
                if await self._is_blocked(status):
                    waits = self.g.get("block_wait_seconds", [60, 300, 900])
                    waits = waits if isinstance(waits, list) else [waits]
                    wait = waits[min(self._blocks, len(waits) - 1)]
                    self._blocks += 1
                    if not self._any_ok and self._blocks >= self.g.get("burned_ip_blocks", 3):
                        # serwer zablokowany od samego początku – czekanie nie pomoże, lepiej oddać pracę innemu serwerowi
                        raise BurnedIP("Adres tego serwera jest zablokowany przez sklep")
                    self.log.warning("Wykryto blokadę/captcha (%s). Czekam %s (blokada nr %d z rzędu)%s",
                                     status, f"{wait // 60} min" if wait >= 120 else f"{wait} s", self._blocks,
                                     " – możesz rozwiązać captcha w oknie przeglądarki" if not self.g.get("headless") else "")
                    await asyncio.sleep(wait)
                    if not await self._is_blocked(0) and attempt == 0 and self._blocks == 1:
                        # pierwsze "sprawdzenie przeglądarki" zwykle mija samo – próbujemy dalej na tej stronie
                        # (licznik NIE jest zerowany – kolejna blokada da już dłuższą przerwę)
                        return 200
                    continue
                self._blocks = 0
                return status
            except (PWTimeout, PWError) as e:
                self.log.warning("Błąd ładowania %s: %s (próba %d/%d)", url, str(e)[:120], attempt + 1, retries + 1)
        raise Blocked(f"Nie udało się otworzyć {url} po {retries + 1} próbach")

    async def _is_blocked(self, status: int) -> bool:
        if status in (403, 429):
            return True
        try:
            title = (await self.page.title()).lower()
        except PWError:
            return False
        return any(m in title for m in BLOCK_MARKERS)

    async def _accept_cookies(self):
        if self._cookies_done:
            return
        for sel in COOKIE_BUTTONS:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click(timeout=2000)
                    self._cookies_done = True
                    self.log.debug("Zaakceptowano cookies (%s)", sel)
                    return
            except (PWTimeout, PWError):
                continue

    async def api_delay(self):
        await asyncio.sleep(random.uniform(self.g.get("api_delay_min", 0.6), self.g.get("api_delay_max", 1.5)))

    async def browser_fetch_json_many(self, urls: list[str]) -> list[Any]:
        """Kilka zapytań naraz z wnętrza przeglądarki (jak strona ładująca kilka rzeczy jednocześnie).
        Zwraca listę: JSON albo None (błąd danego zapytania)."""
        await self.api_delay()
        try:
            res = await self.page.evaluate(
                """async (us) => Promise.all(us.map(async u => {
                    try { const r = await fetch(u, {credentials: 'include', headers: {'Accept': 'application/json'}});
                          return {status: r.status, body: await r.text()}; }
                    catch(e) { return {status: 0, body: ''}; } }))""", urls)
        except PWError as e:
            raise ScraperError(f"Zapytania nie powiodły się: {str(e)[:120]}") from e
        if any(r["status"] in (403, 429) for r in res):
            raise Blocked("Serwer zwrócił 403/429 przy zapytaniach zbiorczych")
        out = []
        for r in res:
            try:
                out.append(json.loads(r["body"]) if r["status"] == 200 else None)
            except json.JSONDecodeError:
                out.append(None)
        return out

    async def browser_fetch_text(self, url: str) -> str:
        """Jak browser_fetch_json, ale zwraca surowy tekst (np. fragment HTML)."""
        await self.api_delay()
        try:
            res = await self.page.evaluate(
                """async (u) => { const r = await fetch(u, {credentials: 'include'});
                                  return {status: r.status, body: await r.text()}; }""", url)
        except PWError as e:
            raise ScraperError(f"Zapytanie nie powiodło się ({url[:80]}): {str(e)[:120]}") from e
        if res["status"] in (403, 429):
            raise Blocked(f"Serwer zwrócił {res['status']}")
        if res["status"] >= 400:
            raise ScraperError(f"Serwer zwrócił {res['status']}: {url[:100]}")
        return res["body"]

    async def browser_fetch_json(self, url: str, headers: dict | None = None, delay: bool = True) -> Any:
        """Zapytanie do wewnętrznego API wykonane Z WNĘTRZA przeglądarki (fetch).
        Ten sam odcisk TLS, ciasteczka i nagłówki co prawdziwy użytkownik – omija większość filtrów
        lepiej niż requests/curl_cffi."""
        if delay:
            await self.api_delay()
        try:
            res = await self.page.evaluate(
                """async ([u, h]) => {
                    const r = await fetch(u, {credentials: u.startsWith(location.origin) ? 'include' : 'omit',
                                              headers: Object.assign({'Accept': 'application/json'}, h || {})});
                    return {status: r.status, body: await r.text()};
                }""", [url, headers or {}])
        except PWError as e:
            raise ScraperError(f"Zapytanie nie powiodło się ({url[:80]}): {str(e)[:120]}") from e
        if res["status"] in (403, 429):
            raise Blocked(f"API zwróciło {res['status']}")
        if res["status"] >= 400:
            raise ScraperError(f"API zwróciło {res['status']}: {url}")
        try:
            return json.loads(res["body"])
        except json.JSONDecodeError as e:
            raise LayoutChanged(f"API nie zwróciło JSON-a ({url})") from e

    # ============================================================ page parsing
    async def read_product(self) -> ProductInfo:
        """Czyta dane z JSON-LD (schema.org/Product) – najbardziej stabilne źródło, bo sklepy
        utrzymują je dla Google. Fallback: meta tagi."""
        info = ProductInfo()
        try:   # strony renderowane w przeglądarce – dane produktu dochodzą z opóźnieniem
            await self.page.wait_for_function(
                """() => [...document.querySelectorAll("script[type='application/ld+json']")]
                         .some(s => /"Product"/.test(s.textContent))""", timeout=15000)
        except (PWTimeout, PWError):
            pass
        blocks = await self.page.locator("script[type='application/ld+json']").all_text_contents()
        product = find_jsonld_product(blocks)
        if product:
            info.raw_jsonld = product
            info.name = product.get("name")
            info.sku = str(product.get("sku") or product.get("productID") or "") or None
            info.gtin = str(product.get("gtin13") or product.get("gtin") or product.get("gtin8") or "") or None
            info.price, info.availability = parse_offers(product.get("offers"))
        if info.price is None:
            for sel in ("meta[property='product:price:amount']", "[itemprop='price']"):
                try:
                    v = await self.page.locator(sel).first.get_attribute("content", timeout=1000)
                    if v:
                        info.price = float(v.replace(",", "."))
                        break
                except (PWTimeout, PWError, ValueError):
                    continue
        try:
            info.page_text = await self.page.locator("body").inner_text(timeout=5000)
        except (PWTimeout, PWError):
            info.page_text = ""
        info.price_m2 = parse_price_m2(info.page_text)
        return info

    async def is_discontinued_page(self) -> bool:
        try:
            text = (await self.page.locator("body").inner_text(timeout=3000)).lower()
        except (PWTimeout, PWError):
            return False
        return any(p.lower() in text for p in self.m.get("discontinued_phrases", []))

    async def page_contains_ean(self, ean: str) -> bool:
        try:
            return ean in await self.page.content()
        except PWError:
            return False

    def product_id_from(self, url: str, info: ProductInfo) -> str | None:
        if info.sku:
            return info.sku
        rx = self.m.get("product_id_regex")
        if rx:
            found = re.findall(rx, url)
            if found:
                return found[-1]
        return None

    # =============================================================== ETAP 1
    async def discover(self, product: dict, force: bool = False) -> str:
        """Zwraca URL karty produktu (z cache lub przez wyszukiwarkę sklepu)."""
        ean = product["EAN"]
        cached = self.cache.get(self.key, ean)
        if cached and cached.get("status") == "FOUND" and not force:
            return cached["url"]
        if not force and self.cache.should_skip_not_found(self.key, ean, self.g.get("not_found_recheck_days", 7)):
            raise ProductNotFound("Nie znaleziono przy poprzednim wyszukiwaniu (cache)")

        url = await self.search(ean)
        if not url:
            self.cache.set(self.key, ean, status="NOT_FOUND", url="")
            raise ProductNotFound(f"Brak wyników dla EAN {ean}")
        self.cache.set(self.key, ean, status="FOUND", url=url, name=product.get("Nazwa_Produktu"))
        self.log.info("Znaleziono %s -> %s", ean, url)
        return url

    async def _type_search(self, ean: str):
        """Wyszukiwanie jak człowiek: wpisanie EAN w okienko i Enter (gdy adres wyszukiwarki nie działa)."""
        sel = self.m["search_input_selector"]
        # zawsze świeża strona główna – żadne otwarte panele nie zasłaniają okienka wyszukiwania
        await self.goto(self.m["base_url"])
        box = self.page.locator(sel).first
        try:
            if not await box.is_visible(timeout=8000):
                # zwykle strona "sprawdzania przeglądarki" – chwila przerwy i ponowne wejście
                await asyncio.sleep(random.uniform(15, 25))
                await self.goto(self.m["base_url"])
                box = self.page.locator(sel).first
        except (PWTimeout, PWError):
            pass
        try:
            await box.click(timeout=10000)
            await box.fill("")
            await box.type(ean, delay=random.randint(60, 140))
            await asyncio.sleep(random.uniform(0.4, 1.0))
            await box.press("Enter")
        except (PWTimeout, PWError) as e:
            raise LayoutChanged(f"Nie znaleziono okienka wyszukiwania ({sel})") from e
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=20000)
            await self.page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        if await self._is_blocked(0):
            raise Blocked("Blokada po wyszukaniu")

    async def direct_url(self, ean: str) -> str | None:
        """Adres karty produktu zbudowany wprost z EAN (jeśli sieć to umożliwia)."""
        return None

    async def search(self, ean: str) -> str | None:
        direct = await self.direct_url(ean)
        if direct:
            try:
                await self.goto(direct)
                info = await self.read_product()
                if info.raw_jsonld and (info.gtin == ean or await self.page_contains_ean(ean)):
                    return self.page.url.split("#")[0]
            except ScraperError:
                pass
        return await self._search(ean)

    async def _search(self, ean: str) -> str | None:
        if self.m.get("search_input_selector"):
            await self._type_search(ean)
        else:
            await self.goto(self.m["search_url"].format(query=quote(ean)))

        # 1) wyszukiwarka przekierowała od razu na kartę produktu
        #    (szukaliśmy dokładnego EAN, więc bezpośrednie przekierowanie = trafienie)
        info = await self.read_product()
        if info.raw_jsonld:
            if not (info.gtin == ean or await self.page_contains_ean(ean)):
                self.log.info("EAN %s: przekierowanie na kartę produktu, EAN niewidoczny na stronie – przyjmuję", ean)
            return self.page.url

        # 2) lista wyników – sprawdzamy maks. 3 kandydatów, weryfikując EAN na karcie
        candidates = await self._collect_candidates()
        if not candidates:
            self._nf = getattr(self, "_nf", 0) + 1
            if self._nf <= 3:
                await self.debug_dump(f"{ean}_brak_wynikow")
            return None
        for url in candidates[:3]:
            await self.goto(url)
            info = await self.read_product()
            if info.gtin == ean or await self.page_contains_ean(ean):
                return self.page.url
        self.log.debug("Kandydaci dla %s nie zawierali tego EAN: %s", ean, candidates[:3])
        return None

    async def _collect_candidates(self) -> list[str]:
        domain = urlparse(self.m["base_url"]).netloc
        sels = self.m.get("product_link_selectors", [])
        if sels:   # wyniki wyszukiwania też potrafią dochodzić z opóźnieniem
            try:
                await self.page.wait_for_selector(", ".join(sels), timeout=12000)
            except (PWTimeout, PWError):
                pass
        seen: list[str] = []
        for sel in self.m.get("product_link_selectors", []):
            try:
                hrefs = await self.page.eval_on_selector_all(sel, "els => els.map(e => e.href)")
            except PWError:
                continue
            for h in hrefs:
                if h and urlparse(h).netloc.endswith(domain.replace("www.", "")) and h not in seen:
                    seen.append(h)
            if seen:
                break
        return seen

    # =============================================================== ETAP 2
    async def check(self, product: dict, force_discover: bool = False) -> list[StockResult]:
        """Pełny odczyt jednego produktu: sklep internetowy + sklepy stacjonarne z config.yaml."""
        ean = product["EAN"]
        url = await self.discover(product, force=force_discover)

        self._captured.clear()
        status = await self.goto(url)
        if status in (404, 410):
            self.cache.drop(self.key, ean)  # przy następnym uruchomieniu wyszukamy od nowa
            raise ProductDiscontinued(f"Strona produktu zwraca {status} (usunięta / zmieniony URL)")

        info = await self.read_product()
        await self._flush_captures()
        if self.g.get("snoop"):
            self._dump_snoop(ean)

        if info.raw_jsonld is None and info.price is None:
            # druga próba: przerwa i ponowne wczytanie (często to tylko wolne ładowanie strony)
            await asyncio.sleep(random.uniform(8, 15))
            await self.goto(url)
            info = await self.read_product()
        if info.raw_jsonld is None and info.price is None:
            await self.debug_dump(f"{ean}_brak_danych")
            if await self.is_discontinued_page():
                raise ProductDiscontinued("Strona informuje, że produkt jest niedostępny/wycofany")
            raise LayoutChanged("Brak danych produktu (JSON-LD i meta) – możliwa zmiana układu strony")

        self._any_ok = True
        results = [self.online_result(info, url)]
        all_stores = await self.all_stores_stock(product, info, url)
        if all_stores:
            results.extend(all_stores)
        else:
            try:  # stan sklepu mógł się pojawić dopiero po wybraniu sklepu – czytamy tekst ponownie
                info.page_text = await self.page.locator("body").inner_text(timeout=5000)
            except (PWTimeout, PWError):
                pass
            page_store = self.page_store_stock(info, url)
            if page_store:
                results.append(page_store)
        if self.m.get("store_stock_api"):
            product_id = self.product_id_from(url, info)
            for store in self.m.get("stores", []):
                if not store.get("id"):
                    continue
                try:
                    results.append(await self.store_stock(product, info, product_id, store, url))
                except ScraperError as e:
                    results.append(StockResult(self._store_label(store), price=info.price, url=url,
                                               status=e.status, note=str(e)))
        for r in results:
            r.price_m2, r.m2_per_pack = info.price_m2, info.m2_per_pack
        return results

    async def all_stores_stock(self, product: dict, info: ProductInfo, url: str) -> list[StockResult]:
        """Stany we WSZYSTKICH sklepach sieci. Nadpisywane w obi.py / castorama.py / leroy.py."""
        return []

    def page_store_stock(self, info: ProductInfo, url: str) -> StockResult | None:
        """Stan sklepu stacjonarnego pokazany wprost na karcie produktu
        (np. LM: "13 na stanie w sklepie Gdańsk Oliwa"). Wzorzec: page_store_regex w config.yaml."""
        rx = self.m.get("page_store_regex")
        if not rx or not info.page_text:
            return None
        m = re.search(rx, info.page_text, re.I)
        if not m:
            return None
        qty = int(m.group("qty")) if m.groupdict().get("qty") and m.group("qty").isdigit() else 0
        store = m.group("store").strip()
        return StockResult(store, qty, info.price, url, note="sklep wybrany na stronie (opakowania)")

    def online_result(self, info: ProductInfo, url: str) -> StockResult:
        av = (info.availability or "").lower()
        if any(k in av for k in ("instock", "limitedavailability", "onlineonly", "preorder")):
            stock = "Dostępny"
        elif any(k in av for k in ("outofstock", "discontinued", "soldout", "instoreonly")):
            stock = "Niedostępny"
        else:
            stock = "Brak danych"
        note = "" if info.availability else "sklep nie podaje dostępności online"
        return StockResult("Sklep internetowy", stock, info.price, url, note=note)

    async def store_stock(self, product: dict, info: ProductInfo, product_id: str | None,
                          store: dict, url: str) -> StockResult:
        """Stan w sklepie stacjonarnym. Domyślnie przez `store_stock_api` z config.yaml.
        Sieci mogą nadpisać tę metodę (np. wybór sklepu na stronie)."""
        label = self._store_label(store)
        tmpl = self.m.get("store_stock_api")
        if not tmpl or not store.get("id"):
            return StockResult(label, None, info.price, url, status="DO_KONFIGURACJI",
                               note="Uzupełnij store_stock_api i id sklepu w config.yaml (README → 'API')")
        api_url = tmpl.format(product_id=product_id or "", store_id=store["id"], ean=product["EAN"])
        data = await self.browser_fetch_json(api_url)
        qty = find_first_number(data, self.m.get("quantity_keys", []))
        if qty is None:
            raise LayoutChanged("Odpowiedź API nie zawiera znanego pola ilości (sprawdź quantity_keys)")
        return StockResult(label, round(qty, 2), info.price, url,
                           note="" if qty > 0 else "0 w sklepie")

    @staticmethod
    def _store_label(store: dict) -> str:
        return " / ".join(x for x in (str(store.get("id") or ""), store.get("name", "")) if x) or "?"

    async def debug_dump(self, tag: str):
        """Zrzut ekranu + tekst strony do data/debug/ – trafia do paczki dla Claude."""
        out = DATA_DIR / "debug" / self.key
        out.mkdir(parents=True, exist_ok=True)
        stamp = f"{tag}_{datetime.now():%H%M%S}"
        try:
            await self.page.screenshot(path=str(out / f"{stamp}.png"))
            (out / f"{stamp}.txt").write_text(await self.page.locator("body").inner_text(timeout=5000),
                                              encoding="utf-8")
        except Exception:
            pass

    def _dump_snoop(self, ean: str):
        """Tryb 'API snooping': zapisuje wszystkie odpowiedzi XHR/Fetch z karty produktu."""
        out = DATA_DIR / "snoop" / self.key
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{ean}_{datetime.now():%H%M%S}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._captured, f, ensure_ascii=False, indent=2)
        hints = [c["url"] for c in self._captured
                 if re.search(r"stock|avail|inventor|store|magazyn|dostep", c["url"], re.I)]
        self.log.info("SNOOP: zapisano %d odpowiedzi JSON -> %s", len(self._captured), path)
        for h in hints:
            self.log.info("SNOOP: podejrzany endpoint stanów: %s", h)


# ======================================================== helpers
def parse_price_m2(text: str) -> float | None:
    """Cena za m² z tekstu strony, np. "90,98 zł / m²", "71\n,99 zł / m²", "79,\n99 zł / m2"."""
    t = re.sub(r"\s+", " ", text or "")
    t = re.sub(r"(\d) ?, ?(\d)", r"\1,\2", t)
    m = re.search(r"(?<![\d,])(\d{1,5}),(\d{2}) ?zł ?/ ?m[²2]", t)
    return float(f"{m.group(1)}.{m.group(2)}") if m else None


# ======================================================== JSON-LD helpers
def find_jsonld_product(blocks: list[str]) -> dict | None:
    for raw in blocks:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            t = item.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                return item
            if "@graph" in item:
                stack.extend(item["@graph"] if isinstance(item["@graph"], list) else [item["@graph"]])
    return None


def parse_offers(offers: Any) -> tuple[float | None, str | None]:
    if not offers:
        return None, None
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return None, None
    price = offers.get("price", offers.get("lowPrice"))
    if price is None and isinstance(offers.get("priceSpecification"), dict):
        price = offers["priceSpecification"].get("price")
    try:
        price = float(str(price).replace(",", ".")) if price is not None else None
    except ValueError:
        price = None
    avail = offers.get("availability")
    if avail:
        avail = str(avail).rstrip("/").split("/")[-1]
    return price, avail
