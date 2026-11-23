"""Read an index/{corpus}/ directory and answer (key, commit) lookups."""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path


class VersionIndex:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.commits: list[dict] = []
        self.seq_of: dict[str, int | None] = {}   # main-line commit -> seq; side commit -> None
        self.first_parent: dict[str, bool] = {}
        self.merged_by: dict[str, str] = {}       # side commit -> main-line merge commit ("" = unmerged)
        self.base_seq: dict[str, int | None] = {}  # side commit -> newest main-line ancestor
        with open(self.dir / "commits.csv", newline="") as f:
            for r in csv.DictReader(f):
                fp = r["first_parent"] == "true"
                if fp:
                    self.commits.append(r)
                self.seq_of[r["commit"]] = int(r["seq"]) if r["seq"] else None
                self.first_parent[r["commit"]] = fp
                if not fp:
                    self.merged_by[r["commit"]] = r.get("merged_by", "")
                    self.base_seq[r["commit"]] = int(r["base_seq"]) if r.get("base_seq") else None
        self.last_seq = len(self.commits) - 1
        self.intervals: list[dict] = []
        self.by_key: dict[str, list[dict]] = defaultdict(list)
        with open(self.dir / "components.csv", newline="") as f:
            for r in csv.DictReader(f):
                iv = {**r, "from_seq": int(r["from_seq"]),
                      "to_seq": int(r["to_seq"]) if r["to_seq"] else self.last_seq}
                self.intervals.append(iv)
                self.by_key[iv["key"]].append(iv)
        self.side_delta: dict[str, list[dict]] = defaultdict(list)
        side_f = self.dir / "side_commits.csv"
        if side_f.exists():
            with open(side_f, newline="") as f:
                for r in csv.DictReader(f):
                    self.side_delta[r["commit"]].append(r)

    def resolve_commit(self, commit: str) -> int | None:
        """Full or abbreviated (>= 7 chars) main-line commit -> seq; None for side or unknown commits."""
        if commit in self.seq_of:
            return self.seq_of[commit]
        hits = [c for c in self.seq_of if c.startswith(commit)] if len(commit) >= 7 else []
        if len(hits) == 1:
            return self.seq_of[hits[0]]
        return None

    def lookup(self, key: str, seq: int) -> list[dict]:
        """All intervals of `key` valid at `seq` (more than one = duplicate ID).

        `key` is the play's current DraCor ID (the latest ID its text carried); it also finds
        versions from before the file had that ID (id_method lineage/legacy/title)."""
        return [iv for iv in self.by_key.get(key, ()) if iv["from_seq"] <= seq <= iv["to_seq"]]

    def lookup_in_file(self, value: str, seq: int) -> list[dict]:
        """Intervals valid at `seq` whose file itself carries `value` as its ID (historic view)."""
        return [iv for iv in self.valid_at(seq) if iv.get("id_in_file") == value]

    def id_at(self, path: str, seq: int) -> str | None:
        """Reverse lookup: the key of the file at `path` in commit `seq`."""
        hit = [iv for iv in self.valid_at(seq) if iv["path"] == path]
        return hit[0]["key"] if hit else None

    def valid_at(self, seq: int) -> list[dict]:
        return [iv for iv in self.intervals if iv["from_seq"] <= seq <= iv["to_seq"]]

    # ---- any commit (main line or side branch)
    def full_commit(self, commit: str) -> str | None:
        if commit in self.first_parent:
            return commit
        hits = [c for c in self.first_parent if c.startswith(commit)] if len(commit) >= 7 else []
        return hits[0] if len(hits) == 1 else None

    def tree_at(self, commit: str) -> list[dict] | None:
        """All files of any commit: main line from the intervals, side commits as their
        base main-line tree plus the stored differences. None for unknown commits."""
        c = self.full_commit(commit)
        if c is None:
            return None
        if self.first_parent[c]:
            return self.valid_at(self.seq_of[c])
        b = self.base_seq.get(c)
        tree = {iv["path"]: iv for iv in (self.valid_at(b) if b is not None else [])}
        for r in self.side_delta.get(c, ()):
            if r["change"] == "D":
                tree.pop(r["path"], None)
            else:
                tree[r["path"]] = r
        return list(tree.values())

    def lookup_at(self, key: str, commit: str) -> list[dict] | None:
        """lookup() for any commit, including side-branch commits. None for unknown commits."""
        tree = self.tree_at(commit)
        return None if tree is None else [iv for iv in tree if iv["key"] == key]

    # ---- resolve a reference by name: DraCor ID, path or play name (file name without .xml)
    def resolve_name(self, name: str, commit: str, steps: tuple[str, ...] | None = None) -> dict:
        """Find the file `name` refers to at `commit`, trying in this order:

          id        `name` is a DraCor ID (key)                      ger000088
          path      `name` is a path in the repository               tei/lessing-emilia-galotti.xml
          name      a file of that name (without .xml) at `commit`   lessing-emilia-galotti
          history   a play that had that file name at another commit (renamed before or after)
          partial   a file at `commit` whose name contains `name` as whole words  emilia-galotti
          in-file   a file at `commit` that carries `name` as its ID (an older ID)

        `steps` limits the steps tried (resolve_ref uses ("id", "in-file") for DraCor IDs).
        Returns {status, commit, matched_by, hits}; status is ok, ambiguous
        (several hits, see hits), not-found, or unknown-commit."""
        res = {"commit": self.full_commit(commit), "matched_by": None, "hits": []}
        tree = self.tree_at(commit)
        if tree is None:
            return {**res, "status": "unknown-commit"}
        n = name.strip().strip("/")
        stem = n[:-4] if n.endswith(".xml") else n
        stem = stem.rsplit("/", 1)[-1]

        use = set(steps or ("id", "path", "name", "history", "partial", "in-file"))

        def done(how, hits):
            return {**res, "status": "ok" if len(hits) == 1 else "ambiguous",
                    "matched_by": how, "hits": hits}

        for how, test in (("id", lambda iv: iv["key"] == n),
                          ("path", lambda iv: iv["path"] == n),
                          ("name", lambda iv: _stem(iv["path"]) == stem and iv["kind"] != "other")):
            hits = [iv for iv in tree if test(iv)] if how in use else []
            if hits:
                return done(how, hits)
        # history: keys whose file had this name at any (other) commit
        keys = set() if "history" not in use else {iv["key"] for iv in self.intervals if _stem(iv["path"]) == stem and iv["kind"] != "other"}
        keys |= {r["key"] for rows in self.side_delta.values() for r in rows
                 if r["change"] != "D" and _stem(r["path"]) == stem and r["kind"] != "other"}
        hits = [iv for iv in tree if iv["key"] in keys]
        if hits:
            return done("history", hits)
        words = f"-{stem.lower()}-"
        hits = [iv for iv in tree if iv["kind"] != "other" and words in f"-{_stem(iv['path']).lower()}-"] \
            if "partial" in use else []
        if hits:
            return done("partial", hits)
        hits = [iv for iv in tree if iv.get("id_in_file") == n] if "in-file" in use else []
        if hits:
            return done("in-file", hits)
        return {**res, "status": "not-found"}


