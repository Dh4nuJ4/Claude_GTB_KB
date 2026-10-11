"""Find duplicate and near-duplicate items.

Exact duplicates (same file bytes, or same text after normalisation) are safe to drop
automatically: one canonical copy is kept. Near-duplicates are only *reported* in
review/near_duplicates.csv; a person decides what to drop via its `decision` column,
which survives re-runs.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any

from .config import paths_for, read_jsonl
from .extract import normalized_text

DECISIONS = ("", "keep_both", "drop_a", "drop_b")
TYPE_RANK = {"article": 0, "pdf": 1, "other": 2}


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


DATE_FORMATS = ("%Y-%m-%d", "%B %d %Y", "%b %d %Y", "%d %B %Y", "%d %b %Y",
                "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%m/%d/%y", "%d/%m/%y")  # GTB is US-based: m/d first


def _date_key(updated: str) -> str:
    """Best-effort ISO form of the many date formats a KB shows; "" if unparseable."""
    from datetime import datetime

    raw = (updated or "").strip()
    for candidate in (raw, re.sub(r"\s+", " ", re.sub(r"(?<=[A-Za-z])\.|,", " ", raw)).strip()):
        for fmt in DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
    return ""


def _canonical(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Within an exact-duplicate group keep the newest, then articles before files, then the shortest URL."""
    ordered = sorted(group, key=lambda it: (TYPE_RANK.get(it["type"], 9), len(it["source_url"]), it["source_url"]))
    ordered.sort(key=lambda it: _date_key(it.get("updated", "")), reverse=True)  # stable: undated last
    return ordered[0]


def _shingles(text: str, k: int) -> set[int]:
    words = normalized_text(text).split()
    if len(words) < k:
        return {hash(" ".join(words))} if words else set()
    return {hash(" ".join(words[i:i + k])) for i in range(len(words) - k + 1)}


VERSION = re.compile(r"\bv?(\d+(?:\.\d+)+)\b", re.I)


def _suggest(a: dict[str, Any], b: dict[str, Any], jac: float, cont: float) -> str:
    va, vb = set(VERSION.findall(a["title"])), set(VERSION.findall(b["title"]))
    if va and vb and va != vb:
        return "different product versions? keep both unless the older one is obsolete"
    if cont >= 0.9 and jac < 0.8:
        smaller = "a" if a["words"] < b["words"] else "b"
        return f"{smaller} is mostly contained in the other - drop_{smaller} if nothing unique in it"
    da, db = _date_key(a.get("updated", "")), _date_key(b.get("updated", ""))
    if da and db and da != db:
        older = "a" if da < db else "b"
        return f"revised copy? {'b' if older == 'a' else 'a'} is newer - drop_{older} if it supersedes"
    if jac >= 0.95:
        return "almost identical - drop either (drop_b keeps the first)"
    return "similar - review"


