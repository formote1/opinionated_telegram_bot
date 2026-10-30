import asyncio
import functools
import logging
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone

from bson import ObjectId
from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
)
from telegram import Update
from werkzeug.serving import make_server

from config import ADMIN_ID, PORT, TOKEN, logger
from database import (
    codes_col,
    group_keys_col,
    logs_col,
    prefix_labels_col,
    reminders_col,
    unlocked_groups_col,
    users_col,
)
from handlers.admin import parse_duration, resolve_target_user
from handlers.media import execute_file_delivery
from handlers.reminders import format_time_left, schedule_reminder_job
from services.access import (
    check_access_gate,
    invalidate_group_keys_cache,
    is_locked_out,
    is_user_banned,
    record_failed_attempt,
    reset_failed_attempts,
)
from services.ascii_art import SUPPORTED_ASCII_FONTS, generate_ascii_art
from utils.auth import (
    generate_auth_token,
    verify_auth_pin,
    verify_auth_token,
    verify_telegram_init_data,
)
from utils.sentinel import log_event

# Silence per-request werkzeug stdout logging to improve throughput
logging.getLogger("werkzeug").setLevel(logging.WARNING)

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

flask_app = Flask(
    __name__,
    template_folder=TEMPLATE_DIR,
    static_folder=STATIC_DIR
)
flask_app.secret_key = os.getenv("SECRET_KEY", TOKEN or "opinionated-bot-secure-session-key-32bytes")
is_https = bool(os.getenv("RENDER_EXTERNAL_URL", "").startswith("https://"))
flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
flask_app.config["SESSION_COOKIE_SAMESITE"] = "None" if is_https else "Lax"
flask_app.config["SESSION_COOKIE_SECURE"] = is_https


@flask_app.after_request
def add_security_headers(response):
    response.headers.pop("X-Frame-Options", None)
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org telegram:;"
    )
    return response


_application = None
_main_loop = None


def init_server(application, main_loop):
    """Binds the Telegram Application and event loop to the webhook & companion server."""
    global _application, _main_loop
    _application = application
    _main_loop = main_loop


def run_async(coro, timeout: float = 15.0):
    """
    Executes an async coroutine on the bot's running event loop from Flask's synchronous thread.
    Falls back gracefully when running in standalone tests or outside the active bot loop.
    """
    global _main_loop
    if _main_loop is not None and _main_loop.is_running():
        fut = asyncio.run_coroutine_threadsafe(coro, _main_loop)
        try:
            return fut.result(timeout=timeout)
        except Exception:
            fut.cancel()
            raise

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result(timeout=timeout)
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


def serialize_doc(doc):
    """Recursively converts MongoDB ObjectIds, datetimes, and dates into JSON-serializable types."""
    if doc is None:
        return None
    if isinstance(doc, dict):
        clean = {}
        for k, v in doc.items():
            if k == "_id":
                clean["id"] = str(v)
                clean["_id"] = str(v)
            elif isinstance(v, ObjectId):
                clean[k] = str(v)
            elif isinstance(v, (datetime, date)):
                clean[k] = v.isoformat()
            elif isinstance(v, (dict, list)):
                clean[k] = serialize_doc(v)
            else:
                clean[k] = v
        return clean
    if isinstance(doc, list):
        return [serialize_doc(item) for item in doc]
    if isinstance(doc, ObjectId):
        return str(doc)
    if isinstance(doc, (datetime, date)):
        return doc.isoformat()
    return doc


def get_current_user():
    """
    Retrieves authenticated user from session cookie or token (Authorization / X-Auth-Token headers).
    Returns dict {"user_id": int, "username": str, "is_admin": bool} or None.
    """
    # 1. Session cookie
    uid = session.get("user_id")
    if uid:
        return {
            "user_id": uid,
            "username": session.get("username", ""),
            "is_admin": bool(ADMIN_ID and uid == ADMIN_ID)
        }

    # 2. Authorization header: Bearer <token>
    auth_hdr = request.headers.get("Authorization", "").strip()
    token = None
    if auth_hdr.startswith("Bearer "):
        token = auth_hdr[7:].strip()
    elif auth_hdr and " " not in auth_hdr:
        token = auth_hdr

    # 3. X-Auth-Token header
    if not token:
        token = request.headers.get("X-Auth-Token", "").strip() or None

    # 4. Query parameter token
    if not token:
        token = request.args.get("token", "").strip() or None

    if token:
        user_info = verify_auth_token(token)
        if user_info:
            return user_info

    # 5. Telegram WebApp initData header / parameter
    init_data = request.headers.get("X-Telegram-Init-Data", "").strip() or request.args.get("init_data", "").strip()
    if init_data:
        user_info = verify_telegram_init_data(init_data)
        if user_info:
            return user_info

    return None


def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or not user.get("user_id"):
            return jsonify({"success": False, "error": "Authentication required. Use /web in Telegram."}), 401
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or not user.get("user_id"):
            return jsonify({"success": False, "error": "Authentication required."}), 401
        user_id = user["user_id"]
        if not ADMIN_ID or user_id != ADMIN_ID:
            return jsonify({"success": False, "error": "Admin privileges required."}), 403
        return f(*args, **kwargs)
    return decorated_function


# --- WEB COMPANION PAGES & AUTH ---

@flask_app.route('/')
def index():
    """Serves the Single-Page Application interface or a fallback health status."""
    index_file = os.path.join(TEMPLATE_DIR, "index.html")
    if os.path.exists(index_file):
        return render_template("index.html")
    return "Opinionated Telegram Bot & Companion Online."


@flask_app.route('/auth')
@flask_app.route('/login')
def web_login():
    """Handles Telegram magic link authentication via HMAC token or PIN."""
    token = request.args.get('token', '').strip()
    pin = request.args.get('pin', '').strip()

    user_info = None
    if pin:
        user_info = verify_auth_pin(pin)
    elif token and re.match(r'^\d{6}$', token):
        user_info = verify_auth_pin(token)
    elif token:
        user_info = verify_auth_token(token)

    if not user_info:
        return redirect('/?auth=invalid' if (token or pin) else '/?auth=missing')

    session['user_id'] = user_info['user_id']
    session['username'] = user_info['username']
    session['is_admin'] = user_info['is_admin']
    effective_token = token if (token and '.' in token) else generate_auth_token(user_info['user_id'], user_info['username'])
    return redirect(f'/?auth=success&token={effective_token}')


@flask_app.route('/logout')
def web_logout():
    """Clears the session and returns to companion home."""
    session.clear()
    return redirect('/')


# --- TELEGRAM WEBHOOK ROUTE ---

if TOKEN:
    @flask_app.route(f'/{TOKEN}', methods=['POST'])
    def webhook():
        if _main_loop and _application:
            try:
                update_data = request.get_json(force=True, silent=True)
                if update_data and isinstance(update_data, dict):
                    update = Update.de_json(update_data, _application.bot)
                    if update:
                        asyncio.run_coroutine_threadsafe(
                            _application.process_update(update),
                            _main_loop
                        )
            except Exception as e:
                logger.error(f"Webhook processing error: {e}")
        return "OK"


# --- REST API: AUTHENTICATION ---

@flask_app.route('/api/status', methods=['GET'])
def api_status():
    """Returns general server status, bot username, and connectivity state."""
    bot_uname = ""
    if _application and getattr(_application, "bot", None):
        try:
            bot_uname = getattr(_application.bot, "username", "") or ""
        except Exception:
            pass
    if not bot_uname:
        bot_uname = os.getenv("BOT_USERNAME", "")
    return jsonify({
        "status": "operational",
        "bot_active": bool(_application is not None),
        "bot_username": bot_uname,
        "database_connected": bool(codes_col is not None),
        "timestamp": int(time.time())
    })


@flask_app.route('/api/auth/me', methods=['GET'])
def api_auth_me():
    """Returns the current authenticated session or token state."""
    user = get_current_user()
    if not user or not user.get('user_id'):
        return jsonify({
            "authenticated": False,
            "user": None
        })
    user_id = user['user_id']
    return jsonify({
        "authenticated": True,
        "user": {
            "user_id": user_id,
            "username": user.get('username', ''),
            "is_admin": bool(ADMIN_ID and user_id == ADMIN_ID)
        }
    })


