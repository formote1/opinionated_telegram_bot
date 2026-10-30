import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId

from config import ADMIN_ID
from server import flask_app, run_async, serialize_doc
from utils.auth import generate_auth_token


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    flask_app.config["SECRET_KEY"] = "test-secret-key-123"
    with flask_app.test_client() as c:
        yield c


def test_run_async_standalone():
    async def sample_coro():
        await asyncio.sleep(0.01)
        return "async_success"

    res = run_async(sample_coro())
    assert res == "async_success"


def test_index_route(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Opinionated" in res.data or b"Web Companion" in res.data


def test_login_and_logout_routes(client):
    token = generate_auth_token(12345, "alice")
    res = client.get(f"/login?token={token}", follow_redirects=False)
    assert res.status_code == 302
    assert "auth=success" in res.headers["Location"]

    # Verify session established
    with client.session_transaction() as sess:
        assert sess.get("user_id") == 12345
        assert sess.get("username") == "alice"

    # Test logout
    logout_res = client.get("/logout", follow_redirects=False)
    assert logout_res.status_code == 302
    with client.session_transaction() as sess:
        assert sess.get("user_id") is None


def test_api_status(client):
    res = client.get("/api/status")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "operational"


def test_api_auth_me(client):
    # Unauthenticated
    res = client.get("/api/auth/me")
    assert res.status_code == 200
    assert res.get_json()["authenticated"] is False

    # Authenticated
    with client.session_transaction() as sess:
        sess["user_id"] = 777
        sess["username"] = "bob"
        sess["is_admin"] = False

    res2 = client.get("/api/auth/me")
    data2 = res2.get_json()
    assert data2["authenticated"] is True
    assert data2["user"]["user_id"] == 777
    assert data2["user"]["username"] == "bob"


def test_api_auth_token(client):
    token = generate_auth_token(888, "carol")
    res = client.post("/api/auth/token", json={"token": token})
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["user"]["user_id"] == 888

    # Bad token
    bad_res = client.post("/api/auth/token", json={"token": "invalid.token"})
    assert bad_res.status_code == 401


def test_api_ascii_fonts(client):
    res = client.get("/api/ascii/fonts")
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert "ansi_shadow" in data["fonts"]


def test_api_ascii_generate(client):
    res = client.post("/api/ascii", json={"text": "HELLO", "font": "ansi_shadow", "max_width": 80})
    assert res.status_code == 200
    data = res.get_json()
    assert data["success"] is True
    assert data["art"] is not None
    assert len(data["art"]) > 0

    # Empty text error
    res_empty = client.post("/api/ascii", json={"text": ""})
    assert res_empty.status_code == 400


def test_api_reminders_crud(client):
    user_id = 9001
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = "tester"

    fake_id = ObjectId()
    sample_reminder = {
        "_id": fake_id,
        "user_id": user_id,
        "label": "Pay Rent",
        "target_date": "2030-01-01",
        "reminder_time": "09:00",
        "category": "Bill",
        "recurrence": "once",
        "timezone": "UTC"
    }

    with patch("server.reminders_col") as mock_col:
        # 1. GET /api/reminders
        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[sample_reminder])
        mock_col.find.return_value.sort.return_value = mock_cursor

        res_list = client.get("/api/reminders")
        assert res_list.status_code == 200
        data_list = res_list.get_json()
        assert data_list["success"] is True
        assert len(data_list["reminders"]) == 1
        assert data_list["reminders"][0]["label"] == "Pay Rent"

        # 2. POST /api/reminders
        mock_col.insert_one = AsyncMock(return_value=MagicMock(inserted_id=fake_id))
        res_create = client.post("/api/reminders", json={
            "label": "Doctor Appointment",
            "target_date": "2030-05-15",
            "reminder_time": "14:00",
            "category": "General",
            "recurrence": "once"
        })
        assert res_create.status_code == 200
        assert res_create.get_json()["success"] is True

        # 3. POST snooze
        mock_col.find_one = AsyncMock(return_value=dict(sample_reminder))
        mock_col.update_one = AsyncMock()
        res_snooze = client.post(f"/api/reminders/{str(fake_id)}/snooze")
        assert res_snooze.status_code == 200
        assert res_snooze.get_json()["target_date"] == "2030-01-02"

        # 4. POST done (one-off completes and deletes)
        mock_col.delete_one = AsyncMock(return_value=MagicMock(deleted_count=1))
        res_done = client.post(f"/api/reminders/{str(fake_id)}/done")
        assert res_done.status_code == 200
        assert res_done.get_json()["action"] == "completed"

        # 5. DELETE reminder
        res_del = client.delete(f"/api/reminders/{str(fake_id)}")
        assert res_del.status_code == 200
        assert res_del.get_json()["success"] is True


