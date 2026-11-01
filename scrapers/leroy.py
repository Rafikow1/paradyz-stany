"""Leroy Merlin – stany w sklepach.

Ustalone w diagnozie (30.10.2026):
  * LM pokazuje stany dopiero, gdy w przeglądarce jest WYBRANY sklep (zapamiętywane w profilu).
  * Stan wybranego sklepu jest wprost na karcie: "13 na stanie w sklepie Gdańsk Oliwa".
  * Kliknięcie "Dostępność w sklepie >" otwiera panel "Sklepy w pobliżu" z ilościami
    dla kolejnych sklepów; po wpisaniu miasta pokazuje sklepy z tamtego regionu.
Program robi dokładnie to, co człowiek: otwiera panel, wpisuje miasta z config.yaml i czyta listę.
Ilości = opakowania.

Uwaga: produkty marki własnej LM (EAN 3276…, np. ARCTIC, SHALA, MOHA) mają nazwy LM, nie "Paradyż".
"""
import asyncio
import random
import re

from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout

import json
from html import unescape
from urllib.parse import quote

from scrapers.base import BaseScraper, Blocked, ScraperError, StockResult
from utils.common import DATA_DIR

# Ustalone 30.10.2026 z podsłuchu: panel "Sklepy w pobliżu" pobiera gotowy fragment HTML
PLACES_URL = "https://www.leroymerlin.pl/store-header-module/services/v2/places?search={q}"
STOCK_URL = ("https://www.leroymerlin.pl/store-header-module/services/contextlayer/store-search-result"
             "?latitude={lat}&longitude={lng}&productRef={ref}&storeSearchType=STOCK")
CARD = re.compile(r'data-store-city="(?P<name>[^"]+)"(?P<body>.*?)(?=data-store-city=|</section>\s*$|\Z)', re.S)
TITLE = re.compile(r'm-store-info-header--title"[^>]*>\s*([^<]+?)\s*<')
COORDS_FILE = DATA_DIR / "lm_wspolrzedne_miast.json"

# "Gdańsk Oliwa · 3,1 km" – początek bloku sklepu w panelu
STORE_HEAD = re.compile(r"^[ \t]*(?P<name>[^\n·•]{2,60}?)[ \t]*[·•\-–][ \t]*(?P<km>[\d.,]+)[ \t]*km[ \t]*$", re.M)
QTY = re.compile(r"(?P<qty>\d+)\s*na stanie", re.I)
NONE = re.compile(r"brak|niedostępn|0 na stanie", re.I)

DEFAULT_CITIES = ["Gdańsk", "Szczecin", "Olsztyn", "Białystok", "Bydgoszcz", "Poznań", "Warszawa",
                  "Łódź", "Lublin", "Wrocław", "Katowice", "Kraków", "Rzeszów", "Kielce", "Zielona Góra"]