@flask_app.route('/api/auth/token', methods=['POST'])
def api_auth_token():
    """Validates an HMAC token, 6-digit PIN, pasted URL, or Telegram WebApp initData string."""
    payload = request.get_json(force=True, silent=True) or {}
    raw_token = str(payload.get('token', '')).strip()
    raw_pin = str(payload.get('pin', '')).strip()
    init_data = payload.get('init_data')

    user_info = None
    token_to_verify = raw_token

    # 1. Direct PIN parameter
    if raw_pin:
        user_info = verify_auth_pin(raw_pin)

    # 2. Extract token/PIN from pasted URL, prefix, or text if needed
    if not user_info and token_to_verify:
        from urllib.parse import parse_qs, urlparse

        # If user pasted "token=..." or "pin=..." directly
        if token_to_verify.startswith("token="):
            token_to_verify = token_to_verify[6:].strip()
        elif token_to_verify.startswith("pin="):
            raw_pin = token_to_verify[4:].strip()
            user_info = verify_auth_pin(raw_pin)

        if not user_info and ("http://" in token_to_verify or "https://" in token_to_verify or "?" in token_to_verify or "token=" in token_to_verify or "pin=" in token_to_verify):
            try:
                url_match = re.search(r'https?://[^\s<>"]+', token_to_verify)
                target_url = url_match.group(0) if url_match else token_to_verify
                parsed = urlparse(target_url)
                qs = parse_qs(parsed.query)
                if not qs and '=' in parsed.path:
                    qs = parse_qs(parsed.path)
                if 'pin' in qs and qs['pin']:
                    pin_cand = qs['pin'][0].strip()
                    user_info = verify_auth_pin(pin_cand)
                if not user_info and 'token' in qs and qs['token']:
                    tok_cand = qs['token'][0].strip()
                    if re.match(r'^\d{6}$', tok_cand):
                        user_info = verify_auth_pin(tok_cand)
                    else:
                        token_to_verify = tok_cand
            except Exception:
                pass

        # Also extract 6-digit PIN if pasted inside text (e.g. "PIN: 123456")
        if not user_info and not token_to_verify.startswith("ey"):
            pin_match = re.search(r'\b(\d{6})\b', token_to_verify)
            if pin_match:
                cand_pin = pin_match.group(1)
                user_info = verify_auth_pin(cand_pin)

    # 3. Check if token_to_verify is a 6-digit PIN
    if not user_info and token_to_verify and re.match(r'^\d{6}$', token_to_verify):
        user_info = verify_auth_pin(token_to_verify)

    # 4. HMAC Token verification
    if not user_info and token_to_verify:
        user_info = verify_auth_token(token_to_verify)

    # 5. Telegram WebApp initData
    if not user_info and init_data:
        user_info = verify_telegram_init_data(init_data)

    if not user_info:
        return jsonify({"success": False, "error": "Invalid or expired credentials."}), 401

    session['user_id'] = user_info['user_id']
    session['username'] = user_info['username']
    session['is_admin'] = user_info['is_admin']

    # Always provide an active companion token for Bearer authentication
    issued_token = token_to_verify if (token_to_verify and "." in token_to_verify) else generate_auth_token(user_info['user_id'], user_info['username'])

    return jsonify({
        "success": True,
        "token": issued_token,
        "user": {
            "user_id": user_info['user_id'],
            "username": user_info['username'],
            "is_admin": user_info['is_admin']
        }
    })


@flask_app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    """Clears user session."""
    session.clear()
    return jsonify({"success": True})


# --- REST API: ASCII STUDIO ---

@flask_app.route('/api/ascii/fonts', methods=['GET'])
def api_ascii_fonts():
    """Returns the list of supported ASCII art fonts and their descriptions."""
    return jsonify({
        "success": True,
        "fonts": SUPPORTED_ASCII_FONTS
    })


@flask_app.route('/api/ascii', methods=['POST'])
def api_ascii_generate():
    """Generates ASCII art from text with the requested font and width."""
    payload = request.get_json(force=True, silent=True) or {}
    text = str(payload.get('text', '')).strip()
    font = str(payload.get('font', 'ansi_shadow')).strip()
    try:
        max_width = int(payload.get('max_width', 80))
        max_width = max(20, min(max_width, 200))
    except (ValueError, TypeError):
        max_width = 80

    if not text:
        return jsonify({"success": False, "error": "Text is required."}), 400
    if len(text) > 200:
        return jsonify({"success": False, "error": "Text must not exceed 200 characters."}), 400

    art = generate_ascii_art(text, font=font, max_width=max_width)
    if not art:
        return jsonify({"success": False, "error": "Could not generate ASCII art for given input."}), 400

    return jsonify({
        "success": True,
        "art": art,
        "font": font
    })


# --- REST API: REMINDERS DASHBOARD ---

@flask_app.route('/api/reminders', methods=['GET'])
@login_required
def api_reminders_list():
    """Fetches all active reminders for the authenticated user."""
    user = get_current_user()
    user_id = user['user_id']

    async def _fetch():
        if reminders_col is None:
            return []
        cursor = reminders_col.find({"user_id": user_id}).sort("target_date", 1)
        items = await cursor.to_list(length=200)

        today_utc = datetime.now(timezone.utc).date()
        enriched = []
        for r in items:
            try:
                t_date = datetime.strptime(r['target_date'], "%Y-%m-%d").date()
                days_left = (t_date - today_utc).days
            except Exception:
                days_left = 0

            status = "today" if days_left == 0 else (
                "past_due" if days_left < 0 else (
                    "urgent" if days_left <= 3 else (
                        "active" if days_left <= 90 else "dormant"
                    )
                )
            )

            doc = serialize_doc(r)
            doc["days_left"] = days_left
            doc["days_left_text"] = format_time_left(days_left)
            doc["status"] = status
            enriched.append(doc)
        return enriched

    try:
        reminders = run_async(_fetch())
        return jsonify({"success": True, "reminders": reminders})
    except Exception as e:
        logger.error(f"Error fetching reminders: {e}")
        return jsonify({"success": False, "error": "Failed to fetch reminders."}), 500


@flask_app.route('/api/reminders', methods=['POST'])
@login_required
def api_reminders_create():
    """Creates a new countdown reminder and registers its notification job."""
    user = get_current_user()
    user_id = user['user_id']
    payload = request.get_json(force=True, silent=True) or {}

    label = str(payload.get('label', '')).strip()
    target_date = str(payload.get('target_date', '')).strip()
    reminder_time = str(payload.get('reminder_time', '09:00')).strip()
    category = str(payload.get('category', 'General')).strip()
    recurrence = str(payload.get('recurrence', 'once')).strip().lower()
    tz_name = str(payload.get('timezone', 'UTC')).strip()

    if not label:
        return jsonify({"success": False, "error": "Reminder label is required."}), 400
    if len(label) > 120:
        return jsonify({"success": False, "error": "Label must be under 120 characters."}), 400

    try:
        t_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"success": False, "error": "Invalid date format. Expected YYYY-MM-DD."}), 400

    today_utc = datetime.now(timezone.utc).date()
    if recurrence == "once" and t_date < today_utc:
        return jsonify({"success": False, "error": "Target date for one-off reminder cannot be in the past."}), 400

    if recurrence != "once" and t_date < today_utc:
        rollover_days = {"daily": 1, "weekly": 7, "monthly": 30, "annually": 365}.get(
            recurrence, 365 if category == "Birthday" else 7
        )
        new_date = t_date
        while new_date < today_utc:
            new_date += timedelta(days=rollover_days)
        target_date = new_date.strftime("%Y-%m-%d")

    try:
        parts = reminder_time.split(":")
        if len(parts) != 2 or not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59):
            reminder_time = "09:00"
    except Exception:
        reminder_time = "09:00"

    if recurrence not in ["once", "daily", "weekly", "monthly", "annually"]:
        recurrence = "once"

    doc = {
        "user_id": user_id,
        "timezone": tz_name or "UTC",
        "target_date": target_date,
        "reminder_time": reminder_time,
        "label": label,
        "category": category or "General",
        "recurrence": recurrence,
        "last_notified": ""
    }

    async def _insert():
        if reminders_col is None:
            return None
        res = await reminders_col.insert_one(doc)
        doc['_id'] = res.inserted_id
        if _application is not None:
            schedule_reminder_job(_application, doc)
        return doc

    try:
        inserted = run_async(_insert())
        if not inserted:
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        return jsonify({"success": True, "reminder": serialize_doc(inserted)})
    except Exception as e:
        logger.error(f"Error creating reminder: {e}")
        return jsonify({"success": False, "error": "Failed to create reminder."}), 500


@flask_app.route('/api/reminders/<reminder_id>/snooze', methods=['POST'])
@login_required
def api_reminders_snooze(reminder_id):
    """Snoozes an active reminder by 1 day."""
    user = get_current_user()
    user_id = user['user_id']

    async def _snooze():
        if reminders_col is None:
            return None
        try:
            oid = ObjectId(reminder_id)
        except Exception:
            return "invalid_id"

        rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
        if not rec:
            return "not_found"

        curr_date = datetime.strptime(rec['target_date'], "%Y-%m-%d")
        new_date = (curr_date + timedelta(days=1)).strftime("%Y-%m-%d")
        await reminders_col.update_one({"_id": oid}, {"$set": {"target_date": new_date, "last_notified": ""}})

        rec["target_date"] = new_date
        if _application is not None:
            schedule_reminder_job(_application, rec)
        return new_date

    try:
        result = run_async(_snooze())
        if result == "invalid_id":
            return jsonify({"success": False, "error": "Invalid reminder ID."}), 400
        if result == "not_found":
            return jsonify({"success": False, "error": "Reminder not found."}), 404
        return jsonify({"success": True, "target_date": result})
    except Exception as e:
        logger.error(f"Error snoozing reminder: {e}")
        return jsonify({"success": False, "error": "Failed to snooze reminder."}), 500