def test_api_media_catalog(client):
    with patch("server.prefix_labels_col") as mock_labels, \
         patch("server.group_keys_col") as mock_keys, \
         patch("server.codes_col") as mock_codes:

        mock_cursor = MagicMock()
        mock_cursor.to_list = AsyncMock(return_value=[
            {
                "prefix": "MOV",
                "name": "Movie Title",
                "category": "movie",
                "tmdb": {"rating": 8.5, "year": "2024"},
                "seasons": []
            }
        ])
        mock_labels.find.return_value.sort.return_value = mock_cursor

        # Test categories
        res_cats = client.get("/api/media/categories")
        assert res_cats.status_code == 200
        assert len(res_cats.get_json()["categories"]) == 3

        # Test items list
        res_items = client.get("/api/media/items?category=movie")
        assert res_items.status_code == 200
        items = res_items.get_json()["items"]
        assert len(items) == 1
        assert items[0]["prefix"] == "MOV"

        # Test item detail
        mock_labels.find_one = AsyncMock(return_value={
            "prefix": "MOV",
            "name": "Movie Title",
            "category": "movie",
            "tmdb": {"rating": 8.5}
        })
        code_cursor = MagicMock()
        code_cursor.to_list = AsyncMock(return_value=[
            {"code": "MOV001", "file_name": "movie.mp4", "chat_id": 100}
        ])
        mock_codes.find.return_value.sort.return_value = code_cursor
        mock_keys.find_one = AsyncMock(return_value=None)  # not locked

        res_detail = client.get("/api/media/item/MOV")
        assert res_detail.status_code == 200
        detail = res_detail.get_json()["item"]
        assert detail["prefix"] == "MOV"
        assert len(detail["files"]) == 1


def test_api_media_unlock_and_lockout(client):
    user_id = 99991
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = "unlocker"

    with patch("server.group_keys_col") as mock_keys, \
         patch("server.unlocked_groups_col") as mock_unlocked:

        mock_keys.find_one = AsyncMock(return_value={
            "prefix": "VIP",
            "chat_id": 100,
            "secret_key": "secret123"
        })
        mock_unlocked.update_one = AsyncMock()

        # Bad passkey
        bad_res = client.post("/api/media/unlock", json={"prefix": "VIP", "passkey": "wrong"})
        assert bad_res.status_code == 400
        assert "Incorrect" in bad_res.get_json()["error"]

        # Valid passkey
        good_res = client.post("/api/media/unlock", json={"prefix": "VIP", "passkey": "secret123"})
        assert good_res.status_code == 200
        assert good_res.get_json()["success"] is True


def test_api_admin_routes(client):
    # Non-admin forbidden
    with client.session_transaction() as sess:
        sess["user_id"] = 1111111  # Not ADMIN_ID
        sess["is_admin"] = False

    res_forbidden = client.get("/api/admin/stats")
    assert res_forbidden.status_code == 403

    # Admin authorized
    admin_uid = ADMIN_ID if ADMIN_ID != 0 else 7777777
    with client.session_transaction() as sess:
        sess["user_id"] = admin_uid
        sess["is_admin"] = True

    with patch("server.ADMIN_ID", admin_uid), \
         patch("server.reminders_col") as mock_r, \
         patch("server.codes_col") as mock_c, \
         patch("server.logs_col") as mock_l, \
         patch("server.group_keys_col") as mock_k, \
         patch("server.users_col") as mock_u:

        mock_r.count_documents = AsyncMock(return_value=10)
        mock_c.count_documents = AsyncMock(return_value=50)
        mock_l.count_documents = AsyncMock(return_value=120)
        mock_k.count_documents = AsyncMock(return_value=5)
        mock_u.count_documents = AsyncMock(return_value=25)

        res_stats = client.get("/api/admin/stats")
        assert res_stats.status_code == 200
        stats = res_stats.get_json()["stats"]
        assert stats["reminders"] == 10
        assert stats["assets"] == 50
        assert stats["locked_prefixes"] == 5
        assert stats["logs"] == 120
        assert stats["users"] == 25


def test_api_reminders_unauthenticated(client):
    res = client.get("/api/reminders")
    assert res.status_code == 401
    assert "Authentication required" in res.get_json()["error"]

    res_post = client.post("/api/reminders", json={"label": "Test", "target_date": "2030-01-01"})
    assert res_post.status_code == 401


