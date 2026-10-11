"""End-to-end run against the mock KB: login, crawl, extract, dedup, review, bundle."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path

import pytest
import yaml

from gtbkb.bundle import bundle
from gtbkb.config import load_config, read_jsonl
from gtbkb.crawl import crawl
from gtbkb.dedup import dedup
from gtbkb.extract import extract
from gtbkb.login import login
from gtbkb.urls import is_allowed, normalize
from tests import mock_kb

if Path("/opt/pw-browsers/chromium").exists():
    os.environ.setdefault("KB_CHROMIUM_PATH", "/opt/pw-browsers/chromium")


@pytest.fixture()
def kb(tmp_path, monkeypatch):
    server, base = mock_kb.serve()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "base_url": base, "login_url": base + "login.php", "start_urls": [base],
        "allowed_hosts": ["127.0.0.1"], "workdir": str(tmp_path / "kb_data"), "delay_seconds": 0,
    }))
    monkeypatch.setenv("KB_USERNAME", mock_kb.USER)
    monkeypatch.setenv("KB_PASSWORD", mock_kb.PASSWORD)
    yield load_config(cfg_path), tmp_path / "kb_data"
    server.shutdown()


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_end_to_end(kb, capsys):
    cfg, work = kb
    login(cfg, headless=True)
    state = json.loads((work / "auth/state.json").read_text())
    assert any(c["name"] == "kbsess" for c in state["cookies"])
    assert mock_kb.PASSWORD not in (work / "auth/state.json").read_text()

    crawl(cfg)
    hits = mock_kb.Handler.hits
    for trap in ("/logout.php", "/rate.php", "/print.php", "/export.php", "/email-article.php", "/search.php"):
        assert hits[trap] == 0, f"crawler requested {trap}"
    assert hits["/article.php"] == 8  # tracking params and #fragments collapsed: one fetch per article
    report = (work / "crawl_report.txt").read_text()
    assert "/article.php?id=N" in report

    extract(cfg)
    items = read_jsonl(work / "items.jsonl")
    arts = {i["title"]: i for i in items if i["type"] == "article"}
    assert len(arts) == 8, sorted(arts)
    assert arts["Installing the Endpoint Agent"]["category"] == "Endpoint"
    assert arts["Installing the Endpoint Agent"]["updated"] == "March 5, 2024"
    assert "Helpful" not in Path(arts["Installing the Endpoint Agent"]["path"]).read_text()  # rating widget stripped
    pdfs = {i["filename"]: i for i in items if i["type"] == "pdf"}
    assert len(pdfs) == 4
    assert pdfs["Scanned_Datasheet.pdf"]["flags"] == ["needs_ocr"]
    assert pdfs["GTB_Admin_Guide_v7.0.pdf"]["category"] == "Discovery"  # borrowed from the linking article

    dedup(cfg)
    exact = rows(work / "review/exact_duplicates.csv")
    dropped = {r["title"] for r in exact if r["action"].startswith("drop")}
    # Re-dated copy of article 1 (older one loses) and a byte-identical PDF mirror.
    assert dropped == {"Install Endpoint Agent (old link)", "Admin Guide (mirror)"}, exact

    near = rows(work / "review/near_duplicates.csv")
    pairs = {frozenset((r["a_title"], r["b_title"])): r for r in near}
    email = pairs[frozenset(("Configuring Email Channel Policies", "Configuring Email Channel Policies (2025)"))]
    assert "is newer" in email["suggestion"]
    guide = pairs[frozenset(("Admin Guide 7.0", "Admin Guide 7.1"))]
    assert "versions" in guide["suggestion"]
    contained = pairs[frozenset(("Fingerprinting Overview", "Admin Guide 7.0"))]
    assert "contained" in contained["suggestion"]

    # Reviewer drops the older email article; the decision must survive a re-run.
    email["decision"] = "drop_a" if email["a_title"] == "Configuring Email Channel Policies" else "drop_b"
    with (work / "review/near_duplicates.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(near[0].keys()))
        w.writeheader()
        w.writerows(near)
    dedup(cfg)
    assert [r["decision"] for r in rows(work / "review/near_duplicates.csv") if r["decision"]] == [email["decision"]]

    bundle(cfg)
    up = work / "upload"
    policies = (up / "articles/policies.md").read_text()
    assert "Configuring Email Channel Policies (2025)" in policies
    assert "## Configuring Email Channel Policies\n" not in policies
    assert "Source: " in policies
    assert (up / "pdfs_original/Scanned_Datasheet.pdf").exists()
    assert len(list((up / "pdfs").glob("*.md"))) == 2  # v7.0 and v7.1, mirror dropped, scan kept as original
    index = (up / "INDEX.md").read_text()
    assert "Install Endpoint Agent (old link)" not in index
    inventory = rows(work / "review/final_inventory.csv")
    assert sum(r["status"] == "dropped" for r in inventory) == 3

    # Second crawl resumes from disk: no new requests to the server.
    before = sum(hits.values())
    crawl(cfg)
    assert sum(hits.values()) == before


@pytest.mark.parametrize("url,ok", [
    ("https://kb.gttb.com/article.php?id=12", True),
    ("https://kb.gttb.com/article/email-channel-policy-12.html", True),
    ("https://kb.gttb.com/article/network-share-scanning.html", True),
    ("https://kb.gttb.com/article/printer-control-and-password-protected-files.html", True),
    ("https://kb.gttb.com/article/how-to-generate-a-report.html", True),
    ("https://kb.gttb.com/download.php?id=4", True),
    ("https://downloads.gttb.com/docs/guide.pdf", True),
    ("https://downloads.gttb.com/docs/", False),
    ("https://example.com/guide.pdf", False),
    ("https://kb.gttb.com/logout.php", False),
    ("https://kb.gttb.com/index.php?action=logout", False),
    ("https://kb.gttb.com/rate.php?id=1&v=5", False),
    ("https://kb.gttb.com/print.php?id=1", False),
    ("https://kb.gttb.com/export.php?id=1&type=pdf", False),
    ("https://kb.gttb.com/email-article.php?id=1", False),
    ("https://kb.gttb.com/search.php?q=agent", False),
    ("https://kb.gttb.com/category.php?id=1&sort=title", False),
])
def test_url_filter(url, ok):
    cfg = load_config(None)
    assert is_allowed(url, cfg) is ok


def test_normalize_collapses_variants():
    cfg = load_config(None)
    a = normalize("/article.php?id=5&utm_source=x#top", "https://KB.gttb.com/", cfg)
    b = normalize("https://kb.gttb.com:443//article.php?id=5", "https://kb.gttb.com/", cfg)
    assert a == b == "https://kb.gttb.com/article.php?id=5"


def test_expired_session_stops_and_resumes(kb):
    cfg, work = kb
    login(cfg, headless=True)
    state_path = work / "auth/state.json"
    state = json.loads(state_path.read_text())
    for c in state["cookies"]:
        c["value"] = "expired"
    state_path.write_text(json.dumps(state))
    with pytest.raises(SystemExit, match="Session expired"):
        crawl(cfg)
    login(cfg, headless=True)
    crawl(cfg)
    assert sum(1 for r in read_jsonl(work / "manifest.jsonl") if r.get("status") == "ok") == 19
