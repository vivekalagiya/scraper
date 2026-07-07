#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup


ZYTE_API_URL = "https://api.zyte.com/v1/extract"

BRANDS: List[Tuple[str, str]] = [
    ("catnapper", "https://colemanfurniture.com/catnapper.html"),
    ("jackson", "https://colemanfurniture.com/jackson-furniture.html"),
]


def _norm_url(href: str, base: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return None
    return urljoin(base, href)


def _is_same_host(url: str, host: str) -> bool:
    try:
        return urlparse(url).netloc.lower() == host.lower()
    except Exception:
        return False


def _looks_like_human_verification(html: str, title: str = "") -> bool:
    blob = f"{title}\n{html}".lower()
    return "human verification" in blob or "captcha" in blob or "x-amzn-waf-action" in blob


def _json_loads_maybe(text: str) -> Optional[Any]:
    try:
        return json.loads(text)
    except Exception:
        return None


def fetch_html_with_zyte(url: str, api_key: str, max_retries: int = 3, selector: Optional[str] = None) -> str:
    """
    Zyte fetch with retries + backoff, matching your reference pattern.
    """
    api_key = (api_key or "").strip()
    if not api_key:
        raise RuntimeError("ZYTE_API_KEY not set (env var or --zyte-api-key).")

    payload: Dict[str, Any] = {"url": url, "browserHtml": True, "javascript": True}
    if selector:
        payload["actions"] = [
            {
                "action": "waitForSelector",
                "selector": {"type": "css", "value": selector},
                "timeout": 15,
            }
        ]

    for attempt in range(max_retries):
        try:
            resp = requests.post(ZYTE_API_URL, auth=(api_key, ""), json=payload, timeout=90)
            if resp.status_code == 200:
                data = resp.json() or {}
                html = data.get("browserHtml") or data.get("httpResponseBody")
                if not html:
                    raise RuntimeError("Zyte returned empty HTML/body")
                # httpResponseBody can be base64 (defensive decode)
                if isinstance(html, str) and "<html" not in html.lower() and len(html) > 200:
                    try:
                        decoded = base64.b64decode(html)
                        html = decoded.decode("utf-8", errors="replace")
                    except Exception:
                        pass
                return str(html)

            if resp.status_code in (401, 403):
                raise RuntimeError(f"Zyte unauthorized (HTTP {resp.status_code}). Check API key.")
            raise RuntimeError(f"Zyte API returned {resp.status_code}: {resp.text[:200]}")
        except Exception:
            if attempt < max_retries - 1:
                time.sleep((2**attempt) + random.uniform(0, 1))
            else:
                raise


def _http_session(cookie_header: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    if cookie_header:
        s.headers["Cookie"] = cookie_header
    return s


def _http_get_html(
    session: requests.Session,
    url: str,
    *,
    zyte_fallback: bool = False,
    zyte_api_key: str = "",
    zyte_selector: Optional[str] = None,
    max_retries: int = 2,
    timeout: int = 45,
) -> str:
    zyte_only = os.getenv("COLEMAN_ZYTE_ONLY", "1").strip() in ("1", "true", "True", "yes", "YES")
    if zyte_fallback and zyte_only:
        return fetch_html_with_zyte(url, api_key=zyte_api_key, max_retries=3, selector=zyte_selector)

    last_html = ""
    for attempt in range(max_retries):
        r = session.get(url, timeout=timeout)
        last_html = r.text or ""
        if r.status_code >= 400:
            time.sleep((2**attempt) + 0.25)
            continue
        if zyte_fallback and _looks_like_human_verification(last_html, title=""):
            return fetch_html_with_zyte(url, api_key=zyte_api_key, max_retries=3, selector=zyte_selector)
        return last_html

    if zyte_fallback:
        return fetch_html_with_zyte(url, api_key=zyte_api_key, max_retries=3, selector=zyte_selector)
    return last_html


def _add_or_replace_query(url: str, **params: Any) -> str:
    parts = urlsplit(url)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    for k, v in params.items():
        if v is None:
            q.pop(k, None)
        else:
            q[k] = str(v)
    new_query = urlencode(q, doseq=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))


def _extract_total_results(html: str) -> Optional[int]:
    m = re.search(r"\bof\s+([\d,]+)\s+results\b", html, flags=re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except Exception:
        return None


def _extract_product_links_from_html(html: str, page_url: str, exclude_urls: Sequence[str]) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(page_url).netloc

    exclude = set(exclude_urls)
    exclude.add(page_url)

    urls: List[str] = []
    for s in soup.select('script[type="application/ld+json"]'):
        raw = (s.get_text() or "").strip()
        data = _json_loads_maybe(raw) if raw else None
        if not data:
            continue

        stack: List[Any] = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                t = cur.get("@type")
                if isinstance(t, str) and t.lower() == "itemlist":
                    for el in cur.get("itemListElement") or []:
                        if isinstance(el, dict):
                            u = el.get("url") or (el.get("item") or {}).get("@id")
                            if isinstance(u, str):
                                full = _norm_url(u, page_url)
                                if full and _is_same_host(full, host) and full not in exclude:
                                    urls.append(full)
                for v in cur.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)

    if not urls:
        for a in soup.select("main a[href], a[href]"):
            full = _norm_url(a.get("href") or "", page_url)
            if not full:
                continue
            if not _is_same_host(full, host):
                continue
            if full in exclude:
                continue
            if not urlparse(full).path.lower().endswith((".htm", ".html")):
                continue
            low = full.lower()
            if any(x in low for x in ("/contact", "/privacy", "/terms", "/account", "/cart", "/wishlist", "/track")):
                continue
            urls.append(full)

    seen = set()
    out: List[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _iter_jsonld_products(soup: BeautifulSoup) -> Iterable[Dict[str, Any]]:
    for s in soup.select('script[type="application/ld+json"]'):
        raw = (s.get_text() or "").strip()
        if not raw:
            continue
        data = _json_loads_maybe(raw)
        if data is None:
            continue
        stack: List[Any] = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                t = cur.get("@type") or cur.get("type")
                if isinstance(t, list):
                    if any(str(x).lower() == "product" for x in t):
                        yield cur
                elif isinstance(t, str) and t.lower() == "product":
                    yield cur
                for v in cur.values():
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)


def _extract_product_from_jsonld(soup: BeautifulSoup) -> Dict[str, Any]:
    best: Optional[Dict[str, Any]] = None
    for p in _iter_jsonld_products(soup):
        if not best:
            best = p
            continue
        score = int(bool(p.get("sku"))) * 3 + int(bool(p.get("name"))) * 2 + int(bool(p.get("image")))
        best_score = int(bool(best.get("sku"))) * 3 + int(bool(best.get("name"))) * 2 + int(bool(best.get("image")))
        if score > best_score:
            best = p
    return best or {}


def _extract_sku(soup: BeautifulSoup, html: str) -> Optional[str]:
    p = _extract_product_from_jsonld(soup)
    sku = p.get("sku")
    if isinstance(sku, str) and sku.strip():
        return sku.strip()
    text = soup.get_text(" ", strip=True)
    m = re.search(r"\bSKU\b\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9\-_./]{1,60})", text, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"\bSKU\b\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9\-_./]{1,60})", html or "", flags=re.IGNORECASE)
    return m2.group(1).strip() if m2 else None


def _extract_name(soup: BeautifulSoup) -> Optional[str]:
    p = _extract_product_from_jsonld(soup)
    name = p.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    h1 = soup.select_one("main h1, h1")
    if h1:
        t = h1.get_text(" ", strip=True)
        if t:
            return t
    title = soup.select_one("title")
    if title:
        t = title.get_text(" ", strip=True)
        if t:
            return t
    return None


def _extract_images(soup: BeautifulSoup, base_url: str) -> List[str]:
    urls: List[str] = []

    p = _extract_product_from_jsonld(soup)
    img = p.get("image")
    if isinstance(img, str):
        u = _norm_url(img, base_url)
        if u:
            urls.append(u)
    elif isinstance(img, list):
        for x in img:
            if isinstance(x, str):
                u = _norm_url(x, base_url)
                if u:
                    urls.append(u)
            elif isinstance(x, dict):
                u = x.get("url") or x.get("contentUrl")
                if isinstance(u, str):
                    u2 = _norm_url(u, base_url)
                    if u2:
                        urls.append(u2)

    og = soup.select_one('meta[property="og:image"][content]')
    if og and og.get("content"):
        u = _norm_url(og["content"], base_url)
        if u:
            urls.append(u)

    for img_tag in soup.select("main img[src], img[src]"):
        src = (img_tag.get("src") or "").strip()
        if not src or src.startswith("data:"):
            continue
        u = _norm_url(src, base_url)
        if not u:
            continue
        low = u.lower()
        if any(x in low for x in ("/logo", "sprite", "icon", "favicon")):
            continue
        urls.append(u)

    seen = set()
    out: List[str] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def _scrape_brand_http(
    session: requests.Session,
    brand_url: str,
    exclude_urls: Sequence[str],
    *,
    zyte_fallback: bool,
    zyte_api_key: str,
    max_pages: int = 250,
) -> List[str]:
    html0 = _http_get_html(session, brand_url, zyte_fallback=zyte_fallback, zyte_api_key=zyte_api_key)
    product_urls: List[str] = []
    product_urls.extend(_extract_product_links_from_html(html0, brand_url, exclude_urls=exclude_urls))
    total = _extract_total_results(html0)

    if total is not None and len(set(product_urls)) >= total:
        return list(dict.fromkeys(product_urls))

    page_patterns = [
        lambda n: _add_or_replace_query(brand_url, pagenumber=n),
        lambda n: _add_or_replace_query(brand_url, page=n),
        lambda n: _add_or_replace_query(brand_url, p=n),
        lambda n: _add_or_replace_query(brand_url, page_num=n),
        lambda n: _add_or_replace_query(brand_url, pg=n),
    ]

    seen = set(product_urls)
    best_pattern = page_patterns[0]
    best_new = -1
    for pat in page_patterns:
        html2 = _http_get_html(session, pat(2), zyte_fallback=zyte_fallback, zyte_api_key=zyte_api_key)
        links2 = _extract_product_links_from_html(html2, brand_url, exclude_urls=exclude_urls)
        new = len([u for u in links2 if u not in seen])
        if new > best_new:
            best_new = new
            best_pattern = pat
    if best_new <= 0:
        return list(dict.fromkeys(product_urls))

    zero_add = 0
    for page_num in range(2, max_pages + 1):
        page_url = best_pattern(page_num)
        html = _http_get_html(session, page_url, zyte_fallback=zyte_fallback, zyte_api_key=zyte_api_key)
        links = _extract_product_links_from_html(html, brand_url, exclude_urls=exclude_urls)
        new_urls = [u for u in links if u not in seen]
        if not new_urls:
            zero_add += 1
            if zero_add >= 2:
                break
            continue
        zero_add = 0
        for u in new_urls:
            seen.add(u)
            product_urls.append(u)
        total2 = _extract_total_results(html)
        if total2 is not None:
            total = total2
        if total is not None and len(seen) >= total:
            break

    return list(dict.fromkeys(product_urls))

