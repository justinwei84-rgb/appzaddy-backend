"""
Google Custom Search integration for company research.
Max 4 queries per company, 5 results per query.
Results cached in Redis and Postgres (TTL 48h).

Returns (snippets, queries_made) so callers can record usage.
"""

import httpx
import re
from typing import List, Tuple

from app.config import settings

BASE_URL = "https://www.googleapis.com/customsearch/v1"


def normalize_company_name(name: str) -> str:
    """Lowercase, strip legal suffixes and punctuation for cache keying."""
    name = name.lower().strip()
    name = re.sub(
        r"\b(inc|llc|ltd|corp|corporation|co|group|holdings|technologies|tech)\b\.?",
        "",
        name,
    )
    name = re.sub(r"[^a-z0-9 ]", "", name)
    return " ".join(name.split())


def _build_queries(company_name: str) -> List[str]:
    """Build up to 4 targeted search queries."""
    return [
        f'"{company_name}" funding OR investment OR Series',
        f'"{company_name}" layoffs OR "laid off" OR downsizing',
        f'"{company_name}" reviews Glassdoor OR Indeed OR "company culture"',
        f'"{company_name}" news 2024 OR 2025',
    ]


async def search_company(company_name: str) -> Tuple[List[str], int]:
    """
    Run up to 4 Google Custom Search queries for a company.
    Returns (snippets, queries_made):
      - snippets: flat list of result snippets (max 20 total)
      - queries_made: number of HTTP requests actually sent (for usage billing)
    Fails gracefully if API limit is reached.
    """
    if not settings.google_cse_api_key or not settings.google_cse_id:
        return [f"No search API configured. Limited data available for {company_name}."], 0

    queries = _build_queries(company_name)[: settings.google_search_max_queries]
    snippets: List[str] = []
    queries_made = 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        for query in queries:
            try:
                resp = await client.get(
                    BASE_URL,
                    params={
                        "key": settings.google_cse_api_key,
                        "cx": settings.google_cse_id,
                        "q": query,
                        "num": settings.google_search_results_per_query,
                    },
                )
                if resp.status_code == 429:
                    # Hit daily quota — stop gracefully (don't count this as a billed query)
                    break
                if resp.status_code == 403:
                    # API not enabled or key misconfigured — no point retrying
                    return (
                        [
                            f"Google Search API permission denied for {company_name}. "
                            "Enable the Custom Search JSON API in Google Cloud Console."
                        ],
                        queries_made,
                    )
                resp.raise_for_status()
                queries_made += 1
                data = resp.json()
                for item in data.get("items", []):
                    snippet = item.get("snippet", "").strip()
                    if snippet:
                        snippets.append(snippet)
            except httpx.HTTPError:
                # Network error — continue with what we have
                continue

    return snippets or [f"No public information found for {company_name}."], queries_made
