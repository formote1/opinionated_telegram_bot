import asyncio
import threading
from flask import Flask, request
from werkzeug.serving import make_server
from telegram import Update
from config import TOKEN, PORT, logger

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
            update_data = request.get_json(force=True)
            asyncio.run_coroutine_threadsafe(
                _application.process_update(Update.de_json(update_data, _application.bot)),
                _main_loop
            )
        except Exception as e:
            logger.error(f"Webhook processing error: {e}")
    return "OK"


def start_server_daemon(host='0.0.0.0', port=PORT):
    """Starts the Flask app inside a background daemon thread with Werkzeug make_server."""
    server = make_server(host, port, flask_app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
