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


async def fetch_tmdb_metadata(title: str, category: str = "movie") -> dict | None:
    """
    Fetches rich movie/series metadata (banner, poster, synopsis, genres, year, rating) from TMDB.
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
                        backdrop_path = first.get("backdrop_path")
                        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
                        backdrop_url = f"https://image.tmdb.org/t/p/w780{backdrop_path}" if backdrop_path else None
                        banner_url = backdrop_url or poster_url

                        release_date = first.get("release_date") or first.get("first_air_date") or ""
                        year = release_date.split("-")[0] if release_date else ""

                        # Extract human-readable genre list
                        raw_genre_ids = first.get("genre_ids", [])
                        genre_names = [TMDB_GENRES[gid] for gid in raw_genre_ids if gid in TMDB_GENRES]
                        genres_str = ", ".join(genre_names[:3]) if genre_names else ""

                        raw_vote = first.get("vote_average", 0.0)
                        rating_val = round(float(raw_vote), 1) if raw_vote else 0.0

                        return {
                            "tmdb_id": first.get("id"),
                            "title": first.get("title") or first.get("name"),
                            "overview": first.get("overview", "").strip(),
                            "poster_url": poster_url,
                            "backdrop_url": backdrop_url,
                            "banner_url": banner_url,
                            "genres": genres_str,
                            "year": year,
                            "rating": rating_val
                        }
        except Exception as e:
            logger.warning(f"TMDB query error for '{title}': {e}")
            return None
        return None

    return await asyncio.to_thread(_search)
