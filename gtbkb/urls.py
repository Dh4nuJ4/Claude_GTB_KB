"""URL normalisation and classification.

Normalising is the first line of defence against duplicates: the same article reached
via a tracking parameter, a fragment or a different parameter order is fetched once.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit


def normalize(url: str, base: str, cfg: dict[str, Any]) -> str | None:
    url = (url or "").strip()
    if not url or url.startswith("#"):
        return None
    try:
        absolute = urljoin(base, url)
        parts = urlsplit(absolute)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"
    drop = [re.compile(p, re.I) for p in cfg["drop_query_params"]]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not any(d.search(k) for d in drop)]
    query.sort()
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))


def url_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def is_allowed(url: str, cfg: dict[str, Any]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    in_kb = host in [h.lower() for h in cfg["allowed_hosts"]]
    # Documents may live on another GTB host (e.g. a downloads server); fetch those, but don't crawl it.
    file_host = looks_like_file(url, cfg) and any(
        host == h.lower() or host.endswith("." + h.lower()) for h in cfg["file_hosts"])
    if not (in_kb or file_host):
        return False
    return not _any(cfg["exclude_patterns"], url)


def is_login_url(url: str, cfg: dict[str, Any]) -> bool:
    return _any(cfg["login_markers"], url)


def matches_article_pattern(url: str, cfg: dict[str, Any]) -> bool:
    return _any(cfg["article_patterns"], url)


def looks_like_file(url: str, cfg: dict[str, Any]) -> bool:
    path = urlsplit(url).path.lower()
    return any(path.endswith("." + ext) for ext in cfg["file_extensions"])


def pattern_of(url: str) -> str:
    """Collapse a URL into its shape, e.g. /article.php?id=N, for the crawl report."""
    parts = urlsplit(url)
    path = re.sub(r"\d+", "N", parts.path)
    path = re.sub(r"/[^/]*-[^/]*(?=\.html?$)", "/<slug>", path)
    keys = sorted(k + ("=N" if v.isdigit() else "=…") for k, v in parse_qsl(parts.query, keep_blank_values=True))
    return path + ("?" + "&".join(keys) if keys else "")