def _stem(path: str) -> str:
    base = path.rsplit("/", 1)[-1]
    return base[:-4] if base.endswith(".xml") else base


# ---- references like gerdracor/lessing-emilia-galotti@15a6661

INDEX = Path(__file__).resolve().parent / "index"


def corpus_for_id(dracor_id: str, root: Path = INDEX) -> Path | None:
    """Corpus directory for a DraCor ID, by its prefix (the letters before the number).

    Prefixes are compared exactly (ro ≠ rom, ger ≠ gersh). Sources, in this order:
      1. the DraCor corpus registry ({root}/registry.json, copied by build_index.py): an entry's
         `idPrefix` and `formerIdPrefixes`. The registry has no such fields yet (2026-11);
         they are read as soon as it does;
      2. the prefix the index derived from the IDs at head (meta.json; e.g. dutch → dut) and the
         earlier prefixes in archaeology.HISTORIC_PREFIXES (e.g. ep → eng)."""
    from archaeology import HISTORIC_PREFIXES
    prefix = re.sub(r"\d+$", "", dracor_id)
    reg_f = root / "registry.json"
    if reg_f.exists():
        for c in json.loads(reg_f.read_text()).get("corpora", []):
            if prefix == c.get("idPrefix") or prefix in c.get("formerIdPrefixes", ()):
                d = root / c["name"]
                return d if (d / "meta.json").exists() else None
    for d in sorted(root.iterdir()) if root.exists() else ():
        meta = d / "meta.json"
        if not meta.exists():
            continue
        current = json.loads(meta.read_text()).get("id_archaeology", {}).get("prefix")
        if prefix == current or prefix in HISTORIC_PREFIXES.get(d.name, ()):
            return d
    return None