def test_api_reminders_validation_errors(client):
    with client.session_transaction() as sess:
        sess["user_id"] = 1234
        sess["username"] = "validuser"

    # Empty label
    res1 = client.post("/api/reminders", json={"label": "", "target_date": "2030-01-01"})
    assert res1.status_code == 400
    assert "label is required" in res1.get_json()["error"]

    # Invalid date format
    res2 = client.post("/api/reminders", json={"label": "Test", "target_date": "invalid-date"})
    assert res2.status_code == 400
    assert "Invalid date format" in res2.get_json()["error"]


def test_api_media_unlock_lockout_enforced(client):
    user_id = 776655
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["username"] = "locked_user"

    with patch("server.is_locked_out", return_value=(True, 1800)):
        res = client.post("/api/media/unlock", json={"prefix": "LOK", "passkey": "anything"})
        assert res.status_code == 429
        assert "Locked out" in res.get_json()["error"]


def test_api_admin_user_actions(client):
    admin_uid = ADMIN_ID if ADMIN_ID != 0 else 7777777
    with client.session_transaction() as sess:
        sess["user_id"] = admin_uid
        sess["is_admin"] = True

    with patch("server.ADMIN_ID", admin_uid), \
         patch("server.unlocked_groups_col") as mock_unlocked, \
         patch("server.group_keys_col") as mock_keys:

        mock_unlocked.update_one = AsyncMock()
        mock_keys.find_one = AsyncMock(return_value={"prefix": "ABC", "chat_id": 123})

        # Test grant
        res_grant = client.post("/api/admin/user/grant", json={"user_id": 999, "prefix": "ABC"})
        assert res_grant.status_code == 200
        assert res_grant.get_json()["success"] is True

        # Test ban
        res_ban = client.post("/api/admin/user/ban", json={"user_id": 999})
        assert res_ban.status_code == 200
        assert "Banned" in res_ban.get_json()["message"]

        # Test invalid action
        res_invalid = client.post("/api/admin/user/invalid_action", json={"user_id": 999})
        assert res_invalid.status_code == 400


def test_api_admin_file_lock_unlock(client):
    admin_uid = ADMIN_ID if ADMIN_ID != 0 else 7777777
    with client.session_transaction() as sess:
        sess["user_id"] = admin_uid
        sess["is_admin"] = True

    with patch("server.ADMIN_ID", admin_uid), \
         patch("server.group_keys_col") as mock_keys, \
         patch("server.invalidate_group_keys_cache"):

        mock_keys.update_one = AsyncMock()
        mock_keys.delete_one = AsyncMock()
        mock_keys.delete_many = AsyncMock()

        # Test lock
        res_lock = client.post("/api/admin/file/lock", json={"prefix": "XYZ", "password": "pass"})
        assert res_lock.status_code == 200
        assert res_lock.get_json()["success"] is True

        # Test unlock
        res_unlock = client.post("/api/admin/file/unlock", json={"prefix": "XYZ"})
        assert res_unlock.status_code == 200
        assert res_unlock.get_json()["success"] is True


def test_bearer_token_authentication(client):
    token = generate_auth_token(555, "bearertester")
    # Verify /api/auth/me accepts Bearer header
    res = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["authenticated"] is True
    assert data["user"]["user_id"] == 555
    assert data["user"]["username"] == "bearertester"

    # Verify protected /api/reminders accepts Bearer header
    with patch("server.reminders_col") as mock_reminders:
        cursor = MagicMock()
        cursor.sort.return_value.to_list = AsyncMock(return_value=[])
        mock_reminders.find.return_value = cursor
        rem_res = client.get("/api/reminders", headers={"Authorization": f"Bearer {token}"})
        assert rem_res.status_code == 200
        assert rem_res.get_json()["success"] is True


def test_api_ascii_invalid_max_width(client):
    # Non-integer string max_width should fallback to 80, not 500
    res = client.post("/api/ascii", json={"text": "Test", "font": "slant", "max_width": "invalid"})
    assert res.status_code == 200
    assert res.get_json()["success"] is True

    # Clamping extreme values
    res_high = client.post("/api/ascii", json={"text": "Test", "font": "slant", "max_width": 99999})
    assert res_high.status_code == 200
    res_low = client.post("/api/ascii", json={"text": "Test", "font": "slant", "max_width": -100})
    assert res_low.status_code == 200


def test_serialize_doc_with_date_and_string_id():
    test_doc = {
        "_id": "custom-string-id",
        "created_date": date(2025, 5, 20),
        "obj_id": ObjectId(),
        "nested": {
            "due": date(2025, 6, 1)
        }
    }
    serialized = serialize_doc(test_doc)
    assert serialized["id"] == "custom-string-id"
    assert serialized["_id"] == "custom-string-id"
    assert serialized["created_date"] == "2025-05-20"
    assert serialized["nested"]["due"] == "2025-06-01"
    assert isinstance(serialized["obj_id"], str)


