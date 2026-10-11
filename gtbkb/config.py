"""Configuration loading and the working-directory layout."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "base_url": "https://kb.gttb.com/",
    "login_url": "https://kb.gttb.com/login.php",
    "start_urls": ["https://kb.gttb.com/"],
    "allowed_hosts": ["kb.gttb.com"],
    # Hosts (and their subdomains) that documents like PDFs may be downloaded from.
    "file_hosts": ["gttb.com"],
    "workdir": "kb_data",
    "delay_seconds": 0.5,
    "max_requests": 3000,
    "request_timeout_seconds": 60,
    "render_js": False,
    # A final URL matching any of these means the session is gone.
    "login_markers": [r"login\.php", r"/login\b", r"signin"],
    # Anchored to script names / path segments / action parameters on purpose: plain words
    # like "email", "print", "share" or "password" are also legitimate DLP article topics.
    "exclude_patterns": [
        # session / account
        r"/(logout|logoff|log-out|signout|sign-out)(\.php|/|\?|$)",
        r"/(login|signin|register|signup|forgot[-_a-z]*|lost[-_]?password|reset[-_]?password|my[-_]?account|account|profile|settings)(\.php|/|\?|$)",
        # anything that writes to the portal or produces a copy of an article
        r"/(rate|rating|vote|subscribe|unsubscribe|favou?rites?|bookmarks?|comments?|feedback|delete|remove|add|edit|update|save|submit|post)[-_a-z]*(\.php|/|\?|$)",
        r"/(print|email|export|share|send|mail)[-_a-z]*(\.php|/|\?|$)",
        r"[?&](action|act|do|task|op|cmd|mode)=(logout|delete|remove|rate|vote|subscribe|unsubscribe|print|email|export|favou?rite|bookmark|comment|add[a-z_]*|edit|save)",
        r"[?&](print|email|export|pdf)=", r"[?&](type|format|output)=(pdf|doc|docx|rtf|print)",
        # search and re-sorted listings explode the crawl without adding content
        r"/search[-_a-z]*(\.php|/|\?|$)", r"[?&](q|query|keyword|keywords|search|s)=",
        r"[?&](sort|order|orderby|sortby|dir)=",
        r"^(mailto|javascript|tel):",
        r"\.(png|jpe?g|gif|svg|ico|bmp|webp|css|js|woff2?|ttf|eot|map)(\?|$)",
        r"\.(rss|atom)(\?|$)", r"[?&]format=(rss|atom)", r"/(feed|rss)(\.php|/|\?|$)",
    ],
    "extra_exclude_patterns": [],
    "article_patterns": [
        r"article\.php\?(.*&)?(id|ID|aid)=\d+",
        r"/article/", r"/articles/",
        r"[?&](article|art|aid|kb)=\d+",
        r"/kb/\d+", r"-a\d+\.html?$", r"/\d+/[^/]+\.html?$",
    ],
    "extra_article_patterns": [],
    # Tracking/session parameters that never change the content.
    "drop_query_params": [r"^utm_", r"^PHPSESSID$", r"^sid$", r"^sessid$", r"^fbclid$", r"^gclid$", r"^ref$"],
    "file_extensions": ["pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "rtf", "zip", "msg", "xml", "json"],
    "content_selectors": [
        "#article-content", "#articleContent", ".article-content", ".articleContent",
        "#article-body", ".article-body", ".article_body", ".kb-article", ".kbarticle",
        "#kb-article", ".article-text", "#article", ".article", "article",
        "[itemprop=articleBody]", "#content", ".content", "main", "#main",
    ],
    "extra_content_selectors": [],
    "title_selectors": ["h1.article-title", ".article-title", "#article-title", "[itemprop=headline]", "article h1", "h1", "title"],
    "extra_title_selectors": [],
    "breadcrumb_selectors": [".breadcrumb", ".breadcrumbs", "#breadcrumb", "#breadcrumbs", "nav[aria-label=breadcrumb]", "[itemtype*=BreadcrumbList]", ".crumbs"],
    "extra_breadcrumb_selectors": [],
    # Removed from the article body before converting to Markdown.
    "strip_selectors": [
        "script", "style", "noscript", "nav", "header", "footer", "form", "iframe",
        ".breadcrumb", ".breadcrumbs", ".sidebar", "#sidebar", ".related", ".related-articles",
        ".rating", ".article-rating", ".social", ".share", ".comments", "#comments",
        ".pagination", ".print", ".toolbar", ".article-actions", ".tags",
    ],
    "updated_regex": r"(?:last\s+(?:updated|modified)|updated(?:\s+on)?|modified(?:\s+on)?|date)\s*[:\-]?\s*"
                     r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[/.]\d{1,2}[/.]\d{2,4})",
    # Heuristic classification for pages that don't match an article pattern.
    "min_article_words": 80,
    "max_link_density": 0.4,
    "near_duplicate_threshold": 0.80,
    "containment_threshold": 0.90,
    "min_words_for_near_dup": 40,
    "shingle_size": 5,
    "pdf_mode": "text",
    "bundle_max_chars": 400000,
    "user_agent": "Mozilla/5.0 (KB export for internal Claude project; contact your GTB admin)",
}

# Lists where the config file *adds* to the defaults instead of replacing them.
EXTENDS = {
    "extra_exclude_patterns": "exclude_patterns",
    "extra_article_patterns": "article_patterns",
    "extra_content_selectors": "content_selectors",
    "extra_title_selectors": "title_selectors",
    "extra_breadcrumb_selectors": "breadcrumb_selectors",
}


@dataclass
class Paths:
    root: Path

    @property
    def auth_state(self) -> Path: return self.root / "auth" / "state.json"
    @property
    def raw_pages(self) -> Path: return self.root / "raw" / "pages"
    @property
    def raw_files(self) -> Path: return self.root / "raw" / "files"
    @property
    def manifest(self) -> Path: return self.root / "manifest.jsonl"
    @property
    def crawl_report(self) -> Path: return self.root / "crawl_report.txt"
    @property
    def extracted(self) -> Path: return self.root / "extracted"
    @property
    def items(self) -> Path: return self.root / "items.jsonl"
    @property
    def review(self) -> Path: return self.root / "review"
    @property
    def exact_dups(self) -> Path: return self.review / "exact_duplicates.csv"
    @property
    def near_dups(self) -> Path: return self.review / "near_duplicates.csv"
    @property
    def inventory(self) -> Path: return self.review / "final_inventory.csv"
    @property
    def upload(self) -> Path: return self.root / "upload"

    def ensure(self) -> None:
        for p in (self.auth_state.parent, self.raw_pages, self.raw_files, self.extracted, self.review):
            p.mkdir(parents=True, exist_ok=True)


def load_config(path: str | Path | None) -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    if path and Path(path).exists():
        user = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        for key, value in user.items():
            if key in EXTENDS:
                target = EXTENDS[key]
                cfg[target] = list(value or []) + cfg[target]
            else:
                cfg[key] = value
    return cfg


def paths_for(cfg: dict[str, Any]) -> Paths:
    p = Paths(Path(cfg["workdir"]))
    p.ensure()
    return p


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)
