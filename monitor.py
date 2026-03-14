import json
import os
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Set

import requests
from bs4 import BeautifulSoup
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

# Telegram configuration from environment
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise ValueError("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")

# In-memory set of product names that have already triggered Telegram alerts
alerted_names: Set[str] = set()


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


def fetch_page(url: str) -> Optional[str]:
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        print(f"[{datetime.now().isoformat()}] Network error while fetching page: {exc}")
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

        # Prefer full product name from image alt (usually not truncated)
        img_tag = card.select_one("img[alt]")
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
        }

    return products


def fetch_all_products() -> Dict[str, Dict[str, Any]]:
    """
    Fetch and parse products from all configured category URLs,
    handling pagination for each category.

    For each base URL in TARGET_URLS:
      - page 1: base URL
      - page 2+: base URL + ?page=N

    Products from all categories are merged into a single dict keyed by URL.
    If the same URL appears in more than one category, the first occurrence is kept.
    """
    all_products: Dict[str, Dict[str, Any]] = {}

    for base_url in TARGET_URLS:
        # Derive a simple category label from the URL for logging
        parts = base_url.rstrip("/").split("/")
        category_slug = parts[-2] if len(parts) >= 2 else base_url
        print(f"\nScanning category: {category_slug}")

        # Determine product type based on category URL
        if "mini-gt-blister-pack" in base_url:
            product_type = "Blister"
        elif "mini-gt/MTY1" in base_url or "mini-gt/MTY1" in base_url.replace("+", " "):
            product_type = "Box"
        else:
            product_type = "Unknown"

        page = 1
        while True:
            print(f"  Scanning page {page}...")
            url = base_url if page == 1 else f"{base_url}?page={page}"
            html = fetch_page(url)
            if html is None:
                print(f"  Failed to fetch page {page}, stopping pagination for this category.")
                break

            page_products = parse_products(html, product_type)
            if not page_products:
                print(f"  No products found on page {page}, stopping pagination for this category.")
                break

            # Merge products, but do not overwrite duplicates from other categories
            for prod_url, prod in page_products.items():
                if prod_url in all_products:
                    # Duplicate URL across categories/pages – keep the first one
                    continue
                all_products[prod_url] = prod

            page += 1

    print(f"\nTotal products found across all categories and pages: {len(all_products)}")
    return all_products


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


def notify_new_product(product: Dict[str, Any]) -> None:
    name = product.get("name", "Unknown")
    price = product.get("price", "Unknown")
    stock = product.get("stock_status", "Unknown")
    qty = product.get("quantity")
    url = product.get("url", "")
    prod_type = product.get("type", "Unknown")
    detected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
    lines.extend(
        [
            "",
            f"Detected At: {detected_at}",
            "",
            "Link:",
            url,
        ]
    )
    send_telegram_alert("\n".join(lines))

    alerted_names.add(name)
    save_alerted_names(alerted_names)


def notify_restock(old: Dict[str, Any], new: Dict[str, Any]) -> None:
    name = new.get("name", "Unknown")
    old_stock = old.get("stock_status", "Unknown")
    new_stock = new.get("stock_status", "Unknown")
    url = new.get("url", "")
    prod_type = new.get("type", "Unknown")
    detected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\nRESTOCK ALERT")
    print(f"Name      : {name}")
    print(f"Old Stock : {old_stock}")
    print(f"New Stock : {new_stock}")
    print(f"Type      : {prod_type}")
    print(f"Detected At: {detected_at}")
    print(f"Link      : {url}\n")

    if name in alerted_names:
        return

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
    send_telegram_alert("\n".join(lines))

    alerted_names.add(name)
    save_alerted_names(alerted_names)


# def notify_stock_change(old: Dict[str, Any], new: Dict[str, Any]) -> None:
#     name = new.get("name", "Unknown")
#     old_stock = old.get("stock_status", "Unknown")
#     new_stock = new.get("stock_status", "Unknown")
#     url = new.get("url", "")
#     print("\nSTOCK CHANGE")
#     print(f"Name      : {name}")
#     print(f"Old Stock : {old_stock}")
#     print(f"New Stock : {new_stock}")
#     print(f"Link      : {url}\n")

#     lines = [
#         "🚨 STOCK CHANGE",
#         "",
#         f"Name: {name}",
#         f"Old Stock: {old_stock}",
#         f"New Stock: {new_stock}",
#         "",
#         "Link:",
#         url,
#     ]
#     send_telegram_alert("\n".join(lines))


def notify_quantity_change(old: Dict[str, Any], new: Dict[str, Any]) -> None:
    """
    Only notify when old quantity is 0 and new quantity > 0
    """
    old_qty = old.get("quantity")
    new_qty = new.get("quantity")
    if not (isinstance(old_qty, int) and isinstance(new_qty, int)):
        return
    if old_qty != 0 or new_qty <= 0:
        return  # only notify when 0 -> available

    name = new.get("name", "Unknown")
    url = new.get("url", "")
    prod_type = new.get("type", "Unknown")
    detected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\nQUANTITY RESTOCK")
    print(f"Name   : {name}")
    print(f"Type   : {prod_type}")
    print(f"Old Qty: {old_qty}")
    print(f"New Qty: {new_qty}")
    print(f"Detected At: {detected_at}")
    print(f"Link   : {url}\n")

    if name in alerted_names:
        return

    lines = [
        "🚨 QUANTITY RESTOCK",
        "",
        f"Name: {name}",
        f"Type: {prod_type}",
        f"Old Qty: {old_qty}",
        f"New Qty: {new_qty}",
        "",
        f"Detected At: {detected_at}",
        "",
        "Link:",
        url,
    ]
    send_telegram_alert("\n".join(lines))

    alerted_names.add(name)
    save_alerted_names(alerted_names)


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

            previous_names = {v["name"]: v for v in previous_products.values()}
            current_names = {v["name"]: v for v in current_products.values()}

            changes_detected = False

            # NEW LISTINGS
            new_names = set(current_names.keys()) - set(previous_names.keys())
            for name in sorted(new_names):
                product = current_names[name]
                notify_new_product(product)
                changes_detected = True
            
            # PRODUCTS THAT DISAPPEARED (assume sold out)
            missing_names = set(previous_names.keys()) - set(current_names.keys())
            for name in missing_names:
                old = previous_names[name]
                if old.get("quantity") != 0:
                    print(f"\nPRODUCT REMOVED FROM LISTING (assume sold out): {name}")
                    old["quantity"] = 0
                    old["stock_status"] = "Sold Out"
                    current_products[old["url"]] = old
            
            # EXISTING PRODUCTS
            for name in sorted(set(previous_names.keys()) & set(current_names.keys())):
                old = previous_names[name]
                new = current_names[name]

                old_stock = str(old.get("stock_status", ""))
                new_stock = str(new.get("stock_status", ""))
                old_qty = old.get("quantity")
                new_qty = new.get("quantity")

                if "sold out" in old_stock.lower() and "in stock" in new_stock.lower():
                    notify_restock(old, new)
                    changes_detected = True

                # QUANTITY: only notify when 0 -> available
                notify_quantity_change(old, new)

            save_products(current_products)
            previous_products = current_products

            print(f"Scan completed - {detected_count} products checked.\n")
            time.sleep(CHECK_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nMonitoring stopped by user.")


if __name__ == "__main__":
    monitor()