import re
import json
import urllib.parse
import urllib.request
import asyncio
from config import TMDB_API_KEY, logger


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
    # Strip file extensions
    cleaned = re.sub(r'\.(mkv|mp4|avi|mov|flv|wmv|ts|m4v)$', '', raw_title, flags=re.IGNORECASE)
    # Replace dots, underscores, dashes with spaces
    cleaned = re.sub(r'[\._\-+]', ' ', cleaned)
    # Remove resolution and quality tags
    cleaned = re.sub(
        r'\b(2160p|1080p|720p|480p|4k|uhd|webrip|web-dl|bluray|brrip|dvdrip|hdrip|x264|x265|hevc|aac|ac3|dts|remux|repack|dual|audio|hindi|eng|sub|season|s\d+|e\d+)\b',
        '',
        cleaned,
        flags=re.IGNORECASE
    )
    # Remove brackets and parenthesis content
    cleaned = re.sub(r'\[.*?\]|\(.*?\)', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned or raw_title.strip()


async def fetch_tmdb_metadata(title: str, category: str = "movie") -> dict | None:
    """
    Fetches rich movie/series metadata (official poster, backdrop, overview synopsis, genres, year, rating) from TMDB.
    Supports:
    - Direct TMDB ID lookup (e.g. '378064' or 'tmdb:378064')
    - Year-filtered search (e.g. 'A Silent Voice 2016' or '(2016)')
    - Dual-endpoint fallback (movie <-> tv)
    - Prioritizing official theatrical posters over backdrops
    """
    if not TMDB_API_KEY or not title:
        return None

    def _make_request(url: str) -> dict | None:
        try:
            headers = {
                "Accept": "application/json",
                "User-Agent": "OpinionatedTelegramBot/1.0"
            }
            if len(TMDB_API_KEY) > 40:
                headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
            else:
                sep = "&" if "?" in url else "?"
                url += f"{sep}api_key={TMDB_API_KEY}"

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=7) as response:
                if response.status == 200:
                    return json.loads(response.read().decode('utf-8'))
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

    def _search():
        try:
            raw_query = title.strip()
            primary_endpoint = "movie" if category.lower() == "movie" else "tv"
            secondary_endpoint = "tv" if primary_endpoint == "movie" else "movie"

            # 1. Direct TMDB ID lookup if query is an ID or prefixed with tmdb: / id:
            id_match = re.match(r'^(?:tmdb:|id:)?\s*(\d+)$', raw_query, flags=re.IGNORECASE)
            if id_match:
                tmdb_id = id_match.group(1)
                for ep in [primary_endpoint, secondary_endpoint]:
                    data = _make_request(f"https://api.themoviedb.org/3/{ep}/{tmdb_id}?language=en-US")
                    if data and (data.get("title") or data.get("name")):
                        return _format_item(data, raw_query)

            # 2. Extract Year if explicitly present in the query (e.g. 'A Silent Voice 2016' or '(2016)')
            year_match = re.search(r'\b(19\d\d|20\d\d)\b', raw_query)
            extracted_year = year_match.group(1) if year_match else None
            title_without_year = re.sub(r'[\(\[\{]?\s*\b(19\d\d|20\d\d)\b\s*[\)\]\}]?', '', raw_query).strip()
            cleaned_query = clean_title_for_search(title_without_year)

            def _query_search(ep: str, q: str, yr: str | None = None) -> list:
                if not q:
                    return []
                encoded_q = urllib.parse.quote(q)
                url = f"https://api.themoviedb.org/3/search/{ep}?query={encoded_q}&include_adult=false&language=en-US&page=1"
                if yr:
                    param = "year" if ep == "movie" else "first_air_date_year"
                    url += f"&{param}={yr}"
                data = _make_request(url)
                return data.get("results", []) if data else []

            # Try query permutations
            results = []
            endpoints = [primary_endpoint, secondary_endpoint]

            # Pass A: With Year filter if provided
            if extracted_year:
                for ep in endpoints:
                    results = _query_search(ep, title_without_year, extracted_year)
                    if not results and cleaned_query:
                        results = _query_search(ep, cleaned_query, extracted_year)
                    if results:
                        break

            # Pass B: General search without Year constraint
            if not results:
                for ep in endpoints:
                    for candidate_q in [raw_query, title_without_year, cleaned_query]:
                        if candidate_q:
                            results = _query_search(ep, candidate_q)
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

    return await asyncio.to_thread(_search)

