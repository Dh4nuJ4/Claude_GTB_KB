"""Turn raw pages and files into clean text items (items.jsonl + extracted/*.md).

Pages are classified as articles either by URL pattern or, failing that, by shape:
enough prose and a low share of link text. Category/listing pages are kept out.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import markdownify

from .config import paths_for, read_jsonl, write_jsonl
from .urls import matches_article_pattern

PDF_TYPES = ("application/pdf",)


def normalized_text(text: str) -> str:
    """Lower-case words only; used for hashing and similarity so formatting doesn't matter."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def text_hash(text: str, cfg: dict[str, Any]) -> str:
    """Hash of the wording only: "Last updated: ..." stamps are ignored so re-dated copies still match."""
    text = re.sub(cfg["updated_regex"], " ", text, flags=re.I)
    return hashlib.sha256(normalized_text(text).encode("utf-8")).hexdigest()


def _first(soup: BeautifulSoup, selectors: list[str]):
    for sel in selectors:
        try:
            el = soup.select_one(sel)
        except Exception:
            continue
        if el is not None and el.get_text(strip=True):
            return el, sel
    return None, None


def _clean_title(title: str) -> str:
    title = " ".join(title.split())
    # "Article name - GTB Knowledge Base" -> "Article name"
    parts = re.split(r"\s+[|\-–—]\s+", title)
    return parts[0] if len(parts) > 1 and len(parts[0]) > 3 else title


def _breadcrumb(soup: BeautifulSoup, cfg: dict[str, Any], title: str) -> list[str]:
    el, _ = _first(soup, cfg["breadcrumb_selectors"])
    if el is None:
        return []
    items = [" ".join(a.get_text(" ", strip=True).split()) for a in el.find_all(["a", "li", "span"]) if a.get_text(strip=True)]
    crumbs: list[str] = []
    for c in items:
        if c and c not in crumbs and c not in ("»", ">", "/", "›") and len(c) < 120:
            crumbs.append(c)
    crumbs = [c for c in crumbs if c.lower() not in ("home", "knowledge base", "kb", "knowledgebase") and c != title]
    return crumbs


def _link_density(el) -> float:
    total = len(el.get_text(" ", strip=True).split()) or 1
    linked = sum(len(a.get_text(" ", strip=True).split()) for a in el.find_all("a"))
    return linked / total


def parse_page(html: str, url: str, cfg: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    title_el, _ = _first(soup, cfg["title_selectors"])
    title = _clean_title(title_el.get_text(" ", strip=True)) if title_el else url
    crumbs = _breadcrumb(soup, cfg, title)
    m = re.search(cfg["updated_regex"], soup.get_text(" ", strip=True), re.I)
    updated = m.group(1) if m else ""

    body, selector = _first(soup, cfg["content_selectors"])
    if body is None:
        body, selector = soup.body or soup, "body"
    for sel in cfg["strip_selectors"]:
        try:
            for el in body.select(sel):
                el.decompose()
        except Exception:
            continue
    # Absolute links so the Markdown still points at the portal; images become their alt text.
    for a in body.find_all("a", href=True):
        a["href"] = urljoin(url, a["href"])
    for img in body.find_all("img"):
        alt = img.get("alt", "").strip()
        img.replace_with(f"[image: {alt}]" if alt else "")
    # Drop the title heading from the body; it goes in the item header instead.
    h1 = body.find("h1")
    if h1 is not None and _clean_title(h1.get_text(" ", strip=True)) == title:
        h1.decompose()

    words = len(body.get_text(" ", strip=True).split())
    density = _link_density(body)
    md = markdownify(str(body), heading_style="ATX", bullets="-", strip=["span", "div"])
    md = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", md)).strip()

    by_url = matches_article_pattern(url, cfg)
    by_shape = words >= cfg["min_article_words"] and density <= cfg["max_link_density"]
    return {
        "title": title, "breadcrumb": crumbs, "updated": updated, "markdown": md,
        "words": words, "link_density": round(density, 2), "content_selector": selector,
        "is_article": by_url or by_shape,
        "classified_by": "url_pattern" if by_url else ("shape" if by_shape else "listing"),
    }


def parse_pdf(path: Path) -> dict[str, Any]:
    import pymupdf

    with pymupdf.open(path) as doc:
        pages = [p.get_text("text") for p in doc]
        meta = doc.metadata or {}
    text = "\n\n".join(f"<!-- page {i + 1} -->\n{t.strip()}" for i, t in enumerate(pages) if t.strip())
    chars = sum(len(t.strip()) for t in pages)
    mod = meta.get("modDate") or meta.get("creationDate") or ""
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})", mod)
    return {
        "title": (meta.get("title") or "").strip(),
        "updated": f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else "",
        "markdown": text, "pages": len(pages), "words": len(text.split()),
        # Under ~100 characters a page is almost certainly a scan: needs OCR to be useful as text.
        "needs_ocr": len(pages) > 0 and chars / len(pages) < 100,
    }


