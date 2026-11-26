#!/usr/bin/env python3
"""Build the DraCor versions index for one or more corpus repositories.

For every corpus this writes to index/{corpus}/:

  commits.csv        seq, commit, date, first_parent, merged_by, base_seq
                     Every commit of `git rev-list --all` is a corpus version. Main-line
                     (first-parent) commits have first_parent=true and a unique seq from 0.
                     Side-branch commits have an empty seq, first_parent=false, merged_by =
                     the main-line commit that merged them (empty if never merged) and
                     base_seq = their newest main-line ancestor.
  side_commits.csv   commit, base_seq, change, path, blob, key, kind, id_in_file, id_method
                     each side-branch commit as its difference (A/M/D) from the tree at
                     base_seq; see lookup.VersionIndex.tree_at.
  components.csv     key, path, blob, from_seq, to_seq, kind, id_in_file, id_method
                     one validity interval per file content; empty to_seq = still valid.
                     kind: tei (key = the play's current DraCor ID, resolved by the ID
                     archaeology in archaeology.py), tei-unresolved (no ID could be
                     established; key = latest path), other (key = path).
                     id_in_file: the ID value the file itself carries (may be an older ID,
                     a placeholder, or empty); id_method: file | legacy:<idno type> |
                     lineage | title | title-fuzzy.
  ids.csv            key, path, from_seq, to_seq, id_in_file, id_method
                     filename/ID table: consecutive versions of one play with the same path,
                     ID in the file and method merged into one row.
  blobs.csv          per XML blob read: TEI?, xml:id, idno, older ID encoding, title, authors
                     (cache, so later runs need no old blobs)
  special_cases.csv  case, seq, commit, key, path, blob, detail
  meta.json          repository, branch, head, API-loaded commit, counts, incremental state

Blob hashes come from git only (diff-tree / ls-tree / cat-file).

Usage:
  build_index.py als                 # one corpus, URL from the DraCor API or the registry
  build_index.py --all               # every corpus of the registry and the DraCor API
  build_index.py als ger --full      # rebuild from scratch instead of continuing
  build_index.py --all --cleanup     # delete each clone afterwards; later runs clone blobless
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import json
import re
import shutil
import subprocess
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

from archaeology import ARCH_CASES, HISTORIC_PREFIXES, LEGACY_TYPES, resolve

ROOT = Path(__file__).resolve().parent
REPOS = ROOT / "repos"
INDEX = ROOT / "index"
API = "https://dracor.org/api/v1/corpora"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
GITLINK_MODE = "160000"


# --------------------------------------------------------------------------- git helpers

def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.decode(errors='replace')}")
    return r.stdout.decode("utf-8", errors="surrogateescape")


class CatFile:
    """Persistent `git cat-file --batch` process."""

    def __init__(self, repo: Path):
        self.p = subprocess.Popen(["git", "-C", str(repo), "cat-file", "--batch"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def read(self, sha: str) -> bytes:
        self.p.stdin.write(sha.encode() + b"\n")
        self.p.stdin.flush()
        header = self.p.stdout.readline().split()
        if len(header) < 3 or header[1] == b"missing":
            raise KeyError(sha)
        size = int(header[2])
        data = self.p.stdout.read(size)
        self.p.stdout.read(1)  # trailing LF
        return data

    def close(self):
        self.p.stdin.close()
        self.p.wait()


def diff_tree(repo: Path, prev: str | None, cur: str):
    """Yield (status, old_mode, new_mode, old_blob, new_blob, path) between two trees."""
    args = ["diff-tree", "-r", "-z", "--no-renames", "--no-commit-id"]
    args += ["--root", cur] if prev is None else [prev, cur]
    out = git(repo, *args)
    tok = out.split("\0")
    i = 0
    while i < len(tok) - 1:
        meta = tok[i]
        if not meta.startswith(":"):
            i += 1
            continue
        om, nm, ob, nb, st = meta[1:].split(" ")
        yield st[0], om, nm, ob, nb, tok[i + 1]
        i += 2


def git_renames(repo: Path, prev: str, cur: str):
    """Yield (old_path, new_path, similarity) from git's rename detection between two trees."""
    out = git(repo, "diff-tree", "-r", "-z", "-M", "--diff-filter=R", "--no-commit-id", prev, cur)
    tok = out.split("\0")
    i = 0
    while i < len(tok) - 2:
        meta = tok[i]
        if meta.startswith(":") and meta.split(" ")[-1].startswith("R"):
            yield tok[i + 1], tok[i + 2], int(meta.split(" ")[-1][1:] or 0)
            i += 3
        else:
            i += 1


# --------------------------------------------------------------------------- ID extraction

def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


ID_IDNO_RE = re.compile(rb'<idno[^>]*type="dracor"[^>]*>\s*([^<\s]+)\s*</idno>')
ID_XMLID_RE = re.compile(rb'<TEI\b[^>]*\sxml:id="([^"]+)"')
# What the raw pass accepts as an ID in the file. The archaeology pass is stricter (head prefix).
RAW_ID_RE = re.compile(r"[a-z]+\d+")
LEGACY_RE = re.compile(rb'<idno\b[^>]*\btype="(' + b"|".join(t.encode() for t in LEGACY_TYPES)
                       + rb')"[^>]*>\s*([^<]*?)\s*</idno>')
TITLESTMT_RE = re.compile(rb"<titleStmt\b.*?</titleStmt>", re.S)
TITLE_RE = re.compile(rb"<title\b([^>]*)>(.*?)</title>", re.S)
AUTHOR_RE = re.compile(rb"<author\b[^>]*>(.*?)</author>", re.S)
SURNAME_RE = re.compile(rb"<surname\b[^>]*>(.*?)</surname>", re.S)
TAG_RE = re.compile(rb"<[^>]+>")


