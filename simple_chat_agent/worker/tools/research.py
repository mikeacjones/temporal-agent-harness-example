from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_harness.streaming import StreamContext
from agent_harness.tool_types import ToolType
from agent_harness.tools import ToolContext, ToolResult, tool


SEARCH_WEB_TOOL = "search_web"
SEARCH_FACT_CHECKS_TOOL = "search_fact_checks"
LOOKUP_ENTITY_TOOL = "lookup_entity"
SEARCH_BOOKS_TOOL = "search_books"
SEARCH_YOUTUBE_TOOL = "search_youtube"
CHECK_URL_SAFETY_TOOL = "check_url_safety"

GOOGLE_API_KEY = "GOOGLE_API_KEY"
GOOGLE_FACT_CHECK_API_KEY = "GOOGLE_FACT_CHECK_API_KEY"
GOOGLE_KNOWLEDGE_GRAPH_API_KEY = "GOOGLE_KNOWLEDGE_GRAPH_API_KEY"
GOOGLE_BOOKS_API_KEY = "GOOGLE_BOOKS_API_KEY"
GOOGLE_YOUTUBE_API_KEY = "GOOGLE_YOUTUBE_API_KEY"
GOOGLE_SAFE_BROWSING_API_KEY = "GOOGLE_SAFE_BROWSING_API_KEY"
SEARXNG_BASE_URL = "SIMPLE_CHAT_SEARXNG_BASE_URL"

GOOGLE_RESEARCH_API_KEY_NAMES = (
    GOOGLE_API_KEY,
    GOOGLE_FACT_CHECK_API_KEY,
    GOOGLE_KNOWLEDGE_GRAPH_API_KEY,
    GOOGLE_BOOKS_API_KEY,
    GOOGLE_YOUTUBE_API_KEY,
    GOOGLE_SAFE_BROWSING_API_KEY,
)


class ResearchProvider:
    @tool(
        name=SEARCH_WEB_TOOL,
        description=(
            "Search the web through the app's internal SearXNG service. Use this "
            "for current public web research, finding source URLs, and getting "
            "short snippets before fetching individual pages."
        ),
        tool_type=ToolType.READ,
    )
    async def search_web(
        self,
        ctx: ToolContext,
        query: str,
        max_results: int = 8,
        category: str = "general",
        language: str = "auto",
        time_range: str | None = None,
    ) -> ToolResult:
        payload = await ctx.activity(
            _search_web_activity,
            args={
                "query": query,
                "max_results": max_results,
                "category": category,
                "language": language,
                "time_range": time_range,
            },
            step="searxng",
        )
        return ToolResult(payload=payload, error="error" in payload)

    @tool(
        name=SEARCH_FACT_CHECKS_TOOL,
        description=(
            "Search Google's Fact Check Tools index for published fact checks "
            "matching a claim or topic."
        ),
        tool_type=ToolType.READ,
    )
    async def search_fact_checks(
        self,
        ctx: ToolContext,
        query: str,
        max_results: int = 10,
        language_code: str | None = None,
    ) -> ToolResult:
        payload = await ctx.activity(
            _search_fact_checks_activity,
            args={
                "query": query,
                "max_results": max_results,
                "language_code": language_code,
            },
            step="fact_checks",
        )
        return ToolResult(payload=payload, error="error" in payload)

    @tool(
        name=LOOKUP_ENTITY_TOOL,
        description=(
            "Look up people, organizations, places, products, and other entities "
            "in Google's Knowledge Graph."
        ),
        tool_type=ToolType.READ,
    )
    async def lookup_entity(
        self,
        ctx: ToolContext,
        query: str,
        max_results: int = 5,
        types: str | None = None,
    ) -> ToolResult:
        payload = await ctx.activity(
            _lookup_entity_activity,
            args={
                "query": query,
                "max_results": max_results,
                "types": types,
            },
            step="knowledge_graph",
        )
        return ToolResult(payload=payload, error="error" in payload)

    @tool(
        name=SEARCH_BOOKS_TOOL,
        description=(
            "Search Google Books for book metadata, authors, publication dates, "
            "ISBNs, and preview links."
        ),
        tool_type=ToolType.READ,
    )
    async def search_books(
        self,
        ctx: ToolContext,
        query: str,
        max_results: int = 10,
        print_type: str = "all",
        order_by: str = "relevance",
    ) -> ToolResult:
        payload = await ctx.activity(
            _search_books_activity,
            args={
                "query": query,
                "max_results": max_results,
                "print_type": print_type,
                "order_by": order_by,
            },
            step="books",
        )
        return ToolResult(payload=payload, error="error" in payload)

    @tool(
        name=SEARCH_YOUTUBE_TOOL,
        description=(
            "Search public YouTube videos and channels by query. Use this when "
            "the user asks for talks, conference videos, demos, or video sources."
        ),
        tool_type=ToolType.READ,
    )
    async def search_youtube(
        self,
        ctx: ToolContext,
        query: str,
        max_results: int = 5,
        result_type: str = "video",
    ) -> ToolResult:
        payload = await ctx.activity(
            _search_youtube_activity,
            args={
                "query": query,
                "max_results": max_results,
                "result_type": result_type,
            },
            step="youtube",
        )
        return ToolResult(payload=payload, error="error" in payload)

    @tool(
        name=CHECK_URL_SAFETY_TOOL,
        description=(
            "Check one or more URLs against Google Safe Browsing for suspected "
            "malware, social engineering, unwanted software, or harmful apps."
        ),
        tool_type=ToolType.READ,
    )
    async def check_url_safety(
        self,
        ctx: ToolContext,
        urls: list[str],
    ) -> ToolResult:
        payload = await ctx.activity(
            _check_url_safety_activity,
            args={"urls": urls},
            step="safe_browsing",
        )
        return ToolResult(payload=payload, error="error" in payload)


