from config import logger

logger.info("Initializing TimezoneFinder...")
try:
    from timezonefinder import TimezoneFinder
    tf = TimezoneFinder()
    logger.info("TimezoneFinder initialized.")
except Exception as e:
    logger.warning(f"TimezoneFinder not available, falling back to default timezone: {e}")
    tf = None

