#!/usr/bin/env python3
"""
Scrape a Coleman "set / bundle" product page and export included products
from the "setinclude-products" section.

CSV columns:
  main_url, main_sku, main_name, item_url, item_sku, item_name, item_image_url

Uses Zyte by default (WAF-safe).
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from scrape_coleman_brands import _extract_images, _extract_name, _extract_sku, fetch_html_with_zyte


SKU_RE = re.compile(r"\bSKU\b\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9\-_./]{1,80})", re.IGNORECASE)
GENERIC_SKU_RE = re.compile(r"\b[A-Z]{2,6}-[A-Z0-9][A-Z0-9\-]{3,120}\b")


@dataclass
class BundleItem:
    main_url: str
    main_sku: str
    main_name: str
    item_url: str
    item_sku: str
    item_name: str
    item_image_url: str


def _abs_url(href: str, base: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return None
    return urljoin(base, href)


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _best_img_url(img_tag, base: str) -> str:
    if not img_tag:
        return ""
    for key in ("data-src", "data-original", "data-lazy", "data-amsrc", "src"):
        v = (img_tag.get(key) or "").strip()
        if not v or v.startswith("data:"):
            continue
        u = _abs_url(v, base)
        if u:
            return u
    return ""


def _best_bg_image_url(container, base: str) -> str:
    if not container:
        return ""
    nodes = [container]
    try:
        nodes += list(container.select("[style]"))[:25]
    except Exception:
        pass
    for n in nodes:
        style = (getattr(n, "get", lambda *_: "")("style") or "").strip()
        if "background-image" not in style.lower():
            continue
        m = re.search(r"background-image\s*:\s*url\(([^)]+)\)", style, flags=re.IGNORECASE)
        if not m:
            continue
        raw = m.group(1).strip().strip("\"' ")
        u = _abs_url(raw, base)
        if u:
            return u
    return ""


def _best_image_from_container(container, base: str) -> str:
    if not container:
        return ""
    img = _best_img_url(container.select_one("img"), base)
    if img:
        return img
    bg = _best_bg_image_url(container, base)
    if bg:
        return bg
    return ""


def _find_setinclude_section(soup: BeautifulSoup):
    for sel in (
        "#setinclude-products",
        ".setinclude-products",
        "[id*='setinclude-products']",
        "[class*='setinclude-products']",
    ):
        el = soup.select_one(sel)
        if el:
            return el
    for txt in ("Customize Set", "Customize set", "Included"):
        node = soup.find(string=re.compile(re.escape(txt), re.IGNORECASE))
        if node:
            cur = node.parent
            for _ in range(6):
                if not cur:
                    break
                if len(cur.select("a[href]")) >= 2:
                    return cur
                cur = cur.parent
    return None


def _iter_item_candidates(section: BeautifulSoup, main_url: str) -> Iterable[Tuple[str, Any]]:
    main_host = urlparse(main_url).netloc.lower()
    seen = set()
    for a in section.select("a[href]"):
        u = _abs_url(a.get("href") or "", main_url)
        if not u:
            continue
        if urlparse(u).netloc.lower() != main_host:
            continue
        if not urlparse(u).path.lower().endswith((".htm", ".html")):
            continue
        if u == main_url or u in seen:
            continue
        seen.add(u)
        yield u, a


def _extract_sku_from_container(container, html_text: str) -> str:
    txt = container.get_text(" ", strip=True) if container else ""
    m = SKU_RE.search(txt)
    if m:
        return _clean_text(m.group(1))
    m2 = GENERIC_SKU_RE.search(txt)
    if m2:
        return _clean_text(m2.group(0))
    m3 = SKU_RE.search(html_text or "")
    return _clean_text(m3.group(1)) if m3 else ""


def _extract_name_from_anchor_or_container(a, container) -> str:
    name = _clean_text(a.get_text(" ", strip=True) if a else "")
    if len(name) >= 4 and not name.lower().startswith(("quick view", "add to cart")):
        return name
    for sel in ("h1", "h2", "h3", "h4", ".product-title", "[class*='title']"):
        el = container.select_one(sel) if container else None
        t = _clean_text(el.get_text(" ", strip=True)) if el else ""
        if len(t) >= 4:
            return t
    return name


def parse_bundle_items(html: str, main_url: str) -> List[BundleItem]:
    soup = BeautifulSoup(html, "html.parser")
    section = _find_setinclude_section(soup)
    if not section:
        return []

    main_sku = _clean_text(_extract_sku(soup, html) or "")
    main_name = _clean_text(_extract_name(soup) or "")

    items: List[BundleItem] = []
    for item_url, a in _iter_item_candidates(section, main_url):
        container = a
        chosen = None
        for _ in range(6):
            if not container:
                break
            txt = container.get_text(" ", strip=True)
            if "SKU" in txt or GENERIC_SKU_RE.search(txt):
                chosen = container
                break
            container = container.parent
        if not chosen:
            chosen = a.parent

        container_html = str(chosen) if chosen else ""
        item_sku = _extract_sku_from_container(chosen, container_html)
        item_name = _extract_name_from_anchor_or_container(a, chosen)
        item_img = _best_image_from_container(chosen or a, main_url)

        items.append(
            BundleItem(
                main_url=main_url,
                main_sku=main_sku,
                main_name=main_name,
                item_url=item_url,
                item_sku=item_sku,
                item_name=item_name,
                item_image_url=item_img,
            )
        )

    dedup: Dict[str, BundleItem] = {}
    for it in items:
        if it.item_url not in dedup:
            dedup[it.item_url] = it
    return list(dedup.values())


def write_csv(items: Sequence[BundleItem], out_path: str) -> None:
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "main_url",
                "main_sku",
                "main_name",
                "item_url",
                "item_sku",
                "item_name",
                "item_image_url",
            ],
        )
        w.writeheader()
        for it in items:
            w.writerow(
                {
                    "main_url": it.main_url,
                    "main_sku": it.main_sku,
                    "main_name": it.main_name,
                    "item_url": it.item_url,
                    "item_sku": it.item_sku,
                    "item_name": it.item_name,
                    "item_image_url": it.item_image_url,
                }
            )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Bundle/set product URL")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "bundle_items.csv"))
    ap.add_argument("--zyte-api-key", default="", help="Zyte API key (or set env var ZYTE_API_KEY)")
    args = ap.parse_args()

    zyte_key = (args.zyte_api_key or os.getenv("ZYTE_API_KEY") or "").strip()
    if not zyte_key:
        print("Missing Zyte key. Set ZYTE_API_KEY or pass --zyte-api-key.", file=sys.stderr)
        return 2

    html = fetch_html_with_zyte(args.url, api_key=zyte_key, max_retries=3)
    items = parse_bundle_items(html, args.url)
    if not items:
        print("No items found in setinclude-products section.", file=sys.stderr)
        return 1

    write_csv(items, args.out)
    print(f"Saved {len(items)} rows to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