def find_corpus(name: str, root: Path = INDEX) -> Path | None:
    """Corpus directory for a corpus name (ger) or repository name (gerdracor, dracor-org/gerdracor)."""
    name = name.lower().rstrip("/").removesuffix(".git").rsplit("/", 1)[-1]
    for d in sorted(root.iterdir()) if root.exists() else ():
        meta = d / "meta.json"
        if not meta.exists():
            continue
        repo = json.loads(meta.read_text())["repository"].split("#")[0].rstrip("/").rsplit("/", 1)[-1].lower()
        if name in (d.name.lower(), repo):
            return d
    return None


def resolve_ref(ref: str, root: Path = INDEX) -> dict:
    """Resolve `{corpus or repository}/{name}@{commit}`; name = DraCor ID, path or play name.

    The corpus part may also be a full repository URL (https://github.com/dracor-org/gerdracor).
    Returns resolve_name()'s result plus corpus, repository and, for a unique hit, URLs."""
    s = ref.strip()
    idm = re.match(r"^(?:(?:https?://)?(?:www\.)?dracor\.org/id/)?(?P<id>[a-z]+[0-9]+)"
                   r"@(?P<commit>[0-9a-fA-F]{7,40})$", s)
    if idm:  # versioned DraCor ID: https://dracor.org/id/ger000088@15a6661 or ger000088@15a6661
        d = corpus_for_id(idm["id"], root)
        if d is None:
            return {"status": "unknown-corpus", "ref": ref, "corpus": re.sub(r"\d+$", "", idm["id"])}
        name, commit, steps = idm["id"], idm["commit"], ("id", "in-file")
    else:
        s = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", s)
        s = re.sub(r"^dracor-org/", "", s)
        m = re.match(r"^(?P<corpus>[^/@]+)/(?P<name>[^@]+)@(?P<commit>[0-9a-fA-F]{7,40})$", s)
        if not m:
            return {"status": "bad-reference", "ref": ref,
                    "error": "expected {DraCor ID}@{commit} (ger000088@15a6661) or "
                             "{corpus}/{name}@{commit} (gerdracor/lessing-emilia-galotti@15a6661); "
                             "commit: 7 to 40 hex characters"}
        d = find_corpus(m["corpus"], root)
        if d is None:
            return {"status": "unknown-corpus", "ref": ref, "corpus": m["corpus"]}
        name, commit, steps = m["name"], m["commit"], None
    ix = VersionIndex(d)
    res = ix.resolve_name(name, commit, steps)
    repo = json.loads((d / "meta.json").read_text())["repository"].split("#")[0].rstrip("/")
    res.update(ref=ref, corpus=d.name, repository=repo)
    if res["status"] == "ok":
        h, c = res["hits"][0], res["commit"]
        owner_repo = repo.split("github.com/", 1)[-1]
        res["date"] = next((r["date"] for r in ix.commits if r["commit"] == c), None) or _side_date(d, c)
        res["license"] = _registry_license(root, d.name)
        res["urls"] = {"github": f"{repo}/blob/{c}/{h['path']}",
                       "raw": f"https://raw.githubusercontent.com/{owner_repo}/{c}/{h['path']}",
                       "blob_api": f"https://api.github.com/repos/{owner_repo}/git/blobs/{h['blob']}"}
    return res


