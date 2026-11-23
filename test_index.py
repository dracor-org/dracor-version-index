#!/usr/bin/env python3
"""Acceptance tests for index/{corpus}/ against the git repository in repos/{corpus}/.

  test_index.py als                  # exhaustive
  test_index.py ger --sample 2000    # random (ID, commit) sample for large corpora

Optional KG fixtures in tests/fixtures/:
  kg_{corpus}_pins.json            {"commit": ..., "pins": {id: path}}
  kg_{corpus}_corpus_versions.json {"play_counts": {commit: n}}
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

from build_index import CatFile, extract_id, git
from lookup import VersionIndex

ROOT = Path(__file__).resolve().parent


class Result:
    def __init__(self):
        self.failures: list[str] = []
        self.lines: list[str] = []

    def check(self, name: str, ok: bool, detail: str = ""):
        self.lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            self.failures.append(name)


def ls_tree(repo: Path, commit: str) -> dict[str, str]:
    out = git(repo, "ls-tree", "-r", "-z", commit)
    res = {}
    for entry in out.split("\0"):
        if entry:
            meta, path = entry.split("\t", 1)
            res[path] = meta.split()[2]
    return res


def blob_info(cat: CatFile, cache: dict, blob: str) -> dict:
    if blob not in cache:
        cache[blob] = extract_id(cat.read(blob))
    return cache[blob]


def ids_in_blob(cat: CatFile, cache: dict, blob: str) -> set[str]:
    info = blob_info(cat, cache, blob)
    return {i for i in (info["idno"], info["xmlid"]) if i} if info["tei"] else set()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus")
    ap.add_argument("--sample", type=int, default=0, help="number of random (ID, commit) pairs; 0 = all")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    corpus, repo = args.corpus, ROOT / "repos" / args.corpus
    ix = VersionIndex(ROOT / "index" / corpus)
    cat, idcache = CatFile(repo), {}
    rng = random.Random(args.seed)
    R = Result()
    seqs = range(ix.last_seq + 1)
    sha = [c["commit"] for c in ix.commits]

    # 0. Completeness: the index reproduces every tree exactly (all files, not only TEI).
    bad = []
    check_seqs = seqs if not args.sample else sorted(rng.sample(list(seqs), min(len(seqs), 200)))
    for s in check_seqs:
        tree = ls_tree(repo, sha[s])
        idx = {iv["path"]: iv["blob"] for iv in ix.valid_at(s)}
        if tree != idx:
            bad.append((s, set(tree.items()) ^ set(idx.items())))
    R.check("index == git ls-tree for every checked commit", not bad,
            f"{len(check_seqs)} commits" + (f"; first mismatch seq {bad[0][0]}: {sorted(bad[0][1])[:4]}" if bad else ""))

    # 1. (ID, commit) -> path + blob; rev-parse gives same blob; blob contains the ID.
    tei_keys = sorted({iv["key"] for iv in ix.intervals if iv["kind"] == "tei"})
    pairs = [(k, s) for k in tei_keys for s in seqs if ix.lookup(k, s)]
    if args.sample and len(pairs) > args.sample:
        pairs = rng.sample(pairs, args.sample)
    errs, dup_pairs, methods = [], 0, {}
    for k, s in pairs:
        hits = ix.lookup(k, s)
        if len(hits) > 1:
            dup_pairs += 1
        for iv in hits:
            m = iv["id_method"]
            methods[m.split(":")[0]] = methods.get(m.split(":")[0], 0) + 1
            blob = git(repo, "rev-parse", f"{sha[s]}:{iv['path']}").strip()
            if blob != iv["blob"]:
                errs.append(f"{k}@{s}: index {iv['blob'][:10]} git {blob[:10]}")
                continue
            in_blob = ids_in_blob(cat, idcache, blob)
            info = blob_info(cat, idcache, blob)
            if m == "file" and k not in in_blob:
                errs.append(f"{k}@{s}: method file but ID not in blob {blob[:10]}")
            elif m.startswith("legacy:") and info.get("legacy_type") != m.split(":", 1)[1]:
                errs.append(f"{k}@{s}: method {m} but blob {blob[:10]} has no such idno")
            elif iv["id_in_file"] and iv["id_in_file"] not in in_blob:
                errs.append(f"{k}@{s}: id_in_file {iv['id_in_file']} not in blob {blob[:10]}")
    R.check("lookup(ID, commit) matches git rev-parse; the evidence for the ID is in the blob", not errs,
            f"{len(pairs)} pairs by method {methods}, {dup_pairs} with >1 hit"
            + (f"; {errs[:3]}" if errs else ""))

    # 2. Unchanged file across several commits -> same blob for all of them.
    long_ivs = [iv for iv in ix.intervals if iv["kind"] == "tei" and iv["to_seq"] > iv["from_seq"]]
    sample = long_ivs if not args.sample else rng.sample(long_ivs, min(300, len(long_ivs)))
    errs = []
    for iv in sample:
        for s in range(iv["from_seq"], iv["to_seq"] + 1) if not args.sample else (iv["from_seq"], iv["to_seq"]):
            b = git(repo, "rev-parse", f"{sha[s]}:{iv['path']}").strip()
            if b != iv["blob"]:
                errs.append(f"{iv['key']} seq {s}")
    R.check("unchanged file keeps the same blob over its interval", not errs,
            f"{len(sample)} multi-commit intervals" + (f"; {errs[:3]}" if errs else ""))

    # 3. Commits directly before and after a change differ in blob.
    errs, n = [], 0
    for k, ivs in ix.by_key.items():
        ivs = sorted(ivs, key=lambda i: i["from_seq"])
        for a, b in zip(ivs, ivs[1:]):
            if a["path"] == b["path"] and a["to_seq"] + 1 == b["from_seq"]:
                n += 1
                before = git(repo, "rev-parse", f"{sha[a['to_seq']]}:{a['path']}").strip()
                after = git(repo, "rev-parse", f"{sha[b['from_seq']]}:{b['path']}").strip()
                if before == after or before != a["blob"] or after != b["blob"]:
                    errs.append(f"{k} {a['to_seq']}->{b['from_seq']}")
    R.check("blob changes between the commits before and after each change", not errs and n > 0,
            f"{n} change points" + (f"; {errs[:3]}" if errs else ""))

    # 4. A commit before a play was added gives no hit. Since keys are resolved IDs, git may
    # hold a file with this ID string at that commit only if the index files it as another
    # play's earlier ID (id_in_file), e.g. the fre renumbering.
    errs, n = [], 0
    first = {}
    for iv in ix.intervals:
        if iv["kind"] == "tei":
            first[iv["key"]] = min(first.get(iv["key"], 10**9), iv["from_seq"])
    keys = list(first) if not args.sample else rng.sample(list(first), min(200, len(first)))
    for k in keys:
        s = first[k] - 1
        if s < 0:
            continue
        n += 1
        if ix.lookup(k, s):
            errs.append(f"{k}@{s}: index hit")
        tree = ls_tree(repo, sha[s])
        filed = {iv["path"] for iv in ix.lookup_in_file(k, s)}
        for p, blob in tree.items():
            if p.endswith(".xml") and k in ids_in_blob(cat, idcache, blob) and p not in filed:
                errs.append(f"{k}@{s}: git has {p} with this ID, index does not record it")
    R.check("commit before a play was added gives no hit", not errs,
            f"{n} plays checked" + (" (n/a: every play is in the first commit)" if n == 0 else "")
            + (f"; {errs[:3]}" if errs else ""))

    # 5. Every commit in `git rev-list --all` is in commits.csv (main line or mapped).
    allc = set(git(repo, "rev-list", "--all").split())
    missing = allc - set(ix.seq_of)
    side = [c for c in allc if c in ix.first_parent and not ix.first_parent[c]]
    unmerged = [c for c in side if not ix.merged_by.get(c)]
    bad_merge = [c for c in side if ix.merged_by.get(c) and not ix.first_parent.get(ix.merged_by[c])]
    R.check("every commit of rev-list --all is in commits.csv",
            not missing and not bad_merge,
            f"{len(allc)} commits: {len(ix.commits)} main line, {len(side)} side "
            f"({len(unmerged)} unmerged)" + (f"; missing {sorted(missing)[:3]}" if missing else "")
            + (f"; merged_by not on main line {bad_merge[:3]}" if bad_merge else ""))

    # 5b. Side commits: base tree + stored differences reproduce git ls-tree exactly.
    side_check = side if not args.sample else rng.sample(sorted(side), min(100, len(side)))
    bad, tei_unres = [], 0
    for c in side_check:
        tree = {iv["path"]: iv["blob"] for iv in ix.tree_at(c)}
        if tree != ls_tree(repo, c):
            bad.append(c[:7])
    for rows in ix.side_delta.values():
        tei_unres += sum(1 for r in rows if r["kind"] == "tei-unresolved")
    R.check("side commit trees (base_seq + differences) == git ls-tree", not bad,
            f"{len(side_check)} side commits, {sum(len(v) for v in ix.side_delta.values())} difference rows, "
            f"{tei_unres} TEI rows unresolved" + (f"; mismatch {bad[:3]}" if bad else ""))

    # 7. ID archaeology: every TEI version has an ID or is listed as unresolved.
    unres = [iv for iv in ix.intervals if iv["kind"] == "tei-unresolved"]
    import csv as _csv
    listed = {r["path"] for r in _csv.DictReader(open(ix.dir / "special_cases.csv"))
              if r["case"] == "id-unresolved"}
    missing = [iv["key"] for iv in unres if iv["key"] not in listed]
    meth = {}
    for iv in ix.intervals:
        if iv["kind"].startswith("tei"):
            m = (iv["id_method"] or "unresolved").split(":")[0]
            meth[m] = meth.get(m, 0) + 1
    R.check("every TEI version has an ID or is reported as id-unresolved", not missing,
            f"versions by method {meth}" + (f"; unreported {missing[:3]}" if missing else ""))

    # 6. KG cross-checks.
    pins_f = ROOT / "tests" / "fixtures" / f"kg_{corpus}_pins.json"
    if pins_f.exists():
        pins = json.loads(pins_f.read_text())
        tree = ix.tree_at(pins["commit"])
        errs = []
        for k, path in pins["pins"].items():  # the KG saw the ID in the file at that commit
            hits = [iv for iv in tree if iv.get("id_in_file") == k] if tree is not None else []
            if [h["path"] for h in hits] != [path]:
                errs.append(f"{k}: KG {path}, index {[h['path'] for h in hits]}")
        R.check(f"KG pins at {pins['commit'][:7]} match the index", tree is not None and not errs,
                f"{len(pins['pins'])} plays" + (f"; {errs[:3]}" if errs else ""))
    cv_f = ROOT / "tests" / "fixtures" / f"kg_{corpus}_corpus_versions.json"
    if cv_f.exists():
        counts = json.loads(cv_f.read_text())["play_counts"]
        errs, n_fp, n_side, not_in_kg = [], 0, 0, []
        for c, n in counts.items():
            if ix.first_parent.get(c):
                n_fp += 1
                got = sum(1 for iv in ix.valid_at(ix.seq_of[c]) if iv["kind"].startswith("tei"))
            elif c in ix.first_parent:
                n_side += 1
                got = sum(1 for iv in ix.tree_at(c) if iv["kind"].startswith("tei"))
            else:
                errs.append(f"{c[:7]} not in repo")
                continue
            if got != n:
                errs.append(f"{c[:7]}: KG {n}, index {got}")
        not_in_kg = [c["commit"][:7] for c in ix.commits if c["commit"] not in counts]
        R.check("KG corpus-versions play counts match TEI files valid per commit", not errs,
                f"{n_fp} main-line + {n_side} side commits compared; main-line commits not in KG: "
                f"{not_in_kg}" + (f"; {errs[:5]}" if errs else ""))
    sum_f = ROOT / "tests" / "fixtures" / "kg_pins_summary.json"
    if sum_f.exists() and corpus in json.loads(sum_f.read_text())["pins"]:
        summary = json.loads(sum_f.read_text())
        commit, n = summary["pins"][corpus]
        known = summary.get("known_differences", {}).get(corpus)  # explained, documented gaps
        tree = ix.tree_at(commit)
        if tree is None:
            R.check(f"KG pin commit {commit[:7]} is in the index", False)
        else:
            full = ix.full_commit(commit)
            where = f"seq {ix.seq_of[full]}" if ix.first_parent[full] else \
                f"side commit, base seq {ix.base_seq[full]}, merged by {ix.merged_by[full][:7] or '-'}"
            files = [iv for iv in tree if iv["kind"].startswith("tei")]
            expected = known["index_minus_kg"] if known else 0
            R.check(f"KG pin {commit[:7]} ({where}): TEI files in index == KG play files"
                    + (" + known difference" if known else ""),
                    len(files) - n == expected,
                    f"index {len(files)}, KG {n}" + (f"; known: +{expected}, {known['reason']}" if known else ""))
    cat.close()
    print(f"== {corpus}")
    print("\n".join(R.lines))
    sys.exit(1 if R.failures else 0)


if __name__ == "__main__":
    main()
