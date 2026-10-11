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


SITE_SUFFIX = re.compile(r"\s+[|\-–—:]\s+[^|\-–—]*\b(knowledge\s*base|help\s*cent(er|re)|support|kb|documentation)\b[^|\-–—]*$", re.I)


def _clean_title(title: str) -> str:
    """"Article name - GTB Knowledge Base" -> "Article name", but "Upgrade - Linux" stays whole."""
    return SITE_SUFFIX.sub("", " ".join(title.split())).strip()


def _breadcrumb(soup: BeautifulSoup, cfg: dict[str, Any], title: str) -> list[str]:
    el, _ = _first(soup, cfg["breadcrumb_selectors"])
    if el is None:
        return []
    items = [" ".join(a.get_text(" ", strip=True).split()) for a in el.find_all(["a", "li", "span"]) if a.get_text(strip=True)]
    crumbs: list[str] = []
    for c in items:
        if c and c not in crumbs and c not in ("»", ">", "/", "›") and len(c) < 120:
            crumbs.append(c)
    skip = ("home", "knowledge base", "kb", "knowledgebase", "categories", "all categories")
    return [c for c in crumbs if c.lower() not in skip and c != title]


def _words(el) -> tuple[int, int]:
    """(words not inside links, words inside links)."""
    total = len(el.get_text(" ", strip=True).split())
    linked = sum(len(a.get_text(" ", strip=True).split()) for a in el.find_all("a"))
    return total - linked, linked


def _describe(el) -> str:
    ident = f"#{el['id']}" if el.get("id") else ""
    classes = "".join(f".{c}" for c in (el.get("class") or [])[:3])
    return f"{el.name}{ident}{classes}"


def _title_heading(soup: BeautifulSoup, page_title: str):
    h1s = [h for h in soup.find_all("h1") if h.get_text(strip=True)]
    for h in h1s:
        if _clean_title(h.get_text(" ", strip=True)).lower() == page_title.lower():
            return h
    # The banner may hold an h1 too (category name); the article title is the last one on the page.
    return h1s[-1] if h1s else None


def content_candidates(soup: BeautifulSoup, heading, cfg: dict[str, Any]) -> list[tuple[int, Any, str]]:
    """Score possible article containers: prose counts for, link text (menus, category trees, TOCs) against."""
    found: dict[int, tuple[Any, str]] = {}
    for sel in cfg["content_selectors"]:
        try:
            for el in soup.select(sel):
                found.setdefault(id(el), (el, sel))
        except Exception:
            continue
    if heading is not None:
        for anc in heading.parents:
            if anc.name in ("body", "html", "[document]"):
                break
            found.setdefault(id(anc), (anc, "around title"))
    scored = []
    for el, how in found.values():
        prose, linked = _words(el)
        if prose:
            scored.append((prose - linked, el, how))
    # Best score first; on a tie the smaller (more specific) element wins.
    scored.sort(key=lambda t: (-t[0], len(t[1].get_text(" ", strip=True))))
    return scored


def _own_id(url: str) -> str | None:
    m = re.search(r"[?&](?:id|aid|article|art)=(\d+)", url, re.I)
    return m.group(1) if m else None


def _is_meta_line(line: str, own_id: str, cfg: dict[str, Any]) -> bool:
    """PHPKB's bar under the title, e.g. "402 [Security Events](...) [0](#comments)": the article's own id,
    links and maybe a date, nothing else. Its id would make identical articles hash differently."""
    if not re.search(rf"(?<![\w.]){own_id}(?![\w.])", line):
        return False
    rest = re.sub(r"\[[^\]]*\]\([^)]*\)", " ", line)
    rest = re.sub(cfg["updated_regex"], " ", rest, flags=re.I)
    rest = re.sub(rf"(?<![\w.]){own_id}(?![\w.])", " ", rest)
    return not re.search(r"[A-Za-z]{2,}", rest)


