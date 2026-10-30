import time

from utils.rate_limiter import _user_hits, is_rate_limited


def test_rate_limiter_allow_under_threshold():
    user_id = 999901
    _user_hits.pop(user_id, None)

    for _ in range(5):
        limited, retry_after = is_rate_limited(user_id, max_hits=5, window_seconds=10)
        assert not limited
        assert retry_after == 0

def test_rate_limiter_blocks_over_threshold():
    user_id = 999902
    _user_hits.pop(user_id, None)

    for _ in range(5):
        is_rate_limited(user_id, max_hits=5, window_seconds=10)

    limited, retry_after = is_rate_limited(user_id, max_hits=5, window_seconds=10)
    assert limited is True
    assert 1 <= retry_after <= 10

def test_rate_limiter_resets_after_window():
    user_id = 999903
    _user_hits[user_id] = [time.time() - 20]

    limited, retry_after = is_rate_limited(user_id, max_hits=2, window_seconds=5)
    assert limited is False
    assert retry_after == 0
