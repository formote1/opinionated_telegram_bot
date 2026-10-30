import time

from services.access import (
    _group_keys_cache,
    invalidate_group_keys_cache,
    is_locked_out,
    record_failed_attempt,
    reset_failed_attempts,
)


def test_failed_attempts_lockout():
    user_id = 888801
    reset_failed_attempts(user_id)

    # 4 attempts should not lock out
    for i in range(4):
        fails, locked = record_failed_attempt(user_id)
        assert fails == i + 1
        assert locked is False
        is_locked, rem = is_locked_out(user_id)
        assert is_locked is False

    # 5th attempt triggers lockout
    fails, locked = record_failed_attempt(user_id)
    assert fails == 5
    assert locked is True

    is_locked, rem = is_locked_out(user_id)
    assert is_locked is True
    assert 3500 <= rem <= 3600

    # Reset cleans it up
    reset_failed_attempts(user_id)
    is_locked, rem = is_locked_out(user_id)
    assert is_locked is False
    assert rem == 0


def test_group_keys_cache_invalidation():
    invalidate_group_keys_cache()
    _group_keys_cache[(123, "ABC")] = (time.time(), {"secret_key": "pass1"})
    _group_keys_cache[(123, "XYZ")] = (time.time(), {"secret_key": "pass2"})

    # Invalidate single prefix
    invalidate_group_keys_cache("ABC")
    assert (123, "ABC") not in _group_keys_cache
    assert (123, "XYZ") in _group_keys_cache

    # Invalidate all
    invalidate_group_keys_cache()
    assert len(_group_keys_cache) == 0


from unittest.mock import AsyncMock, patch

import pytest

from services.access import check_access_gate, get_group_gate, is_user_banned


@pytest.mark.asyncio
async def test_get_group_gate_cached():
    invalidate_group_keys_cache()
    _group_keys_cache[(123, "LOK")] = (time.time(), {"secret_key": "pass123"})

    # Cache hit (even with lowercase input prefix)
    gate = await get_group_gate(123, "lok")
    assert gate is not None
    assert gate["secret_key"] == "pass123"

@pytest.mark.asyncio
async def test_is_user_banned():
    with patch("services.access.unlocked_groups_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value={"user_id": 444, "is_banned": True})
        banned = await is_user_banned(444)
        assert banned is True

        mock_col.find_one = AsyncMock(return_value={"user_id": 555, "is_banned": False})
        not_banned = await is_user_banned(555)
        assert not_banned is False

@pytest.mark.asyncio
async def test_check_access_gate_banned_user():
    with patch("services.access.unlocked_groups_col") as mock_col:
        mock_col.find_one = AsyncMock(return_value={"user_id": 9999, "is_banned": True})
        authorized, prefix, chat_id, frozen, expired = await check_access_gate(
            9999, {"code": "FRE001", "chat_id": 100}
        )
        assert authorized is False
