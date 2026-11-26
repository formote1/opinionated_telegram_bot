from config import logger

logger.info("Initializing TimezoneFinder...")
try:
    from timezonefinder import TimezoneFinder
    tf = TimezoneFinder()
    logger.info("TimezoneFinder initialized.")
except Exception as e:
    logger.warning("TimezoneFinder not available, falling back to default timezone: %s", type(e).__name__)
    tf = None