def _side_date(d: Path, commit: str) -> str | None:
    with open(d / "commits.csv", newline="") as f:
        return next((r["date"] for r in csv.DictReader(f) if r["commit"] == commit), None)


def _registry_license(root: Path, corpus: str) -> dict | None:
    f = root / "registry.json"
    if not f.exists():
        return None
    return next((c.get("license") for c in json.loads(f.read_text())["corpora"] if c["name"] == corpus), None)


ERRORS = {"bad-reference": 400, "unknown-corpus": 404, "unknown-commit": 404,
          "ambiguous-commit": 404, "not-found": 404, "ambiguous": 300}


def version_record(res: dict) -> dict:
    """The resolver's JSON for one play version (profile `dog`, RESOLVER.md sections 5.3 and 8).

    A resolved result gives the record; anything else gives the error body
    {status, error, message}. `loaded` and the api/page URLs need the DraCor API and are left
    to the resolver."""
    st = res["status"]
    if st == "ok":
        h = res["hits"][0]
        return {"id": h["key"], "corpus": res["corpus"], "commit": res["commit"], "date": res["date"],
                "path": h["path"], "blob": h["blob"], "id_in_file": h.get("id_in_file") or None,
                "id_method": h.get("id_method") or None, "license": res.get("license"),
                "urls": {k: res["urls"][k] for k in ("github", "raw")}}
    code = "duplicate-id" if st == "ambiguous" else st
    if st == "unknown-commit" and res.get("commit") is None and "corpus" in res:
        msg = f"Commit not in the version index of corpus {res['corpus']}; it may be wrong or newer than the index."
    else:
        msg = {"bad-reference": res.get("error", ""),
               "unknown-corpus": f"No corpus has the ID prefix '{res.get('corpus')}'.",
               "not-found": f"No play with this ID in {res.get('corpus')} at commit {res.get('commit')}.",
               "ambiguous": "Several files carry this ID at this commit."}.get(st, st)
    body = {"status": ERRORS.get(st, 404), "error": code, "message": msg}
    if st == "ambiguous":
        body["files"] = [{"path": h["path"], "blob": h["blob"]} for h in res["hits"]]
    return body


def _print(res: dict):
    st = res["status"]
    print(f"{res['ref']}: {st}")
    if st in ("bad-reference",):
        print(f"  {res['error']}")
        return
    if st == "unknown-corpus":
        print(f"  no index for '{res['corpus']}' in {INDEX}")
        return
    print(f"  corpus      {res['corpus']} ({res['repository']})")
    if st == "unknown-commit":
        print("  commit      not in this corpus (or an abbreviation that fits several commits)")
        return
    ix_commit = res["commit"]
    print(f"  commit      {ix_commit}")
    if st == "not-found":
        print("  no file of that name, ID or path at this commit")
        return
    print(f"  matched by  {res['matched_by']}")
    for h in res["hits"]:
        print(f"  key         {h['key']}  ({h['kind']}, id_method {h.get('id_method') or '-'}, "
              f"ID in file: {h.get('id_in_file') or '-'})")
        print(f"  path        {h['path']}")
        print(f"  blob        {h['blob']}")
    if st == "ambiguous":
        print("  several files match; give the full file name, path or DraCor ID")
    for k, u in res.get("urls", {}).items():
        print(f"  {k:<11} {u}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Resolve {corpus}/{name}@{commit} to path and blob.")
    ap.add_argument("refs", nargs="+", help="e.g. gerdracor/lessing-emilia-galotti@15a6661, ger/ger000088@15a6661")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--profile", choices=["dog"],
                    help="print the resolver's version record (profile dog, see RESOLVER.md)")
    args = ap.parse_args()
    out = [resolve_ref(r) for r in args.refs]
    if args.profile:
        recs = [version_record(r) for r in out]
        print(json.dumps(recs if len(recs) > 1 else recs[0], indent=2, ensure_ascii=False))
    elif args.json:
        print(json.dumps(out if len(out) > 1 else out[0], indent=2, ensure_ascii=False))
    else:
        for r in out:
            _print(r)
    sys.exit(0 if all(r["status"] == "ok" for r in out) else 1)

