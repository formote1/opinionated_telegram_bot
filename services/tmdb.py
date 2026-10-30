import re
import time
import urllib.parse

import httpx

from config import TMDB_API_KEY, logger

# Pre-compiled regular expressions for high-performance title cleaning & query extraction
RE_EXTENSIONS = re.compile(r'\.(mkv|mp4|avi|mov|flv|wmv|ts|m4v)$', re.IGNORECASE)
RE_DELIMITERS = re.compile(r'[\._\-+]')
RE_QUALITY_TAGS = re.compile(
    r'\b(2160p|1080p|720p|480p|4k|uhd|webrip|web-dl|web|hdtv|bluray|brrip|dvdrip|hdrip|x264|x265|hevc|aac|ac3|dts|remux|repack|dual|audio|hindi|eng|sub|season\s*\d+|season|s\d+\s*e\d+|s\d+|episode\s*\d+|ep\s*\d+|e\d+)\b',
    re.IGNORECASE
)

RE_BRACKETS = re.compile(r'\[.*?\]|\(.*?\)')
RE_SPACES = re.compile(r'\s+')
RE_ID_QUERY = re.compile(r'^(?:tmdb:|id:)?\s*(\d+)$', re.IGNORECASE)
RE_YEAR = re.compile(r'\b(19\d\d|20\d\d)\b')
RE_YEAR_STRIP = re.compile(r'[\(\[\{]?\s*\b(19\d\d|20\d\d)\b\s*[\)\]\}]?')

# In-memory TMDB cache: cache_key -> (timestamp, data)
_tmdb_cache: dict[str, tuple[float, dict | None]] = {}
TMDB_CACHE_TTL = 86400  # 24 hours for successful metadata
TMDB_NEGATIVE_CACHE_TTL = 300  # 5 minutes for missing/failed queries
_last_tmdb_prune = 0.0

# Persistent HTTPX client for HTTP connection pooling & keep-alive reuse
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(8.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            headers={"User-Agent": "OpinionatedTelegramBot/1.0", "Accept": "application/json"}
        )
    return _http_client