GENERIC_TITLE = re.compile(r"^(untitled|document\d*|download|click here|here|pdf|view|open|link|attachment|file)\b", re.I)


def _pdf_title(meta_title: str, link_text: str, filename: str) -> str:
    """Best human title for a PDF: its own metadata, then the link text, then the file name."""
    meta_title = re.sub(r"^Microsoft (Word|PowerPoint|Excel) - ", "", meta_title or "").strip()
    meta_title = re.sub(r"\.(docx?|pptx?|xlsx?)$", "", meta_title, flags=re.I)
    for candidate in (meta_title, link_text or ""):
        if len(candidate) > 3 and not GENERIC_TITLE.match(candidate):
            return candidate
    return Path(filename).stem.replace("_", " ")


def extract(cfg: dict[str, Any]) -> None:
    paths = paths_for(cfg)
    manifest = [r for r in read_jsonl(paths.manifest) if r.get("status") == "ok"]
    if not manifest:
        raise SystemExit("Nothing crawled yet. Run `python -m gtbkb crawl` first.")
    by_url = {r["url"]: r for r in manifest}
    pages: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []

    for rec in manifest:
        if rec.get("kind") != "page":
            continue
        html = Path(rec["path"]).read_text(encoding="utf-8", errors="replace")
        info = parse_page(html, rec.get("final_url") or rec["url"], cfg)
        pages[rec["url"]] = info
        if not info["is_article"]:
            continue
        out = paths.extracted / f"{rec['id']}.md"
        out.write_text(info["markdown"], encoding="utf-8")
        items.append({
            "id": rec["id"], "type": "article", "title": info["title"],
            "category": " > ".join(info["breadcrumb"]) or "General",
            "source_url": rec["url"], "updated": info["updated"], "words": info["words"],
            "sha256": rec["sha256"], "text_sha256": text_hash(info["markdown"], cfg),
            "path": str(out), "raw_path": rec["path"], "classified_by": info["classified_by"],
            "content_selector": info["content_selector"], "flags": [] if info["words"] else ["empty"],
        })

    def context_of(rec: dict[str, Any]) -> tuple[str, str]:
        """Category and title for a file, borrowed from the page that linked to it."""
        ref = pages.get(rec.get("referrer", ""))
        if not ref:
            return "General", ""
        crumbs = ref["breadcrumb"] + ([ref["title"]] if not ref["is_article"] else [])
        return " > ".join(crumbs) or "General", ref["title"]

    for rec in manifest:
        if rec.get("kind") != "file":
            continue
        path = Path(rec["path"])
        name = path.name.split("__", 1)[-1]
        category, ref_title = context_of(rec)
        base = {
            "id": rec["id"], "category": category, "source_url": rec["url"], "sha256": rec["sha256"],
            "raw_path": rec["path"], "filename": name, "linked_from": rec.get("referrer", ""),
        }
        is_pdf = any(t in rec.get("content_type", "") for t in PDF_TYPES) or name.lower().endswith(".pdf")
        if is_pdf:
            try:
                info = parse_pdf(path)
            except Exception as exc:
                items.append({**base, "type": "pdf", "title": rec.get("link_text") or name, "updated": "",
                              "words": 0, "text_sha256": "", "path": "", "flags": [f"unreadable: {exc}"[:200]]})
                continue
            out = paths.extracted / f"{rec['id']}.md"
            out.write_text(info["markdown"], encoding="utf-8")
            title = _pdf_title(info["title"], rec.get("link_text", ""), name)
            items.append({**base, "type": "pdf", "title": title, "updated": info["updated"],
                          "words": info["words"], "pages": info["pages"],
                          "text_sha256": text_hash(info["markdown"], cfg) if info["words"] else "",
                          "path": str(out), "flags": ["needs_ocr"] if info["needs_ocr"] else []})
        else:
            items.append({**base, "type": "other", "title": rec.get("link_text") or name, "updated": "",
                          "words": 0, "text_sha256": "", "path": "", "flags": []})

    write_jsonl(paths.items, items)
    listing = sum(1 for p in pages.values() if not p["is_article"])
    by_type: dict[str, int] = {}
    for it in items:
        by_type[it["type"]] = by_type.get(it["type"], 0) + 1
    print(f"Pages: {len(pages)} ({len(pages) - listing} articles, {listing} listing/other pages skipped)")
    print("Items: " + ", ".join(f"{v} {k}" for k, v in sorted(by_type.items())))
    shape = sum(1 for it in items if it.get("classified_by") == "shape")
    if shape:
        print(f"Note: {shape} articles were recognised by shape, not URL pattern. "
              "If kb_data/crawl_report.txt shows the article URL shape, add it to extra_article_patterns.")
    fallback = sum(1 for it in items if it.get("content_selector") == "body")
    if fallback:
        print(f"Note: {fallback} articles fell back to the whole <body>; add the right selector to extra_content_selectors.")
    ocr = sum(1 for it in items if "needs_ocr" in it.get("flags", []))
    if ocr:
        print(f"Note: {ocr} PDFs look scanned (little or no text); they'll be uploaded as original PDFs.")