@flask_app.route('/api/reminders/<reminder_id>/done', methods=['POST'])
@login_required
def api_reminders_done(reminder_id):
    """Marks a reminder done, rolling recurring events forward or clearing one-offs."""
    user = get_current_user()
    user_id = user['user_id']

    async def _done():
        if reminders_col is None:
            return None
        try:
            oid = ObjectId(reminder_id)
        except Exception:
            return "invalid_id"

        rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
        if not rec:
            return "not_found"

        recurrence = rec.get("recurrence", "once")
        if recurrence != "once":
            rollover_days = {"daily": 1, "weekly": 7, "monthly": 30, "annually": 365}.get(
                recurrence, 365 if rec.get("category") == "Birthday" else 7
            )
            curr_date = datetime.strptime(rec['target_date'], "%Y-%m-%d")
            new_date = (curr_date + timedelta(days=rollover_days)).strftime("%Y-%m-%d")
            await reminders_col.update_one({"_id": oid}, {"$set": {"target_date": new_date, "last_notified": ""}})
            rec["target_date"] = new_date
            if _application is not None:
                schedule_reminder_job(_application, rec)
            return {"action": "rolled_over", "new_date": new_date}
        else:
            await reminders_col.delete_one({"_id": oid})
            if _application and _application.job_queue:
                for job in _application.job_queue.get_jobs_by_name(str(reminder_id)):
                    job.schedule_removal()
                remaining = await reminders_col.count_documents({"user_id": user_id})
                if remaining == 0:
                    for job in _application.job_queue.jobs():
                        if job.name and job.name.startswith(f"user_rem_{user_id}_"):
                            job.schedule_removal()
            return {"action": "completed"}

    try:
        result = run_async(_done())
        if result == "invalid_id":
            return jsonify({"success": False, "error": "Invalid reminder ID."}), 400
        if result == "not_found":
            return jsonify({"success": False, "error": "Reminder not found."}), 404
        return jsonify({"success": True, **result})
    except Exception as e:
        logger.error(f"Error marking reminder done: {e}")
        return jsonify({"success": False, "error": "Failed to complete reminder."}), 500


@flask_app.route('/api/reminders/<reminder_id>', methods=['DELETE'])
@login_required
def api_reminders_delete(reminder_id):
    """Deletes a reminder and cancels its background scheduler task."""
    user = get_current_user()
    user_id = user['user_id']

    async def _delete():
        if reminders_col is None:
            return None
        try:
            oid = ObjectId(reminder_id)
        except Exception:
            return "invalid_id"

        res = await reminders_col.delete_one({"_id": oid, "user_id": user_id})
        if res.deleted_count == 0:
            return "not_found"

        if _application and _application.job_queue:
            for job in _application.job_queue.get_jobs_by_name(str(reminder_id)):
                job.schedule_removal()
            remaining = await reminders_col.count_documents({"user_id": user_id})
            if remaining == 0:
                for job in _application.job_queue.jobs():
                    if job.name and job.name.startswith(f"user_rem_{user_id}_"):
                        job.schedule_removal()
        return "deleted"

    try:
        result = run_async(_delete())
        if result == "invalid_id":
            return jsonify({"success": False, "error": "Invalid reminder ID."}), 400
        if result == "not_found":
            return jsonify({"success": False, "error": "Reminder not found."}), 404
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error deleting reminder: {e}")
        return jsonify({"success": False, "error": "Failed to delete reminder."}), 500


@flask_app.route('/api/reminders/<reminder_id>', methods=['PUT', 'PATCH'])
@login_required
def api_reminders_update(reminder_id):
    """Updates an existing reminder (label, category, date, time, recurrence)."""
    user = get_current_user()
    user_id = user['user_id']
    payload = request.get_json(force=True, silent=True) or {}

    async def _update():
        if reminders_col is None:
            return "db_unavailable", None
        try:
            oid = ObjectId(reminder_id)
        except Exception:
            return "invalid_id", None

        rec = await reminders_col.find_one({"_id": oid, "user_id": user_id})
        if not rec:
            return "not_found", None

        updates = {}
        if "label" in payload:
            label = str(payload["label"]).strip()
            if not label:
                return "empty_label", None
            if len(label) > 120:
                return "label_too_long", None
            updates["label"] = label

        if "category" in payload:
            cat = str(payload["category"]).strip()
            if cat:
                updates["category"] = cat

        if "target_date" in payload:
            t_date_str = str(payload["target_date"]).strip()
            try:
                datetime.strptime(t_date_str, "%Y-%m-%d")
                updates["target_date"] = t_date_str
                updates["last_notified"] = ""
            except ValueError:
                return "invalid_date", None

        if "reminder_time" in payload:
            r_time = str(payload["reminder_time"]).strip()
            try:
                parts = r_time.split(":")
                if len(parts) == 2 and 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59:
                    updates["reminder_time"] = f"{int(parts[0]):02d}:{int(parts[1]):02d}"
            except Exception:
                pass

        if "recurrence" in payload:
            rec_val = str(payload["recurrence"]).strip().lower()
            if rec_val in ["once", "daily", "weekly", "monthly", "annually"]:
                updates["recurrence"] = rec_val

        if "timezone" in payload:
            updates["timezone"] = str(payload["timezone"]).strip() or "UTC"

        if not updates:
            return "no_updates", serialize_doc(rec)

        await reminders_col.update_one({"_id": oid}, {"$set": updates})
        updated_rec = await reminders_col.find_one({"_id": oid})

        if _application is not None and updated_rec:
            schedule_reminder_job(_application, updated_rec)

        return "success", serialize_doc(updated_rec)

    try:
        status, updated_doc = run_async(_update())
        if status == "db_unavailable":
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        if status == "invalid_id":
            return jsonify({"success": False, "error": "Invalid reminder ID."}), 400
        if status == "not_found":
            return jsonify({"success": False, "error": "Reminder not found."}), 404
        if status == "empty_label":
            return jsonify({"success": False, "error": "Reminder label cannot be empty."}), 400
        if status == "label_too_long":
            return jsonify({"success": False, "error": "Label must be under 120 characters."}), 400
        if status == "invalid_date":
            return jsonify({"success": False, "error": "Invalid date format. Expected YYYY-MM-DD."}), 400

        return jsonify({"success": True, "reminder": updated_doc})
    except Exception as e:
        logger.error(f"Error updating reminder: {e}")
        return jsonify({"success": False, "error": "Failed to update reminder."}), 500


# --- REST API: MEDIA LIBRARY & TELEGRAM DELIVERY ---

@flask_app.route('/api/media/categories', methods=['GET'])
def api_media_categories():
    """Returns available media catalog browsing categories."""
    return jsonify({
        "success": True,
        "categories": [
            {"id": "all", "name": "All Titles", "icon": "🌐"},
            {"id": "movie", "name": "Movies", "icon": "🎬"},
            {"id": "series", "name": "Series", "icon": "📺"}
        ]
    })