class LeroyScraper(BaseScraper):
    key = "leroy"
    _api_dumped = False

    async def _ensure_store_selected(self) -> bool:
        """Jeśli w profilu nie ma wybranego sklepu – wybiera pierwszy z listy dla Gdańska."""
        btn = self.page.get_by_text("Wyświetl zapas w sklepach", exact=False).first
        try:
            if not await btn.is_visible(timeout=1500):
                return True
            await btn.click(timeout=4000)
            await asyncio.sleep(2)
            await self._type_city(self.m.get("home_city", "Gdańsk"))
            if not await self._click_choose():
                raise PWError("nie znaleziono widocznego przycisku 'Wybierz'")
            self.log.info("LM: wybrano sklep domowy (%s)", self.m.get("home_city", "Gdańsk"))
            await self.page.wait_for_load_state("domcontentloaded", timeout=20000)
            await asyncio.sleep(3)
            return True
        except (PWTimeout, PWError) as e:
            self.log.warning("LM: nie udało się wybrać sklepu: %s", str(e)[:100])
            return False

    async def _click_choose(self) -> bool:
        """Klika pierwszy WIDOCZNY przycisk "Wybierz" na liście sklepów."""
        await asyncio.sleep(1.5)
        cands = self.page.locator("button, a, [role=button]").filter(has_text=re.compile(r"^\s*Wybierz\s*$"))
        for i in range(await cands.count()):
            el = cands.nth(i)
            try:
                if await el.is_visible(timeout=800):
                    await el.scroll_into_view_if_needed(timeout=3000)
                    await el.click(timeout=4000)
                    return True
            except (PWTimeout, PWError):
                continue
        try:
            await self.page.get_by_text("Wybierz", exact=True).first.click(timeout=4000)
            return True
        except (PWTimeout, PWError):
            return False

    async def _type_city(self, city: str):
        box = self.page.locator("input[placeholder*='Miasto lub kod pocztowy']").first
        await box.click(timeout=5000)
        await box.fill("")
        await box.type(city, delay=random.randint(60, 120))
        await asyncio.sleep(0.8)
        await box.press("Enter")
        await asyncio.sleep(random.uniform(2.5, 3.5))

    async def _panel_input_visible(self, wait_s: float = 8) -> bool:
        box = self.page.locator("input[placeholder*='Miasto lub kod pocztowy']")
        for _ in range(int(wait_s / 0.5)):
            for i in range(await box.count()):
                try:
                    if await box.nth(i).is_visible(timeout=200):
                        return True
                except (PWTimeout, PWError):
                    pass
            await asyncio.sleep(0.5)
        return False

    async def _open_panel(self) -> bool:
        """Otwiera "Sklepy w pobliżu". Próbuje kilku sposobów, bo kliknąć trzeba w cały wiersz
        "Dostępność w sklepie  >", a nie tylko w napis."""
        p = self.page
        rx = re.compile(r"Dostępność w sklepie")
        attempts = [
            ("przycisk wiersza", p.get_by_role("button", name=rx)),
            ("link wiersza", p.get_by_role("link", name=rx)),
            ("element z tekstem", p.locator("button:has-text('Dostępność w sklepie'), a:has-text('Dostępność w sklepie'), "
                                            "[role=button]:has-text('Dostępność w sklepie')")),
            ("wiersz ze stanem", p.locator("button:has-text('na stanie w sklepie'), a:has-text('na stanie w sklepie'), "
                                           "[role=button]:has-text('na stanie w sklepie')")),
            ("napis + rodzic", p.get_by_text("Dostępność w sklepie", exact=True)),
            ("napis stanu + rodzic", p.get_by_text(re.compile(r"na stanie w sklepie"))),
        ]
        for label, loc in attempts:
            try:
                n = await loc.count()
            except PWError:
                continue
            for i in range(min(n, 3)):
                el = loc.nth(i)
                try:
                    if not await el.is_visible(timeout=800):
                        continue
                    await el.scroll_into_view_if_needed(timeout=3000)
                    if label.endswith("+ rodzic"):
                        # klik w najbliższy klikalny element nad napisem
                        await el.evaluate("e => (e.closest('button,a,[role=button],[tabindex],li,section,div[class*=\"click\"]') || e).click()")
                    else:
                        await el.click(timeout=4000)
                    if await self._panel_input_visible(8):
                        self.log.debug("LM: panel otwarty (%s)", label)
                        return True
                    await p.keyboard.press("Escape")
                    await asyncio.sleep(0.5)
                except (PWTimeout, PWError):
                    continue
        return False

    async def _scroll_panel(self):
        vp = self.page.viewport_size or {"width": 1366, "height": 860}
        await self.page.mouse.move(vp["width"] - 220, vp["height"] // 2)
        for _ in range(8):
            await self.page.mouse.wheel(0, 1500)
            await asyncio.sleep(0.4)

    async def _read_panel(self) -> dict[str, int]:
        try:
            text = await self.page.locator("body").inner_text(timeout=5000)
        except (PWTimeout, PWError):
            return {}
        return parse_store_panel(text)

    # ------------------------------------------------------------ szybka droga
    _coords: dict | None = None

    async def _city_coords(self, cities: list[str]) -> dict[str, tuple[float, float]]:
        if self._coords is None:
            try:
                self._coords = json.loads(COORDS_FILE.read_text(encoding="utf-8")) if COORDS_FILE.exists() else {}
            except (OSError, json.JSONDecodeError):
                self._coords = {}
        missing = [c for c in cities if c not in self._coords]
        for city in missing:
            try:
                data = await self.browser_fetch_json(PLACES_URL.format(q=quote(city)))
                loc = (data.get("localities") or [{}])[0].get("coordinates") or {}
                if "lat" in loc:
                    self._coords[city] = (loc["lat"], loc["lng"])
            except ScraperError as e:
                self.log.debug("LM: brak współrzędnych %s: %s", city, e)
        if missing:
            COORDS_FILE.parent.mkdir(parents=True, exist_ok=True)
            COORDS_FILE.write_text(json.dumps(self._coords, ensure_ascii=False, indent=1), encoding="utf-8")
        return {c: tuple(self._coords[c]) for c in cities if c in self._coords}

    _cover: list[str] | None = None   # najmniejszy zestaw miast, który obejmuje wszystkie sklepy

    async def _fast_all_stores(self, ref: str) -> dict[str, int]:
        cities = self._cover or self.m.get("store_search_cities") or DEFAULT_CITIES
        coords = await self._city_coords(cities)
        found: dict[str, int] = {}
        per_city: dict[str, set] = {}
        for city, (lat, lng) in coords.items():
            html = await self.browser_fetch_text(STOCK_URL.format(lat=lat, lng=lng, ref=ref))
            got = parse_stock_html(html)
            per_city[city] = set(got)
            found.update(got)
        if self._cover is None and len(found) >= 50:
            self._cover = greedy_cover(per_city)
            self.log.info("LM: wystarczy %d z %d miast, żeby objąć %d sklepów – mniej zapytań",
                          len(self._cover), len(per_city), len(found))
        return found

    async def all_stores_stock(self, product, info, url):
        ref = self.product_id_from(url, info)
        if ref and not self.m.get("disable_fast_path"):
            try:
                found = await self._fast_all_stores(ref)
                if len(found) >= 10:
                    self.log.info("LM %s: odczytano %d sklepów (szybka droga)", product["EAN"], len(found))
                    return [StockResult(n, q, info.price, url, note="opakowania") for n, q in sorted(found.items())]
                self.log.warning("LM %s: szybka droga dała tylko %d sklepów – przechodzę na panel",
                                 product["EAN"], len(found))
            except Blocked:
                raise   # blokada – nie ma sensu klikać w panel; produkt trafi do "dokończ"
            except ScraperError as e:
                self.log.warning("LM %s: szybka droga nie działa (%s) – przechodzę na panel", product["EAN"], e)
        return await self._panel_all_stores(product, info, url)

    async def _panel_all_stores(self, product, info, url):
        if not await self._ensure_store_selected():
            return []
        if self.page.url.split("?")[0] != url.split("?")[0]:
            await self.goto(url)
        if not await self._open_panel():
            self.log.warning("LM %s: nie udało się otworzyć panelu sklepów", product["EAN"])
            await self.debug_dump(f"{product['EAN']}_panel_nie_otwarty")
            return []

        found: dict[str, int] = {}
        cities = self.m.get("store_search_cities") or DEFAULT_CITIES
        for i, city in enumerate(cities):
            sniff = i == 0 and not self._api_dumped
            if sniff:  # jednorazowo: zapisz, skąd strona bierze stany (szukamy szybszej drogi)
                self._captured.clear()
                snoop_before, self.g["snoop"] = self.g.get("snoop"), True
            try:
                await self._type_city(city)
                await self._scroll_panel()
            except (PWTimeout, PWError):
                self.log.debug("LM: nie udało się wyszukać %s", city)
                continue
            if sniff:
                await self._flush_captures()
                self.g["snoop"] = snoop_before
                self._api_dumped = True
                self._dump_api_sniff(product["EAN"])
            new = await self._read_panel()
            if i == 0 and not new:
                await self.debug_dump(f"{product['EAN']}_panel_pusty_{city}")
            before = len(found)
            found.update(new)
            # jeśli pierwsza lista zawiera już (prawie) wszystkie sklepy – nie ma sensu szukać dalej
            if i == 0 and len(new) >= self.m.get("full_list_threshold", 40):
                break
            self.log.debug("LM %s: %s -> +%d sklepów", product["EAN"], city, len(found) - before)

        try:
            await self.page.keyboard.press("Escape")
        except PWError:
            pass
        self.log.info("LM %s: odczytano %d sklepów", product["EAN"], len(found))
        return [StockResult(name, qty, info.price, url, note="opakowania")
                for name, qty in sorted(found.items())]


    def _dump_api_sniff(self, ean: str):
        import json
        from utils.common import DATA_DIR
        out = DATA_DIR / "debug" / self.key
        out.mkdir(parents=True, exist_ok=True)
        keep = [c for c in self._captured if "leroymerlin" in c["url"]]
        (out / f"{ean}_api_po_wpisaniu_miasta.json").write_text(
            json.dumps(keep, ensure_ascii=False, indent=1)[:3_000_000], encoding="utf-8")
        self.log.info("LM: zapisano %d zapytań strony (do analizy)", len(keep))


def greedy_cover(per_city: dict[str, set]) -> list[str]:
    """Wybiera jak najmniej miast, które razem pokazują wszystkie sklepy."""
    left = set().union(*per_city.values()) if per_city else set()
    chosen: list[str] = []
    while left:
        best = max(per_city, key=lambda c: len(per_city[c] & left))
        if not per_city[best] & left:
            break
        chosen.append(best)
        left -= per_city[best]
    return chosen


def parse_stock_html(html: str) -> dict[str, int]:
    """Fragment HTML z store-search-result: <article data-store-city="Gdańsk"> ... "14 na stanie"."""
    out: dict[str, int] = {}
    for m in CARD.finditer(html or ""):
        t = TITLE.search(m.group("body"))   # pełna nazwa sklepu, np. "Poznań Swadzim" (data-store-city to tylko miasto)
        name = unescape(t.group(1) if t else m.group("name")).strip()
        txt = re.sub(r"<[^>]+>", " ", m.group("body"))
        q = QTY.search(txt)
        if q:
            out[name] = int(q.group("qty"))
        elif NONE.search(txt):
            out[name] = 0
    return out


def parse_store_panel(text: str) -> dict[str, int]:
    """Dzieli tekst panelu na bloki sklepów ("Nazwa · 9,7 km" ... "20 na stanie")."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    heads = list(STORE_HEAD.finditer(text))
    out: dict[str, int] = {}
    for i, h in enumerate(heads):
        block = text[h.end(): heads[i + 1].start() if i + 1 < len(heads) else h.end() + 400]
        m = QTY.search(block)
        if m:
            out[h.group("name").strip()] = int(m.group("qty"))
        elif NONE.search(block):
            out[h.group("name").strip()] = 0
    return out
