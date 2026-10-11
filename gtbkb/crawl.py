"""Breadth-first crawl of the logged-in knowledge base.

Every HTML page is saved as-is to raw/pages (classification into articles vs listing pages
happens later, in extract), every attachment to raw/files, and one manifest.jsonl row is
written per URL. Re-running resumes: anything already on disk is re-read instead of re-fetched.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections import Counter, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from .browser import launch
from .config import paths_for, read_jsonl, write_jsonl
from .urls import is_allowed, is_login_url, looks_like_file, normalize, pattern_of, url_id

HTML_TYPES = ("text/html", "application/xhtml")


class SessionExpired(RuntimeError):
    pass


def _filename(url: str, headers: dict[str, str], content_type: str) -> str:
    cd = headers.get("content-disposition", "")
    m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
    name = unquote(m.group(1).strip()) if m else unquote(Path(urlsplit(url).path).name)
    if "." not in name:
        ext = {"application/pdf": ".pdf"}.get(content_type.split(";")[0].strip(), "")
        name = (name or "file") + ext
    return re.sub(r"[^\w.\- ]+", "_", name).strip()[:120] or "file"


def _links(html: str, page_url: str, cfg: dict[str, Any]) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    base = soup.find("base", href=True)
    base_url = base["href"] if base else page_url
    found = []
    for tag, attr in (("a", "href"), ("iframe", "src"), ("embed", "src"), ("object", "data"), ("area", "href")):
        for el in soup.find_all(tag):
            href = el.get(attr)
            if not href:
                continue
            norm = normalize(href, base_url, cfg)
            if norm:
                found.append((norm, " ".join(el.get_text(" ", strip=True).split())[:200]))
    return found


class Crawler:
    def __init__(self, cfg: dict[str, Any], refresh: bool = False):
        self.cfg = cfg
        self.paths = paths_for(cfg)
        self.refresh = refresh
        self.records: dict[str, dict[str, Any]] = {r["url"]: r for r in read_jsonl(self.paths.manifest)}
        self.skipped: Counter[str] = Counter()
        self.requests = 0

    # -- fetching ---------------------------------------------------------------
    def _cached(self, url: str) -> dict[str, Any] | None:
        rec = self.records.get(url)
        if self.refresh or not rec or rec.get("status") != "ok":
            return None
        return rec if Path(rec["path"]).exists() else None

    def _fetch(self, req, page, url: str) -> dict[str, Any]:
        self.requests += 1
        time.sleep(self.cfg["delay_seconds"])
        resp = req.get(url, timeout=self.cfg["request_timeout_seconds"] * 1000, max_redirects=10)
        final = resp.url
        if is_login_url(final, self.cfg):
            raise SessionExpired(f"{url} redirected to the login page")
        headers = {k.lower(): v for k, v in resp.headers.items()}
        ctype = headers.get("content-type", "").lower()
        rec: dict[str, Any] = {
            "url": url, "id": url_id(url), "final_url": final, "http_status": resp.status,
            "content_type": ctype, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if not resp.ok:
            rec.update(status=f"http_{resp.status}")
            return rec
        body = resp.body()
        rec["sha256"] = hashlib.sha256(body).hexdigest()
        rec["size"] = len(body)
        if any(t in ctype for t in HTML_TYPES):
            if self.cfg["render_js"] and page is not None:
                page.goto(url, wait_until="networkidle")
                body = page.content().encode("utf-8")
            lower = body.lower()
            if re.search(rb"type\s*=\s*[\"']?password", lower) and b"logout" not in lower:
                raise SessionExpired(f"{url} shows a login form")
            out = self.paths.raw_pages / f"{rec['id']}.html"
            out.write_bytes(body)
            rec.update(kind="page", path=str(out), status="ok")
        else:
            out = self.paths.raw_files / f"{rec['id']}__{_filename(final, headers, ctype)}"
            out.write_bytes(body)
            rec.update(kind="file", path=str(out), status="ok")
        return rec

    # -- main loop --------------------------------------------------------------
    def run(self) -> None:
        cfg = self.cfg
        if not self.paths.auth_state.exists():
            raise SystemExit("No saved session. Run `python -m gtbkb login` first.")

        queue: deque[tuple[str, str, str, int]] = deque()
        seen: set[str] = set()
        for start in cfg["start_urls"]:
            u = normalize(start, cfg["base_url"], cfg)
            if u and u not in seen:
                seen.add(u)
                queue.append((u, "", "", 0))

        with sync_playwright() as p:
            browser = page = None
            req = p.request.new_context(storage_state=str(self.paths.auth_state), user_agent=cfg["user_agent"])
            if cfg["render_js"]:
                browser = launch(p, headless=True)
                page = browser.new_context(storage_state=str(self.paths.auth_state)).new_page()
            try:
                while queue:
                    url, referrer, link_text, depth = queue.popleft()
                    rec = self._cached(url)
                    if rec is None:
                        if self.requests >= cfg["max_requests"]:
                            print(f"Stopped at max_requests={cfg['max_requests']}; re-run to continue.")
                            break
                        try:
                            rec = self._fetch(req, page, url)
                        except SessionExpired:
                            raise
                        except Exception as exc:  # network error: record and move on
                            rec = {"url": url, "id": url_id(url), "status": f"error: {exc.__class__.__name__}: {exc}"[:300]}
                        print(f"[{len(self.records) + 1:>4}] {rec.get('status'):<8} {rec.get('kind', '-'):<4} {url}")
                        if self.requests % 25 == 0:
                            self._save()
                    # Keep the first place we saw a link from; it gives files their category and title.
                    prev = self.records.get(url, {})
                    rec["referrer"] = prev.get("referrer") or referrer
                    rec["link_text"] = prev.get("link_text") or link_text
                    rec["depth"] = min(depth, prev.get("depth", depth))
                    self.records[url] = rec

                    if rec.get("status") != "ok" or rec.get("kind") != "page":
                        continue
                    html = Path(rec["path"]).read_text(encoding="utf-8", errors="replace")
                    for link, text in _links(html, rec.get("final_url") or url, cfg):
                        if link in seen:
                            continue
                        seen.add(link)
                        if not is_allowed(link, cfg):
                            self.skipped[pattern_of(link)] += 1
                            continue
                        queue.append((link, url, text, depth + 1))
            except SessionExpired as exc:
                self._save()
                raise SystemExit(f"Session expired ({exc}). Run `python -m gtbkb login` again, then re-run crawl; it resumes.")
            finally:
                self._save()
                req.dispose()
                if browser:
                    browser.close()
        self._report()

    def _save(self) -> None:
        write_jsonl(self.paths.manifest, list(self.records.values()))

    def _report(self) -> None:
        recs = list(self.records.values())
        ok = [r for r in recs if r.get("status") == "ok"]
        pages = Counter(pattern_of(r["url"]) for r in ok if r.get("kind") == "page")
        files = Counter(Path(r["path"]).suffix.lower() or "(none)" for r in ok if r.get("kind") == "file")
        failed = [r for r in recs if r.get("status") != "ok"]
        by_hash = Counter(r.get("sha256") for r in ok if r.get("kind") == "file")
        lines = [
            "GTB KB crawl report (URLs and counts only, no article content - safe to share)",
            f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "",
            f"URLs recorded: {len(recs)}   ok: {len(ok)}   failed: {len(failed)}",
            f"HTML pages: {sum(pages.values())}   files: {sum(files.values())}   identical file downloads: {sum(c - 1 for c in by_hash.values() if c > 1)}",
            "",
            "Page URL shapes (count  pattern):",
            *[f"  {c:>5}  {pat}" for pat, c in pages.most_common()],
            "",
            "File types:",
            *[f"  {c:>5}  {ext}" for ext, c in files.most_common()],
            "",
            "Links skipped by exclude_patterns / other hosts (count  pattern):",
            *[f"  {c:>5}  {pat}" for pat, c in self.skipped.most_common(60)],
            "",
            "Failures:",
            *[f"  {r.get('status')}  {r['url']}" for r in failed[:200]],
        ]
        self.paths.crawl_report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n{len(ok)} items saved ({sum(pages.values())} pages, {sum(files.values())} files), {len(failed)} failed.")
        print(f"Report: {self.paths.crawl_report}")


def crawl(cfg: dict[str, Any], refresh: bool = False) -> None:
    Crawler(cfg, refresh=refresh).run()
