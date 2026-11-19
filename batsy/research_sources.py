"""Finding sources for a research: real papers (Semantic Scholar, arXiv: free, no keys) and the web.

    papers(query) -> [{title, authors, year, venue, url, abstract, citations, pdf}]
    web(query)    -> [{title, url, abstract}]
"""
import json
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

log = logging.getLogger("batsy.research")
UA = {"User-Agent": "Batsy research assistant (personal use)"}


def _get(url: str, timeout: int = 15) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def semantic_scholar(query: str, n: int = 6) -> list[dict]:
    fields = "title,authors,year,venue,url,abstract,citationCount,openAccessPdf,externalIds"
    url = ("https://api.semanticscholar.org/graph/v1/paper/search?" +
           urllib.parse.urlencode({"query": query, "limit": n, "fields": fields}))
    try:
        data = json.loads(_get(url)).get("data") or []
    except Exception as e:
        log.info("semantic scholar: %s", e)
        return []
    out = []
    for p in data:
        doi = (p.get("externalIds") or {}).get("DOI")
        out.append({"title": p.get("title") or "", "year": p.get("year") or "", "venue": p.get("venue") or "",
                    "authors": _names([a.get("name", "") for a in p.get("authors") or []]),
                    "url": f"https://doi.org/{doi}" if doi else p.get("url") or "",
                    "abstract": (p.get("abstract") or "")[:1500], "citations": p.get("citationCount") or 0,
                    "pdf": (p.get("openAccessPdf") or {}).get("url") or "", "from": "Semantic Scholar"})
    return out


def openalex(query: str, n: int = 6) -> list[dict]:
    """OpenAlex: ~250M works, citation counts and abstracts, free and without a key (the main source)."""
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode({
        "search": query, "per-page": n, "mailto": "batsy@localhost",
        "select": "title,publication_year,authorships,cited_by_count,doi,primary_location,abstract_inverted_index,"
                  "open_access"})
    try:
        data = json.loads(_get(url)).get("results") or []
    except Exception as e:
        log.info("openalex: %s", e)
        return []
    out = []
    for w in data:
        inv = w.get("abstract_inverted_index") or {}
        words = sorted(((i, word) for word, idx in inv.items() for i in idx))
        loc = (w.get("primary_location") or {}).get("source") or {}
        out.append({"title": w.get("title") or "", "year": w.get("publication_year") or "",
                    "venue": loc.get("display_name") or "",
                    "authors": _names([(a.get("author") or {}).get("display_name", "") for a in w.get("authorships") or []]),
                    "url": w.get("doi") or "", "abstract": " ".join(x for _, x in words)[:1500],
                    "citations": w.get("cited_by_count") or 0, "pdf": (w.get("open_access") or {}).get("oa_url") or "",
                    "from": "OpenAlex"})
    return out


def arxiv(query: str, n: int = 5) -> list[dict]:
    words = [w for w in re.findall(r"[A-Za-z0-9-]{3,}", query)][:6]  # every word must match (not any of them)
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": " AND ".join(f"all:{w}" for w in words) or f"all:{query}", "start": 0, "max_results": n,
         "sortBy": "relevance"})
    try:
        root = ET.fromstring(_get(url))
    except Exception as e:
        log.info("arxiv: %s", e)
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    out = []
    for e in root.findall("a:entry", ns):
        link = e.findtext("a:id", "", ns)
        out.append({"title": re.sub(r"\s+", " ", e.findtext("a:title", "", ns)).strip(),
                    "year": e.findtext("a:published", "", ns)[:4], "venue": "arXiv",
                    "authors": _names([a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)]),
                    "url": link, "abstract": re.sub(r"\s+", " ", e.findtext("a:summary", "", ns)).strip()[:1500],
                    "citations": 0, "pdf": link.replace("/abs/", "/pdf/"), "from": "arXiv"})
    return out


def papers(query: str, n: int = 5) -> list[dict]:
    """The best few papers: OpenAlex (relevance, citations, abstracts), Semantic Scholar if OpenAlex is down, then
    the newest arXiv preprints; no duplicates."""
    main = openalex(query, n + 3) or semantic_scholar(query, n + 2)
    found, seen = [], set()
    for p in main + arxiv(query, 3):
        key = re.sub(r"[^a-z0-9]", "", p["title"].lower())[:60]
        if p["title"] and key not in seen:
            seen.add(key)
            found.append(p)
    top = [p for p in found if p["from"] != "arXiv" and p["abstract"]] or [p for p in found if p["from"] != "arXiv"]
    pre = [p for p in found if p["from"] == "arXiv"]
    return (top[:max(3, n - 1)] + pre)[:n]


def web(query: str, n: int = 5) -> list[dict]:
    from batsy.shopping import web_search

    return [{"title": r.get("title", ""), "url": r.get("href", ""), "abstract": r.get("body", ""), "from": "web"}
            for r in web_search(query, n)]


def _names(names: list[str]) -> str:
    names = [x for x in names if x]
    if not names:
        return ""
    last = lambda s: s.split()[-1]  # noqa: E731
    return f"{last(names[0])}" + (" et al." if len(names) > 2 else f" and {last(names[1])}" if len(names) == 2 else "")


def spoken_list(found: list[dict]) -> str:
    words = ["One", "Two", "Three", "Four", "Five", "Six"]
    return " ".join(f"{words[i]}: {p['title']}" + (f", {p['authors']}" if p.get("authors") else "") +
                    (f", {p['year']}" if p.get("year") else "") +
                    (f", cited {p['citations']} times" if p.get("citations") else "") + "."
                    for i, p in enumerate(found[:6]))
