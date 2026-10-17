import time
from collections import defaultdict

# In-memory storage for sliding window timestamps: user_id -> list of float timestamps
_user_hits = defaultdict(list)


def is_rate_limited(user_id: int, max_hits: int = 10, window_seconds: int = 30) -> tuple[bool, int]:
    """
    Checks if a user has exceeded the request threshold within the sliding window.
    Returns (is_limited: bool, retry_after_seconds: int).
    """
    now = time.time()
    cutoff = now - window_seconds
    timestamps = [t for t in _user_hits[user_id] if t > cutoff]
    
    if len(timestamps) >= max_hits:
        oldest_hit = timestamps[0]
        retry_after = int(window_seconds - (now - oldest_hit)) + 1
        _user_hits[user_id] = timestamps
        return True, max(1, retry_after)
        
    timestamps.append(now)
    _user_hits[user_id] = timestamps
    return False, 0
