"""Build the folder you upload to the Claude project.

upload/
  INDEX.md                      every kept item: title, type, category, source URL, which file it's in
  articles/<category>.md        articles bundled per category, each with a source header
  pdfs/<title>.md               extracted PDF text (pdf_mode "text" or "both")
  pdfs_original/<file>.pdf      original PDFs (pdf_mode "original"/"both", and always for scanned PDFs)
  other_files/                  non-PDF attachments, as downloaded
"""

from __future__ import annotations

import csv
import re
import shutil
from pathlib import Path
from typing import Any

from .config import paths_for, read_jsonl
from .dedup import load_drops


def slug(text: str, limit: int = 80) -> str:
    s = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)[:limit].strip("-") or "untitled"


def _header(it: dict[str, Any]) -> str:
    lines = [f"Source: {it['source_url']}", f"Category: {it['category']}"]
    if it.get("updated"):
        lines.append(f"Last updated: {it['updated']}")
    if it.get("pages"):
        lines.append(f"Pages: {it['pages']}")
    return "\n".join(lines)


def _unique(path: Path, used: set[Path]) -> Path:
    candidate, n = path, 2
    while candidate in used:
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        n += 1
    used.add(candidate)
    return candidate


def bundle(cfg: dict[str, Any]) -> None:
    paths = paths_for(cfg)
    items = read_jsonl(paths.items)
    if not items:
        raise SystemExit("No items yet. Run extract and dedup first.")
    if not paths.exact_dups.exists():
        raise SystemExit("Run `python -m gtbkb dedup` before bundling.")
    drops = load_drops(cfg)
    out = paths.upload
    if out.exists():
        shutil.rmtree(out)
    for sub in ("articles", "pdfs", "pdfs_original", "other_files"):
        (out / sub).mkdir(parents=True)

    kept = [it for it in items if it["id"] not in drops]
    placed: dict[str, str] = {}
    used: set[Path] = set()

    # Articles: one bundle per category, split when it gets large.
    by_cat: dict[str, list[dict[str, Any]]] = {}
    for it in kept:
        if it["type"] == "article":
            by_cat.setdefault(it["category"], []).append(it)
    for cat, arts in sorted(by_cat.items()):
        arts.sort(key=lambda a: a["title"].lower())
        parts: list[list[tuple[str, str]]] = [[]]
        size = 0
        for a in arts:
            body = Path(a["path"]).read_text(encoding="utf-8")
            block = f"## {a['title']}\n\n{_header(a)}\n\n{body}"
            if parts[-1] and size + len(block) > cfg["bundle_max_chars"]:
                parts.append([])
                size = 0
            parts[-1].append((block, a["id"]))
            size += len(block)
        for n, chunk in enumerate(parts, 1):
            name = slug(cat) + (f"-part{n}" if len(parts) > 1 else "") + ".md"
            target = _unique(out / "articles" / name, used)
            target.write_text(f"# GTB DLP Knowledge Base - {cat}\n\n" + "\n\n---\n\n".join(b for b, _ in chunk) + "\n",
                              encoding="utf-8")
            for _, item_id in chunk:
                placed[item_id] = str(target.relative_to(out))

    # PDFs: extracted text and/or the original file.
    mode = cfg["pdf_mode"]
    for it in kept:
        if it["type"] == "pdf":
            scanned = "needs_ocr" in it["flags"] or not it.get("path")
            dests = []
            if mode in ("text", "both") and not scanned:
                target = _unique(out / "pdfs" / f"{slug(it['title'])}.md", used)
                body = Path(it["path"]).read_text(encoding="utf-8")
                target.write_text(f"# {it['title']}\n\n{_header(it)}\nOriginal file: {it['filename']}\n\n{body}\n", encoding="utf-8")
                dests.append(str(target.relative_to(out)))
            if mode in ("original", "both") or scanned:
                target = _unique(out / "pdfs_original" / it["filename"], used)
                shutil.copy2(it["raw_path"], target)
                dests.append(str(target.relative_to(out)))
            placed[it["id"]] = " + ".join(dests)
        elif it["type"] == "other":
            target = _unique(out / "other_files" / it["filename"], used)
            shutil.copy2(it["raw_path"], target)
            placed[it["id"]] = str(target.relative_to(out))

    # Index, so Claude (and you) can see what's in the project and where it came from.
    rows = sorted(kept, key=lambda i: (i["category"].lower(), i["type"], i["title"].lower()))
    index = ["# GTB DLP Knowledge Base - Index", "",
             f"{len(kept)} items exported from {cfg['base_url']}. Duplicates removed: {len(drops)}.", "",
             "| Title | Type | Category | Updated | File | Source |", "|---|---|---|---|---|---|"]
    for it in rows:
        cell = lambda s: str(s).replace("|", "\\|")
        index.append(f"| {cell(it['title'])} | {it['type']} | {cell(it['category'])} | {it.get('updated', '')} "
                     f"| {cell(placed.get(it['id'], ''))} | {it['source_url']} |")
    (out / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")

    with paths.inventory.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["status", "reason", "id", "type", "title", "category", "updated", "words", "flags", "upload_file", "source_url"])
        for it in sorted(items, key=lambda i: (i["id"] in drops, i["category"], i["title"])):
            w.writerow(["dropped" if it["id"] in drops else "kept", drops.get(it["id"], ""), it["id"], it["type"],
                        it["title"], it["category"], it.get("updated", ""), it["words"], ";".join(it["flags"]),
                        placed.get(it["id"], ""), it["source_url"]])

    for sub in ("pdfs", "pdfs_original", "other_files"):
        if not any((out / sub).iterdir()):
            (out / sub).rmdir()
    files = [p for p in out.rglob("*") if p.is_file()]
    text_chars = sum(p.stat().st_size for p in files if p.suffix == ".md")
    print(f"Kept {len(kept)} of {len(items)} items ({len(drops)} dropped as duplicates).")
    print(f"Upload folder: {out}  ({len(files)} files, ~{text_chars // 4:,} tokens of Markdown)")
    print(f"Full kept/dropped list: {paths.inventory}")