@flask_app.route('/api/media/items', methods=['GET'])
def api_media_items():
    """Lists media titles with TMDB metadata, posters, and partition lock states."""
    category = request.args.get('category', 'all').lower()
    letter = request.args.get('letter', '').strip()
    search = request.args.get('search', '').strip()
    user = get_current_user()
    user_id = user['user_id'] if user else None

    async def _fetch():
        items = []
        if prefix_labels_col is not None:
            query_filter = {}
            if category in ["movie", "series"]:
                query_filter["category"] = category

            if letter:
                if letter == "#":
                    query_filter["name"] = {"$regex": "^[^A-Za-z]", "$options": "i"}
                else:
                    query_filter["name"] = {"$regex": f"^{re.escape(letter)}", "$options": "i"}

            if search:
                query_filter["$or"] = [
                    {"name": {"$regex": re.escape(search), "$options": "i"}},
                    {"prefix": {"$regex": re.escape(search), "$options": "i"}}
                ]

            cursor = prefix_labels_col.find(query_filter).sort("name", 1)
            items = await cursor.to_list(length=200)

        # Fallback for unindexed prefixes directly present in codes_col
        labeled_prefixes = {it.get("prefix", "").upper() for it in items if it.get("prefix")}
        if codes_col is not None and (not items or len(items) < 10):
            try:
                pipeline = [
                    {"$project": {"prefix": {"$substr": ["$code", 0, 3]}}},
                    {"$group": {"_id": "$prefix"}}
                ]
                raw_prefixes = await codes_col.aggregate(pipeline).to_list(length=100)
                for rp in raw_prefixes:
                    p = (rp.get("_id") or "").upper()
                    if p and p not in labeled_prefixes:
                        if letter:
                            if letter == "#" and p[0].isalpha():
                                continue
                            elif letter != "#" and not p.upper().startswith(letter.upper()):
                                continue
                        if search and search.upper() not in p:
                            continue
                        items.append({"prefix": p, "name": p, "category": "movie", "seasons": []})
                        labeled_prefixes.add(p)
            except Exception:
                pass

        # Batch query gates to determine locked partitions
        locked_prefixes = {}
        if group_keys_col is not None:
            async for g in group_keys_col.find({}):
                pfx = g.get("prefix", "").upper()
                if pfx:
                    locked_prefixes[pfx] = g

        unlocked_set = set()
        if user_id and unlocked_groups_col is not None:
            user_doc = await unlocked_groups_col.find_one({"user_id": user_id})
            if user_doc:
                unlocked_set = set(user_doc.get("unlocked_prefixes", []))

        result = []
        for it in items:
            pfx = it.get("prefix", "").upper()
            gate = locked_prefixes.get(pfx)
            is_locked = False
            if gate:
                if user_id == ADMIN_ID and ADMIN_ID != 0:
                    is_locked = False
                else:
                    auth_key = f"{gate.get('chat_id')}_{pfx}"
                    is_locked = auth_key not in unlocked_set and pfx not in unlocked_set

            tmdb = it.get("tmdb") or {}
            banner_mode = it.get("banner_mode", "poster")
            poster = it.get("custom_banner")
            if not poster:
                if banner_mode == "backdrop":
                    poster = tmdb.get("backdrop_url") or tmdb.get("poster_url") or ""
                else:
                    poster = tmdb.get("poster_url") or tmdb.get("backdrop_url") or ""

            result.append({
                "prefix": pfx,
                "name": it.get("name", pfx),
                "category": it.get("category", "movie"),
                "poster_url": poster,
                "rating": tmdb.get("rating"),
                "year": tmdb.get("year"),
                "genres": tmdb.get("genres"),
                "overview": tmdb.get("overview"),
                "is_locked": is_locked,
                "seasons_count": len(it.get("seasons", []))
            })
        return result

    try:
        items = run_async(_fetch())
        return jsonify({"success": True, "items": items})
    except Exception as e:
        logger.error(f"Error fetching media items: {e}")
        return jsonify({"success": False, "error": "Failed to fetch media catalog."}), 500


@flask_app.route('/api/media/item/<prefix>', methods=['GET'])
def api_media_item_detail(prefix):
    """Retrieves full item details, TMDB metadata, seasons, and episode files."""
    user = get_current_user()
    user_id = user['user_id'] if user else None
    pfx = prefix.upper().strip()

    async def _fetch():
        label_rec = await prefix_labels_col.find_one({"prefix": pfx}) if prefix_labels_col is not None else None
        if not label_rec:
            # Check codes_col for unindexed prefix
            code_sample = await codes_col.find_one({"code": {"$regex": f"^{re.escape(pfx)}", "$options": "i"}}) if codes_col is not None else None
            if not code_sample:
                return None
            label_rec = {"prefix": pfx, "name": pfx, "category": "movie"}

        cursor = codes_col.find(
            {"code": {"$regex": f"^{re.escape(pfx)}", "$options": "i"}},
            {"code": 1, "file_name": 1, "caption": 1, "file_type": 1, "chat_id": 1}
        ).sort("code", 1) if codes_col is not None else None
        files = await cursor.to_list(length=300) if cursor else []

        gate = await group_keys_col.find_one({"prefix": pfx}) if group_keys_col is not None else None
        is_locked = False
        if gate:
            if user_id == ADMIN_ID and ADMIN_ID != 0:
                is_locked = False
            else:
                auth_key = f"{gate.get('chat_id')}_{pfx}"
                unlocked_doc = await unlocked_groups_col.find_one({"user_id": user_id}) if (user_id and unlocked_groups_col is not None) else None
                unlocked_set = set(unlocked_doc.get("unlocked_prefixes", [])) if unlocked_doc else set()
                is_locked = auth_key not in unlocked_set and pfx not in unlocked_set

        tmdb = label_rec.get("tmdb") or {}
        poster = label_rec.get("custom_banner") or tmdb.get("poster_url") or tmdb.get("backdrop_url") or ""
        backdrop = tmdb.get("backdrop_url") or poster
        seasons_raw = label_rec.get("seasons", [])
        clean_seasons = []
        if isinstance(seasons_raw, list):
            for s in seasons_raw:
                if isinstance(s, dict) and "num" in s:
                    try:
                        clean_seasons.append({
                            "num": int(s["num"]),
                            "start": int(s.get("start", 1)),
                            "end": int(s.get("end", 1))
                        })
                    except (ValueError, TypeError):
                        continue
            clean_seasons.sort(key=lambda x: x["num"])

        return {
            "prefix": pfx,
            "name": label_rec.get("name", pfx),
            "category": label_rec.get("category", "movie"),
            "poster_url": poster,
            "backdrop_url": backdrop,
            "rating": tmdb.get("rating"),
            "year": tmdb.get("year"),
            "genres": tmdb.get("genres"),
            "overview": tmdb.get("overview"),
            "seasons": clean_seasons,
            "is_locked": is_locked,
            "files": [serialize_doc(f) for f in files]
        }

    try:
        item = run_async(_fetch())
        if not item:
            return jsonify({"success": False, "error": f"Item '{prefix}' not found."}), 404
        return jsonify({"success": True, "item": item})
    except Exception as e:
        logger.error(f"Error fetching item detail: {e}")
        return jsonify({"success": False, "error": "Failed to fetch item."}), 500


@flask_app.route('/api/media/unlock', methods=['POST'])
@login_required
def api_media_unlock():
    """Unlocks a password-protected media partition for the user."""
    user = get_current_user()
    user_id = user['user_id']
    username = user.get('username', '')
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()
    passkey = str(payload.get('passkey', '')).strip()

    if not prefix or not passkey:
        return jsonify({"success": False, "error": "Prefix and passkey are required."}), 400

    locked, rem = is_locked_out(user_id)
    if locked:
        return jsonify({"success": False, "error": f"Too many failed attempts. Locked out for {rem} seconds."}), 429

    async def _unlock():
        if group_keys_col is None:
            return "db_unavailable"
        gate = await group_keys_col.find_one({"prefix": prefix})
        if not gate:
            return "not_locked"

        if passkey != gate.get("secret_key", "").strip():
            fails, just_locked = record_failed_attempt(user_id)
            if just_locked:
                return "just_locked"
            return f"failed_{fails}"

        reset_failed_attempts(user_id)
        chat_id = gate.get("chat_id")
        if (not chat_id or chat_id == 0) and codes_col is not None:
            sample = await codes_col.find_one({"code": {"$regex": f"^{re.escape(prefix)}"}}, {"chat_id": 1})
            if sample and sample.get("chat_id"):
                chat_id = sample["chat_id"]

        keys_to_unlock = [prefix]
        if chat_id:
            keys_to_unlock.append(f"{chat_id}_{prefix}")
        if gate.get("chat_id"):
            keys_to_unlock.append(f"{gate.get('chat_id')}_{prefix}")

        if unlocked_groups_col is not None:
            await unlocked_groups_col.update_one(
                {"user_id": user_id},
                {
                    "$addToSet": {"unlocked_prefixes": {"$each": keys_to_unlock}},
                    "$pull": {"frozen_prefixes": {"$each": keys_to_unlock}}
                },
                upsert=True
            )
        await log_event(user_id, username, f"Web Unlocked: {prefix}")
        return "success"

    try:
        res = run_async(_unlock())
        if res == "db_unavailable":
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        if res == "not_locked":
            return jsonify({"success": True, "message": "Partition is not locked."})
        if res == "just_locked":
            return jsonify({"success": False, "error": "5 failed attempts. Account locked from passkeys for 1 hour."}), 429
        if isinstance(res, str) and res.startswith("failed_"):
            fails = res.split("_")[1]
            return jsonify({"success": False, "error": f"Incorrect passkey. (Attempt {fails}/5)"}), 400
        return jsonify({"success": True, "message": f"Partition '{prefix}' unlocked successfully!"})
    except Exception as e:
        logger.error(f"Error unlocking partition: {e}")
        return jsonify({"success": False, "error": "Passkey check failed."}), 500


