import json
import urllib.parse
import urllib.request
import asyncio
from config import TMDB_API_KEY, logger


async def fetch_tmdb_metadata(title: str, category: str = "movie") -> dict | None:
    """
    Fetches rich movie/series metadata (poster, synopsis, year, rating) from TMDB.
    Gracefully returns None if TMDB_API_KEY is not set or if no match is found.
    """
    if not TMDB_API_KEY:
        return None

    def _search():
        try:
            endpoint = "movie" if category.lower() == "movie" else "tv"
            encoded_query = urllib.parse.quote(title.strip())
            url = f"https://api.themoviedb.org/3/search/{endpoint}?query={encoded_query}&include_adult=false&language=en-US&page=1"

            headers = {
                "Accept": "application/json",
                "User-Agent": "OpinionatedTelegramBot/1.0"
            }
            if len(TMDB_API_KEY) > 40:
                headers["Authorization"] = f"Bearer {TMDB_API_KEY}"
            else:
                url += f"&api_key={TMDB_API_KEY}"

            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=5) as response:
                if response.status == 200:
                    data = json.loads(response.read().decode('utf-8'))
                    results = data.get("results", [])
                    if results:
                        first = results[0]
                        poster_path = first.get("poster_path")
                        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
                        release_date = first.get("release_date") or first.get("first_air_date") or ""
                        year = release_date.split("-")[0] if release_date else ""

                        return {
                            "tmdb_id": first.get("id"),
                            "title": first.get("title") or first.get("name"),
                            "overview": first.get("overview", ""),
                            "poster_url": poster_url,
                            "year": year,
                            "rating": first.get("vote_average", 0.0)
                        }
        except Exception as e:
            logger.warning(f"TMDB query error for '{title}': {e}")
            return None
        return None

    return await asyncio.to_thread(_search)
