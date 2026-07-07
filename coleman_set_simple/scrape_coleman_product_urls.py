#!/usr/bin/env python3
"""
Collect ONLY product URLs from both brand pages and write:
  brand, product_url

Designed for GitHub Actions (WAF-safe): uses Zyte by default.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from typing import List

from scrape_coleman_brands import BRANDS, _http_session, _scrape_brand_http


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.getcwd(), "coleman_product_urls.csv"))
    ap.add_argument("--max-urls", type=int, default=0, help="Limit URLs per brand (0 = no limit)")
    ap.add_argument("--zyte-api-key", default="", help="Zyte API key (or set env var ZYTE_API_KEY)")
    args = ap.parse_args()

    zyte_key = (args.zyte_api_key or os.getenv("ZYTE_API_KEY") or "").strip()
    if not zyte_key:
        print("Missing Zyte key. Set ZYTE_API_KEY or pass --zyte-api-key.", file=sys.stderr)
        return 2

    # Force Zyte-only in CI to avoid wasting time on WAF-blocked HTTP tries.
    os.environ.setdefault("COLEMAN_ZYTE_ONLY", "1")

    session = _http_session("")
    exclude_urls = [u for _, u in BRANDS]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    wrote = 0
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["brand", "product_url"])
        w.writeheader()
        f.flush()

        for brand, brand_url in BRANDS:
            print(f"[{brand}] collecting URLs")
            urls: List[str] = _scrape_brand_http(
                session,
                brand_url,
                exclude_urls=exclude_urls,
                zyte_fallback=True,
                zyte_api_key=zyte_key,
            )
            if args.max_urls and args.max_urls > 0:
                urls = urls[: args.max_urls]

            for u in urls:
                w.writerow({"brand": brand, "product_url": u})
                wrote += 1
            f.flush()
            print(f"[{brand}] wrote {len(urls)} URLs")

    print(f"Saved {wrote} rows to: {args.out}")
    return 0 if wrote else 1


if __name__ == "__main__":
    raise SystemExit(main())

