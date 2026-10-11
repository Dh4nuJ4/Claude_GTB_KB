"""Command line: python -m gtbkb <login|crawl|extract|dedup|bundle|all> [--config config.yaml]"""

from __future__ import annotations

import argparse

from .config import load_config


def main() -> None:
    ap = argparse.ArgumentParser(prog="gtbkb", description="Export the GTB DLP knowledge base for a Claude project.")
    ap.add_argument("--config", default="config.yaml", help="path to config.yaml (default: ./config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="log in once and save the session cookies")
    lg.add_argument("--headless", action="store_true", help="no browser window (needs KB_USERNAME/KB_PASSWORD)")
    cr = sub.add_parser("crawl", help="download every page and attachment (resumable)")
    cr.add_argument("--refresh", action="store_true", help="re-download everything instead of resuming")
    sub.add_parser("extract", help="turn pages and PDFs into clean text items")
    sub.add_parser("dedup", help="drop exact duplicates, report near-duplicates for review")
    sub.add_parser("bundle", help="build the upload/ folder for the Claude project")
    al = sub.add_parser("all", help="crawl, extract, dedup and bundle in one go")
    al.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.cmd == "login":
        from .login import login
        login(cfg, headless=args.headless)
        return
    if args.cmd in ("crawl", "all"):
        from .crawl import crawl
        crawl(cfg, refresh=args.refresh)
    if args.cmd in ("extract", "all"):
        from .extract import extract
        extract(cfg)
    if args.cmd in ("dedup", "all"):
        from .dedup import dedup
        dedup(cfg)
    if args.cmd in ("bundle", "all"):
        from .bundle import bundle
        bundle(cfg)


if __name__ == "__main__":
    main()