def norm_text(b: bytes) -> str:
    """Loose normalisation for title matching: no tags, no diacritics, lowercase words."""
    s = TAG_RE.sub(b" ", b).decode("utf-8", "replace")
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return " ".join(re.findall(r"\w+", s))


def header_meta(data: bytes) -> dict:
    """Older ID encoding, main title and author surnames from the teiHeader (regex, no parse)."""
    head = data.split(b"</teiHeader>", 1)[0][:300000]
    res = {"legacy_type": "", "legacy_value": "", "title": "", "authors": ""}
    m = LEGACY_RE.search(head)
    if m and m.group(2).strip():
        res["legacy_type"], res["legacy_value"] = m.group(1).decode(), m.group(2).decode("utf-8", "replace").strip()
    ts = TITLESTMT_RE.search(head)
    if ts:
        titles = TITLE_RE.findall(ts.group(0))
        main = ([t for a, t in titles if b"main" in a] or [t for a, t in titles if b"sub" not in a]
                or [t for _, t in titles])
        res["title"] = norm_text(main[0]) if main else ""
        names = []
        for a in AUTHOR_RE.findall(ts.group(0)):
            sn = SURNAME_RE.findall(a)
            if sn:
                names.append(norm_text(sn[0]))
            else:
                words = norm_text(a).split()
                if words:  # "Surname, Forename" or "Forename Surname"
                    names.append(words[0] if b"," in TAG_RE.sub(b"", a) else words[-1])
        res["authors"] = "|".join(sorted({n for n in names if n}))
    return res


def extract_id(data: bytes) -> dict:
    """Return {'tei', 'idno', 'xmlid', 'malformed'} plus header_meta() fields.

    Files with a TEI P4 root (<TEI.2>, imported sources before conversion) count as TEI
    files; they never carry a DraCor ID.
    DraCor IDs appear in two encodings over time: <idno type="dracor"> in the
    publicationStmt (older) and TEI/@xml:id (schema since 2022). Both are read.
    Parsing stops at the end of the teiHeader.
    """
    res = _extract_id(data)
    if res["tei"]:
        res.update(header_meta(data))
    return res


def _extract_id(data: bytes) -> dict:
    res = {"tei": False, "idno": None, "xmlid": None, "malformed": False}
    root_seen = False
    try:
        for ev, el in ET.iterparse(io.BytesIO(data), events=("start", "end")):
            if ev == "start" and not root_seen:
                root_seen = True
                if local(el.tag) not in ("TEI", "TEI.2"):  # TEI.2 = TEI P4 source files
                    return res
                res["tei"] = True
                res["xmlid"] = el.get(XML_ID)
            elif ev == "end":
                name = local(el.tag)
                if name == "idno" and el.get("type") == "dracor" and (el.text or "").strip():
                    res["idno"] = el.text.strip()
                elif name == "teiHeader":
                    break
    except ET.ParseError:
        # Fall back to regex so a broken file in history does not stop the index.
        head = data[:20000]
        if re.search(rb"<TEI(\.2)?[\s>]", head):
            res["tei"] = True
            res["malformed"] = True
            m = ID_XMLID_RE.search(head)
            res["xmlid"] = m.group(1).decode() if m else None
            m = ID_IDNO_RE.search(data[:200000])
            res["idno"] = m.group(1).decode() if m else None
    return res


# --------------------------------------------------------------------------- repository

def parse_repo_url(url: str) -> tuple[str, str | None]:
    if "#" in url:
        base, branch = url.split("#", 1)
        return base, branch or None
    return url, None


# Git without stored credentials or prompts: the index is public, so it only covers repositories
# anyone can read. A private repository (e.g. a corpus in preparation) must fail here, also on a
# machine where git could log in.
ANON_GIT = ["git", "-c", "credential.helper=", "-c", "core.askPass="]
ANON_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "", "SSH_ASKPASS": ""}


def is_public(url: str) -> bool:
    r = subprocess.run(ANON_GIT + ["ls-remote", "--heads", url], capture_output=True,
                       env=ANON_ENV, timeout=60, check=False)
    return r.returncode == 0


def ensure_repo(name: str, url: str, blobless: bool) -> Path:
    """Clone (full, or blobless for incremental runs) or fetch the corpus repository.

    A blobless clone (--filter=blob:none) has every commit and tree but no file contents;
    the indexer fetches the blobs it must read in one batch (see Indexer.prefetch).
    """
    repo = REPOS / name
    if not repo.exists():
        REPOS.mkdir(exist_ok=True)
        cmd = ANON_GIT + ["clone", "--quiet", "--no-checkout"]
        if blobless:
            cmd.append("--filter=blob:none")
        subprocess.run(cmd + [url, str(repo)], check=True, env=ANON_ENV)
    else:
        subprocess.run(ANON_GIT + ["-C", str(repo), "fetch", "--quiet", "--prune", "--tags", "origin"],
                       check=True, env=ANON_ENV)
    return repo


def resolve_branch(repo: Path, fragment: str | None, api_commit: str | None) -> tuple[str, str, str | None]:
    """Pick the branch to index: the one the API loads.

    Normally that is the URL fragment, or the default branch without one. If the commit the
    API reports is not on that branch's first-parent line but is on another branch's, index
    that branch instead and return a warning (greek: URL says #reboot, API loads main).
    """
    default = git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").strip().split("/", 1)[1]
    branch = fragment or default
    warning = None
    if api_commit:
        def on_fp(b):
            fp = git(repo, "rev-list", "--first-parent", f"refs/remotes/origin/{b}", check=False)
            return api_commit in fp.split()
        if not on_fp(branch):
            others = [default] + [r.strip().split("/", 1)[1] for r in
                                  git(repo, "branch", "-r", "--format=%(refname:short)").splitlines()
                                  if r.strip() not in ("origin/HEAD", "origin", f"origin/{default}")]
            hit = next((b for b in others if b != branch and on_fp(b)), None)
            if hit:
                warning = (f"API commit {api_commit[:7]} is not on the first-parent line of "
                           f"'{branch}' but of '{hit}'; indexing '{hit}'")
                branch = hit
            else:
                warning = f"API commit {api_commit[:7]} is on no branch's first-parent line"
    head = git(repo, "rev-parse", f"refs/remotes/origin/{branch}").strip()
    return branch, head, warning