async def close_tmdb_client():
    """Gracefully shuts down the persistent HTTPX AsyncClient and releases connections."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
        _http_client = None



TMDB_GENRES = {
    28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime",
    99: "Documentary", 18: "Drama", 10751: "Family", 14: "Fantasy", 36: "History",
    27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
    10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
    10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality",
    10765: "Sci-Fi & Fantasy", 10766: "Soap", 10767: "Talk", 10768: "War & Politics"
}


def clean_title_for_search(raw_title: str) -> str:
    """Cleans noisy file names, quality tags, and scene notations into searchable titles."""
    if not raw_title:
        return ""
    cleaned = RE_EXTENSIONS.sub('', raw_title)
    cleaned = RE_DELIMITERS.sub(' ', cleaned)
    cleaned = RE_QUALITY_TAGS.sub('', cleaned)
    cleaned = RE_BRACKETS.sub('', cleaned)
    cleaned = RE_SPACES.sub(' ', cleaned).strip()
    return cleaned or raw_title.strip()



async def fetch_tmdb_metadata(title: str, category: str = "movie") -> dict | None:
    """
    Fetches rich movie/series metadata (official poster, backdrop, overview synopsis, genres, year, rating) from TMDB.
    Supports:
    - Direct TMDB ID lookup (e.g. '378064' or 'tmdb:378064')
    - Year-filtered search (e.g. 'A Silent Voice 2016' or '(2016)')
    - Dual-endpoint fallback (movie <-> tv)
    - Prioritizing official theatrical posters over backdrops
    - In-memory 24-hour TTL caching for zero latency on repeated queries
    """
    if not TMDB_API_KEY or not title:
        return None

    cache_key = f"{category.lower()}:{title.strip().lower()}"
    if cache_key in _tmdb_cache:
        ts, cached_val = _tmdb_cache[cache_key]
        ttl = TMDB_CACHE_TTL if cached_val is not None else TMDB_NEGATIVE_CACHE_TTL
        if time.time() - ts < ttl:
            return cached_val

    async def _make_request(url: str) -> dict | None:
        try:
            client = _get_http_client()
            headers = {}
            if len(TMDB_API_KEY) > 40:
                headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
            else:
                sep = "&" if "?" in url else "?"
                url += f"{sep}api_key={TMDB_API_KEY}"

            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            logger.warning(f"TMDB request failed for {url}: {e}")
        return None

    def _format_item(item: dict, fallback_title: str) -> dict:
        poster_path = item.get("poster_path")
        backdrop_path = item.get("backdrop_path")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
        backdrop_url = f"https://image.tmdb.org/t/p/w780{backdrop_path}" if backdrop_path else None
        banner_url = poster_url or backdrop_url

        release_date = item.get("release_date") or item.get("first_air_date") or ""
        year = release_date.split("-")[0] if release_date else ""

        # Genres could be list of dicts (from direct ID lookup) or list of ints (from search)
        raw_genres = item.get("genres") or []
        if raw_genres and isinstance(raw_genres[0], dict):
            genres_str = ", ".join([g.get("name", "") for g in raw_genres[:3] if g.get("name")])
        else:
            raw_genre_ids = item.get("genre_ids", [])
            genre_names = [TMDB_GENRES[gid] for gid in raw_genre_ids if gid in TMDB_GENRES]
            genres_str = ", ".join(genre_names[:3]) if genre_names else ""

        raw_vote = item.get("vote_average", 0.0)
        rating_val = round(float(raw_vote), 1) if raw_vote else 0.0

        return {
            "tmdb_id": item.get("id"),
            "title": item.get("title") or item.get("name") or fallback_title,
            "overview": item.get("overview", "").strip(),
            "poster_url": poster_url,
            "backdrop_url": backdrop_url,
            "banner_url": banner_url,
            "genres": genres_str,
            "year": year,
            "rating": rating_val
        }

    async def _search():
        try:
            raw_query = title.strip()
            primary_endpoint = "movie" if category.lower() == "movie" else "tv"
            secondary_endpoint = "tv" if primary_endpoint == "movie" else "movie"

            # 1. Direct TMDB ID lookup if query is an ID or prefixed with tmdb: / id:
            id_match = RE_ID_QUERY.match(raw_query)
            if id_match:
                tmdb_id = id_match.group(1)
                for ep in [primary_endpoint, secondary_endpoint]:
                    data = await _make_request(f"https://api.themoviedb.org/3/{ep}/{tmdb_id}?language=en-US")
                    if data and (data.get("title") or data.get("name")):
                        return _format_item(data, raw_query)

            # 2. Extract Year if explicitly present in the query (e.g. 'A Silent Voice 2016' or '(2016)')
            year_match = RE_YEAR.search(raw_query)
            extracted_year = year_match.group(1) if year_match else None
            title_without_year = RE_YEAR_STRIP.sub('', raw_query).strip()
            cleaned_query = clean_title_for_search(title_without_year)

            async def _query_search(ep: str, q: str, yr: str | None = None) -> list:
                if not q:
                    return []
                encoded_q = urllib.parse.quote(q)
                url = f"https://api.themoviedb.org/3/search/{ep}?query={encoded_q}&include_adult=false&language=en-US&page=1"
                if yr:
                    param = "year" if ep == "movie" else "first_air_date_year"
                    url += f"&{param}={yr}"
                data = await _make_request(url)
                return data.get("results", []) if data else []

            # Try query permutations with prioritized deduplication (cleaned_query first)
            results = []
            endpoints = [primary_endpoint, secondary_endpoint]
            candidates = []
            for candidate in [cleaned_query, title_without_year, raw_query]:
                candidate_clean = candidate.strip()
                if candidate_clean and candidate_clean not in candidates:
                    candidates.append(candidate_clean)

            # Pass A: With Year filter if provided
            if extracted_year:
                for ep in endpoints:
                    for candidate_q in candidates:
                        results = await _query_search(ep, candidate_q, extracted_year)
                        if results:
                            break
                    if results:
                        break

            # Pass B: General search without Year constraint
            if not results:
                for ep in endpoints:
                    for candidate_q in candidates:
                        results = await _query_search(ep, candidate_q)
                        if results:
                            break
                    if results:
                        break

            if not results:
                return None

            first = results[0]
            # Fallback for missing poster image if subsequent results have it
            if not first.get("poster_path") and len(results) > 1:
                for alt in results[1:3]:
                    if alt.get("poster_path"):
                        first["poster_path"] = alt.get("poster_path")
                        break

            return _format_item(first, raw_query)
        except Exception as e:
            logger.warning(f"TMDB query search pipeline error for '{title}': {e}")
            return None

    global _last_tmdb_prune
    result = await _search()
    now = time.time()
    _tmdb_cache[cache_key] = (now, result)
    if len(_tmdb_cache) > 500 and (now - _last_tmdb_prune > 300.0):
        _last_tmdb_prune = now
        stale_keys = [
            k for k, (t, v) in _tmdb_cache.items()
            if now - t > (TMDB_CACHE_TTL if v is not None else TMDB_NEGATIVE_CACHE_TTL)
        ]
        for k in stale_keys:
            _tmdb_cache.pop(k, None)
    return result



