"""Explain how a few crawled pages were read: title, category, which element was taken as the
article body and why. Prints structure and counts only (safe to share) unless --preview is given."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .config import paths_for, read_jsonl
from .extract import _clean_title, _describe, _title_heading, _words, content_candidates, parse_page
from .urls import matches_article_pattern


def diagnose(cfg: dict[str, Any], url: str | None = None, count: int = 3, preview: bool = False) -> None:
    paths = paths_for(cfg)
    pages = [r for r in read_jsonl(paths.manifest) if r.get("status") == "ok" and r.get("kind") == "page"]
    if url:
        pages = [r for r in pages if r["url"] == url or r.get("final_url") == url]
        if not pages:
            raise SystemExit(f"{url} isn't in the crawl manifest.")
    else:
        articles = [r for r in pages if matches_article_pattern(r["url"], cfg)]
        pages = (articles or pages)[:: max(1, len(articles or pages) // count)][:count]

    for rec in pages:
        html = Path(rec["path"]).read_text(encoding="utf-8", errors="replace")
        info = parse_page(html, rec.get("final_url") or rec["url"], cfg)
        soup = BeautifulSoup(html, "lxml")
        page_title = _clean_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
        heading = _title_heading(soup, page_title)
        print("=" * 80)
        print(f"URL:        {rec['url']}")
        print(f"Title:      {info['title']}")
        print(f"Category:   {' > '.join(info['breadcrumb']) or '(none found)'}")
        print(f"Updated:    {info['updated'] or '(none found)'}")
        print(f"Classified: {'article' if info['is_article'] else 'listing'} by {info['classified_by']}, "
              f"{info['words']} words, link density {info['link_density']}")
        print(f"Body taken: {info['content_selector']}")
        print("Candidates (score = prose words - link words):")
        for score, el, how in content_candidates(soup, heading, cfg)[:6]:
            prose, linked = _words(el)
            print(f"  {score:>6}  prose {prose:>5}  links {linked:>5}  {_describe(el)}  [{how}]")
        if preview:
            print("--- first 600 characters of the extracted Markdown ---")
            print(info["markdown"][:600])
    print("=" * 80)
