#!/usr/bin/env python3
"""
Парсер dvt-spb.ru по главным категориям с полной пагинацией.

Собирает:
1. Название
2. Стоимость
3. Фото (все найденные фото товара)
4. Описание
5. Характеристики
6. Description (meta description)
7. keywords (meta keywords)

Дополнительно:
- category_path — путь по каталогу, например:
  ["Станки по металлу", "Токарные", "Настольные"]
- category_url — URL категории, из которой найден товар.

Примеры:
    python parser.py --test 20
    python parser.py --max-pages 100
    python parser.py --start "https://dvt-spb.ru/stanki/po-metallu"

Для первого теста рекомендуется:
    python parser.py --test 20
"""

import argparse
import csv
import json
import logging
import random
import re
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://dvt-spb.ru/"
DOMAIN = "dvt-spb.ru"

# По сайту достаточно обойти только главные категории.
# В каждой главной категории уже показываются товары всех её подкатегорий.
DEFAULT_START = "__ALL_MAIN_CATEGORIES__"

MAIN_CATEGORIES = [
    ("Станки по металлу", "https://dvt-spb.ru/stanki/po-metallu"),
    ("Кузнечно-прессовое", "https://dvt-spb.ru/stanki/kpo"),
    ("Лазерные станки", "https://dvt-spb.ru/stanki/lazernye"),
    ("Станки по дереву", "https://dvt-spb.ru/stanki/po-derevu"),
    ("Мебельное", "https://dvt-spb.ru/stanki/dlya-korpusnoj-mebeli"),
    ("Специальные", "https://dvt-spb.ru/stanki/spetsializirovannye"),
    ("Станки б/у", "https://dvt-spb.ru/stanki/bu"),
    ("Компрессорное", "https://dvt-spb.ru/stanki/kompressory"),
    ("Автосервисное", "https://dvt-spb.ru/stanki/avtoservisnoe"),
    ("Оснастка", "https://dvt-spb.ru/stanki/tehnologicheskaya-osnastka"),
    ("Приспособления", "https://dvt-spb.ru/stanki/stanochnye-prisposobleniya"),
    ("Инструмент", "https://dvt-spb.ru/stanki/instrumenty"),
]

OUT_JSON = "products.json"
OUT_CSV = "products.csv"
LOG_FILE = "parser.log"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36"
)

MIN_DELAY = 1.2
MAX_DELAY = 2.5
TIMEOUT = 20
MAX_RETRIES = 3


def normalize_url(url: str) -> str:
    if not url:
        return ""

    url = urldefrag(url)[0].strip()
    p = urlparse(url)

    if p.scheme not in ("http", "https"):
        return ""

    host = p.netloc.lower().replace("www.", "")
    if host != DOMAIN:
        return ""

    # Убираем fragment, сохраняем query (?page=2 и т.п.).
    path = p.path or "/"
    if path != "/":
        path = path.rstrip("/")

    return f"https://{DOMAIN}{path}" + (f"?{p.query}" if p.query else "")