def configured_research_tool_names() -> list[str]:
    names: list[str] = []
    if _env(SEARXNG_BASE_URL):
        names.append(SEARCH_WEB_TOOL)
    if _google_api_key(GOOGLE_FACT_CHECK_API_KEY):
        names.append(SEARCH_FACT_CHECKS_TOOL)
    if _google_api_key(GOOGLE_KNOWLEDGE_GRAPH_API_KEY):
        names.append(LOOKUP_ENTITY_TOOL)
    if _google_api_key(GOOGLE_BOOKS_API_KEY):
        names.append(SEARCH_BOOKS_TOOL)
    if _google_api_key(GOOGLE_YOUTUBE_API_KEY):
        names.append(SEARCH_YOUTUBE_TOOL)
    if _google_api_key(GOOGLE_SAFE_BROWSING_API_KEY):
        names.append(CHECK_URL_SAFETY_TOOL)
    return names


async def _search_web_activity(
    query: str,
    max_results: int = 8,
    category: str = "general",
    language: str = "auto",
    time_range: str | None = None,
    *,
    stream: StreamContext,
) -> dict[str, Any]:
    base_url = _env(SEARXNG_BASE_URL)
    if not base_url:
        return {"error": f"{SEARXNG_BASE_URL} is not configured."}
    query = query.strip()
    if not query:
        return {"error": "query is required."}

    max_results = _clamp(max_results, 1, 20)
    params: dict[str, str | int] = {
        "q": query,
        "format": "json",
        "categories": category or "general",
        "language": language or "auto",
    }
    if time_range:
        params["time_range"] = time_range

    await stream.emit(
        {"query": query, "max_results": max_results, "category": params["categories"]},
        kind="search_start",
    )
    response = await asyncio.to_thread(
        _get_json,
        _join_url(base_url, "/search"),
        params,
        None,
    )
    if "error" in response:
        await stream.emit(response, kind="search_complete")
        return response

    results = [
        _searxng_result(result)
        for result in list(response.get("results") or [])[:max_results]
        if isinstance(result, dict)
    ]
    payload = {
        "query": query,
        "results": results,
        "suggestions": list(response.get("suggestions") or [])[:5],
        "engine_errors": response.get("unresponsive_engines") or [],
    }
    await stream.emit(
        {"query": query, "result_count": len(results)},
        kind="search_complete",
    )
    return payload


