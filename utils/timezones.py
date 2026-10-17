from timezonefinder import TimezoneFinder
from config import logger

logger.info("Initializing TimezoneFinder...")
try:
    tf = TimezoneFinder()
    logger.info("TimezoneFinder initialized.")
except Exception as e:
    logger.error(f"Failed to initialize TimezoneFinder: {e}")
    tf = None