@flask_app.route('/api/media/deliver', methods=['POST'])
@login_required
def api_media_deliver():
    """Triggers instant delivery of the media file directly to the user's Telegram chat."""
    user = get_current_user()
    user_id = user['user_id']
    username = user.get('username', '')
    payload = request.get_json(force=True, silent=True) or {}
    code = str(payload.get('code', '')).strip().upper()

    if not code:
        return jsonify({"success": False, "error": "Code is required."}), 400

    class SafeJobQueue:
        def __init__(self, real_jq=None):
            self._real = real_jq

        def run_once(self, *args, **kwargs):
            if self._real and hasattr(self._real, "run_once"):
                return self._real.run_once(*args, **kwargs)
            return None

        def get_jobs_by_name(self, name):
            if self._real and hasattr(self._real, "get_jobs_by_name"):
                return self._real.get_jobs_by_name(name)
            return []

        def jobs(self):
            if self._real and hasattr(self._real, "jobs"):
                return self._real.jobs()
            return []

    class WebContext:
        def __init__(self, bot, job_queue):
            self.bot = bot
            self.job_queue = job_queue

    class WebUser:
        def __init__(self, uid, uname):
            self.id = uid
            self.username = uname

    async def _deliver():
        if await is_user_banned(user_id):
            return "banned", None

        record = await codes_col.find_one({"code": code}) if codes_col is not None else None
        if not record:
            return "not_found", None

        authorized, prefix, _chat_id, is_frozen, is_expired = await check_access_gate(user_id, record)
        if not authorized:
            if is_frozen:
                return "frozen", prefix
            elif is_expired:
                return "expired", prefix
            return "locked", prefix

        if not _application or not _application.bot:
            return "bot_offline", None

        jq = SafeJobQueue(_application.job_queue if _application else None)
        ctx = WebContext(_application.bot, jq)
        usr = WebUser(user_id, username)
        msg = await execute_file_delivery(user_id, record, ctx, usr, send_alert=True)
        return "ok" if msg else "send_failed", prefix

    try:
        status, prefix = run_async(_deliver())
        if status == "banned":
            return jsonify({"success": False, "error": "Account is restricted from media delivery."}), 403
        if status == "not_found":
            return jsonify({"success": False, "error": f"Media '{code}' not found."}), 404
        if status == "frozen":
            return jsonify({"success": False, "error": f"Access to partition '{prefix}' is frozen.", "locked": True, "prefix": prefix}), 403
        if status == "expired":
            return jsonify({"success": False, "error": f"Access to partition '{prefix}' has expired.", "locked": True, "prefix": prefix}), 403
        if status == "locked":
            return jsonify({"success": False, "error": f"Partition '{prefix}' is locked with a passkey.", "locked": True, "prefix": prefix}), 403
        if status == "bot_offline":
            return jsonify({"success": False, "error": "Telegram bot service is offline."}), 503
        if status == "send_failed":
            return jsonify({
                "success": False,
                "error": "Failed to send to Telegram. Make sure you have started a private conversation with the bot (/start)."
            }), 400

        return jsonify({
            "success": True,
            "message": f"Media '{code}' delivered to your Telegram chat!"
        })
    except Exception as e:
        logger.error(f"Media delivery error: {e}")
        return jsonify({"success": False, "error": f"Delivery error: {e}"}), 500


# --- REST API: ADMIN CONTROL PORTAL ---

@flask_app.route('/api/admin/stats', methods=['GET'])
@admin_required
def api_admin_stats():
    """Returns system-wide database counts and audit statistics."""
    async def _fetch():
        r_task = reminders_col.count_documents({}) if reminders_col is not None else asyncio.sleep(0, result=0)
        c_task = codes_col.count_documents({}) if codes_col is not None else asyncio.sleep(0, result=0)
        l_task = logs_col.count_documents({}) if logs_col is not None else asyncio.sleep(0, result=0)
        k_task = group_keys_col.count_documents({}) if group_keys_col is not None else asyncio.sleep(0, result=0)
        u_task = users_col.count_documents({}) if users_col is not None else asyncio.sleep(0, result=0)
        r_c, c_c, l_c, k_c, u_c = await asyncio.gather(r_task, c_task, l_task, k_task, u_task)
        return {
            "reminders": r_c,
            "assets": c_c,
            "locked_prefixes": k_c,
            "logs": l_c,
            "users": u_c
        }

    try:
        stats = run_async(_fetch())
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        logger.error(f"Admin stats error: {e}")
        return jsonify({"success": False, "error": "Failed to retrieve stats."}), 500


@flask_app.route('/api/admin/keys', methods=['GET'])
@admin_required
def api_admin_keys():
    """Returns matrix of indexed prefixes, item counts, labels, and secret passkeys."""
    async def _fetch():
        results = []
        if codes_col is not None:
            pipeline = [
                {"$project": {"prefix": {"$substr": ["$code", 0, 3]}, "chat_id": 1}},
                {"$group": {"_id": {"prefix": "$prefix", "chat_id": "$chat_id"}, "count": {"$sum": 1}}}
            ]
            results = await codes_col.aggregate(pipeline).to_list(length=100)

        found_prefixes = {r['_id']['prefix'] for r in results}

        async def _read_cursor(cursor):
            if cursor is None:
                return []
            if hasattr(cursor, "to_list"):
                return await cursor.to_list(length=500)
            return [d async for d in cursor]

        # Include prefixes from group_keys and prefix_labels that have 0 indexed assets
        extra_prefixes = set()
        if group_keys_col is not None:
            all_keys = await _read_cursor(group_keys_col.find({}))
            for k_doc in all_keys:
                p = k_doc.get("prefix")
                if p and p not in found_prefixes:
                    extra_prefixes.add((p, k_doc.get("chat_id", 0)))
        if prefix_labels_col is not None:
            all_labels = await _read_cursor(prefix_labels_col.find({}))
            for l_doc in all_labels:
                p = l_doc.get("prefix")
                if p and p not in found_prefixes and not any(p == ep[0] for ep in extra_prefixes):
                    extra_prefixes.add((p, 0))

        for p, cid in extra_prefixes:
            results.append({"_id": {"prefix": p, "chat_id": cid}, "count": 0})

        prefixes = list({r['_id']['prefix'] for r in results})

        keys_map = {}
        if group_keys_col is not None and prefixes:
            matching_keys = await _read_cursor(group_keys_col.find({"prefix": {"$in": prefixes}}))
            for k_doc in matching_keys:
                keys_map[(k_doc.get("chat_id"), k_doc.get("prefix"))] = k_doc
                keys_map[k_doc.get("prefix")] = k_doc

        labels_map = {}
        if prefix_labels_col is not None and prefixes:
            matching_labels = await _read_cursor(prefix_labels_col.find({"prefix": {"$in": prefixes}}))
            for l_doc in matching_labels:
                labels_map[l_doc.get("prefix")] = l_doc

        matrix = []
        for r in results:
            prefix, chat_id, count = r['_id']['prefix'], r['_id']['chat_id'], r['count']
            key_record = keys_map.get((chat_id, prefix)) or keys_map.get(prefix)
            label_doc = labels_map.get(prefix)
            matrix.append({
                "prefix": prefix,
                "chat_id": chat_id,
                "count": count,
                "name": (label_doc.get("name") or label_doc.get("label") or "Unlabeled") if label_doc else "Unlabeled",
                "secret_key": key_record.get("secret_key") if key_record else None,
                "is_locked": bool(key_record)
            })
        return matrix

    try:
        keys = run_async(_fetch())
        return jsonify({"success": True, "keys": keys})
    except Exception as e:
        logger.error(f"Key matrix error: {e}")
        return jsonify({"success": False, "error": "Failed to fetch key matrix."}), 500


@flask_app.route('/api/admin/access', methods=['GET'])
@admin_required
def api_admin_access():
    """Returns complete user access matrix with active, frozen, and expiration states."""
    async def _fetch():
        if unlocked_groups_col is None:
            return []
        users_access = await unlocked_groups_col.find({}).to_list(length=100)
        u_ids = [u.get("user_id") for u in users_access if u.get("user_id")]

        users_map = {}
        if users_col is not None and u_ids:
            async for u_doc in users_col.find({"user_id": {"$in": u_ids}}):
                users_map[u_doc["user_id"]] = u_doc

        now = time.time()
        matrix = []
        for u in users_access:
            u_id = u.get("user_id")
            u_info = users_map.get(u_id)
            uname = f"@{u_info.get('username')}" if u_info and u_info.get('username') else f"User {u_id}"

            unlocked = u.get("unlocked_prefixes", [])
            frozen = u.get("frozen_prefixes", [])
            expirations = u.get("prefix_expirations", {})

            prefixes_data = []
            all_prefixes = sorted(list(set(unlocked + frozen)))
            for p_key in all_prefixes:
                parts = p_key.split('_')
                display_prefix = parts[-1] if len(parts) > 1 else p_key
                is_frozen = p_key in frozen
                exp_ts = expirations.get(p_key)
                remaining = int(exp_ts - now) if exp_ts else None

                prefixes_data.append({
                    "auth_key": p_key,
                    "prefix": display_prefix,
                    "is_frozen": is_frozen,
                    "expires_at": exp_ts,
                    "remaining_seconds": remaining
                })

            matrix.append({
                "user_id": u_id,
                "username": uname,
                "is_banned": bool(u.get("is_banned")),
                "prefixes": prefixes_data
            })
        return matrix

    try:
        access_list = run_async(_fetch())
        return jsonify({"success": True, "access": access_list})
    except Exception as e:
        logger.error(f"Access matrix error: {e}")
        return jsonify({"success": False, "error": "Failed to fetch access matrix."}), 500


