"""A small fake knowledge base behind a login, with planted duplicates and trap URLs."""

from __future__ import annotations

import random
import threading
from collections import Counter
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pymupdf

USER, PASSWORD, SESSION = "analyst", "s3cret", "sess-123"
VOCAB = ("policy endpoint agent channel classification fingerprint scan discovery incident "
         "rule console server network printer removable media encryption exception report "
         "deployment upgrade license certificate proxy cloud content detection alert workflow").split()


def words(seed: int, n: int) -> str:
    rnd = random.Random(seed)
    sentences = []
    while sum(len(s.split()) for s in sentences) < n:
        sentences.append(" ".join(rnd.choice(VOCAB) for _ in range(rnd.randint(8, 14))).capitalize() + ".")
    return " ".join(sentences)


BASE = {i: words(i, 220) for i in (1, 2, 3, 4, 7, 8)}
ARTICLES = {
    1: ("Installing the Endpoint Agent", "Endpoint", BASE[1], "March 5, 2024"),
    2: ("Configuring Email Channel Policies", "Policies", BASE[2], "January 10, 2024"),
    3: ("Fingerprinting Overview", "Policies", BASE[3], "February 1, 2023"),
    4: ("Upgrading the Server", "Endpoint", BASE[4], "April 2, 2024"),
    5: ("Install Endpoint Agent (old link)", "Endpoint", BASE[1], "March 5, 2023"),  # exact text dup of 1
    6: ("Configuring Email Channel Policies (2025)", "Policies", BASE[2].replace("policy", "rule", 3) + " Updated for 2025.", "May 1, 2025"),  # near dup of 2
    7: ("Network Share Scanning", "Discovery", BASE[7], "June 6, 2024"),
    8: ("Product Documentation", "Discovery", BASE[8], "July 7, 2024"),
}
CATEGORIES = {1: ("Endpoint", [1, 4, 5]), 2: ("Policies", [2, 3, 6]), 3: ("Discovery", [7, 8])}
CAT_ID = {name: c for c, (name, _) in CATEGORIES.items()}


def make_pdf(text: str) -> bytes:
    doc = pymupdf.open()
    chunk = text.split()
    for i in range(0, len(chunk), 300):
        page = doc.new_page()
        page.insert_textbox(pymupdf.Rect(50, 50, 550, 800), " ".join(chunk[i:i + 300]), fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def blank_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


GUIDE = words(100, 900) + " " + BASE[3]  # article 3 is copied out of this guide
PDFS = {
    "10": ("GTB_Admin_Guide_v7.0.pdf", make_pdf(GUIDE)),
    "11": ("GTB_Admin_Guide_v7.1.pdf", make_pdf(GUIDE + " " + words(101, 60))),  # near dup of v7.0
    "12": ("Scanned_Datasheet.pdf", blank_pdf()),
}
PDFS["copy"] = ("admin-guide-copy.pdf", PDFS["10"][1])  # identical bytes, different URL


def page(title: str, body: str, crumbs: str = "") -> bytes:
    return f"""<!doctype html><html><head><title>{title} - GTB Knowledge Base</title></head><body>
<header><nav><a href="/index.php">Home</a> <a href="/search.php?q=agent">Search</a>
<a href="/logout.php">Logout</a></nav></header>
<div class="breadcrumb">{crumbs}</div>
<div class="sidebar"><a href="/category.php?id=1">Endpoint</a> <a href="/category.php?id=2">Policies</a>
<a href="/category.php?id=3">Discovery</a></div>
{body}
<footer>Copyright GTB <a href="/logout.php">Sign out</a></footer></body></html>""".encode()


class Handler(BaseHTTPRequestHandler):
    hits: Counter = Counter()

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/html; charset=utf-8", headers=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        c = SimpleCookie(self.headers.get("Cookie", ""))
        return "kbsess" in c and c["kbsess"].value == SESSION

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        form = parse_qs(self.rfile.read(length).decode())
        if form.get("username") == [USER] and form.get("password") == [PASSWORD]:
            self._send(302, headers={"Location": "/index.php", "Set-Cookie": f"kbsess={SESSION}; Path=/; HttpOnly"})
        else:
            self._send(200, page("Login", '<p class="error">Invalid username or password</p>' + LOGIN_FORM))

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        Handler.hits[url.path] += 1
        if url.path == "/login.php":
            return self._send(200, f"<html><body>{LOGIN_FORM}</body></html>".encode())
        if not self._authed():
            return self._send(302, headers={"Location": "/login.php"})
        if url.path in ("/", "/index.php"):
            links = "".join(f'<li><a href="/category.php?id={c}">{name}</a></li>' for c, (name, _) in CATEGORIES.items())
            return self._send(200, page("Home", f"<main><h1>Knowledge Base</h1><ul>{links}</ul></main>"))
        if url.path == "/category.php":
            name, ids = CATEGORIES[int(q["id"])]
            pg = int(q.get("page", 1))
            shown = ids[:2] if pg == 1 else ids[2:]
            links = "".join(f'<li><a href="/article.php?id={i}&utm_source=nav#top">{ARTICLES[i][0]}</a></li>' for i in shown)
            more = f'<a href="/category.php?id={q["id"]}&page=2">Next page</a>' if pg == 1 and len(ids) > 2 else ""
            sort = f'<a href="/category.php?id={q["id"]}&sort=title">Sort by title</a>'
            return self._send(200, page(name, f"<main><h1>{name}</h1><ul>{links}</ul>{more} {sort}</main>",
                                        '<a href="/index.php">Home</a> &gt; ' + name))
        if url.path == "/article.php":
            i = int(q["id"])
            title, cat, text, updated = ARTICLES[i]
            sentences = text.split(". ")
            paras = "".join(f"<p>{'. '.join(sentences[k:k + 4])}</p>" for k in range(0, len(sentences), 4))
            extra = ""
            if i == 8:
                extra = ('<ul><li><a href="/download.php?id=10">Admin Guide 7.0</a></li>'
                         '<li><a href="/files/admin-guide-copy.pdf">Admin Guide (mirror)</a></li>'
                         '<li><a href="/download.php?id=11">Admin Guide 7.1</a></li>'
                         '<li><a href="/download.php?id=12">Datasheet</a></li></ul>')
            body = (f'<div id="article-content"><h1>{title}</h1><p class="meta">Last Updated: {updated}</p>{paras}{extra}'
                    f'<div class="rating"><a href="/rate.php?id={i}&v=5">Helpful</a></div></div>'
                    f'<a href="/print.php?id={i}">Print</a> <a href="/export.php?id={i}&type=pdf">PDF</a>'
                    f' <a href="/email-article.php?id={i}">Email</a>')
            return self._send(200, page(title, body, f'<a href="/index.php">Home</a> &gt; <a href="/category.php?id={CAT_ID[cat]}">{cat}</a>'))
        if url.path in ("/download.php", "/files/admin-guide-copy.pdf"):
            name, data = PDFS[q.get("id", "copy")]
            return self._send(200, data, "application/pdf", {"Content-Disposition": f'attachment; filename="{name}"'})
        if url.path in ("/logout.php", "/rate.php", "/print.php", "/export.php", "/email-article.php", "/search.php"):
            return self._send(200, b"<html><body>trap</body></html>")
        self._send(404, b"not found")


LOGIN_FORM = ('<form method="post" action="/login.php"><input type="text" name="username">'
              '<input type="password" name="password"><button type="submit">Log in</button></form>')


def serve() -> tuple[ThreadingHTTPServer, str]:
    Handler.hits = Counter()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_port}/"
