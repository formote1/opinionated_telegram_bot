import time

# In-memory storage for sliding window timestamps: user_id -> list of float timestamps
_user_hits: dict[int, list[float]] = {}
_last_prune_time = 0.0


def is_rate_limited(user_id: int, max_hits: int = 10, window_seconds: int = 30) -> tuple[bool, int]:
    """
    Checks if a user has exceeded the request threshold within the sliding window.
    Returns (is_limited: bool, retry_after_seconds: int).
    """
    global _last_prune_time
    now = time.time()
    cutoff = now - window_seconds
    user_timestamps = _user_hits.get(user_id)
    timestamps = [t for t in user_timestamps if t > cutoff] if user_timestamps else []

    if len(timestamps) >= max_hits:
        oldest_hit = timestamps[0]
        retry_after = int(window_seconds - (now - oldest_hit)) + 1
        _user_hits[user_id] = timestamps
        return True, max(1, retry_after)

    timestamps.append(now)
    _user_hits[user_id] = timestamps

    # Periodic memory pruning for inactive users (at most once every 60s when large)
    if len(_user_hits) > 200 and (now - _last_prune_time > 60.0):
        _last_prune_time = now
        stale = [u for u, ts in _user_hits.items() if not ts or ts[-1] <= cutoff]
        for u in stale:
            _user_hits.pop(u, None)

    return False, 0