async def _search_fact_checks_activity(
    query: str,
    max_results: int = 10,
    language_code: str | None = None,
    *,
    stream: StreamContext,
) -> dict[str, Any]:
    api_key = _google_api_key(GOOGLE_FACT_CHECK_API_KEY)
    if not api_key:
        return {"error": _google_api_key_error(GOOGLE_FACT_CHECK_API_KEY)}
    query = query.strip()
    if not query:
        return {"error": "query is required."}

    max_results = _clamp(max_results, 1, 20)
    params: dict[str, str | int] = {"query": query, "pageSize": max_results}
    if language_code:
        params["languageCode"] = language_code

    await stream.emit({"query": query, "max_results": max_results}, kind="fact_check_start")
    response = await asyncio.to_thread(
        _get_json,
        "https://factchecktools.googleapis.com/v1alpha1/claims:search",
        params,
        api_key,
    )
    if "error" in response:
        await stream.emit(response, kind="fact_check_complete")
        return response

    claims = [
        _fact_check_claim(claim)
        for claim in response.get("claims") or []
        if isinstance(claim, dict)
    ]
    payload = {"query": query, "claims": claims[:max_results]}
    await stream.emit(
        {"query": query, "claim_count": len(payload["claims"])},
        kind="fact_check_complete",
    )
    return payload


async def _lookup_entity_activity(
    query: str,
    max_results: int = 5,
    types: str | None = None,
    *,
    stream: StreamContext,
) -> dict[str, Any]:
    api_key = _google_api_key(GOOGLE_KNOWLEDGE_GRAPH_API_KEY)
    if not api_key:
        return {"error": _google_api_key_error(GOOGLE_KNOWLEDGE_GRAPH_API_KEY)}
    query = query.strip()
    if not query:
        return {"error": "query is required."}

    max_results = _clamp(max_results, 1, 20)
    params: dict[str, str | int | bool] = {
        "query": query,
        "limit": max_results,
        "indent": False,
    }
    if types:
        params["types"] = types

    await stream.emit({"query": query, "max_results": max_results}, kind="entity_start")
    response = await asyncio.to_thread(
        _get_json,
        "https://kgsearch.googleapis.com/v1/entities:search",
        params,
        api_key,
    )
    if "error" in response:
        await stream.emit(response, kind="entity_complete")
        return response

    entities = [
        _knowledge_graph_entity(item)
        for item in response.get("itemListElement") or []
        if isinstance(item, dict)
    ]
    payload = {"query": query, "entities": entities[:max_results]}
    await stream.emit(
        {"query": query, "entity_count": len(payload["entities"])},
        kind="entity_complete",
    )
    return payload


async def _search_books_activity(
    query: str,
    max_results: int = 10,
    print_type: str = "all",
    order_by: str = "relevance",
    *,
    stream: StreamContext,
) -> dict[str, Any]:
    api_key = _google_api_key(GOOGLE_BOOKS_API_KEY)
    if not api_key:
        return {"error": _google_api_key_error(GOOGLE_BOOKS_API_KEY)}
    query = query.strip()
    if not query:
        return {"error": "query is required."}

    max_results = _clamp(max_results, 1, 20)
    params = {
        "q": query,
        "maxResults": max_results,
        "printType": print_type if print_type in {"all", "books", "magazines"} else "all",
        "orderBy": order_by if order_by in {"relevance", "newest"} else "relevance",
    }

    await stream.emit({"query": query, "max_results": max_results}, kind="books_start")
    response = await asyncio.to_thread(
        _get_json,
        "https://www.googleapis.com/books/v1/volumes",
        params,
        api_key,
    )
    if "error" in response:
        await stream.emit(response, kind="books_complete")
        return response

    books = [
        _book_result(item)
        for item in response.get("items") or []
        if isinstance(item, dict)
    ]
    payload = {
        "query": query,
        "total_items": response.get("totalItems"),
        "books": books[:max_results],
    }
    await stream.emit(
        {"query": query, "book_count": len(payload["books"])},
        kind="books_complete",
    )
    return payload


