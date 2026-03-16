import json
import os
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Set
import pytz
import copy
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

BASE_URL = "https://www.karzanddolls.com"
TARGET_URLS = [
    "https://www.karzanddolls.com/details/mini+gt+/mini-gt-blister-pack/MTY2",
    "https://www.karzanddolls.com/details/mini+gt+/mini-gt/MTY1",
]
PRODUCTS_FILE = "products.json"
ALERTS_FILE = "alerts.json"
CHECK_INTERVAL_SECONDS = 15
IST = pytz.timezone("Asia/Kolkata")

def is_monitoring_time():
    now = datetime.now(IST)
    hour = now.hour
    return 9 <= hour < 22

# Telegram configuration from environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")

# In-memory set of product names that have already triggered Telegram alerts
alerted_names: Set[str] = set()

# --------------------------
# TELEGRAM ALERT FUNCTIONS
# --------------------------
def send_telegram_alert(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram alert failed with status {resp.status_code}: {resp.text}")
    except requests.RequestException as exc:
        print(f"Telegram alert error: {exc}")

def send_telegram_photo(message: str, image_url: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "photo": image_url,
        "caption": message,
        "parse_mode": "HTML"
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            print(f"Telegram photo alert failed: {resp.text}")
    except requests.RequestException as exc:
        print(f"Telegram photo error: {exc}")

# --------------------------
# DATA STORAGE FUNCTIONS
# --------------------------
def load_alerted_names(path: str = ALERTS_FILE) -> Set[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return {str(u) for u in data}
        if isinstance(data, dict):
            return {str(u) for u in data.keys()}
        return set()
    except FileNotFoundError:
        return set()
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: could not read {path}: {exc}")
        return set()

def save_alerted_names(names: Set[str], path: str = ALERTS_FILE) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(names), f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"Error: could not save alerts to {path}: {exc}")

def load_previous_products(path: str = PRODUCTS_FILE) -> Dict[str, Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"Warning: could not read {path}: {exc}")
        return {}

def save_products(products: Dict[str, Dict[str, Any]], path: str = PRODUCTS_FILE) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(products, f, indent=2, ensure_ascii=False)
    except OSError as exc:
        print(f"Error: could not save products to {path}: {exc}")

# --------------------------
# FETCHING FUNCTIONS
# --------------------------
def fetch_page(url: str) -> Optional[str]:
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        print(f"[{datetime.now().isoformat()}] Network error while fetching page: {exc}")
        return None

def fetch_product_image(product_url: str) -> Optional[str]:
    """
    Robustly fetch main product image URL from a listing page.
    """
    html = fetch_page(product_url)
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Try multiple possible image classes
    img_tag = (
        soup.select_one("img.gc-display-display") or
        soup.select_one("img.gc-overlay-display") or
        soup.select_one("img.lazy")
    )
    if img_tag:
        image_url = img_tag.get("data-src") or img_tag.get("data-original") or img_tag.get("src")
        if image_url:
            image_url = image_url.strip().split("?")[0]
            if not image_url.startswith("http"):
                image_url = BASE_URL.rstrip("/") + "/" + image_url.lstrip("/")
            return image_url
    return None

def _extract_stock_info(card: BeautifulSoup) -> Dict[str, Any]:
    size_li = card.select_one(".add-top-size li[data-qty]")
    quantity: Optional[int] = None
    if size_li:
        qty_str = size_li.get("data-qty")
        try:
            quantity = int(qty_str) if qty_str is not None else None
        except (TypeError, ValueError):
            quantity = None
    if quantity is None:
        stock_status = "Unknown"
    elif quantity > 0:
        stock_status = f"In stock (qty {quantity})"
    elif quantity == 0:
        stock_status = "Sold Out"
    else:
        stock_status = "Unknown"
    return {"stock_status": stock_status, "quantity": quantity}

def parse_products(html: str, product_type: str) -> Dict[str, Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    products: Dict[str, Dict[str, Any]] = {}
    right_column = soup.select_one(".product-show-right")
    if right_column is None:
        print("Could not find product listing container on the page.")
        return products

    cards: List[BeautifulSoup] = right_column.select(".show-product-small-bx")
    for card in cards:
        link_tag = card.select_one("a.block-active[href]") or card.find("a", href=True)
        if not link_tag:
            continue
        url = link_tag["href"]
        if not url.startswith("http"):
            url = BASE_URL.rstrip("/") + "/" + url.lstrip("/")

        img_tag = card.select_one("img[alt]")
        image_url = None
        if img_tag:
            image_url = img_tag.get("data-src") or img_tag.get("data-original") or img_tag.get("src")
            if image_url and not image_url.startswith("http"):
                image_url = BASE_URL.rstrip("/") + "/" + image_url.lstrip("/")

        if img_tag and img_tag.get("alt"):
            name = img_tag.get("alt").strip()
        else:
            name_tag = card.select_one(".detail-text h3")
            name = name_tag.get_text(strip=True) if name_tag else "Unknown"

        price_tag = card.select_one(".detail-price .rs")
        price = price_tag.get_text(strip=True) if price_tag else "Unknown"

        stock_info = _extract_stock_info(card)

        products[url] = {
            "name": name,
            "price": price,
            "url": url,
            "stock_status": stock_info["stock_status"],
            "quantity": stock_info["quantity"],
            "type": product_type,
            "image_url": image_url,
            "last_seen": datetime.now().isoformat(),
        }

    return products

def fetch_all_products() -> Dict[str, Dict[str, Any]]:
    all_products: Dict[str, Dict[str, Any]] = {}
    urls_to_fetch = []

    for base_url in TARGET_URLS:
        parts = base_url.rstrip("/").split("/")
        category_slug = parts[-2] if len(parts) >= 2 else base_url
        print(f"\nScanning category: {category_slug}")

        if "mini-gt-blister-pack" in base_url:
            product_type = "Blister"
        elif "mini-gt/MTY1" in base_url:
            product_type = "Box"
        else:
            product_type = "Unknown"

        page = 1
        while True:
            if not is_monitoring_time():
                now_str = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{now_str} IST] Outside monitoring hours (9 AM – 10 PM). Sleeping for 5 min...")
                time.sleep(300)  # sleep 5 minutes
                continue

            url = base_url if page == 1 else f"{base_url}?page={page}"
            html = fetch_page(url)
            if html is None:
                break

            soup = BeautifulSoup(html, "html.parser")
            cards = soup.select(".show-product-small-bx")
            if not cards:
                break

            urls_to_fetch.append((url, product_type))
            page += 1

    print(f"\nTotal pages found across all categories: {len(urls_to_fetch)}")
    print("Starting parallel page fetching...")

    with ThreadPoolExecutor(max_workers=8) as executor:
        future_to_url = {executor.submit(fetch_page, item[0]): item for item in urls_to_fetch}
        for future in as_completed(future_to_url):
            url, product_type = future_to_url[future]
            try:
                html = future.result()
                if html is None:
                    continue
                page_products = parse_products(html, product_type)
                for prod_url, prod in page_products.items():
                    all_products[prod_url] = prod
            except Exception as e:
                print(f"Error processing page {url}: {e}")

    print(f"\nTotal products found across all pages: {len(all_products)}")
    return all_products

# --------------------------
# NOTIFICATION FUNCTIONS
# --------------------------
def notify_new_product(product: Dict[str, Any]) -> None:
    name = product.get("name", "Unknown")
    price = product.get("price", "Unknown")
    stock = product.get("stock_status", "Unknown")
    qty = product.get("quantity")
    url = product.get("url", "")
    prod_type = product.get("type", "Unknown")
    detected_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    image_url = product.get("image_url")

    print("\nNEW LISTING DETECTED")
    print(f"Name : {name}")
    print(f"Type : {prod_type}")
    print(f"Price: {price}")
    print(f"Stock: {stock}")
    if qty is not None:
        print(f"Qty  : {qty}")
    print(f"Detected At: {detected_at}")
    print(f"Link : {url}\n")

    if name in alerted_names:
        return
    if qty is None or qty <= 0:
        print("Skipping alert: product not in stock.\n")
        return

    lines = [
        "🚨 NEW LISTING",
        "",
        f"Name: {name}",
        f"Type: {prod_type}",
        f"Price: {price}",
        f"Stock: {stock}",
    ]
    if qty is not None:
        lines.append(f"Qty: {qty}")
    lines.extend(["", f"Detected At: {detected_at}", "", "Link:", url])

    if image_url:
        send_telegram_photo("\n".join(lines), image_url)
    else:
        send_telegram_alert("\n".join(lines))

    alerted_names.add(name)
    save_alerted_names(alerted_names)

def notify_restock(old: Dict[str, Any], new: Dict[str, Any]) -> None:
    name = new.get("name", "Unknown")
    old_stock = old.get("stock_status", "Unknown")
    new_stock = new.get("stock_status", "Unknown")
    url = new.get("url", "")
    prod_type = new.get("type", "Unknown")
    detected_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    image_url = new.get("image_url")

    print("\nRESTOCK ALERT")
    print(f"Name      : {name}")
    print(f"Old Stock : {old_stock}")
    print(f"New Stock : {new_stock}")
    print(f"Type      : {prod_type}")
    print(f"Detected At: {detected_at}")
    print(f"Link      : {url}\n")

    lines = [
        "🚨 RESTOCK ALERT",
        "",
        f"Name: {name}",
        f"Old Stock: {old_stock}",
        f"New Stock: {new_stock}",
        f"Type: {prod_type}",
        "",
        f"Detected At: {detected_at}",
        "",
        "Link:",
        url,
    ]
    if image_url:
        send_telegram_photo("\n".join(lines), image_url)
    else:
        send_telegram_alert("\n".join(lines))

# def notify_quantity_change(old: Dict[str, Any], new: Dict[str, Any]) -> None:
#     old_qty = old.get("quantity")
#     new_qty = new.get("quantity")
#     if not (isinstance(old_qty, int) and isinstance(new_qty, int)):
#         return
#     if old_qty != 0 or new_qty <= 0:
#         return

#     name = new.get("name", "Unknown")
#     url = new.get("url", "")
#     prod_type = new.get("type", "Unknown")
#     detected_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
#     image_url = new.get("image_url")

#     print("\nQUANTITY RESTOCK")
#     print(f"Name   : {name}")
#     print(f"Type   : {prod_type}")
#     print(f"Old Qty: {old_qty}")
#     print(f"New Qty: {new_qty}")
#     print(f"Detected At: {detected_at}")
#     print(f"Link   : {url}\n")

#     lines = [
#         "🚨 QUANTITY RESTOCK",
#         "",
#         f"Name: {name}",
#         f"Type: {prod_type}",
#         f"Old Qty: {old_qty}",
#         f"New Qty: {new_qty}",
#         "",
#         f"Detected At: {detected_at}",
#         "",
#         "Link:",
#         url,
#     ]
#     if image_url:
#         send_telegram_photo("\n".join(lines), image_url)
#     else:
#         send_telegram_alert("\n".join(lines))

def notify_sold_out(old: Dict[str, Any], new: Dict[str, Any]) -> None:
    """
    Notify when a product that was in stock is now sold out (quantity 0).
    """
    old_qty = old.get("quantity")
    new_qty = new.get("quantity")

    # Only trigger if product was in stock and now 0
    if not (isinstance(old_qty, int) and isinstance(new_qty, int)):
        return
    if old_qty <= 0 or new_qty != 0:
        return  # skip if it wasn't in stock before or not sold out now

    name = new.get("name", "Unknown")
    url = new.get("url", "")
    prod_type = new.get("type", "Unknown")
    detected_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    stock_status = new.get("stock_status", "Sold Out")

    print("\nSOLD OUT ALERT")
    print(f"Name   : {name}")
    print(f"Type   : {prod_type}")
    print(f"Qty    : {new_qty}")
    print(f"Detected At: {detected_at}")
    print(f"Link   : {url}\n")

    lines = [
        "⚠️ SOLD OUT ALERT",
        "",
        f"Name: {name}",
        f"Type: {prod_type}",
        f"Stock Status: {stock_status}",
        "",
        f"Detected At: {detected_at}",
        "",
        "Link:",
        url,
    ]

    image_url = new.get("image_url")
    if image_url:
        send_telegram_photo("\n".join(lines), image_url)
    else:
        send_telegram_alert("\n".join(lines))

# --------------------------
# MONITOR FUNCTION
# --------------------------
def monitor() -> None:
    print("Starting monitor for configured categories:")
    for url in TARGET_URLS:
        print(f"  - {url}")
    print(f"Checking every {CHECK_INTERVAL_SECONDS} seconds...\n")

    global alerted_names
    alerted_names = load_alerted_names()
    print(f"Loaded {len(alerted_names)} previously alerted product names from {ALERTS_FILE}.\n")

    previous_products = load_previous_products()
    print(f"Loaded {len(previous_products)} previously saved products from {PRODUCTS_FILE}.\n")

    try:
        while True:
            current_products = fetch_all_products()
            detected_count = len(current_products)

            # previous_names = {v["name"]: v for v in previous_products.values()} # Only name as key
            # current_names = {v["name"]: v for v in current_products.values()}
            previous_names = {f'{v["name"]}|{v["type"]}': v for v in previous_products.values()} # Name and type as key
            current_names = {f'{v["name"]}|{v["type"]}': v for v in current_products.values()}

            changes_detected = False

            # NEW LISTINGS
            new_names = set(current_names.keys()) - set(previous_names.keys())
            for name in sorted(new_names):
                product = current_names[name]
                notify_new_product(product)
                changes_detected = True
            
            # Skip missing products — do not assume sold out
            missing_names = set(previous_names.keys()) - set(current_names.keys())
            for name in missing_names:
                old = previous_names[name]
                if name not in alerted_names:
                    # Only alert if it was recently seen (optional: add grace period)
                    last_seen_str = old.get("last_seen")
                    if last_seen_str:
                        last_seen_dt = datetime.fromisoformat(last_seen_str)
                        time_diff = (datetime.now() - last_seen_dt).total_seconds()
                    else:
                        time_diff = 0
                    if time_diff < 24 * 3600:
                        new_sold_out = old.copy()
                        new_sold_out["quantity"] = 0
                        new_sold_out["stock_status"] = "Sold Out"
                        notify_sold_out(old, new_sold_out)
                        alerted_names.add(name)

            for name in sorted(set(previous_names.keys()) & set(current_names.keys())):
                old = previous_names[name]
                new = current_names[name]

                old_stock = str(old.get("stock_status", ""))
                new_stock = str(new.get("stock_status", ""))

                if "sold out" in old_stock.lower() and "in stock" in new_stock.lower():
                    notify_restock(old, new)
                    changes_detected = True

                notify_sold_out(old, new)

            save_products(current_products)
            previous_products = copy.deepcopy(current_products)
            print(f"Scan completed - {detected_count} products checked.\n")
            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nMonitoring stopped by user.")

# --------------------------
# TEST BLOCK
# --------------------------
if __name__ == "__main__":
    # To run full monitor, uncomment:
    monitor()

    # Real product test
    # print("\n===== RUNNING TEST ALERTS =====\n")

    # test_url = "https://www.karzanddolls.com/product/mini-gt/mini-gt-shelby-gt500-dragon-snake-concept-blackgold%C2%A0/82014a1efeab3a92d1a48f7784604b8783c3c9e561e169330e68ef954bc0430337a967c5d7d15b45a05101c7393ad9a9cda8844f8fc00cfd3bcd7a5a71f68109LsVSmuou6WXaYJgRSf7b9H4oxaIJ_w7g+mQMXMt3S54-"

    # # NEW LISTING TEST
    # new_listing = {
    #     "name": "LB-WORKS FORD MUSTANG TRIPLE YELLOW",
    #     "type": "Box",
    #     "price": "Rs. 1199",
    #     "stock_status": "In stock (qty 107)",
    #     "quantity": 107,
    #     "url": test_url,
    #     "image_url": fetch_product_image(test_url)
    # }
    # print("--- NEW LISTING ALERT ---")
    # notify_new_product(new_listing)

    # # RESTOCK TEST (sold out -> in stock)
    # old_restock = {
    #     "name": "LB-WORKS FORD MUSTANG TRIPLE YELLOW",
    #     "type": "Box",
    #     "stock_status": "Sold Out",
    #     "quantity": 0,
    #     "url": test_url,
    # }
    # new_restock = {
    #     "name": "LB-WORKS FORD MUSTANG TRIPLE YELLOW",
    #     "type": "Box",
    #     "stock_status": "In stock (qty 107)",
    #     "quantity": 107,
    #     "url": test_url,
    #     "image_url": fetch_product_image(test_url)
    # }
    # print("--- RESTOCK ALERT ---")
    # notify_restock(old_restock, new_restock)

    # SOLD OUT TEST
    # old_sold_out = {
    #     "name": "LB-WORKS FORD MUSTANG TRIPLE YELLOW",
    #     "type": "Box",
    #     "stock_status": "In stock (qty 107)",
    #     "quantity": 107,
    #     "url": test_url,
    # }
    # new_sold_out = {
    #     "name": "LB-WORKS FORD MUSTANG TRIPLE YELLOW",
    #     "type": "Box",
    #     "stock_status": "Sold Out",
    #     "quantity": 0,
    #     "url": test_url,
    # }
    # print("--- SOLD OUT ALERT ---")
    # notify_sold_out(old_sold_out, new_sold_out)

    # print("\n===== TESTING COMPLETED =====\n")