@flask_app.route('/api/admin/logs', methods=['GET'])
@admin_required
def api_admin_logs():
    """Returns the most recent system activity and sentinel logs."""
    async def _fetch():
        if logs_col is None:
            return []
        cursor = logs_col.find({}).sort("timestamp", -1).limit(50)
        logs = await cursor.to_list(length=50)
        return [serialize_doc(log_entry) for log_entry in logs]

    try:
        logs = run_async(_fetch())
        return jsonify({"success": True, "logs": logs})
    except Exception as e:
        logger.error(f"Logs error: {e}")
        return jsonify({"success": False, "error": "Failed to fetch logs."}), 500


@flask_app.route('/api/admin/user/<action>', methods=['POST'])
@admin_required
def api_admin_user_action(action):
    """Executes admin user management operations: grant, revoke, freeze, unfreeze, ban, unban."""
    if action not in ["grant", "revoke", "freeze", "unfreeze", "ban", "unban"]:
        return jsonify({"success": False, "error": "Invalid action."}), 400

    payload = request.get_json(force=True, silent=True) or {}
    user_id_raw = payload.get('user_id')
    prefix = str(payload.get('prefix', '0')).strip().upper()
    duration_str = payload.get('duration')

    if not user_id_raw:
        return jsonify({"success": False, "error": "user_id is required."}), 400

    target_str = str(user_id_raw).strip()

    async def _execute():
        if unlocked_groups_col is None:
            return "db_unavailable"

        # Resolve username or numeric ID
        target_lookup = target_str if (target_str.isdigit() or target_str.startswith("@")) else f"@{target_str}"
        target_res = await resolve_target_user(target_lookup)
        resolved_uid = target_res[0] if isinstance(target_res, tuple) else target_res
        if not resolved_uid:
            try:
                resolved_uid = int(target_str)
            except ValueError:
                return "user_not_found"

        user_id = resolved_uid

        if action == "ban":
            await unlocked_groups_col.update_one({"user_id": user_id}, {"$set": {"is_banned": True}}, upsert=True)
            await log_event(user_id, "", "Banned via Web Admin")
            return f"Banned user {user_id}"

        if action == "unban":
            await unlocked_groups_col.update_one({"user_id": user_id}, {"$set": {"is_banned": False}}, upsert=True)
            await log_event(user_id, "", "Unbanned via Web Admin")
            return f"Unbanned user {user_id}"

        # Resolve target keys
        if prefix == "0":
            if group_keys_col is None:
                return "db_unavailable"
            cursor = group_keys_col.find({})
            keys_to_target = [f"{d['chat_id']}_{d['prefix']}" async for d in cursor]
        else:
            gate = await group_keys_col.find_one({"prefix": prefix}) if group_keys_col is not None else None
            chat_id = gate.get("chat_id", 0) if gate else 0
            if (not chat_id or chat_id == 0) and codes_col is not None:
                sample = await codes_col.find_one({"code": {"$regex": f"^{re.escape(prefix)}", "$options": "i"}}, {"chat_id": 1})
                if sample and sample.get("chat_id"):
                    chat_id = sample["chat_id"]
            keys_to_target = [f"{chat_id}_{prefix}", prefix]

        if not keys_to_target:
            return "no_keys"

        if action == "grant":
            expires_at = parse_duration(duration_str) if duration_str else None
            update_op = {
                "$addToSet": {"unlocked_prefixes": {"$each": keys_to_target}},
                "$pull": {"frozen_prefixes": {"$each": keys_to_target}}
            }
            if expires_at:
                exp_dict = {f"prefix_expirations.{k}": expires_at for k in keys_to_target}
                update_op["$set"] = exp_dict
            else:
                unsets = {f"prefix_expirations.{k}": "" for k in keys_to_target}
                update_op["$unset"] = unsets
            await unlocked_groups_col.update_one({"user_id": user_id}, update_op, upsert=True)
            await log_event(user_id, "", f"Granted {prefix} via Web Admin")
            return f"Granted {prefix} to user {user_id}"

        elif action == "revoke":
            unsets = {f"prefix_expirations.{k}": "" for k in keys_to_target}
            await unlocked_groups_col.update_one(
                {"user_id": user_id},
                {
                    "$pull": {
                        "unlocked_prefixes": {"$each": keys_to_target},
                        "frozen_prefixes": {"$each": keys_to_target}
                    },
                    "$unset": unsets
                }
            )
            await log_event(user_id, "", f"Revoked {prefix} via Web Admin")
            return f"Revoked {prefix} from user {user_id}"

        elif action == "freeze":
            expires_at = parse_duration(duration_str) if duration_str else None
            update_op = {
                "$addToSet": {"frozen_prefixes": {"$each": keys_to_target}},
                "$pull": {"unlocked_prefixes": {"$each": keys_to_target}}
            }
            if expires_at:
                exp_dict = {f"prefix_expirations.{k}": expires_at for k in keys_to_target}
                update_op["$set"] = exp_dict
            await unlocked_groups_col.update_one({"user_id": user_id}, update_op, upsert=True)
            await log_event(user_id, "", f"Froze {prefix} via Web Admin")
            return f"Froze {prefix} for user {user_id}"

        elif action == "unfreeze":
            await unlocked_groups_col.update_one(
                {"user_id": user_id},
                {
                    "$addToSet": {"unlocked_prefixes": {"$each": keys_to_target}},
                    "$pull": {"frozen_prefixes": {"$each": keys_to_target}}
                }
            )
            await log_event(user_id, "", f"Unfroze {prefix} via Web Admin")
            return f"Unfroze {prefix} for user {user_id}"

        return "unknown"

    try:
        result = run_async(_execute())
        if result == "db_unavailable":
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        if result == "user_not_found":
            return jsonify({"success": False, "error": f"Target user '{user_id_raw}' could not be resolved."}), 404
        if result == "no_keys":
            return jsonify({"success": False, "error": "No matching partition keys found."}), 404
        return jsonify({"success": True, "message": result})
    except Exception as e:
        logger.error(f"User admin action error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/file/lock', methods=['POST'])
@admin_required
def api_admin_file_lock():
    """Sets or updates the secret passkey for a partition (or all unlocked partitions if prefix='0')."""
    from pymongo import UpdateOne
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()
    password = str(payload.get('password') or payload.get('passkey') or '').strip()
    chat_id = int(payload.get('chat_id', 0))

    if not prefix or not password:
        return jsonify({"success": False, "error": "Prefix and password are required."}), 400

    async def _lock():
        if group_keys_col is None:
            return 0

        if prefix == "0":
            if codes_col is None:
                return 0
            pipeline = [
                {"$project": {"prefix": {"$substr": ["$code", 0, 3]}, "chat_id": 1}},
                {"$group": {"_id": {"prefix": "$prefix", "chat_id": "$chat_id"}}}
            ]
            all_groups = await codes_col.aggregate(pipeline).to_list(length=200)
            locked_prefixes = set()
            async for k in group_keys_col.find({}):
                locked_prefixes.add(k.get("prefix"))

            unlocked = [g['_id'] for g in all_groups if g['_id']['prefix'] not in locked_prefixes]
            ops = []
            for g in unlocked:
                p = g['prefix']
                cid = g['chat_id']
                ops.append(UpdateOne(
                    {"prefix": p},
                    {"$set": {"secret_key": password, "chat_id": cid}},
                    upsert=True
                ))
            if ops:
                await group_keys_col.bulk_write(ops, ordered=False)
            invalidate_group_keys_cache()
            await log_event(ADMIN_ID, "ADMIN", f"Locked all {len(ops)} unlocked partitions with passkey via Web")
            return len(ops)
        else:
            target_chat_id = chat_id
            if target_chat_id == 0 and codes_col is not None:
                sample = await codes_col.find_one({"code": {"$regex": f"^{re.escape(prefix)}", "$options": "i"}}, {"chat_id": 1})
                if sample and sample.get("chat_id"):
                    target_chat_id = sample["chat_id"]

            await group_keys_col.update_one(
                {"prefix": prefix},
                {"$set": {"secret_key": password, "chat_id": target_chat_id}},
                upsert=True
            )
            invalidate_group_keys_cache(prefix)
            await log_event(ADMIN_ID, "ADMIN", f"Locked {prefix} with passkey via Web")
            return 1

    try:
        count = run_async(_lock())
        msg = f"Locked all {count} unlocked partition(s) with passkey." if prefix == "0" else f"Partition '{prefix}' locked with passkey."
        return jsonify({"success": True, "message": msg})
    except Exception as e:
        logger.error(f"Admin lock error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/file/unlock', methods=['POST'])
@admin_required
def api_admin_file_unlock():
    """Removes the passkey from a partition, making it public."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()

    if not prefix:
        return jsonify({"success": False, "error": "Prefix is required."}), 400

    async def _unlock():
        if group_keys_col is None:
            return False
        await group_keys_col.delete_many({"prefix": prefix})
        invalidate_group_keys_cache(prefix)
        await log_event(ADMIN_ID, "ADMIN", f"Unlocked partition {prefix} via Web")
        return True

    try:
        run_async(_unlock())
        return jsonify({"success": True, "message": f"Partition '{prefix}' unlocked."})
    except Exception as e:
        logger.error(f"Admin unlock error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/file/change_passkey', methods=['POST'])
@admin_required
def api_admin_file_change_passkey():
    """Rotates/changes passkey for a partition or across all partitions."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()
    old_pass = str(payload.get('old_pass') or payload.get('old_passkey') or '').strip()
    new_pass = str(payload.get('new_pass') or payload.get('new_passkey') or '').strip()

    if not new_pass:
        return jsonify({"success": False, "error": "New password is required."}), 400

    async def _change():
        if group_keys_col is None:
            return "db_unavailable", 0
        if prefix == "0":
            if not old_pass:
                return "old_pass_required", 0
            res = await group_keys_col.update_many({"secret_key": old_pass}, {"$set": {"secret_key": new_pass}})
            invalidate_group_keys_cache()
            await log_event(ADMIN_ID, "ADMIN", f"Passkey rotated for {res.modified_count} partition(s)")
            return "success", res.modified_count
        else:
            if not prefix:
                return "prefix_required", 0
            gate = await group_keys_col.find_one({"prefix": prefix})
            if not gate:
                return "not_found", 0
            if old_pass and gate.get("secret_key", "").strip() != old_pass:
                return "old_pass_mismatch", 0
            await group_keys_col.update_one({"prefix": prefix}, {"$set": {"secret_key": new_pass}})
            invalidate_group_keys_cache(prefix)
            await log_event(ADMIN_ID, "ADMIN", f"Passkey rotated for {prefix}")
            return "success", 1

    try:
        status, count = run_async(_change())
        if status == "db_unavailable":
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        if status == "prefix_required":
            return jsonify({"success": False, "error": "Prefix is required."}), 400
        if status == "old_pass_required":
            return jsonify({"success": False, "error": "Old password is required when rotating all keys."}), 400
        if status == "not_found":
            return jsonify({"success": False, "error": f"Partition '{prefix}' is not locked."}), 404
        if status == "old_pass_mismatch":
            return jsonify({"success": False, "error": "Current passkey does not match."}), 400
        return jsonify({"success": True, "message": f"Passkey updated for {count} partition(s)."})
    except Exception as e:
        logger.error(f"Change passkey error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/file/mv', methods=['POST'])
@admin_required
def api_admin_file_mv():
    """Migrates / renames prefix across assets, keys, and labels."""
    from pymongo import UpdateOne
    payload = request.get_json(force=True, silent=True) or {}
    old_pfx = str(payload.get('old_prefix', '')).strip().upper()[:3]
    new_pfx = str(payload.get('new_prefix', '')).strip().upper()[:3]

    if not old_pfx or not new_pfx:
        return jsonify({"success": False, "error": "Both old_prefix and new_prefix (3 chars) are required."}), 400
    if old_pfx == new_pfx:
        return jsonify({"success": False, "error": "Old and new prefixes cannot be identical."}), 400

    async def _mv():
        if codes_col is None:
            return "db_unavailable", 0
        cursor = codes_col.find({"code": {"$regex": f"^{re.escape(old_pfx)}", "$options": "i"}})
        count = 0
        ops = []
        async for r in cursor:
            old_code = r["code"]
            new_code = f"{new_pfx}{old_code[3:]}"
            ops.append(UpdateOne({"_id": r["_id"]}, {"$set": {"code": new_code}}))
            count += 1
            if len(ops) >= 500:
                await codes_col.bulk_write(ops, ordered=False)
                ops = []
        if ops:
            await codes_col.bulk_write(ops, ordered=False)

        if group_keys_col is not None:
            await group_keys_col.update_many({"prefix": old_pfx}, {"$set": {"prefix": new_pfx}})
            invalidate_group_keys_cache()
        if prefix_labels_col is not None:
            await prefix_labels_col.update_many({"prefix": old_pfx}, {"$set": {"prefix": new_pfx}})

        await log_event(ADMIN_ID, "ADMIN", f"Web Rename Prefix: {old_pfx} -> {new_pfx} ({count} items)")
        return "success", count

    try:
        status, count = run_async(_mv())
        if status == "db_unavailable":
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        return jsonify({"success": True, "message": f"Successfully migrated {count} items from {old_pfx} to {new_pfx}."})
    except Exception as e:
        logger.error(f"Move prefix error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/file/del', methods=['POST'])
@admin_required
def api_admin_file_del():
    """Deletes a single file asset by code, or a surgical range of assets."""
    payload = request.get_json(force=True, silent=True) or {}
    code = str(payload.get('code', '')).strip().upper()
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    start = payload.get('start')
    end = payload.get('end')

    if not code and not (prefix and start is not None and end is not None):
        return jsonify({"success": False, "error": "Provide either 'code' for single deletion, or 'prefix', 'start', 'end' for range deletion."}), 400

    async def _del():
        if codes_col is None:
            return "db_unavailable", 0
        if code:
            res = await codes_col.delete_one({"code": code})
            if res.deleted_count > 0:
                await log_event(ADMIN_ID, "ADMIN", f"Web Del Single: {code}")
                return "success", res.deleted_count
            return "not_found", 0
        else:
            try:
                s_int = int(start)
                e_int = int(end)
                if s_int < 1 or e_int < s_int or (e_int - s_int + 1) > 100:
                    return "range_limit", 0
                target_codes = [f"{prefix}{i:03d}" for i in range(s_int, e_int + 1)]
                res = await codes_col.delete_many({"code": {"$in": target_codes}})
                await log_event(ADMIN_ID, "ADMIN", f"Web Range Del: {prefix}{s_int:03d}-{e_int:03d} ({res.deleted_count} items)")
                return "success", res.deleted_count
            except ValueError:
                return "invalid_range", 0

    try:
        status, count = run_async(_del())
        if status == "db_unavailable":
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        if status == "not_found":
            return jsonify({"success": False, "error": f"File '{code}' not found."}), 404
        if status == "range_limit":
            return jsonify({"success": False, "error": "Range error: Maximum 100 items can be deleted at once."}), 400
        if status == "invalid_range":
            return jsonify({"success": False, "error": "Start and end must be integers."}), 400
        return jsonify({"success": True, "message": f"Deleted {count} asset(s)."})
    except Exception as e:
        logger.error(f"Admin del error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/label/set', methods=['POST'])
@admin_required
def api_admin_media_label_set():
    """Assigns custom catalog title and category (movie or series) to a prefix."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    category = str(payload.get('category', 'movie')).strip().lower()
    title = str(payload.get('title') or payload.get('name') or '').strip()

    if not prefix or not title:
        return jsonify({"success": False, "error": "Prefix and title are required."}), 400
    if category not in ["movie", "series"]:
        return jsonify({"success": False, "error": "Category must be 'movie' or 'series'."}), 400

    async def _set():
        if prefix_labels_col is None:
            return False
        await prefix_labels_col.update_one(
            {"prefix": prefix},
            {"$set": {"name": title, "category": category}},
            upsert=True
        )
        await log_event(ADMIN_ID, "ADMIN", f"Web Set Label: {prefix} -> {title} [{category}]")
        return True

    try:
        run_async(_set())
        return jsonify({"success": True, "message": f"Label set for {prefix}: {title} [{category}]."})
    except Exception as e:
        logger.error(f"Set label error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/label/del', methods=['POST'])
@admin_required
def api_admin_media_label_del():
    """Removes catalog title/category label from a prefix."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    if not prefix:
        return jsonify({"success": False, "error": "Prefix is required."}), 400

    async def _del():
        if prefix_labels_col is None:
            return False
        res = await prefix_labels_col.delete_one({"prefix": prefix})
        await log_event(ADMIN_ID, "ADMIN", f"Web Del Label: {prefix}")
        return res.deleted_count > 0

    try:
        deleted = run_async(_del())
        if not deleted:
            return jsonify({"success": False, "error": f"No label found for {prefix}."}), 404
        return jsonify({"success": True, "message": f"Label for {prefix} removed."})
    except Exception as e:
        logger.error(f"Del label error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/tmdb/set', methods=['POST'])
@admin_required
def api_admin_media_tmdb_set():
    """Links TMDB metadata (poster, backdrop, rating, synopsis) to prefix."""
    from services.tmdb import fetch_tmdb_metadata
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    query = str(payload.get('query', '')).strip()

    if not prefix or not query:
        return jsonify({"success": False, "error": "Prefix and query / TMDB ID are required."}), 400

    async def _set():
        if prefix_labels_col is None:
            return "db_unavailable", None
        rec = await prefix_labels_col.find_one({"prefix": prefix})
        category = rec.get("category", "movie") if rec else "movie"
        tmdb_info = await fetch_tmdb_metadata(query, category)
        if not tmdb_info:
            return "not_found", None
        tmdb_info["pinned"] = True
        await prefix_labels_col.update_one(
            {"prefix": prefix},
            {"$set": {"tmdb": tmdb_info}, "$setOnInsert": {"name": tmdb_info.get("title", prefix), "category": category}},
            upsert=True
        )
        await log_event(ADMIN_ID, "ADMIN", f"Web TMDB Set: {prefix} -> {tmdb_info.get('title')}")
        return "success", tmdb_info

    try:
        status, data = run_async(_set())
        if status == "db_unavailable":
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        if status == "not_found":
            return jsonify({"success": False, "error": f"No TMDB matches found for '{query}'."}), 404
        return jsonify({"success": True, "message": f"Linked TMDB: {data.get('title')}", "tmdb": data})
    except Exception as e:
        logger.error(f"Set TMDB error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/tmdb/del', methods=['POST'])
@admin_required
def api_admin_media_tmdb_del():
    """Unlinks TMDB metadata from a prefix."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    if not prefix:
        return jsonify({"success": False, "error": "Prefix is required."}), 400

    async def _del():
        if prefix_labels_col is None:
            return False
        res = await prefix_labels_col.update_one({"prefix": prefix}, {"$unset": {"tmdb": ""}})
        await log_event(ADMIN_ID, "ADMIN", f"Web TMDB Del: {prefix}")
        return res.modified_count > 0

    try:
        modified = run_async(_del())
        if not modified:
            return jsonify({"success": False, "error": f"No TMDB metadata linked to {prefix}."}), 404
        return jsonify({"success": True, "message": f"TMDB metadata unlinked from {prefix}."})
    except Exception as e:
        logger.error(f"Del TMDB error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/banner', methods=['POST'])
@admin_required
def api_admin_media_banner():
    """Controls banner mode (poster vs backdrop), custom banner URL, or reset."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    action = str(payload.get('action', 'toggle')).strip().lower()
    url = str(payload.get('url') or payload.get('banner_url') or '').strip()

    if not prefix:
        return jsonify({"success": False, "error": "Prefix is required."}), 400

    async def _banner():
        if prefix_labels_col is None:
            return "db_unavailable", ""
        rec = await prefix_labels_col.find_one({"prefix": prefix})
        if not rec:
            return "not_found", ""

        if action == "toggle":
            current_mode = rec.get("banner_mode", "poster")
            new_mode = "backdrop" if current_mode == "poster" else "poster"
            await prefix_labels_col.update_one(
                {"prefix": prefix},
                {"$set": {"banner_mode": new_mode}, "$unset": {"custom_banner": ""}}
            )
            await log_event(ADMIN_ID, "ADMIN", f"Web Banner Toggle: {prefix} -> {new_mode}")
            return "toggled", new_mode
        elif action in ["reset", "clear", "del"]:
            await prefix_labels_col.update_one({"prefix": prefix}, {"$unset": {"custom_banner": ""}})
            await log_event(ADMIN_ID, "ADMIN", f"Web Banner Reset: {prefix}")
            return "reset", ""
        elif action in ["set", "url"]:
            if not url or not (url.startswith("http://") or url.startswith("https://")):
                return "invalid_url", ""
            await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"custom_banner": url}})
            await log_event(ADMIN_ID, "ADMIN", f"Web Banner Set: {prefix} -> {url}")
            return "set", url
        return "invalid_action", ""

    try:
        status, val = run_async(_banner())
        if status == "db_unavailable":
            return jsonify({"success": False, "error": "Database unavailable."}), 500
        if status == "not_found":
            return jsonify({"success": False, "error": f"Partition {prefix} not found."}), 404
        if status == "invalid_url":
            return jsonify({"success": False, "error": "Valid HTTP/HTTPS image URL is required."}), 400
        if status == "invalid_action":
            return jsonify({"success": False, "error": "Action must be 'toggle', 'set', or 'reset'."}), 400
        msg = f"Banner mode set to {val.upper()}." if status == "toggled" else ("Custom banner set." if status == "set" else "Custom banner reset to TMDB.")
        return jsonify({"success": True, "message": msg})
    except Exception as e:
        logger.error(f"Banner action error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/season/set', methods=['POST'])
@admin_required
def api_admin_media_season_set():
    """Maps season episode range for series."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    try:
        sn_num = int(payload.get('num') or payload.get('season_num') or 1)
        start = int(payload.get('start') or payload.get('start_ep') or 1)
        end = int(payload.get('end') or payload.get('end_ep') or 1)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Season num, start, and end must be integers."}), 400

    if not prefix:
        return jsonify({"success": False, "error": "Prefix is required."}), 400
    if start < 1 or end < start:
        return jsonify({"success": False, "error": "Start must be >= 1 and end must be >= start."}), 400

    async def _set():
        if prefix_labels_col is None:
            return False
        rec = await prefix_labels_col.find_one({"prefix": prefix})
        seasons = rec.get("seasons", []) if rec else []
        seasons = [s for s in seasons if s.get("num") != sn_num]
        seasons.append({"num": sn_num, "start": start, "end": end})
        seasons.sort(key=lambda x: x["num"])
        await prefix_labels_col.update_one(
            {"prefix": prefix},
            {"$set": {"seasons": seasons, "category": "series"}},
            upsert=True
        )
        await log_event(ADMIN_ID, "ADMIN", f"Web Season Set: {prefix} S{sn_num} ({start}-{end})")
        return True

    try:
        run_async(_set())
        return jsonify({"success": True, "message": f"Season {sn_num} ({start}-{end}) mapped for {prefix}."})
    except Exception as e:
        logger.error(f"Season set error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/season/del', methods=['POST'])
@admin_required
def api_admin_media_season_del():
    """Deletes specific season mapping."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    try:
        sn_num = int(payload.get('num') or payload.get('season_num') or 1)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "Season num must be an integer."}), 400

    async def _del():
        if prefix_labels_col is None:
            return False
        rec = await prefix_labels_col.find_one({"prefix": prefix})
        if not rec or "seasons" not in rec:
            return False
        seasons = [s for s in rec.get("seasons", []) if s.get("num") != sn_num]
        await prefix_labels_col.update_one({"prefix": prefix}, {"$set": {"seasons": seasons}})
        await log_event(ADMIN_ID, "ADMIN", f"Web Season Del: {prefix} S{sn_num}")
        return True

    try:
        deleted = run_async(_del())
        if not deleted:
            return jsonify({"success": False, "error": f"Season {sn_num} not found for {prefix}."}), 404
        return jsonify({"success": True, "message": f"Season {sn_num} deleted for {prefix}."})
    except Exception as e:
        logger.error(f"Season del error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/season/clear', methods=['POST'])
@admin_required
def api_admin_media_season_clear():
    """Clears all mapped seasons for a prefix."""
    payload = request.get_json(force=True, silent=True) or {}
    prefix = str(payload.get('prefix', '')).strip().upper()[:3]
    if not prefix:
        return jsonify({"success": False, "error": "Prefix is required."}), 400

    async def _clear():
        if prefix_labels_col is None:
            return False
        await prefix_labels_col.update_one({"prefix": prefix}, {"$unset": {"seasons": ""}})
        await log_event(ADMIN_ID, "ADMIN", f"Web Season Clear: {prefix}")
        return True

    try:
        run_async(_clear())
        return jsonify({"success": True, "message": f"All mapped seasons cleared for {prefix}."})
    except Exception as e:
        logger.error(f"Season clear error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@flask_app.route('/api/admin/media/refresh', methods=['POST'])
@admin_required
def api_admin_media_refresh():
    """Triggers synchronization of TMDB metadata for all linked media."""
    from services.tmdb import fetch_tmdb_metadata
    async def _refresh():
        if prefix_labels_col is None:
            return 0
        cursor = prefix_labels_col.find({"tmdb": {"$exists": True, "$ne": None}})
        synced = 0
        async for rec in cursor:
            tmdb_obj = rec.get("tmdb")
            if not tmdb_obj:
                continue
            query = str(tmdb_obj.get("id") or tmdb_obj.get("title") or rec.get("name") or rec.get("prefix"))
            cat = rec.get("category", "movie")
            try:
                new_info = await fetch_tmdb_metadata(query, cat)
                if new_info:
                    new_info["pinned"] = True
                    await prefix_labels_col.update_one({"_id": rec["_id"]}, {"$set": {"tmdb": new_info}})
                    synced += 1
            except Exception:
                continue
        await log_event(ADMIN_ID, "ADMIN", f"Web Media Refresh: {synced} titles updated")
        return synced

    try:
        count = run_async(_refresh())
        return jsonify({"success": True, "message": f"Successfully refreshed metadata for {count} titles."})
    except Exception as e:
        logger.error(f"Metadata refresh error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# --- SERVER RUNNER ---

def start_server_daemon(host='0.0.0.0', port=PORT):
    """Starts the Flask app inside a background daemon thread with Werkzeug make_server."""
    server = make_server(host, port, flask_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
