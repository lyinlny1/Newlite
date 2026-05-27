from __future__ import annotations

import re
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

from agent.models import ResearchReport, SearchResult, TokenCandidate


DDG_HTML = "https://html.duckduckgo.com/html/"


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


async def search_web(query: str, max_results: int = 6, timeout_seconds: int = 20) -> list[SearchResult]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {"User-Agent": "Mozilla/5.0 token-research-agent/0.1"}
    url = f"{DDG_HTML}?q={quote_plus(query)}"
    results: list[SearchResult] = []
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    return results
                html = await resp.text()
    except (TimeoutError, aiohttp.ClientError):
        return results

    soup = BeautifulSoup(html, "html.parser")
    for row in soup.select(".result"):
        link = row.select_one(".result__a")
        if not link:
            continue
        snippet = row.select_one(".result__snippet")
        href = link.get("href") or ""
        results.append(
            SearchResult(
                title=_clean(link.get_text(" ")),
                url=href,
                snippet=_clean(snippet.get_text(" ")) if snippet else "",
            )
        )
        if len(results) >= max_results:
            break
    return results


async def check_website(url: str, mint: str, timeout_seconds: int = 12) -> dict[str, object]:
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {"User-Agent": "Mozilla/5.0 token-research-agent/0.1"}
    result: dict[str, object] = {"url": url, "ok": False, "mentions_mint": False, "title": "", "error": ""}
    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                result["status"] = resp.status
                if resp.status >= 400:
                    result["error"] = f"HTTP {resp.status}"
                    return result
                text = await resp.text(errors="ignore")
    except (TimeoutError, aiohttp.ClientError) as exc:
        result["error"] = str(exc)[:160]
        return result

    soup = BeautifulSoup(text[:300_000], "html.parser")
    title = soup.title.get_text(" ") if soup.title else ""
    visible = _clean(soup.get_text(" "))[:20_000]
    result["ok"] = True
    result["title"] = _clean(title)
    result["mentions_mint"] = mint in text
    result["text_sample"] = visible[:400]
    return result


async def research_token(candidate: TokenCandidate, websites: list[str], max_results: int = 6) -> ResearchReport:
    identity = " ".join(part for part in [candidate.symbol, candidate.name, candidate.mint] if part)
    query = f'"{candidate.symbol}" "{candidate.name}" solana pumpfun OR pump.fun' if candidate.symbol and candidate.name else f'"{identity}" solana'
    results = await search_web(query, max_results=max_results)
    website_checks = [await check_website(url, candidate.mint) for url in websites[:3]]

    identity_terms = [term.lower() for term in (candidate.symbol, candidate.name, candidate.mint) if term]
    relevant_hits = 0
    for item in results:
        blob = f"{item.title} {item.snippet} {item.url}".lower()
        if any(term in blob for term in identity_terms):
            relevant_hits += 1

    if relevant_hits >= 5:
        interest = "high"
    elif relevant_hits >= 2:
        interest = "medium"
    elif relevant_hits == 1:
        interest = "low"
    else:
        interest = "unknown"

    notes = [f"Free web search found {len(results)} results, {relevant_hits} looked identity-relevant."]
    if website_checks:
        ok_sites = sum(1 for item in website_checks if item.get("ok"))
        notes.append(f"Checked {len(website_checks)} website(s), {ok_sites} reachable.")
    return ResearchReport(search_query=query, results=results, website_checks=website_checks, estimated_interest=interest, notes=notes)