def absolute_url(href: str, base: str) -> str:
    """Преобразует внутреннюю ссылку сайта в абсолютный URL."""
    if not href:
        return ""

    href = href.strip()

    if href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return ""

    # В карточках сайта ссылки товара выглядят:
    # products/slug
    # Поэтому явно считаем products/ корневым путём.
    if href.startswith(("products/", "/products/")):
        href = "/" + href.lstrip("/")

    return normalize_url(urljoin(base, href))


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def setup_logging():
    logging.basicConfig(
        filename=LOG_FILE,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def save_json(products):
    Path(OUT_JSON).write_text(
        json.dumps(products, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def save_csv(products):
    fields = [
        "url",
        "name",
        "price",
        "images",
        "description",
        "characteristics",
        "meta_description",
        "meta_keywords",
        "category_path",
        "category_url",
        "category_type",
        "advertising_category",
    ]

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        for product in products:
            row = dict(product)
            row["images"] = json.dumps(
                row.get("images", []),
                ensure_ascii=False,
            )
            row["characteristics"] = json.dumps(
                row.get("characteristics", {}),
                ensure_ascii=False,
            )
            row["category_path"] = json.dumps(
                row.get("category_path", []),
                ensure_ascii=False,
            )
            writer.writerow(row)


class SafeSession:
    def __init__(self):
        self.session = requests.Session()

        # Не использовать системный proxy/SOCKS из переменных окружения.
        self.session.trust_env = False

        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            "Connection": "keep-alive",
        })

        self.last_request = 0.0

    def wait(self):
        elapsed = time.monotonic() - self.last_request
        delay = random.uniform(MIN_DELAY, MAX_DELAY)

        if elapsed < delay:
            time.sleep(delay - elapsed)

    def get(self, url):
        for attempt in range(1, MAX_RETRIES + 1):
            self.wait()

            try:
                response = self.session.get(
                    url,
                    timeout=TIMEOUT,
                    allow_redirects=True,
                )

                self.last_request = time.monotonic()

                logging.info(
                    "GET %s -> %s %.2fs final=%s",
                    url,
                    response.status_code,
                    response.elapsed.total_seconds(),
                    response.url,
                )

                if response.status_code in (401, 403, 429):
                    logging.warning(
                        "Protection/rate limit: %s -> %s",
                        response.status_code,
                        url,
                    )
                    return None, response.status_code

                if 500 <= response.status_code <= 599:
                    if attempt < MAX_RETRIES:
                        time.sleep(3 * attempt)
                        continue
                    return None, response.status_code

                if 400 <= response.status_code <= 499:
                    logging.warning(
                        "HTTP %s for %s",
                        response.status_code,
                        url,
                    )
                    return None, response.status_code

                response.raise_for_status()
                return response, response.status_code

            except requests.RequestException as exc:
                logging.warning(
                    "Request error attempt=%s url=%s error=%s",
                    attempt,
                    url,
                    exc,
                )

                if attempt < MAX_RETRIES:
                    time.sleep(2 * attempt)

        return None, None


def load_robots(session):
    robots_url = urljoin(BASE_URL, "robots.txt")
    response, status = session.get(robots_url)

    if response is None:
        logging.warning(
            "robots.txt could not be fetched; continuing cautiously"
        )
        return None

    rp = RobotFileParser()
    rp.set_url(robots_url)
    rp.parse(response.text.splitlines())

    return rp




def path_is_product(url: str) -> bool:
    return urlparse(url).path.lower().startswith("/products/")


def path_is_catalog(url: str) -> bool:
    path = urlparse(url).path.lower().rstrip("/")
    return path == "/stanki" or path.startswith("/stanki/")


def extract_product_links(soup, page_url):
    links = set()
    for selector in ("a.card_model[href]", "a.card_image_wrap[href]"):
        for a in soup.select(selector):
            url = absolute_url(a.get("href", ""), page_url)
            if url and path_is_product(url):
                links.add(url)

    # Если карточки изменились — запасной вариант по всем /products/.
    if not links:
        for a in soup.select("a[href]"):
            url = absolute_url(a.get("href", ""), page_url)
            if url and path_is_product(url):
                links.add(url)
    return links


def extract_catalog_pages(soup, page_url):
    """
    Находит ВСЕ реальные ссылки пагинации, которые отдал сайт.

    ВАЖНО: не добавляем и не меняем on_page=60.
    Сайт сам формирует рабочие URL вида ?page=2, ?page=3 и т.д.
    Если искусственно дописывать on_page, сервер в некоторых случаях
    возвращает одну и ту же страницу, из-за чего количество уникальных
    товаров не растет.
    """
    pages = set()

    # Основной вариант: штатный блок пагинации.
    anchors = []
    seen_nodes = set()

    for block in soup.select(".pagination"):
        for a in block.select("a[href]"):
            node_id = id(a)
            if node_id not in seen_nodes:
                seen_nodes.add(node_id)
                anchors.append(a)

    # Запасной вариант: ссылки next/страницы, если класс пагинации изменится.
    for a in soup.select('a[rel="next"][href], a.next_page_link[href]'):
        node_id = id(a)
        if node_id not in seen_nodes:
            seen_nodes.add(node_id)
            anchors.append(a)

    for a in soup.select('a[href*="page="]'):
        node_id = id(a)
        if node_id not in seen_nodes:
            seen_nodes.add(node_id)
            anchors.append(a)

    current_path = urlparse(page_url).path.rstrip("/")

    for a in anchors:
        href = a.get("href", "")
        url = absolute_url(href, page_url)
        if not url or not path_is_catalog(url):
            continue

        p = urlparse(url)
        if p.path.rstrip("/") != current_path:
            continue

        params = parse_qsl(p.query, keep_blank_values=True)
        page_values = [v for k, v in params if k.lower() == "page" and v.isdigit()]
        if not page_values:
            continue

        # Сохраняем оригинальный URL без изменения параметров.
        pages.add(url)

    def page_number(url):
        for key, value in parse_qsl(urlparse(url).query, keep_blank_values=True):
            if key.lower() == "page" and value.isdigit():
                return int(value)
        return 0

    # Возвращаем список по порядку страниц. Это делает обход предсказуемым:
    # 1 -> 2 -> 3 -> ... вместо случайного порядка set().
    return sorted(pages, key=page_number)


def extract_meta(soup, name):
    node = soup.find(
        "meta",
        attrs={"name": re.compile(rf"^{re.escape(name)}$", re.I)},
    )

    if not node:
        return ""

    return clean_text(node.get("content", ""))


def extract_name(soup):
    selectors = [
        "h1",
        '[itemprop="name"]',
        ".product-title",
        ".product-name",
        ".mainproduct h1",
    ]

    for selector in selectors:
        node = soup.select_one(selector)

        if node:
            value = clean_text(node.get_text(" ", strip=True))

            if value:
                return value

    title = soup.find("title")
    return clean_text(title.get_text(" ", strip=True)) if title else ""


def extract_price(soup):
    selectors = [
        ".card_price",
        '[itemprop="price"]',
        ".product-price",
        ".price",
        "[class*='price']",
    ]

    for selector in selectors:
        for node in soup.select(selector):
            value = node.get("content") or node.get_text(" ", strip=True)
            value = clean_text(value)

            if re.search(r"\d", value):
                return value

    text = clean_text(soup.get_text(" ", strip=True))

    match = re.search(
        r"(\d[\d\s\xa0]*[.,]?\d*)\s*(?:₽|руб\.?|р\.)",
        text,
        re.I,
    )

    return match.group(0).strip() if match else ""


def extract_description(soup):
    selectors = [
        '[itemprop="description"]',
        ".product-description",
        ".product_description",
        ".mainproduct .description",
        "#tab1",
    ]

    candidates = []

    for selector in selectors:
        for node in soup.select(selector):
            text = clean_text(node.get_text(" ", strip=True))

            if len(text) >= 30:
                candidates.append(text)

    if candidates:
        return max(candidates, key=len)

    for heading in soup.find_all(["h2", "h3", "h4"]):
        if clean_text(heading.get_text()).lower().rstrip(":") == "описание":
            parts = []

            for sibling in heading.find_all_next():
                if sibling is not heading and sibling.name in (
                    "h2", "h3", "h4"
                ):
                    break

                text = clean_text(
                    sibling.get_text(" ", strip=True)
                )

                if text:
                    parts.append(text)

                if len(" ".join(parts)) > 20000:
                    break

            result = clean_text(" ".join(parts))

            if result:
                return result

    return ""


def extract_characteristics(soup):
    data = OrderedDict()

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"])

            if len(cells) < 2:
                continue

            key = clean_text(cells[0].get_text(" ", strip=True))
            value = clean_text(cells[1].get_text(" ", strip=True))

            if (
                key
                and value
                and len(key) < 200
                and len(value) < 2000
            ):
                if key.lower() not in {
                    "характеристика",
                    "параметр",
                    "название",
                    "значение",
                }:
                    data[key] = value

    for dl in soup.find_all("dl"):
        for dt in dl.find_all("dt"):
            dd = dt.find_next_sibling("dd")

            if not dd:
                continue

            key = clean_text(dt.get_text(" ", strip=True))
            value = clean_text(dd.get_text(" ", strip=True))

            if key and value:
                data[key] = value

    for row in soup.select(".spec_row"):
        key_node = row.select_one("span")
        value_node = row.select_one("strong")

        if not key_node or not value_node:
            continue

        key = clean_text(
            key_node.get("title")
            or key_node.get_text(" ", strip=True)
        )
        value = clean_text(
            value_node.get_text(" ", strip=True)
        )

        if key and value:
            data[key] = value

    return dict(data)


def extract_images(soup, page_url):
    """
    Собираем изображения товара.
    Фильтр /files/products/ защищает от логотипов и UI-картинок.
    """
    images = []
    seen = set()

    def add(src):
        if not src:
            return

        src = src.strip()

        # srcset: "url 420w, url2 840w"
        if "," in src and any(
            marker in src
            for marker in (" 320w", " 420w", " 640w", " 800w", " 1200w")
        ):
            for item in src.split(","):
                first = item.strip().split()[0]
                add(first)
            return

        url = absolute_url(src, page_url)
        if not url:
            return

        path = urlparse(url).path.lower()

        if "/files/products/" not in path:
            return

        if url not in seen:
            seen.add(url)
            images.append(url)

    gallery_selectors = [
        ".product-gallery img",
        ".product-images img",
        ".product_gallery img",
        ".gallery img",
        ".mainproduct img",
    ]

    for selector in gallery_selectors:
        for img in soup.select(selector):
            for attr in (
                "data-src",
                "data-original",
                "data-large",
                "data-zoom-image",
                "src",
                "data-lazy-src",
                "srcset",
            ):
                add(img.get(attr))

    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if "/files/products/" in href.lower():
            add(href)

    if not images:
        for img in soup.find_all("img"):
            for attr in (
                "data-src",
                "data-original",
                "data-large",
                "data-zoom-image",
                "src",
                "data-lazy-src",
                "srcset",
            ):
                add(img.get(attr))

    return images


def parse_product(html, url, category_path=None, category_url=""):
    soup = BeautifulSoup(html, "lxml")

    return {
        "url": url,
        "name": extract_name(soup),
        "price": extract_price(soup),
        "images": extract_images(soup, url),
        "description": extract_description(soup),
        "characteristics": extract_characteristics(soup),
        "meta_description": extract_meta(soup, "description"),
        "meta_keywords": extract_meta(soup, "keywords"),
        "category_path": category_path or [],
        "category_url": category_url,
    }


def collect_products(session, categories, max_pages=None, test=None, robots=None):
    """
    Обходит только главные категории и их пагинацию.

    Подкатегории намеренно НЕ посещаются: по структуре сайта главная
    категория уже содержит товары из всех вложенных категорий.
    Дубликаты товаров убираются по URL товара.
    """
    product_info = {}
    visited_pages = set()
    queued_pages = set()
    queue = []
    page_seen_sets = set()

    for category_name, category_url in categories:
        url = normalize_url(category_url)
        queue.append((url, category_name, category_url))
        queued_pages.add(url)

    while queue:
        url, category_name, root_category_url = queue.pop(0)

        if url in visited_pages:
            continue

        if max_pages and len(visited_pages) >= max_pages:
            print(f"Достигнут лимит --max-pages={max_pages}")
            break

        visited_pages.add(url)

        if robots and not robots.can_fetch(USER_AGENT, url):
            logging.warning("robots disallows category page: %s", url)
            continue

        response, status = session.get(url)

        if response is None:
            if status in (401, 403, 429):
                print(
                    f"\nОстановка: сервер вернул HTTP {status}.\n"
                    "Парсер не пытается обходить защиту."
                )
                break
            continue

        final_url = normalize_url(response.url) or url
        soup = BeautifulSoup(response.text, "lxml")
        found = extract_product_links(soup, final_url)
        known_before = set(product_info)
        found_key = tuple(sorted(found))
        new_count = sum(1 for product_url in found if product_url not in known_before)

        if found_key and found_key in page_seen_sets:
            logging.warning("Repeated product set on pagination page: %s", url)
            print(f"  ВНИМАНИЕ: повторяется набор товаров, проверь пагинацию: {url}")
        page_seen_sets.add(found_key)

        for product_url in found:
            if product_url not in product_info:
                product_info[product_url] = {
                    "category_path": [category_name],
                    "category_url": root_category_url,
                }

        print(
            f"[страница {len(visited_pages)}] {category_name} | "
            f"на этой странице: {len(found)} | "
            f"новых товаров: {new_count} | "
            f"всего уникальных товаров: {len(product_info)} | "
            f"очередь: {len(queue)}"
        )

        if test and len(product_info) >= test:
            break

        for next_page in extract_catalog_pages(soup, final_url):
            if next_page not in visited_pages and next_page not in queued_pages:
                queue.append((next_page, category_name, root_category_url))
                queued_pages.add(next_page)

    return product_info


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test", type=int, default=None,
        help="Сколько уникальных товаров обработать в тестовом режиме",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="Максимум посещённых страниц категорий/пагинации",
    )
    parser.add_argument(
        "--start", default=DEFAULT_START,
        help="URL одной главной категории. Без --start обрабатываются все главные категории.",
    )
    args = parser.parse_args()

    setup_logging()
    session = SafeSession()

    print("Проверяем robots.txt...")
    robots = load_robots(session)

    if robots:
        check_url = MAIN_CATEGORIES[0][1] if args.start == DEFAULT_START else args.start
        allowed = robots.can_fetch(USER_AGENT, check_url)
        print(
            "robots.txt: "
            f"{'разрешает' if allowed else 'ЗАПРЕЩАЕТ'} "
            f"стартовую страницу: {check_url}"
        )
        if not allowed:
            print("Останавливаемся, чтобы не нарушать robots.txt.")
            return

    if args.start == DEFAULT_START:
        categories = MAIN_CATEGORIES
        print("\nРежим: ВСЕ ГЛАВНЫЕ КАТЕГОРИИ")
    else:
        categories = [("Выбранная категория", args.start)]
        print(f"\nРежим: ОДНА КАТЕГОРИЯ {args.start}")

    print("Подкатегории НЕ обходим.")
    print(f"Главных категорий: {len(categories)}")
    print("Пагинация: используем оригинальные URL сайта (?page=N), без принудительного on_page=60.")

    product_info = collect_products(
        session,
        categories,
        max_pages=args.max_pages,
        test=args.test,
        robots=robots,
    )

    product_urls = sorted(product_info)
    if args.test:
        product_urls = product_urls[:args.test]

    print(f"\nНайдено уникальных URL товаров: {len(product_urls)}")

    if not product_urls:
        print("Товары не найдены. Проверь parser.log.")

    products = []
    for i, url in enumerate(product_urls, 1):
        if robots and not robots.can_fetch(USER_AGENT, url):
            logging.warning("robots disallows product: %s", url)
            print(f"[{i}/{len(product_urls)}] SKIP robots: {url}")
            continue

        response, status = session.get(url)
        if response is None:
            print(f"[{i}/{len(product_urls)}] ERROR HTTP {status}: {url}")
            if status in (401, 403, 429):
                print("Зафиксирована защита/лимит. Сбор остановлен.")
                break
            continue

        final_url = normalize_url(response.url) or url
        info = product_info.get(url, {})
        product = parse_product(
            response.text,
            final_url,
            category_path=info.get("category_path", []),
            category_url=info.get("category_url", ""),
        )

        # Поля оставляем совместимыми с предыдущим форматом CSV/JSON.
        product["category_type"] = "catalog"
        product["advertising_category"] = ""
        products.append(product)

        name = product["name"] or "[без названия]"
        print(
            f"[{i}/{len(product_urls)}] {name[:100]} | "
            f"фото: {len(product['images'])} | "
            f"характеристик: {len(product['characteristics'])} | "
            f"категория: {' > '.join(product['category_path'])}"
        )

        save_json(products)
        save_csv(products)

    print("\nГотово.")
    print(f"Собрано товаров: {len(products)}")
    print(f"JSON: {OUT_JSON}")
    print(f"CSV:  {OUT_CSV}")
    print(f"LOG:  {LOG_FILE}")


if __name__ == "__main__":
    main()