def test_api_reminders_past_date_validation(client):
    token = generate_auth_token(333, "dater")
    headers = {"Authorization": f"Bearer {token}"}

    # One-off past date must fail
    res_past = client.post(
        "/api/reminders",
        headers=headers,
        json={"label": "Past Event", "target_date": "2020-01-01", "recurrence": "once"}
    )
    assert res_past.status_code == 400
    assert "cannot be in the past" in res_past.get_json()["error"]

    # Recurring past date auto-rolls forward
    with patch("server.reminders_col") as mock_reminders:
        mock_reminders.insert_one = AsyncMock(return_value=MagicMock(inserted_id=ObjectId()))
        res_rec = client.post(
            "/api/reminders",
            headers=headers,
            json={"label": "Annual Event", "target_date": "2020-01-01", "recurrence": "annually"}
        )
        assert res_rec.status_code == 200
        assert res_rec.get_json()["success"] is True
        saved = res_rec.get_json()["reminder"]
        # Rolled forward target_date is >= 2025
        assert int(saved["target_date"][:4]) >= 2025


def test_api_media_deliver_safe_job_queue(client):
    token = generate_auth_token(1212, "deliveryuser")
    headers = {"Authorization": f"Bearer {token}"}

    mock_app = MagicMock()
    mock_app.bot = MagicMock()
    mock_app.job_queue = None  # None job_queue to verify SafeJobQueue fallback

    with patch("server._application", mock_app), \
         patch("server.codes_col") as mock_codes, \
         patch("server.is_user_banned", new=AsyncMock(return_value=False)), \
         patch("server.check_access_gate", new=AsyncMock(return_value=(True, "MOV", 12345, False, False))), \
         patch("server.execute_file_delivery", new_callable=AsyncMock) as mock_delivery:

        mock_codes.find_one = AsyncMock(return_value={
            "code": "MOV001",
            "chat_id": 12345,
            "message_id": 67890
        })
        mock_delivery.return_value = MagicMock()  # successful sent message

        res = client.post("/api/media/deliver", headers=headers, json={"code": "MOV001"})
        assert res.status_code == 200
        assert res.get_json()["success"] is True
        mock_delivery.assert_awaited_once()


def test_api_admin_user_action_with_username(client):
    admin_uid = ADMIN_ID if ADMIN_ID != 0 else 7777777
    token = generate_auth_token(admin_uid, "admin")
    headers = {"Authorization": f"Bearer {token}"}

    with patch("server.ADMIN_ID", admin_uid), \
         patch("server.resolve_target_user", new_callable=AsyncMock) as mock_resolve, \
         patch("server.unlocked_groups_col") as mock_unlocked:

        mock_resolve.return_value = (888888, "badactor")
        mock_unlocked.update_one = AsyncMock()

        res = client.post("/api/admin/user/ban", headers=headers, json={"user_id": "@badactor"})
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        assert "Banned user 888888" in data["message"]
        mock_resolve.assert_awaited_once()


def test_api_admin_keys_with_zero_codes(client):
    admin_uid = ADMIN_ID if ADMIN_ID != 0 else 7777777
    token = generate_auth_token(admin_uid, "admin")
    headers = {"Authorization": f"Bearer {token}"}

    with patch("server.ADMIN_ID", admin_uid), \
         patch("server.codes_col") as mock_codes, \
         patch("server.group_keys_col") as mock_keys, \
         patch("server.prefix_labels_col") as mock_labels:

        # Zero codes returned by aggregation
        codes_cursor = MagicMock()
        codes_cursor.to_list = AsyncMock(return_value=[])
        mock_codes.aggregate.return_value = codes_cursor

        # But a locked key exists in group_keys_col
        keys_cursor = MagicMock()
        keys_cursor.to_list = AsyncMock(return_value=[{"prefix": "GHOST", "secret_key": "pass123"}])
        mock_keys.find.return_value = keys_cursor

        labels_cursor = MagicMock()
        labels_cursor.to_list = AsyncMock(return_value=[{"prefix": "GHOST", "label": "Ghost Series"}])
        mock_labels.find.return_value = labels_cursor

        res = client.get("/api/admin/keys", headers=headers)
        assert res.status_code == 200
        data = res.get_json()
        assert data["success"] is True
        prefixes = [k["prefix"] for k in data["keys"]]
        assert "GHOST" in prefixes
        ghost_item = next(k for k in data["keys"] if k["prefix"] == "GHOST")
        assert ghost_item["name"] == "Ghost Series"
        assert ghost_item["secret_key"] == "pass123"
        assert ghost_item["count"] == 0

