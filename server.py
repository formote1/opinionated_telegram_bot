import asyncio
import logging
import threading

from flask import Flask, request
from telegram import Update
from werkzeug.serving import make_server

from config import PORT, TOKEN, logger

# Silence per-request werkzeug stdout logging to improve throughput
logging.getLogger("werkzeug").setLevel(logging.WARNING)

flask_app = Flask(__name__)
_application = None
_main_loop = None


def init_server(application, main_loop):
    """Binds the Telegram Application and event loop to the webhook server."""
    global _application, _main_loop
    _application = application
    _main_loop = main_loop


@flask_app.route('/')
def health():
    return "Ultimate Master Bot Online."


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


def start_server_daemon(host='0.0.0.0', port=PORT):
    """Starts the Flask app inside a background daemon thread with Werkzeug make_server."""
    server = make_server(host, port, flask_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


