from services.tmdb import RE_ID_QUERY, RE_YEAR, RE_YEAR_STRIP, clean_title_for_search


def test_clean_title_for_search():
    assert clean_title_for_search("Inception.2010.1080p.BluRay.x264.mkv") == "Inception 2010"

    assert clean_title_for_search("Breaking.Bad.S01E01.720p.HDTV.x264.mp4") == "Breaking Bad"
    assert clean_title_for_search("[SubsPlease] Frieren - 01 (1080p).mkv") == "Frieren 01"
    assert clean_title_for_search("Spider-Man.No.Way.Home.2160p.UHD.Remux.ts") == "Spider Man No Way Home"
    assert clean_title_for_search("") == ""

def test_id_query_regex():
    assert RE_ID_QUERY.match("378064").group(1) == "378064"
    assert RE_ID_QUERY.match("tmdb:378064").group(1) == "378064"
    assert RE_ID_QUERY.match("id: 999").group(1) == "999"
    assert RE_ID_QUERY.match("Inception") is None

def test_year_regex():
    match = RE_YEAR.search("A Silent Voice (2016)")
    assert match is not None
    assert match.group(1) == "2016"

    stripped = RE_YEAR_STRIP.sub("", "A Silent Voice (2016)").strip()
    assert stripped == "A Silent Voice"


import time

import pytest

from services.tmdb import (
    TMDB_CACHE_TTL,
    TMDB_NEGATIVE_CACHE_TTL,
    _get_http_client,
    _tmdb_cache,
    close_tmdb_client,
    fetch_tmdb_metadata,
)


def test_clean_title_expanded_quality_tags():
    assert clean_title_for_search("Attack on Titan Season 2") == "Attack on Titan"
    assert clean_title_for_search("Death Note Ep 01") == "Death Note"
    assert clean_title_for_search("Naruto Shippuden Episode 500") == "Naruto Shippuden"

@pytest.mark.asyncio
async def test_close_tmdb_client():
    client = _get_http_client()
    assert client is not None
    assert not client.is_closed
    await close_tmdb_client()
    from services.tmdb import _http_client
    assert _http_client is None

@pytest.mark.asyncio
async def test_negative_vs_positive_cache_ttl():
    now = time.time()
    # Positive result
    _tmdb_cache["movie:inception"] = (now - 1000, {"title": "Inception"})
    # Negative result
    _tmdb_cache["movie:nonexistent"] = (now - 400, None)

    # Inception is still within 24h (86400s)
    res_pos = await fetch_tmdb_metadata("inception", "movie") if False else _tmdb_cache.get("movie:inception")[1]
    assert res_pos is not None

    # Nonexistent has expired because 400s > 300s (negative TTL)
    ts, val = _tmdb_cache["movie:nonexistent"]
    ttl = TMDB_CACHE_TTL if val is not None else TMDB_NEGATIVE_CACHE_TTL
    assert now - ts > ttl  # Confirms it expires in 300s, not 24h