def parse_page(html: str, url: str, cfg: dict[str, Any]) -> dict[str, Any]:
    soup = BeautifulSoup(html, "lxml")
    page_title = _clean_title(soup.title.get_text(" ", strip=True)) if soup.title else ""
    heading = _title_heading(soup, page_title)
    title = _clean_title(heading.get_text(" ", strip=True)) if heading is not None else (page_title or url)
    crumbs = _breadcrumb(soup, cfg, title)
    m = re.search(cfg["updated_regex"], soup.get_text(" ", strip=True), re.I)
    updated = m.group(1) if m else ""

    body, selector = _first(soup, cfg.get("extra_content_selectors") or [])  # explicit config wins
    if body is None:
        ranked = content_candidates(soup, heading, cfg)
        if ranked:
            _, body, how = ranked[0]
            selector = f"{_describe(body)} ({how})"
        else:
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
    for h1 in body.find_all("h1"):
        if _clean_title(h1.get_text(" ", strip=True)) == title:
            h1.decompose()
            break

    prose, linked = _words(body)
    words = prose + linked
    density = linked / (words or 1)
    md = markdownify(str(body), heading_style="ATX", bullets="-", strip=["span", "div"])
    own_id = _own_id(url)
    if own_id:
        md = "\n".join(ln for ln in md.splitlines() if not _is_meta_line(ln, own_id, cfg))
    md = re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", md)).strip()

    by_url = matches_article_pattern(url, cfg)
    by_shape = words >= cfg["min_article_words"] and density <= cfg["max_link_density"]
    return {
        "title": title, "breadcrumb": crumbs, "updated": updated, "markdown": md,
        "words": words, "link_density": round(density, 2), "content_selector": selector,
        "is_article": by_url or by_shape,
        "classified_by": "url_pattern" if by_url else ("shape" if by_shape else "listing"),
    }


def _line_key(line: str) -> str:
    """A line with its link targets removed: "[Yes](...rate.php?id=7)" and "[Yes](...id=8)" are the same line."""
    return re.sub(r"\]\([^)]*\)", "]", line.strip())


def boilerplate_lines(markdowns: list[str], cfg: dict[str, Any]) -> dict[str, int]:
    """Lines that recur on at least half the articles are page template, not content
    (e.g. "Was this article helpful? Yes No"). Headings are never treated as boilerplate."""
    if len(markdowns) < cfg["boilerplate_min_pages"]:
        return {}
    counts: dict[str, int] = {}
    for md in markdowns:
        for line in {_line_key(ln) for ln in md.splitlines()}:
            if len(line) >= 3 and not line.startswith("#") and not re.fullmatch(r"[-*_|: ]+", line):
                counts[line] = counts.get(line, 0) + 1
    limit = max(cfg["boilerplate_min_pages"], cfg["boilerplate_share"] * len(markdowns))
    return {line: n for line, n in counts.items() if n >= limit}


def strip_lines(md: str, lines: dict[str, int]) -> str:
    kept = [ln for ln in md.splitlines() if _line_key(ln) not in lines]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


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
    pages: dict[str, dict[str, Any]] = {}
    items: list[dict[str, Any]] = []

    page_recs = [r for r in manifest if r.get("kind") == "page"]
    for rec in page_recs:
        html = Path(rec["path"]).read_text(encoding="utf-8", errors="replace")
        pages[rec["url"]] = parse_page(html, rec.get("final_url") or rec["url"], cfg)

    article_recs = [r for r in page_recs if pages[r["url"]]["is_article"]]
    boiler = boilerplate_lines([pages[r["url"]]["markdown"] for r in article_recs], cfg)
    report = paths.review / "boilerplate_lines.txt"
    report.write_text("Lines removed from every article because they repeat across most pages (count  line):\n"
                      + "".join(f"{n:>5}  {line}\n" for line, n in sorted(boiler.items(), key=lambda t: -t[1])),
                      encoding="utf-8")

    for rec in article_recs:
        info = pages[rec["url"]]
        md = strip_lines(info["markdown"], boiler)
        out = paths.extracted / f"{rec['id']}.md"
        out.write_text(md, encoding="utf-8")
        items.append({
            "id": rec["id"], "type": "article", "title": info["title"],
            "category": " > ".join(info["breadcrumb"]) or "General",
            "source_url": rec["url"], "updated": info["updated"], "words": info["words"],
            "sha256": rec["sha256"], "text_sha256": text_hash(md, cfg),
            "path": str(out), "raw_path": rec["path"], "classified_by": info["classified_by"],
            "content_selector": info["content_selector"], "flags": [] if md.strip() else ["empty"],
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
    if boiler:
        print(f"Removed {len(boiler)} template lines repeated across most articles -> {report}")
    fallback = sum(1 for it in items if it.get("content_selector") == "body")
    if fallback:
        print(f"Note: {fallback} articles fell back to the whole <body>; add the right selector to extra_content_selectors.")
    ocr = sum(1 for it in items if "needs_ocr" in it.get("flags", []))
    if ocr:
        print(f"Note: {ocr} PDFs look scanned (little or no text); they'll be uploaded as original PDFs.")
