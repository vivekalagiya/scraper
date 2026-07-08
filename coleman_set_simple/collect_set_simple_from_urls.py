#!/usr/bin/env python3
"""
Pipeline:
1) Read brand product URLs CSV from scrape_coleman_product_urls.py (brand, product_url)
2) For each URL, fetch HTML via Zyte and parse bundle items (if present)
3) Write one consolidated CSV with:
   brand, main_url, main_sku, main_name, item_url, item_sku, item_name, item_image_url

This avoids launching a new Python process per URL (much faster in workflows).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import Dict

from scrape_coleman_brands import fetch_html_with_zyte
from scrape_coleman_bundle_items import parse_bundle_items


OUT_FIELDS = [
    "brand",
    "main_url",
    "main_sku",
    "main_name",
    "item_url",
    "item_sku",
    "item_name",
    "item_image_url",
    "item_is_required",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", required=True, help="Input CSV with URL column (product_url/url) and optional brand")
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "coleman_set_simple.csv"))
    ap.add_argument("--zyte-api-key", default="", help="Zyte API key (or set env var ZYTE_API_KEY)")
    ap.add_argument("--flush-every", type=int, default=25, help="Flush progress every N written rows")
    ap.add_argument("--max-urls", type=int, default=0, help="Limit number of input URLs processed (0=all)")
    args = ap.parse_args()

    zyte_key = (args.zyte_api_key or os.getenv("ZYTE_API_KEY") or "").strip()
    if not zyte_key:
        print("Missing Zyte key. Set ZYTE_API_KEY or pass --zyte-api-key.", file=sys.stderr)
        return 2

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    wrote = 0
    processed = 0

    with open(args.out, "w", newline="", encoding="utf-8") as out_f:
        w = csv.DictWriter(out_f, fieldnames=OUT_FIELDS)
        w.writeheader()
        out_f.flush()

        with open(args.urls, "r", encoding="utf-8") as in_f:
            r = csv.DictReader(in_f)
            for row in r:
                brand = (row.get("brand") or "").strip()
                url = (row.get("product_url") or row.get("url") or row.get("link") or "").strip()
                if not url:
                    continue

                processed += 1
                if args.max_urls and processed > args.max_urls:
                    break

                # Fetch page and attempt bundle parse.
                try:
                    html = fetch_html_with_zyte(url, api_key=zyte_key, max_retries=3)
                except Exception as e:
                    print(f"[warn] fetch failed: {url} ({e})", file=sys.stderr)
                    continue

                items = parse_bundle_items(html, url)
                if not items:
                    continue

                for it in items:
                    out: Dict[str, str] = {
                        "brand": brand,
                        "main_url": it.main_url,
                        "main_sku": it.main_sku,
                        "main_name": it.main_name,
                        "item_url": it.item_url,
                        "item_sku": it.item_sku,
                        "item_name": it.item_name,
                        "item_image_url": it.item_image_url,
                        "item_is_required": getattr(it, "item_is_required", ""),
                    }
                    w.writerow(out)
                    wrote += 1
                    if wrote % max(1, args.flush_every) == 0:
                        out_f.flush()
                        print(f"[progress] wrote {wrote} rows (processed {processed} urls)")

    # It's normal for many URLs to NOT be set/bundle pages.
    # Always exit 0 so matrix jobs don't fail just because a chunk had no bundles.
    print(f"Saved {wrote} rows to: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