# --------------------------------------------------------------------------- indexer

class Indexer:
    def __init__(self, corpus: str, repo: Path, branch: str, head: str, out: Path):
        self.corpus, self.repo, self.branch, self.head, self.out = corpus, repo, branch, head, out
        self.cat = CatFile(repo)
        self.blob_info: dict[str, dict] = {}
        self.fetched_blobs = 0
        self.commits: list[tuple[int, str, str]] = []  # seq, sha, date
        self.intervals: list[dict] = []                 # all intervals, in creation order
        self.open: dict[str, dict] = {}                 # path -> open interval
        self.cases: list[dict] = []
        self.dups_open: dict[str, frozenset] = {}       # id -> paths currently duplicated
        self.last_seen_id: dict[str, tuple[int, str]] = {}  # id -> (last seq valid, path)

    # ---- helpers
    def case(self, kind, seq, key="", path="", blob="", detail=""):
        self.cases.append({"case": kind, "seq": seq, "commit": self.commits[seq][1] if seq is not None and seq >= 0 else "",
                           "key": key, "path": path, "blob": blob, "detail": detail})

    def classify(self, path: str, mode: str, blob: str) -> dict:
        """Return {'key', 'kind', 'id_source', 'ids'} for a file version."""
        if mode == GITLINK_MODE:
            return {"key": path, "kind": "other", "id_source": "gitlink"}
        if not path.lower().endswith(".xml"):
            return {"key": path, "kind": "other", "id_source": ""}
        info = self.blob_info.get(blob)
        if info is None:
            info = extract_id(self.cat.read(blob))
            self.blob_info[blob] = info
        if not info["tei"]:
            return {"key": path, "kind": "other", "id_source": ""}
        # Only ID-like values count in the raw pass; placeholders ("tochange", "?") and
        # foreign IDs (EEBO "A00456", Wikidata "Q…") are judged again by the archaeology pass.
        idno = info["idno"] if info["idno"] and RAW_ID_RE.fullmatch(info["idno"]) else None
        xmlid = info["xmlid"] if info["xmlid"] and RAW_ID_RE.fullmatch(info["xmlid"]) else None
        res = {"malformed": info["malformed"], "idno": idno, "xmlid": xmlid}
        if xmlid and idno and xmlid != idno:
            res.update(key=xmlid, kind="tei", id_source="conflict")
        elif xmlid:
            res.update(key=xmlid, kind="tei", id_source="xml:id")
        elif idno:
            res.update(key=idno, kind="tei", id_source="idno")
        else:
            res.update(key=path, kind="tei-noid", id_source="")
        return res

    # ---- main loop
    def process(self, seq: int, prev: str | None, cur: str):
        removed, added = [], []
        for st, om, nm, ob, nb, path in diff_tree(self.repo, prev, cur):
            if st == "M" and ob == nb:
                # only the file mode changed (e.g. chmod); same content, same interval
                self.case("mode-change", seq, path=path, detail=f"{om}->{nm}")
                continue
            if st in ("D", "M", "T"):
                iv = self.open.pop(path, None)
                if iv is None:
                    raise RuntimeError(f"{self.corpus}: no open interval for {path} at seq {seq}")
                iv["to_seq"] = seq - 1
                removed.append(iv)
            if st in ("A", "M", "T"):
                c = self.classify(path, nm, nb)
                iv = {"key": c["key"], "path": path, "blob": nb, "from_seq": seq, "to_seq": None,
                      "kind": c["kind"]}
                self.intervals.append(iv)
                self.open[path] = iv
                added.append((iv, c))
            elif st not in ("D",):
                self.case("unhandled-diff-status", seq, path=path, detail=st)

        self.report_changes(seq, removed, added, prev, cur)
        self.check_duplicates(seq)
        for iv in self.open.values():
            if iv["kind"] == "tei":
                self.last_seen_id[iv["key"]] = (seq, iv["path"])

    def report_changes(self, seq, removed, added, prev=None, cur=None):
        rem_by_path = {iv["path"]: iv for iv in removed}
        rem_tei = [iv for iv in removed if iv["kind"].startswith("tei")]
        add_paths = {iv["path"] for iv, _ in added}
        unmatched_new, matched_old = [], set()
        for iv, c in added:
            if c.get("malformed"):
                self.case("malformed-xml", seq, iv["key"], iv["path"], iv["blob"],
                          "TEI file not well-formed; ID taken by regex")
            if c.get("id_source") == "conflict":
                self.case("id-encoding-conflict", seq, iv["key"], iv["path"], iv["blob"],
                          f"xml:id={c['xmlid']} idno={c['idno']}; key=xml:id")
            if iv["kind"] == "tei-noid":
                self.case("tei-without-id", seq, iv["key"], iv["path"], iv["blob"])
            if c.get("id_source") == "gitlink":
                self.case("gitlink", seq, iv["key"], iv["path"], iv["blob"], "submodule entry")
            old = rem_by_path.get(iv["path"])
            if old is not None and old["kind"].startswith("tei") or (old is not None and iv["kind"].startswith("tei")):
                # same path, new content
                if old["kind"] == "tei" and iv["kind"] == "tei" and old["key"] != iv["key"]:
                    self.case("id-change", seq, iv["key"], iv["path"], iv["blob"],
                              f"{old['key']} -> {iv['key']} at same path")
                elif old["kind"] == "tei-noid" and iv["kind"] == "tei":
                    self.case("id-assigned", seq, iv["key"], iv["path"], iv["blob"],
                              "file had no DraCor ID before")
                elif old["kind"] == "tei" and iv["kind"] == "tei-noid":
                    self.case("id-lost", seq, old["key"], iv["path"], iv["blob"],
                              "file had DraCor ID before")
                elif old["kind"] != iv["kind"]:
                    self.case("kind-change", seq, iv["key"], iv["path"], iv["blob"],
                              f"{old['kind']} -> {iv['kind']}")
            if old is None and iv["kind"].startswith("tei"):
                # new path: rename (same blob or same ID), or re-appearance of an ID
                gone = [r for r in rem_tei if r["path"] not in add_paths]
                same_blob = [r for r in gone if r["blob"] == iv["blob"]]
                same_key = [r for r in gone if iv["kind"] == "tei" and r["key"] == iv["key"]]
                if same_blob:
                    matched_old.add(same_blob[0]["path"])
                    self.case("rename-same-blob", seq, iv["key"], iv["path"], iv["blob"],
                              f"from {same_blob[0]['path']}")
                elif same_key:
                    matched_old.add(same_key[0]["path"])
                    self.case("rename-with-change", seq, iv["key"], iv["path"], iv["blob"],
                              f"from {same_key[0]['path']} (blob {same_key[0]['blob'][:10]})")
                else:
                    unmatched_new.append(iv)
                    if iv["kind"] == "tei" and iv["key"] in self.last_seen_id:
                        last, lpath = self.last_seen_id[iv["key"]]
                        if last < seq - 1 and not any(o["key"] == iv["key"] and o is not iv
                                                      for o in self.open.values()):
                            self.case("id-reappears", seq, iv["key"], iv["path"], iv["blob"],
                                      f"last valid at seq {last} as {lpath}")
        # Renames with content change and without a common ID: ask git's rename detection.
        gone = {r["path"]: r for r in rem_tei if r["path"] not in add_paths and r["path"] not in matched_old}
        if unmatched_new and gone and prev is not None:
            new_by_path = {iv["path"]: iv for iv in unmatched_new}
            for old, new, score in git_renames(self.repo, prev, cur):
                if old in gone and new in new_by_path:
                    iv = new_by_path.pop(new)
                    o = gone.pop(old)
                    self.case("rename-similar", seq, iv["key"], new, iv["blob"],
                              f"from {old} (blob {o['blob'][:10]}, git similarity {score}%)")

    def check_duplicates(self, seq):
        by_id = defaultdict(set)
        for iv in self.open.values():
            if iv["kind"] == "tei":
                by_id[iv["key"]].add(iv["path"])
        now = {k: frozenset(v) for k, v in by_id.items() if len(v) > 1}
        for k, paths in now.items():
            if self.dups_open.get(k) != paths:
                self.case("duplicate-id", seq, k, " | ".join(sorted(paths)), "",
                          f"{len(paths)} files valid at the same time")
        for k in self.dups_open.keys() - now.keys():
            self.case("duplicate-id-resolved", seq, k, "", "", "")
        self.dups_open = now

    # ---- persistence
    def load_previous(self) -> int:
        """Load an earlier run; return the next seq to process (0 = full run)."""
        meta_f = self.out / "meta.json"
        if not meta_f.exists():
            return 0
        meta = json.loads(meta_f.read_text())
        st = meta.get("state", {})
        with open(self.out / "commits.csv", newline="") as f:
            self.commits = [(int(r["seq"]), r["commit"], r["date"]) for r in csv.DictReader(f)
                            if r["first_parent"] == "true"]
        blobs_f = self.out / "blobs.csv"
        if not blobs_f.exists() or "id_in_file" not in open(self.out / "components.csv").readline():
            print(f"  {self.corpus}: index from an older script version; rebuilding", file=sys.stderr)
            self.__init__(self.corpus, self.repo, self.branch, self.head, self.out)
            return 0
        with open(blobs_f, newline="") as f:
            for r in csv.DictReader(f):
                self.blob_info[r["blob"]] = {
                    "tei": r["tei"] == "true", "malformed": r["malformed"] == "true",
                    "xmlid": r["xmlid"] or None, "idno": r["idno"] or None,
                    **{k: r[k] for k in ("legacy_type", "legacy_value", "title", "authors")}}
        with open(self.out / "components.csv", newline="") as f:
            for r in csv.DictReader(f):
                iv = {"key": r["key"], "path": r["path"], "blob": r["blob"],
                      "from_seq": int(r["from_seq"]),
                      "to_seq": int(r["to_seq"]) if r["to_seq"] else None, "kind": r["kind"]}
                if iv["kind"] in ("tei", "tei-unresolved"):  # back to what the file itself says
                    c = self.classify(iv["path"], "100644", iv["blob"])
                    iv["key"], iv["kind"] = c["key"], c["kind"]
                self.intervals.append(iv)
                if iv["to_seq"] is None:
                    self.open[iv["path"]] = iv
        with open(self.out / "special_cases.csv", newline="") as f:
            self.cases = [dict(r, seq=int(r["seq"]) if r["seq"] else None) for r in csv.DictReader(f)
                          if r["case"] not in ARCH_CASES]  # recomputed by every run
        self.dups_open = {k: frozenset(v) for k, v in st.get("dups_open", {}).items()}
        self.last_seen_id = {k: tuple(v) for k, v in st.get("last_seen_id", {}).items()}
        return len(self.commits)

    def run(self, full: bool):
        fp = git(self.repo, "rev-list", "--first-parent", "--reverse", self.head).split()
        start = 0 if full else self.load_previous()
        if start:
            known = [c[1] for c in self.commits]
            if fp[:len(known)] != known:
                print(f"  {self.corpus}: first-parent history rewritten since last run; rebuilding",
                      file=sys.stderr)
                self.__init__(self.corpus, self.repo, self.branch, self.head, self.out)
                start = 0
        if start and start == len(fp) and self.up_to_date():
            print(f"  {self.corpus}: up to date", file=sys.stderr)
            self.cat.close()
            return False
        dates = dict(line.split(" ", 1) for line in
                     git(self.repo, "log", "--first-parent", "--format=%H %cI", self.head).splitlines())
        self.prefetch(fp, start)
        for seq in range(start, len(fp)):
            sha = fp[seq]
            self.commits.append((seq, sha, dates[sha]))
            self.process(seq, fp[seq - 1] if seq else None, sha)
        self.write(start, len(fp) - start)
        self.cat.close()
        return True

    def up_to_date(self) -> bool:
        """No new main-line commit, no new side commit, same head, API commit and branch.

        Then nothing is rewritten, so an unchanged corpus produces no diff in index/."""
        meta = json.loads((self.out / "meta.json").read_text())
        if (meta.get("head"), meta.get("api_commit"), meta.get("branch"), meta.get("repository")) != \
                (self.head, getattr(self, "api_commit", None), self.branch, getattr(self, "repo_url", None)):
            return False
        with open(self.out / "commits.csv", newline="") as f:
            known = {r["commit"] for r in csv.DictReader(f)}
        return known == set(git(self.repo, "rev-list", "--all").split())

    def prefetch(self, fp: list[str], start: int):
        """In a blobless clone, fetch the XML blobs this run will read, in one batch.

        Lazy fetching would cost one round trip per blob. Missing objects are found with
        `rev-list --missing=print`, which (unlike cat-file) does not trigger a lazy fetch.
        In a full clone nothing is missing and this does nothing.
        """
        if git(self.repo, "config", "--get", "remote.origin.promisor", check=False).strip() != "true":
            return
        rng = [self.head] + ([f"^{fp[start - 1]}"] if start else [])
        missing = {l[1:] for l in git(self.repo, "rev-list", "--objects", "--no-object-names",
                                      "--missing=print", *rng).splitlines() if l.startswith("?")}
        wanted = set()
        for seq in range(start, len(fp)):
            for st, om, nm, ob, nb, path in diff_tree(self.repo, fp[seq - 1] if seq else None, fp[seq]):
                if st in ("A", "M", "T") and nm != GITLINK_MODE and path.lower().endswith(".xml"):
                    wanted.add(nb)
        need = sorted(wanted & missing)
        for i in range(0, len(need), 1000):
            subprocess.run(["git", "-C", str(self.repo), "-c", "fetch.negotiationAlgorithm=noop",
                            "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                            "--filter=blob:none", "origin", *need[i:i + 1000]], check=True)
        self.fetched_blobs = len(need)

    def side_commits(self) -> list[dict]:
        """Map every commit in `rev-list --all` that is not on the first-parent line."""
        seq_of = {c[1]: c[0] for c in self.commits}
        allc = git(self.repo, "rev-list", "--all", "--format=%H %cI").splitlines()
        all_dates = dict(l.split(" ", 1) for l in allc if not l.startswith("commit "))
        mapped = {}
        for seq, sha, _ in self.commits:
            parents = git(self.repo, "rev-list", "--parents", "-n1", sha).split()[1:]
            if len(parents) < 2:
                continue
            for extra in parents[1:]:
                for c in git(self.repo, "rev-list", extra, "--not", parents[0]).split():
                    if c not in seq_of and c not in mapped:
                        mapped[c] = (seq, sha)
        # base_seq: the newest main-line commit that is an ancestor of the side commit.
        # The side commit's tree is stored as its difference from that commit's tree.
        base = {}
        for line in git(self.repo, "rev-list", "--all", "--topo-order", "--reverse", "--parents").splitlines():
            c, *parents = line.split()
            if c in seq_of:
                continue
            cand = [seq_of[p] if p in seq_of else base.get(p) for p in parents]
            cand = [x for x in cand if x is not None]
            base[c] = max(cand) if cand else None
        rows = []
        for c, d in all_dates.items():
            if c in seq_of:
                continue
            m = mapped.get(c)
            rows.append({"seq": "", "commit": c, "date": d, "first_parent": "false",
                         "merged_by": m[1] if m else "", "_merge_seq": m[0] if m else None,
                         "base_seq": base.get(c)})
        rows.sort(key=lambda r: (r["_merge_seq"] is None, r["_merge_seq"] or 0, r["date"], r["commit"]))
        return rows

    def side_trees(self, side: list[dict], rows: list[dict], arch_stats: dict):
        """Each side commit as its difference from its base main-line commit (base_seq).

        One row per path that differs: change A (added), M (modified/type change) or
        D (deleted, blob empty). The tree of a side commit is the tree of base_seq with these
        rows applied. TEI files get a key like the main line: from a main-line version with
        the same blob, else the ID in the file, an older ID, the same path in the base tree,
        or the title (id_method says which); otherwise kind tei-unresolved, key = path.
        """
        seq_sha = {c[0]: c[1] for c in self.commits}
        last = len(self.commits) - 1
        # what the main line knows
        by_path_blob, by_blob, by_infile, by_legacy, by_title = {}, {}, defaultdict(set), defaultdict(set), defaultdict(set)
        for r in rows:
            if not r["kind"].startswith("tei"):
                continue
            by_path_blob[(r["path"], r["blob"])] = r
            by_blob.setdefault(r["blob"], r)
            if r["kind"] != "tei":
                continue
            if r["id_in_file"]:
                by_infile[r["id_in_file"]].add(r["key"])
            m = self.blob_info.get(r["blob"], {})
            if m.get("legacy_value"):
                by_legacy[(m["legacy_type"], m["legacy_value"])].add(r["key"])
            if m.get("title"):
                by_title[(m["title"], m.get("authors", ""))].add(r["key"])
        prefixes = [p for p in [arch_stats.get("prefix")] if p] + list(HISTORIC_PREFIXES.get(self.corpus, ()))
        valid_re = re.compile("(" + "|".join(map(re.escape, prefixes)) + r")\d+") if prefixes else None

        def base_tree(seq):
            if seq is None:
                return {}
            return {r["path"]: r for r in rows
                    if r["from_seq"] <= seq <= (r["to_seq"] if r["to_seq"] is not None else last)}

        deltas = []
        for sc in side:
            b = sc["base_seq"]
            prev = seq_sha[b] if b is not None else None
            deltas.append((sc, list(diff_tree(self.repo, prev, sc["commit"]))))
        # blobless clone: fetch the side-commit XML blobs not read yet, in one batch
        need = sorted({nb for _, d in deltas for st, om, nm, ob, nb, path in d
                       if st in ("A", "M", "T") and nm != GITLINK_MODE and path.lower().endswith(".xml")
                       and nb not in self.blob_info})
        if need and git(self.repo, "config", "--get", "remote.origin.promisor", check=False).strip() == "true":
            have = subprocess.run(["git", "-C", str(self.repo), "cat-file", "--batch-check=%(objectname)",
                                   "--batch-all-objects"], capture_output=True, text=True).stdout.split()
            miss = sorted(set(need) - set(have))
            for i in range(0, len(miss), 1000):
                subprocess.run(["git", "-C", str(self.repo), "-c", "fetch.negotiationAlgorithm=noop",
                                "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
                                "--filter=blob:none", "origin", *miss[i:i + 1000]], check=True)
            self.fetched_blobs += len(miss)

        out, methods = [], defaultdict(int)
        cache_base = {}
        for sc, delta in deltas:
            b = sc["base_seq"]
            if b not in cache_base:
                cache_base = {b: base_tree(b)}
            bt = cache_base[b]
            for st, om, nm, ob, nb, path in delta:
                r = {"commit": sc["commit"], "base_seq": "" if b is None else b, "path": path}
                if st == "D":
                    old = bt.get(path)
                    r.update(change="D", blob="", key=old["key"] if old else path,
                             kind=old["kind"] if old else "other", id_in_file="", id_method="")
                    out.append(r)
                    continue
                r.update(change="A" if st == "A" else "M", blob=nb)
                c = self.classify(path, nm, nb)
                if c["kind"] == "other":
                    r.update(key=path, kind="other", id_in_file="", id_method="")
                    out.append(r)
                    continue
                m = self.blob_info.get(nb, {})
                raw = [v for v in (m.get("xmlid"), m.get("idno")) if v]
                valid = [v for v in raw if valid_re and valid_re.fullmatch(v)]
                in_file = valid[0] if valid else (raw[0] if raw else "")
                key = how = None
                hit = by_path_blob.get((path, nb)) or by_blob.get(nb)
                if hit and hit["kind"] == "tei":
                    key = hit["key"]
                    how = hit["id_method"] if hit["path"] == path else "lineage"
                elif valid:
                    ks = by_infile.get(valid[0], set())
                    key = next(iter(ks)) if len(ks) == 1 else valid[0]
                    how = "file" if key == valid[0] else "lineage"
                if key is None and m.get("legacy_value"):
                    ks = by_legacy.get((m["legacy_type"], m["legacy_value"]), set())
                    if len(ks) == 1:
                        key, how = next(iter(ks)), f"legacy:{m['legacy_type']}"
                if key is None and path in bt and bt[path]["kind"] == "tei":
                    key, how = bt[path]["key"], "lineage"
                if key is None and m.get("title"):
                    ks = by_title.get((m["title"], m.get("authors", "")), set())
                    if len(ks) == 1:
                        key, how = next(iter(ks)), "title"
                if key is None:
                    r.update(key=path, kind="tei-unresolved", id_in_file=in_file, id_method="")
                else:
                    r.update(key=key, kind="tei", id_in_file=in_file, id_method=how)
                methods[r["id_method"] or "unresolved"] += 1
                out.append(r)
        stats = {"side_commits": len(side), "with_base": sum(1 for s in side if s["base_seq"] is not None),
                 "delta_rows": len(out), "tei_delta_rows_by_method": dict(methods)}
        return out, stats

    def write(self, start: int, n_new: int):
        self.out.mkdir(parents=True, exist_ok=True)
        side = self.side_commits()
        with open(self.out / "commits.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["seq", "commit", "date", "first_parent", "merged_by", "base_seq"])
            for seq, sha, d in self.commits:
                w.writerow([seq, sha, d, "true", "", ""])
            for r in side:
                w.writerow([r["seq"], r["commit"], r["date"], r["first_parent"], r["merged_by"],
                            "" if r["base_seq"] is None else r["base_seq"]])
        last = len(self.commits) - 1
        # ID archaeology: resolved key per TEI interval (see archaeology.py)
        resolved, arch_cases, arch_stats = resolve(self.corpus, self.intervals, self.blob_info, self.cases, last)
        for c in arch_cases:
            c["commit"] = self.commits[c["seq"]][1] if c["seq"] is not None else ""
        rows = []
        for iv in self.intervals:
            r = resolved.get(id(iv))
            if r:
                rows.append({**iv, "key": r["key"], "kind": r["kind"], "id_in_file": r["id_in_file"],
                             "id_method": r["id_method"], "text": r["text"]})
            else:
                rows.append({**iv, "id_in_file": "", "id_method": "", "text": None})
        rows.sort(key=lambda i: (not i["kind"].startswith("tei"), i["kind"], i["key"], i["from_seq"], i["path"]))
        with open(self.out / "components.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["key", "path", "blob", "from_seq", "to_seq", "kind", "id_in_file", "id_method"])
            for iv in rows:
                w.writerow([iv["key"], iv["path"], iv["blob"], iv["from_seq"],
                            "" if iv["to_seq"] is None else iv["to_seq"], iv["kind"],
                            iv["id_in_file"], iv["id_method"]])
        # ids.csv: filename/ID table, consecutive intervals of one text with the same path,
        # ID in the file and method merged into one row
        with open(self.out / "ids.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["key", "path", "from_seq", "to_seq", "id_in_file", "id_method"])
            by_text = defaultdict(list)
            for iv in rows:
                if iv["text"] is not None:
                    by_text[iv["text"]].append(iv)
            out = []
            for ivs in by_text.values():
                ivs.sort(key=lambda i: i["from_seq"])
                cur = None
                for iv in ivs:
                    sig = (iv["key"], iv["path"], iv["id_in_file"], iv["id_method"])
                    if cur and cur["sig"] == sig and cur["to"] is not None and cur["to"] + 1 == iv["from_seq"]:
                        cur["to"] = iv["to_seq"]
                    else:
                        cur = {"sig": sig, "from": iv["from_seq"], "to": iv["to_seq"]}
                        out.append(cur)
            out.sort(key=lambda r: (r["sig"][0], r["from"], r["sig"][1]))
            for r in out:
                k, p, inf, m = r["sig"]
                w.writerow([k, p, r["from"], "" if r["to"] is None else r["to"], inf, m])
        side_rows, side_stats = self.side_trees(side, rows, arch_stats)
        with open(self.out / "side_commits.csv", "w", newline="") as f:
            cols = ["commit", "base_seq", "change", "path", "blob", "key", "kind", "id_in_file", "id_method"]
            w = csv.DictWriter(f, cols, extrasaction="ignore")
            w.writeheader()
            for r in side_rows:
                w.writerow(r)
        with open(self.out / "blobs.csv", "w", newline="") as f:
            cols = ["blob", "tei", "malformed", "xmlid", "idno", "legacy_type", "legacy_value", "title", "authors"]
            w = csv.writer(f)
            w.writerow(cols)
            for b, info in sorted(self.blob_info.items()):
                w.writerow([b] + [("true" if info.get(k) else "false") if k in ("tei", "malformed")
                                  else (info.get(k) or "") for k in cols[1:]])
        with open(self.out / "special_cases.csv", "w", newline="") as f:
            w = csv.DictWriter(f, ["case", "seq", "commit", "key", "path", "blob", "detail"])
            w.writeheader()
            for c in self.cases + arch_cases:
                w.writerow({**c, "seq": "" if c["seq"] is None else c["seq"]})
        all_cases = self.cases + arch_cases

        tei = [iv for iv in rows if iv["kind"] == "tei"]
        tei_any = [iv for iv in rows if iv["kind"].startswith("tei")]
        file_commits = sum(((iv["to_seq"] if iv["to_seq"] is not None else last) - iv["from_seq"] + 1)
                           for iv in tei_any)
        distinct_blobs = len({iv["blob"] for iv in tei_any})
        unmerged = [r for r in side if not r["merged_by"]]
        noid = [iv for iv in self.intervals if iv["kind"] == "tei-noid"]  # raw: no ID in the file
        meta = {
            "corpus": self.corpus,
            "repository": self.repo_url,
            "branch": self.branch,
            "branch_from_url_fragment": self.branch_from_fragment,
            "branch_warning": getattr(self, "branch_warning", None),
            "head": self.head,
            "api_commit": self.api_commit,
            "api_commit_seq": next((c[0] for c in self.commits if c[1] == self.api_commit), None),
            "api_commit_on_side_branch": any(r["commit"] == self.api_commit for r in side),
            "counts": {
                "first_parent_commits": len(self.commits),
                "side_commits_merged": len(side) - len(unmerged),
                "side_commits_unmerged": len(unmerged),
                "intervals": len(self.intervals),
                "tei_ids": len({iv["key"] for iv in tei}),
                "tei_ids_valid_at_head": len({iv["key"] for iv in tei if iv["to_seq"] is None}),
                "tei_files_at_head": sum(1 for iv in tei_any if iv["to_seq"] is None),
                "distinct_tei_blobs": distinct_blobs,
                "tei_file_commit_pairs": file_commits,
                "dedup_factor": round(file_commits / distinct_blobs, 2) if distinct_blobs else None,
                "tei_noid_intervals": len(noid),
                "last_seq_with_noid_tei": max(((iv["to_seq"] if iv["to_seq"] is not None else last)
                                               for iv in noid), default=None),
                "tei_unresolved_intervals": sum(1 for iv in rows if iv["kind"] == "tei-unresolved"),
                "special_cases": dict(sorted(_count(c["case"] for c in all_cases).items())),
            },
            "id_archaeology": arch_stats,
            "side_commits": side_stats,
            "state": {
                "last_commit": self.commits[-1][1] if self.commits else None,
                "last_seq": last,
                "dups_open": {k: sorted(v) for k, v in self.dups_open.items()},
                "last_seen_id": self.last_seen_id,
            },
            "last_run": {"start_seq": start, "commits_processed": n_new,
                         "blobs_fetched_for_blobless_clone": self.fetched_blobs},
        }
        (self.out / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")
        return meta


def _count(it):
    d = defaultdict(int)
    for x in it:
        d[x] += 1
    return d


def corpora_from_api() -> dict[str, dict]:
    with urllib.request.urlopen(API, timeout=60) as r:
        return {c["name"]: c for c in json.load(r)}


def corpora_to_index(api: dict[str, dict], registry: Path) -> dict[str, dict]:
    """All corpora of the registry plus all corpora the DraCor API loads.

    The registry also lists corpora in development (draft, proposed), so that their versions
    resolve before dracor.org loads them. Where the API loads a corpus, its entry wins: its
    repository URL (with branch fragment) and loaded commit decide the branch to index."""
    corpora: dict[str, dict] = {}
    try:
        for c in json.loads(registry.read_text())["corpora"]:
            if c.get("repository"):
                corpora[c["name"]] = {"name": c["name"], "repository": c["repository"]}
    except (OSError, ValueError, KeyError) as e:
        print(f"  WARNING registry not read, indexing API corpora only: {e}", file=sys.stderr)
    corpora.update(api)
    return corpora


REGISTRY_REPO = "https://github.com/dracor-org/dracor-registry"


def save_registry(out: Path):
    """Copy the DraCor corpus registry (corpora.json), pinned to its commit, to {out}/registry.json.

    The resolver (lookup.corpus_for_id) reads ID prefixes from it: an entry's `idPrefix` and
    `formerIdPrefixes`, once the registry has such fields; until then it falls back to the
    prefix derived by the index. Rewritten only when the registry commit changes."""
    try:
        sha = subprocess.run(["git", "ls-remote", REGISTRY_REPO, "refs/heads/main"], capture_output=True,
                             text=True, check=True, timeout=60).stdout.split()[0]
        f = out / "registry.json"
        if f.exists() and json.loads(f.read_text()).get("commit") == sha:
            return
        url = f"https://raw.githubusercontent.com/dracor-org/dracor-registry/{sha}/corpora.json"
        with urllib.request.urlopen(url, timeout=60) as r:
            corpora = json.load(r)
        out.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"source": url, "commit": sha, "corpora": corpora},
                                indent=2, ensure_ascii=False) + "\n")
    except Exception as e:  # the index itself doesn't depend on it
        print(f"  WARNING registry not updated: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpora", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--full", action="store_true", help="rebuild instead of continuing incrementally")
    ap.add_argument("--no-fetch", action="store_true", help="use existing clones as they are")
    ap.add_argument("--rev", help="index up to this commit instead of the branch head (testing)")
    ap.add_argument("--out", type=Path, default=INDEX, help="output root (default index/)")
    ap.add_argument("--cleanup", action="store_true",
                    help="delete repos/{corpus} after a successful run (next run clones blobless)")
    ap.add_argument("--clone", choices=["auto", "full", "blobless"], default="auto",
                    help="how to clone a missing repo; auto = full for a first index, "
                         "blobless when index/{corpus} already exists")
    args = ap.parse_args()
    api = corpora_from_api()
    save_registry(args.out)
    corpora = corpora_to_index(api, args.out / "registry.json")
    names = sorted(corpora) if args.all else args.corpora
    if not names:
        ap.error("name at least one corpus or use --all")
    unknown = [n for n in names if n not in corpora]
    if unknown:
        ap.error(f"not in the registry or the DraCor API: {', '.join(unknown)}")
    skipped, failed = [], []
    for name in names:
        try:
            if not index_corpus(name, corpora[name], args):
                skipped.append(name)
        except Exception as e:  # one broken corpus must not stop the others
            print(f"{name}: ERROR {e}", file=sys.stderr, flush=True)
            failed.append(name)
    if skipped:
        print(f"skipped (repository not public): {', '.join(skipped)}", file=sys.stderr)
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)


def index_corpus(name: str, c: dict, args) -> bool:
    """Index one corpus; False if its repository is not public (skipped)."""
    url, frag = parse_repo_url(c["repository"])
    if not args.no_fetch and not is_public(url):
        msg = f"{name}: WARNING repository {url} is not public; not indexed"
        if (args.out / name).exists():
            msg += f" (but {args.out / name} exists: remove it, the index is public)"
        print(msg, file=sys.stderr, flush=True)
        return False
    blobless = args.clone == "blobless" or (
        args.clone == "auto" and not args.full and (args.out / name / "meta.json").exists())
    repo = REPOS / name if args.no_fetch else ensure_repo(name, url, blobless)
    branch, head, warning = resolve_branch(repo, frag, c.get("commit"))
    if warning:
        print(f"  {name}: WARNING {warning}", file=sys.stderr)
    if args.rev:
        head = git(repo, "rev-parse", args.rev).strip()
    ix = Indexer(name, repo, branch, head, args.out / name)
    ix.repo_url, ix.branch_from_fragment, ix.api_commit = c["repository"], frag is not None, c.get("commit")
    ix.branch_warning = warning
    if not ix.run(args.full):
        print(f"{name}: up to date", flush=True)
        if args.cleanup:
            shutil.rmtree(repo)
        return True
    m = json.loads((args.out / name / "meta.json").read_text())["counts"]
    print(f"{name}: {m['first_parent_commits']} fp commits, {m['side_commits_merged']}+"
          f"{m['side_commits_unmerged']} side, {m['distinct_tei_blobs']} TEI blobs, "
          f"dedup {m['dedup_factor']}, cases {m['special_cases']}", flush=True)
    if args.cleanup:
        shutil.rmtree(repo)
    return True


if __name__ == "__main__":
    main()