async def _search_youtube_activity(
    query: str,
    max_results: int = 5,
    result_type: str = "video",
    *,
    stream: StreamContext,
) -> dict[str, Any]:
    api_key = _google_api_key(GOOGLE_YOUTUBE_API_KEY)
    if not api_key:
        return {"error": _google_api_key_error(GOOGLE_YOUTUBE_API_KEY)}
    query = query.strip()
    if not query:
        return {"error": "query is required."}

    max_results = _clamp(max_results, 1, 10)
    allowed_types = {"video", "channel", "playlist"}
    if result_type not in allowed_types:
        result_type = "video"
    params = {
        "part": "snippet",
        "q": query,
        "maxResults": max_results,
        "type": result_type,
        "safeSearch": "moderate",
    }

    await stream.emit(
        {"query": query, "max_results": max_results, "result_type": result_type},
        kind="youtube_start",
    )
    response = await asyncio.to_thread(
        _get_json,
        "https://www.googleapis.com/youtube/v3/search",
        params,
        api_key,
    )
    if "error" in response:
        await stream.emit(response, kind="youtube_complete")
        return response

    results = [
        _youtube_result(item)
        for item in response.get("items") or []
        if isinstance(item, dict)
    ]
    payload = {"query": query, "results": results[:max_results]}
    await stream.emit(
        {"query": query, "result_count": len(payload["results"])},
        kind="youtube_complete",
    )
    return payload


async def _check_url_safety_activity(
    urls: list[str],
    *,
    stream: StreamContext,
) -> dict[str, Any]:
    api_key = _google_api_key(GOOGLE_SAFE_BROWSING_API_KEY)
    if not api_key:
        return {"error": _google_api_key_error(GOOGLE_SAFE_BROWSING_API_KEY)}
    clean_urls = [url.strip() for url in urls if isinstance(url, str) and url.strip()]
    if not clean_urls:
        return {"error": "At least one URL is required."}
    clean_urls = clean_urls[:20]

    body = {
        "client": {
            "clientId": "temporal-agent-harness",
            "clientVersion": "0.1",
        },
        "threatInfo": {
            "threatTypes": [
                "MALWARE",
                "SOCIAL_ENGINEERING",
                "UNWANTED_SOFTWARE",
                "POTENTIALLY_HARMFUL_APPLICATION",
            ],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": url} for url in clean_urls],
        },
    }

    await stream.emit({"url_count": len(clean_urls)}, kind="safe_browsing_start")
    response = await asyncio.to_thread(
        _post_json,
        "https://safebrowsing.googleapis.com/v4/threatMatches:find",
        body,
        api_key,
    )
    if "error" in response:
        await stream.emit(response, kind="safe_browsing_complete")
        return response

    matches = response.get("matches") or []
    payload = {
        "checked_urls": clean_urls,
        "safe": not matches,
        "matches": matches,
    }
    await stream.emit(
        {"url_count": len(clean_urls), "match_count": len(matches)},
        kind="safe_browsing_complete",
    )
    return payload


def _get_json(
    url: str,
    params: dict[str, Any],
    api_key: str | None,
) -> dict[str, Any]:
    separator = "&" if "?" in url else "?"
    full_url = f"{url}{separator}{urlencode(_clean_params(params))}"
    request = _request(full_url, api_key=api_key)
    return _read_json(request)


