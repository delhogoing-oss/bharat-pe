import logging
import re
import time
import requests
import os
import asyncio
import cloudscraper
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Any
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

MOBILE_STATE, OTP_STATE = range(2)


# Load environment variables
load_dotenv()

# Admin ID from environment variable (with fallback for safety during migration)
ADMIN_ID = int(os.getenv("ADMIN_ID", "5664327265"))
LOG_GROUP_ID = os.getenv("LOG_GROUP_ID") # Telegram Group ID to send logs to

async def notify_log_group(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Helper to send notifications to the log group."""
    if LOG_GROUP_ID:
        try:
            await context.bot.send_message(chat_id=LOG_GROUP_ID, text=f"🔔 *SYSTEM LOG*\n\n{message}", parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send to log group: {e}")

class BharatPeClient:
    def __init__(self, mobile: str):
        self.mobile = mobile
        # Use cloudscraper to bypass Cloudflare protection
        self.session = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'desktop': True
            }
        )
        self.base_url = "https://enterprise.bharatpe.in"
        self.deposit_api = "https://api-deposit.bharatpe.in"
        self.txn_api = "https://payments-tesseract.bharatpe.in"
        self.is_logged_in = False
        self.merchant_id = None
        self.token = None
        self.otp_uuid = None
        self.csrf_token = None
        self.xsrf_token = None
        self.merchant_name = "Merchant"

        # Headers are now managed primarily by cloudscraper, 
        # but we add specific BharatPe requirements
        self.session.headers.update({
            "accept": "application/json, text/javascript, */*; q=0.01",
            "accept-language": "en-US,en;q=0.9",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
            "origin": self.base_url,
            "referer": f"{self.base_url}/",
            "sec-ch-ua-mobile": "?0",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "x-requested-with": "XMLHttpRequest",
        })

    def __getstate__(self):
        """Prepare for pickling: extract session state."""
        state = self.__dict__.copy()
        state["_session_cookies"] = requests.utils.dict_from_cookiejar(self.session.cookies)
        state["_session_headers"] = dict(self.session.headers)
        del state["session"]
        return state

    def __setstate__(self, state):
        """Restore after unpickling."""
        cookies = state.pop("_session_cookies", {})
        headers = state.pop("_session_headers", {})
        self.__dict__.update(state)
        # Reconstruct scraper session
        self.session = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'windows',
                'desktop': True
            }
        )
        self.session.cookies.update(cookies)
        self.session.headers.update(headers)

    def _get_csrf_token(self, use_cache_buster: bool = False) -> bool:
        """Load main page and extract both HTML token and XSRF cookie."""
        try:
            url = self.base_url
            if use_cache_buster:
                url += f"?_={int(time.time())}"
            resp = self.session.get(url, timeout=10, headers={
                "Cache-Control": "no-cache, no-store, must-revalidate"
            })
            logger.debug(f"GET {url} -> status {resp.status_code}")
            logger.debug(f"Cookies after GET: {dict(self.session.cookies)}")
            
            if resp.status_code != 200:
                logger.error(f"Failed to load main page: {resp.status_code}")
                logger.error(f"Main page response: {resp.text[:500]}")
                return False
            
            # 1. Extract HTML token (_token)
            html_token = None
            match = re.search(r'<input type="hidden" name="_token" value="([^"]+)"', resp.text)
            if match:
                html_token = match.group(1)
            else:
                match = re.search(r'<meta name="csrf-token" content="([^"]+)"', resp.text)
                if match:
                    html_token = match.group(1)
            
            if html_token:
                self.csrf_token = html_token
                logger.info(f"HTML CSRF token extracted: {self.csrf_token}")
            
            # 2. Extract XSRF-TOKEN cookie (often used in X-XSRF-TOKEN header)
            xsrf_cookie = self.session.cookies.get("XSRF-TOKEN")
            if xsrf_cookie:
                import urllib.parse
                self.xsrf_token = urllib.parse.unquote(xsrf_cookie)
                logger.info(f"XSRF token from cookie: {self.xsrf_token}")
            
            if not self.csrf_token and not self.xsrf_token:
                logger.error("No token found in page or cookies")
                return False
                
            return True
        except Exception as e:
            logger.error(f"CSRF fetch error: {e}")
            return False

    def request_otp(self) -> bool:
        """Send OTP and store UUID. Flexible enough to handle redirects or varying JSON."""
        if not self._get_csrf_token(use_cache_buster=True):
            return False

        url = f"{self.base_url}/v1/api/user/requestotp"
        data = {"mobile": self.mobile, "_token": self.csrf_token}
        
        extra_headers = {
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01"
        }
        if self.xsrf_token:
            extra_headers["X-XSRF-TOKEN"] = self.xsrf_token
        
        logger.info(f"POST {url} with data {data}")
        try:
            # Allow redirects because some servers redirect to a 'verify' page on success
            resp = self.session.post(url, data=data, headers=extra_headers, allow_redirects=True)
            logger.debug(f"OTP Final status: {resp.status_code}")
            logger.debug(f"OTP Final URL: {resp.url}")
            
            # If we were redirected to a page containing 'verify', it's likely a success
            if "verify" in resp.url.lower():
                logger.info("Redirected to verify page - treating as success")
                # Try to extract UUID from URL if present
                match = re.search(r'uuid=([^&]+)', resp.url)
                if match:
                    self.otp_uuid = match.group(1)
                    logger.info(f"UUID extracted from URL: {self.otp_uuid}")
                return True

            if resp.status_code != 200:
                logger.error(f"Unexpected status: {resp.status_code}")
                logger.error(f"Response text: {resp.text[:500]}")
                return False
            
            # Try to parse as JSON
            try:
                result = resp.json()
                logger.info(f"OTP response JSON: {result}")
                
                # Check for various success keys
                success = result.get("success") or result.get("status") == "success" or result.get("status") is True
                
                if success:
                    self.otp_uuid = result.get("uuid") or result.get("data", {}).get("uuid")
                    if not self.otp_uuid:
                        # Check cookies for UUID
                        self.otp_uuid = self.session.cookies.get("otp_uuid") or self.session.cookies.get("uuid")
                    
                    if not self.otp_uuid:
                        logger.warning("OTP success but UUID not found in JSON or cookies. Verification might fail.")
                    else:
                        logger.info(f"OTP sent successfully, UUID: {self.otp_uuid}")
                    
                    time.sleep(2)
                    return True
                else:
                    logger.error(f"OTP request failed according to JSON: {result}")
                    return False
            except Exception:
                # If not JSON, but status is 200 and we were redirected or it looks like success
                if resp.status_code == 200 and ("otp" in resp.text.lower() or "sent" in resp.text.lower()):
                    logger.info("Non-JSON success response detected")
                    return True
                return False

        except Exception as e:
            logger.error(f"OTP request exception: {e}")
            return False

    def verify_otp(self, otp: str, retry_on_419: bool = True) -> bool:
        """Verify OTP with session persistence and 419 retry logic."""
        logger.debug(f"verify_otp called. Current UUID: {self.otp_uuid}, Logged In: {self.is_logged_in}")
        
        url = f"{self.base_url}/v1/api/user/verifyotp"
        data = {
            "mobile": self.mobile,
            "otp": otp,
            "_token": self.csrf_token,
        }
        if self.otp_uuid:
            data["uuid"] = self.otp_uuid
        
        extra_headers = {
            "X-CSRF-TOKEN": self.csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": f"{self.base_url}/",
        }
        if self.xsrf_token:
            extra_headers["X-XSRF-TOKEN"] = self.xsrf_token

        logger.info(f"POST {url} with data {data}")
        try:
            logger.debug(f"Cookies before verify POST: {dict(self.session.cookies)}")
            resp = self.session.post(url, data=data, headers=extra_headers)
            logger.debug(f"Verify response status: {resp.status_code}")
            
            # Handle Laravel 'Page Expired' error
            if resp.status_code == 419 and retry_on_419:
                logger.warning("Received 419 - CSRF token expired. Refreshing token and retrying...")
                if self._get_csrf_token(use_cache_buster=True):
                    # Update data with new token
                    data["_token"] = self.csrf_token
                    extra_headers["X-CSRF-TOKEN"] = self.csrf_token
                    if self.xsrf_token:
                        extra_headers["X-XSRF-TOKEN"] = self.xsrf_token
                    return self.verify_otp(otp, retry_on_419=False)
            
            if resp.status_code != 200:
                logger.error(f"Verify failed with status {resp.status_code}")
                logger.debug(f"Verify response text: {resp.text[:1000]}")
                return False

            result = resp.json()
            logger.info(f"Verify response JSON keys: {list(result.keys())}")
            if result.get("success"):
                self.is_logged_in = True
                # Extract accessToken using all possible keys found in BharatPe APIs
                self.token = (
                    result.get("data", {}).get("accessToken") or 
                    result.get("data", {}).get("token") or
                    result.get("token") or
                    result.get("accessToken")
                )
                logger.info(f"Token extracted: {bool(self.token)}")
                if self.token:
                    self.session.headers.update({"token": self.token})
                self._fetch_merchant_id()
                return True
            else:
                logger.error(f"Verification failed in JSON: {result}")
                return False
        except Exception as e:
            logger.error(f"OTP verify exception: {e}")
            return False

    def _fetch_merchant_id(self):
        if self.token:
            try:
                url = f"{self.deposit_api}/bharatpe-account/v1/account"
                headers = {"token": self.token}
                resp = self.session.get(url, headers=headers)
                if resp.ok:
                    data = resp.json()
                    m_id = data.get("data", {}).get("merchantId")
                    m_name = data.get("data", {}).get("merchantName")
                    if m_id:
                        self.merchant_id = str(m_id)
                        if m_name:
                            self.merchant_name = m_name
                        logger.info(f"Fetched Merchant ID: {self.merchant_id}, Name: {self.merchant_name}")
                        return
            except Exception as e:
                logger.error(f"Merchant ID fetch error: {e}")
        
        self.merchant_id = "71038550"
        logger.info(f"Using fallback merchant ID: {self.merchant_id}")

    def get_account_details(self) -> Optional[Dict]:
        if not self.is_logged_in or not self.token:
            return None
        url = f"{self.deposit_api}/bharatpe-account/v1/account"
        try:
            resp = self.session.get(url)
            resp.raise_for_status()
            data = resp.json()
            if data.get("data"):
                self.merchant_name = data["data"].get("merchantName", self.merchant_name)
            return data
        except Exception as e:
            logger.error(f"Account error: {e}")
            return None

    def get_transactions(self, days_back: int = 30, limit: int = 20) -> Optional[List[Dict]]:
        if not self.is_logged_in or not self.token or not self.merchant_id or self.merchant_id == "None":
            return None
            
        end_date = int(time.time() * 1000)
        start_date = int((time.time() - (days_back * 24 * 3600)) * 1000)
        
        params = {
            "module": "PAYMENT_QR",
            "merchantId": self.merchant_id,
            "sDate": str(start_date),
            "eDate": str(end_date),
            "pageSize": str(limit),
            "pageCount": "0",
            "isFromOtDashboard": "1",
        }
        
        url = f"{self.txn_api}/api/v1/merchant/transactions"
        try:
            txn_headers = self.session.headers.copy()
            txn_headers.update({"token": self.token})
            resp = self.session.get(url, params=params, headers=txn_headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            
            txns = []
            if "data" in data and isinstance(data["data"], dict):
                txns = data["data"].get("transactions", data["data"].get("list", []))
            elif "data" in data and isinstance(data["data"], list):
                txns = data["data"]
            else:
                txns = data.get("list", data.get("transactions", []))
            return txns
        except Exception as e:
            logger.error(f"Transaction error: {e}")
            return None

    def get_stats(self) -> Dict[str, Any]:
        """Calculate stats for today, week, and month."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today_start - timedelta(days=now.weekday())
        month_start = today_start.replace(day=1)

        # Fetch enough transactions to cover the month (approximate)
        txns = self.get_transactions(days_back=31, limit=100) or []
        
        stats = {
            "today_total": 0.0,
            "today_count": 0,
            "week_total": 0.0,
            "week_count": 0,
            "month_total": 0.0,
            "month_count": 0,
            "last_txn": None
        }

        for txn in txns:
            ts = txn.get('paymentTimestamp') or txn.get('transactionTime')
            if not ts: continue
            
            try:
                if isinstance(ts, (int, float)) and ts > 10**10:
                    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                else:
                    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
                
                amount = float(txn.get('amount', 0))
                status = txn.get('status', '').upper()
                
                if status != "SUCCESS" and status != "COMPLETED":
                    # Some APIs use different success status strings
                    if status not in ["SUCCESS", "COMPLETED", "SETTLED"]:
                        continue

                if dt >= today_start:
                    stats["today_total"] += amount
                    stats["today_count"] += 1
                    if not stats["last_txn"]:
                        stats["last_txn"] = txn
                
                if dt >= week_start:
                    stats["week_total"] += amount
                    stats["week_count"] += 1
                
                if dt >= month_start:
                    stats["month_total"] += amount
                    stats["month_count"] += 1
            except:
                continue
        
        return stats

    def find_utr(self, utr: str) -> Optional[Dict]:
        """Search for a UTR in recent transactions."""
        txns = self.get_transactions(days_back=30, limit=50) or []
        for txn in txns:
            txn_utr = txn.get('bankReferenceNo') or txn.get('referenceId')
            if txn_utr and str(txn_utr).strip() == utr.strip():
                return txn
        return None


# ---------- Telegram Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    client = context.user_data.get("client")
    
    if client and client.is_logged_in:
        msg = (
            f"👋 Welcome back, *{client.merchant_name}*!\n\n"
            "📱 *Your Account*: `{}`\n\n"
            "📜 *Commands*:\n"
            "/stats - 📊 Today/Weekly/Monthly Stats\n"
            "/check - 🕒 Recent Transactions\n"
            "/account - 💰 Account Balance\n"
            "/utr <id> - 🔍 Check UTR\n"
            "/logout - 🚪 Exit session"
        ).format(client.mobile)
        
        if user_id == ADMIN_ID:
            msg += "\n\n👑 *Admin Commands*:\n/admin - 🛠 Admin Panel\n/broadcast <msg> - 📢 Send to all"
            
        await update.message.reply_text(msg, parse_mode="Markdown")
        return ConversationHandler.END

    await update.message.reply_text("Welcome! Send your 10-digit BharatPe mobile number to login.\n/cancel to abort.")
    return MOBILE_STATE

async def receive_mobile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    mobile = update.message.text.strip()
    if not mobile.isdigit() or len(mobile) != 10:
        await update.message.reply_text("Invalid number. Send 10 digits.")
        return MOBILE_STATE
    
    client = BharatPeClient(mobile)
    await update.message.reply_text("Requesting OTP...")
    
    if client.request_otp():
        context.user_data["client"] = client
        await update.message.reply_text(f"✅ OTP sent to {mobile}\nEnter OTP:")
        return OTP_STATE
    else:
        await update.message.reply_text("❌ Failed to send OTP. Check number or try later.")
        return ConversationHandler.END

async def receive_otp(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    otp = update.message.text.strip()
    client = context.user_data.get("client")
    if not client:
        await update.message.reply_text("Session expired. Use /start")
        return ConversationHandler.END
    
    await update.message.reply_text("Verifying OTP...")
    if client.verify_otp(otp):
        await update.message.reply_text(
            "✅ Login successful!\n\n"
            "Use /stats to see your performance or /check for transactions.",
            parse_mode="Markdown"
        )
        # Notify log group
        await notify_log_group(context, 
            f"👤 *New Login*\n"
            f"🏪 Merchant: {client.merchant_name}\n"
            f"📱 Mobile: `{client.mobile}`\n"
            f"🆔 User ID: `{update.effective_user.id}`"
        )
        return ConversationHandler.END
    else:
        await update.message.reply_text("❌ Invalid OTP. Try again or /cancel")
        return OTP_STATE

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data.get("client")
    if not client or not client.is_logged_in:
        await update.message.reply_text("Not logged in. Use /start")
        return
    
    await update.message.reply_text("📊 Calculating stats...")
    data = client.get_stats()
    
    msg = f"📊 *Payment Report: {client.merchant_name}*\n\n"
    msg += f"📅 *Today*: ₹{data['today_total']:,.2f} ({data['today_count']} txns)\n"
    msg += f"🗓 *This Week*: ₹{data['week_total']:,.2f} ({data['week_count']} txns)\n"
    msg += f"🗓 *This Month*: ₹{data['month_total']:,.2f} ({data['month_count']} txns)\n\n"
    
    if data['last_txn']:
        txn = data['last_txn']
        amount = txn.get('amount', '0')
        payer = txn.get('payerName', 'Unknown')
        utr = txn.get('bankReferenceNo') or txn.get('referenceId') or 'N/A'
        msg += f"✨ *Last Transaction Today*:\n💰 ₹{amount} from {payer}\n🆔 `{utr}`"
    else:
        msg += "✨ *No transactions yet today.*"
        
    await update.message.reply_text(msg, parse_mode="Markdown")

async def utr_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data.get("client")
    if not client or not client.is_logged_in:
        await update.message.reply_text("Not logged in.")
        return
        
    if not context.args:
        await update.message.reply_text("Usage: /utr <transaction_id_or_utr>")
        return
        
    utr_to_find = context.args[0]
    await update.message.reply_text(f"🔍 Searching for UTR: `{utr_to_find}`...", parse_mode="Markdown")
    
    txn = client.find_utr(utr_to_find)
    if txn:
        amount = txn.get('amount', 'N/A')
        status = txn.get('status', 'N/A')
        payer = txn.get('payerName', 'Unknown')
        ts = txn.get('paymentTimestamp') or txn.get('transactionTime')
        time_str = 'N/A'
        if ts:
            try:
                dt = datetime.fromtimestamp(ts / 1000 if ts > 10**10 else ts, tz=timezone.utc)
                ist_dt = dt + timedelta(hours=5, minutes=30)
                time_str = ist_dt.strftime('%d %b %Y, %H:%M')
            except: pass
            
        msg = (
            f"✅ *Transaction Found*\n\n"
            f"💰 *Amount*: ₹{amount}\n"
            f"👤 *Payer*: {payer}\n"
            f"📊 *Status*: {status}\n"
            f"🕒 *Time*: {time_str}\n"
            f"🆔 *UTR*: `{utr_to_find}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("❌ Transaction not found in recent history.")

# --- Admin Handlers ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    all_user_data = context.application.persistence.get_user_data()
    if not all_user_data:
        await update.message.reply_text("No users found.")
        return

    msg = "👑 *Admin Dashboard - All Users*\n\n"
    count = 0
    for uid, data in all_user_data.items():
        client = data.get("client")
        if client and client.is_logged_in:
            count += 1
            msg += f"👤 *User ID*: `{uid}`\n"
            msg += f"📱 *Mobile*: `{client.mobile}`\n"
            msg += f"🏪 *Merchant*: {client.merchant_name}\n"
            msg += f"🔍 /inspect_{uid}\n"
            msg += "------------------\n"
            
            if count % 10 == 0:
                await update.message.reply_text(msg, parse_mode="Markdown")
                msg = ""
    
    if msg:
        await update.message.reply_text(f"{msg}\nTotal Logged-in Users: {count}", parse_mode="Markdown")

async def inspect_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
        
    # Handle both /inspect_123 and /inspect 123
    user_id_str = ""
    if context.args:
        user_id_str = context.args[0]
    else:
        # Check if it was a command like /inspect_123
        command = update.message.text.split()[0]
        if "_" in command:
            user_id_str = command.split("_")[1]
            
    if not user_id_str or not user_id_str.isdigit():
        await update.message.reply_text("Usage: /inspect <user_id>")
        return
        
    target_uid = int(user_id_str)
    all_user_data = context.application.persistence.get_user_data()
    target_data = all_user_data.get(target_uid)
    
    if not target_data or "client" not in target_data:
        await update.message.reply_text("User not found or no session.")
        return
        
    client = target_data["client"]
    await update.message.reply_text(f"🔍 Inspecting User `{target_uid}` ({client.merchant_name})...", parse_mode="Markdown")
    
    acc = client.get_account_details()
    stats = client.get_stats()
    
    msg = f"👤 *User Inspection: {target_uid}*\n\n"
    msg += f"📱 *Mobile*: `{client.mobile}`\n"
    msg += f"🏪 *Merchant*: {client.merchant_name}\n"
    msg += f"🆔 *M-ID*: `{client.merchant_id}`\n\n"
    
    if acc:
        data = acc.get("data", {})
        msg += f"💰 *Balance*: ₹{data.get('balance','0')}\n"
        msg += f"📊 *Status*: {data.get('accountStatus','Active')}\n\n"
        
    msg += "📈 *Performance*:\n"
    msg += f"📅 Today: ₹{stats['today_total']:,.2f} ({stats['today_count']} txns)\n"
    msg += f"🗓 Week: ₹{stats['week_total']:,.2f}\n"
    msg += f"🗓 Month: ₹{stats['month_total']:,.2f}\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
        
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
        
    text = " ".join(context.args)
    all_user_data = context.application.persistence.get_user_data()
    
    success = 0
    fail = 0
    await update.message.reply_text(f"📢 Starting broadcast to {len(all_user_data)} users...")
    
    for uid in all_user_data.keys():
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 *BROADCAST*\n\n{text}", parse_mode="Markdown")
            success += 1
        except Exception:
            fail += 1
            
    await update.message.reply_text(f"✅ Broadcast complete.\nSuccess: {success}\nFailed: {fail}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Logged out successfully.")

async def check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data.get("client")
    if not client or not client.is_logged_in:
        await update.message.reply_text("Not logged in. Use /start")
        return
    await update.message.reply_text("Fetching latest transactions...")
    txn_list = client.get_transactions(limit=10)
    if txn_list is None:
        await update.message.reply_text("Error fetching transactions.")
        return
    if txn_list:
        # Report the latest transaction to log group if it's new (simple check)
        latest = txn_list[0]
        utr = latest.get('bankReferenceNo') or latest.get('referenceId') or 'N/A'
        amount = latest.get('amount', '0')
        payer = latest.get('payerName', 'Unknown')
        await notify_log_group(context, 
            f"💰 *Transaction Checked*\n"
            f"🏪 Merchant: {client.merchant_name}\n"
            f"👤 Payer: {payer}\n"
            f"💵 Amount: ₹{amount}\n"
            f"🆔 UTR: `{utr}`"
        )

    msg = f"📊 *Latest Transactions: {client.merchant_name}*\n\n"
    for txn in txn_list[:10]:
        amount = txn.get('amount', 'N/A')
        status = txn.get('status', 'N/A')
        payer = txn.get('payerName', 'Unknown')
        
        ts = txn.get('paymentTimestamp') or txn.get('transactionTime')
        time_str = 'N/A'
        if ts:
            try:
                dt = datetime.fromtimestamp(ts / 1000 if ts > 10**10 else ts, tz=timezone.utc)
                ist_dt = dt + timedelta(hours=5, minutes=30)
                time_str = ist_dt.strftime('%d %b, %H:%M')
            except: pass
                
        utr = txn.get('bankReferenceNo') or txn.get('referenceId') or 'N/A'
        msg += f"💰 ₹{amount} | {status}\n👤 {payer}\n🕒 {time_str}\n🆔 `{utr}`\n——————————\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client = context.user_data.get("client")
    if not client or not client.is_logged_in:
        await update.message.reply_text("Not logged in.")
        return
    acc = client.get_account_details()
    if not acc:
        await update.message.reply_text("Failed to fetch account.")
        return
    data = acc.get("data", {})
    msg = (
        f"🏪 *{data.get('merchantName','Merchant')}*\n"
        f"💰 Balance: ₹{data.get('balance','0')}\n"
        f"📊 Status: {data.get('accountStatus','Active')}\n"
        f"📱 Mobile: {client.mobile}"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

def main():
    # Token from environment variable
    TOKEN = os.getenv("BOT_TOKEN")
    
    if not TOKEN:
        logger.error("BOT_TOKEN not found in environment variables!")
        return
    
    # Enable persistence
    persistence = PicklePersistence(filepath="bot_session.pickle")
    
    # Build application with explicit defaults to avoid version-specific builder issues
    app = (
        Application.builder()
        .token(TOKEN)
        .persistence(persistence)
        .build()
    )
    
    # Login Conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MOBILE_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_mobile)],
            OTP_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_otp)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    
    app.add_handler(conv)
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("account", account))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("utr", utr_check))
    
    # Admin handlers
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("inspect", inspect_user))
    app.add_handler(MessageHandler(filters.Regex(r"^/inspect_\d+$"), inspect_user))
    
    logger.info("Bot started.")
    app.run_polling()

if __name__ == "__main__":
    main()
