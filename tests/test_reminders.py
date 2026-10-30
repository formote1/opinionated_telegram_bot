from handlers.admin import parse_duration
from handlers.reminders import format_time_left


def test_format_time_left():
    assert format_time_left(0) == "today"
    assert format_time_left(1) == "1 day"
    assert format_time_left(-1) == "yesterday"
    assert format_time_left(-5) == "5 days ago"
    assert "week" in format_time_left(7)
    assert "month" in format_time_left(30)
    assert "2 months" in format_time_left(65)

def test_parse_duration():
    import time
    now = time.time()

    t_m = parse_duration("30m")
    assert t_m is not None
    assert int(t_m - now) in range(1790, 1810)

    t_h = parse_duration("24h")
    assert t_h is not None
    assert int(t_h - now) in range(86390, 86410)

    t_d = parse_duration("7d")
    assert t_d is not None
    assert int(t_d - now) in range(604790, 604810)

    assert parse_duration("invalid") is None
    assert parse_duration("10x") is None