def _post_json(url: str, body: dict[str, Any], api_key: str) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    request = _request(url, api_key=api_key, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    return _read_json(request)


def _request(
    url: str,
    *,
    api_key: str | None,
    data: bytes | None = None,
    method: str = "GET",
) -> Request:
    headers = {
        "Accept": "application/json",
        "User-Agent": "temporal-agent-harness-example/0.1",
    }
    if api_key:
        headers["x-goog-api-key"] = api_key
    return Request(url, data=data, headers=headers, method=method)


def _read_json(request: Request) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read(2_000_000)
            if len(raw) >= 2_000_000:
                return {"error": "Response exceeded the 2 MB limit."}
            return json.loads(raw.decode("utf-8"))
    except HTTPError as err:
        body = err.read(4000).decode("utf-8", errors="replace")
        return {"error": f"HTTP {err.code}: {err.reason}", "detail": body}
    except URLError as err:
        return {"error": str(err.reason)}
    except TimeoutError:
        return {"error": "Request timed out."}
    except json.JSONDecodeError as err:
        return {"error": f"Response was not valid JSON: {err}"}


def _searxng_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": result.get("title"),
        "url": result.get("url"),
        "content": result.get("content"),
        "engine": result.get("engine"),
        "category": result.get("category"),
        "score": result.get("score"),
        "published_date": result.get("publishedDate") or result.get("published_date"),
    }


def _fact_check_claim(claim: dict[str, Any]) -> dict[str, Any]:
    reviews = []
    for review in claim.get("claimReview") or []:
        if not isinstance(review, dict):
            continue
        publisher = review.get("publisher") or {}
        reviews.append(
            {
                "publisher": publisher.get("name") if isinstance(publisher, dict) else None,
                "site": publisher.get("site") if isinstance(publisher, dict) else None,
                "url": review.get("url"),
                "title": review.get("title"),
                "rating": review.get("textualRating"),
                "review_date": review.get("reviewDate"),
                "language_code": review.get("languageCode"),
            }
        )
    return {
        "text": claim.get("text"),
        "claimant": claim.get("claimant"),
        "claim_date": claim.get("claimDate"),
        "reviews": reviews,
    }


def _knowledge_graph_entity(item: dict[str, Any]) -> dict[str, Any]:
    result = item.get("result") or {}
    detailed = result.get("detailedDescription") or {}
    return {
        "name": result.get("name"),
        "description": result.get("description"),
        "types": result.get("@type") or [],
        "id": result.get("@id"),
        "url": result.get("url"),
        "score": item.get("resultScore"),
        "detailed_description": (
            {
                "text": detailed.get("articleBody"),
                "url": detailed.get("url"),
                "license": detailed.get("license"),
            }
            if isinstance(detailed, dict)
            else None
        ),
    }


def _book_result(item: dict[str, Any]) -> dict[str, Any]:
    volume = item.get("volumeInfo") or {}
    return {
        "id": item.get("id"),
        "title": volume.get("title"),
        "subtitle": volume.get("subtitle"),
        "authors": volume.get("authors") or [],
        "publisher": volume.get("publisher"),
        "published_date": volume.get("publishedDate"),
        "description": volume.get("description"),
        "industry_identifiers": volume.get("industryIdentifiers") or [],
        "categories": volume.get("categories") or [],
        "preview_link": volume.get("previewLink"),
        "info_link": volume.get("infoLink"),
        "canonical_volume_link": volume.get("canonicalVolumeLink"),
    }


def _youtube_result(item: dict[str, Any]) -> dict[str, Any]:
    item_id = item.get("id") or {}
    snippet = item.get("snippet") or {}
    kind = item_id.get("kind")
    video_id = item_id.get("videoId")
    channel_id = item_id.get("channelId")
    playlist_id = item_id.get("playlistId")
    url = None
    if video_id:
        url = f"https://www.youtube.com/watch?v={video_id}"
    elif channel_id:
        url = f"https://www.youtube.com/channel/{channel_id}"
    elif playlist_id:
        url = f"https://www.youtube.com/playlist?list={playlist_id}"
    return {
        "kind": kind,
        "video_id": video_id,
        "channel_id": channel_id,
        "playlist_id": playlist_id,
        "url": url,
        "title": snippet.get("title"),
        "description": snippet.get("description"),
        "published_at": snippet.get("publishedAt"),
        "channel_title": snippet.get("channelTitle"),
    }


def _clean_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value is not None}


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _google_api_key(service_env_name: str) -> str:
    return _env(service_env_name) or _env(GOOGLE_API_KEY)


def _google_api_key_error(service_env_name: str) -> str:
    return f"{service_env_name} or {GOOGLE_API_KEY} is not configured."


def _clamp(value: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(parsed, maximum))