def dedup(cfg: dict[str, Any]) -> None:
    paths = paths_for(cfg)
    items = read_jsonl(paths.items)
    if not items:
        raise SystemExit("No items yet. Run `python -m gtbkb extract` first.")
    by_id = {it["id"]: it for it in items}

    # 1) exact duplicates: identical bytes, or identical normalised text
    uf = UnionFind()
    for key in ("sha256", "text_sha256"):
        first: dict[str, str] = {}
        for it in items:
            h = it.get(key)
            if not h or (key == "text_sha256" and it["words"] < 5):
                continue
            if h in first:
                uf.union(it["id"], first[h])
            else:
                first[h] = it["id"]
    groups: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        groups.setdefault(uf.find(it["id"]), []).append(it)

    exact_rows = []
    dropped: set[str] = set()
    for n, group in enumerate([g for g in groups.values() if len(g) > 1], 1):
        keep = _canonical(group)
        for it in group:
            reason = "same file" if it["sha256"] == keep["sha256"] else "same text"
            if it["id"] != keep["id"]:
                dropped.add(it["id"])
            exact_rows.append({
                "group": n, "action": "keep" if it["id"] == keep["id"] else f"drop ({reason} as {keep['id']})",
                "id": it["id"], "type": it["type"], "title": it["title"], "updated": it.get("updated", ""),
                "words": it["words"], "source_url": it["source_url"],
            })
    with paths.exact_dups.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["group", "action", "id", "type", "title", "updated", "words", "source_url"])
        w.writeheader()
        w.writerows(exact_rows)

    # 2) near duplicates among the survivors
    k, thr, cthr = cfg["shingle_size"], cfg["near_duplicate_threshold"], cfg["containment_threshold"]
    candidates = [it for it in items if it["id"] not in dropped and it["words"] >= cfg["min_words_for_near_dup"] and it.get("path")]
    shingles = {it["id"]: _shingles(Path(it["path"]).read_text(encoding="utf-8"), k) for it in candidates}

    previous: dict[tuple[str, str], str] = {}
    if paths.near_dups.exists():
        with paths.near_dups.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                previous[(row["a_id"], row["b_id"])] = (row.get("decision") or "").strip()

    near_rows = []
    for i, a in enumerate(candidates):
        sa = shingles[a["id"]]
        for b in candidates[i + 1:]:
            sb = shingles[b["id"]]
            if not sa or not sb:
                continue
            inter = len(sa & sb)
            if not inter:
                continue
            jac = inter / len(sa | sb)
            cont = inter / min(len(sa), len(sb))
            if jac < thr and cont < cthr:
                continue
            decision = previous.get((a["id"], b["id"])) or previous.get((b["id"], a["id"]), "")
            near_rows.append({
                "decision": decision, "suggestion": _suggest(a, b, jac, cont),
                "jaccard": f"{jac:.2f}", "containment": f"{cont:.2f}",
                "a_id": a["id"], "a_type": a["type"], "a_title": a["title"], "a_updated": a.get("updated", ""),
                "a_words": a["words"], "a_url": a["source_url"],
                "b_id": b["id"], "b_type": b["type"], "b_title": b["title"], "b_updated": b.get("updated", ""),
                "b_words": b["words"], "b_url": b["source_url"],
            })
    near_rows.sort(key=lambda r: -float(r["jaccard"]))
    with paths.near_dups.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(near_rows[0].keys()) if near_rows else
                           ["decision", "suggestion", "jaccard", "containment", "a_id", "a_type", "a_title", "a_updated",
                            "a_words", "a_url", "b_id", "b_type", "b_title", "b_updated", "b_words", "b_url"])
        w.writeheader()
        w.writerows(near_rows)

    undecided = sum(1 for r in near_rows if not r["decision"])
    print(f"Exact duplicates: {len(dropped)} items dropped automatically "
          f"({len([g for g in groups.values() if len(g) > 1])} groups) -> {paths.exact_dups}")
    print(f"Near duplicates: {len(near_rows)} pairs to review, {undecided} without a decision -> {paths.near_dups}")
    if undecided:
        print("  Fill the `decision` column with keep_both / drop_a / drop_b. Blank means keep both.")


def load_drops(cfg: dict[str, Any]) -> dict[str, str]:
    """Item id -> reason, combining automatic exact drops and the reviewer's near-duplicate decisions."""
    paths = paths_for(cfg)
    drops: dict[str, str] = {}
    if paths.exact_dups.exists():
        with paths.exact_dups.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["action"].startswith("drop"):
                    drops[row["id"]] = "exact duplicate: " + row["action"]
    if paths.near_dups.exists():
        with paths.near_dups.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = (row.get("decision") or "").strip().lower()
                if d not in DECISIONS:
                    print(f"Warning: unknown decision {d!r} for {row['a_id']}/{row['b_id']} - treated as keep_both")
                elif d == "drop_a":
                    drops[row["a_id"]] = f"near duplicate of {row['b_id']} (reviewed)"
                elif d == "drop_b":
                    drops[row["b_id"]] = f"near duplicate of {row['a_id']} (reviewed)"
    return drops
