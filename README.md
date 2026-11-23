# dracor-version-index

A **versions index** for the corpora of [DraCor](https://dracor.org), built from their git
history. For every DraCor play and every state of its corpus it tells you which file the play
was in and which exact content that file had. It consists of plain CSV files per corpus plus
the scripts that build and update them. There is no server and no client: you read the files
directly (see [Answering questions with the index](#answering-questions-with-the-index)).

## Why this exists

### DraCor corpora are Living Corpora

DraCor implements the concept of **Programmable Corpora**: drama corpora in TEI that can be
queried, analysed and reused through an API, not only read
([Fischer et al. 2019](https://doi.org/10.5281/zenodo.4284002);
[D7.1](https://doi.org/10.5281/zenodo.7664964)). Its corpora are also **Living Corpora**
([D7.3](https://doi.org/10.5281/zenodo.11081934)). They don't only grow by new plays; the
plays already in them change too. Texts are corrected, structure and metadata are added, the
encoding is revised, files are renamed, and even the play identifiers changed over the years.
Each corpus lives in a git repository on GitHub (`dracor-org/gerdracor`, `…/greekdracor`, …),
and every commit to it is a new state of the corpus.

### The problem: a result belongs to one state of the corpus

A research result computed on DraCor depends on the exact texts it was computed on. But:

- the DraCor API serves only the state it last loaded. Older states are not available through
  it, so earlier research can't be reproduced against the live API;
- a play's URL or DOI alone doesn't pin its text, since the text behind it keeps changing;
- play identifiers and file names changed over time, so an old ID or path may not lead to
  today's file, and today's ID isn't found in an old file.

D7.3 therefore recommends citing the exact **corpus version**, i.e. the git commit, e.g.
"GerDraCor, corpus version `a0bf290…` of January 6, 2018". Since API version 1.1.0 DraCor also
records with every play the commit it was loaded from
([D7.4](https://doi.org/10.5281/zenodo.15301341)), so data obtained from the API can be traced
back to a corpus version. Corpus maintainers can additionally publish **releases** (tagged
GitHub releases archived on Zenodo, with a DOI; see the
[DraCor documentation](https://dracor-org.github.io/dracor-tech-reports/docs/corpora/#docs-corpora-releases)).

### What the index adds

A commit hash pins a whole repository. But to answer "which text of play X does it contain?"
you then need the repository, git, and knowledge of how that corpus encoded its IDs at the
time. The index does that work once, for every commit of every corpus:

- **(DraCor ID, corpus version) → file path + git blob**, for any commit, released or not,
  including commits on side branches;
- **by today's DraCor ID**, also for states before the play carried that ID (the "ID
  archaeology" below): `ger000088` finds *Emilia Galotti* in GerDraCor's first commit of 2016,
  when the file was called `data/1772-Lessing_Gotthold_Ephraim-Emilia_Galotti-lina.xml` and
  had only a DLINA number;
- **the other way round**: which play a path or a blob was, and in which corpus versions;
- **which plays a corpus version contained**, with the exact content of each.

A release is one commit, so the index also makes every release resolvable down to the single
play.

Possible uses built on it: a resolver that answers versioned requests for a DraCor ID,
citation and provenance records for single play versions, CMDI or other metadata records, and
comparisons of plays or corpora across versions. None of them is agreed or deployed yet; a
demonstrator of the resolver exists (see [Use in the DraCor API](#use-in-the-dracor-api-demonstrator)).

## Terminology

| term | meaning |
|---|---|
| **corpus version** | a git commit of a corpus repository, as in D7.3. Every commit is one, even one that only touches the README. |
| **release** | a tagged GitHub release of a corpus, archived on Zenodo with a DOI. Every release is a corpus version; not every corpus version is a release. |
| **play version** | introduced here: the content of a play's TEI file, identified by its **git blob hash**. A new play version exists only when the file content changes, not with every corpus commit. Not the same as an edition. |
| **DraCor ID** | the play identifier, e.g. `ger000088` |
| **main line** | the first-parent history of the branch DraCor loads (normally `main`) |
| **side-branch commit** | a commit that isn't on the main line: work done on a branch and merged later, or never merged |
| **API version** | the version of the DraCor software (e.g. 1.4.1). Unrelated to corpus versions. |

## Data model

The index is derived from git alone. Blob hashes come from `git ls-tree`, `git rev-parse` and
`git cat-file`, never from eXist or API output: eXist reserialises XML, so a hash over its
output does not match the git blob.

### Corpus versions: main line and side branches

- **Main-line commits** are numbered `seq` = 0, 1, 2, … from oldest to newest. Files are
  connected to them by validity intervals (below).
- **Side-branch commits** have no place in the linear `seq`. Each one is stored as the
  difference from its **base**: the newest main-line commit it descends from (`base_seq`). Its
  tree is the base's tree with those differences applied. `merged_by` names the main-line
  commit that merged it.

```
main:     A ── B ─────────── M ── D        A, B, M, D: seq 0, 1, 2, 3
                \           /
branch:          x1 ── x2 ─┘               x1, x2: base_seq = 1 (B), merged_by = M
```

A change made on a branch reaches the main line at the merge M, so on the main line a play
added in x1 appears from M's `seq` on. The states x1 and x2 themselves exist only on the
branch, and they are corpus versions too: DraCor has loaded branch states (the DraCor Open
Knowledge Graph pins a side-branch commit for GreekDraCor and for GerShDraCor).

### Play versions and validity intervals

Corpus versions and files are connected by **intervals**, not by one row per commit × file:
each file content is valid from `from_seq` to `to_seq`. The whole repository is indexed (TEI
files, README, `corpus.xml`, scripts, …), because every file change is a corpus version.

```
ger000088   tei/lessing-emilia-galotti.xml   blob 51324cb…   valid from seq 1418 (still valid)
```

GerDraCor has 1,897 commits and 788 TEI files at its newest one, but only 30 versions of
*Emilia Galotti*: the intervals store each content once.

### Keys: the play's current DraCor ID

TEI files are keyed by the play's **DraCor ID**, and the key is the **latest ID** the play
carried. A play keeps its key across renames, directory moves, ID format changes
(`am658` → `am000658`, `ep000001` → `eng000001`) and the commits before it had any DraCor ID.
The ID the file itself carried in a version is kept in `id_in_file`, so nothing that was
actually in git is hidden.

The DraCor ID wasn't always in the file. GerDraCor, for example, had only
`<idno type="DLINA-ID">` until 2019, then `<idno type="dracor">`, and has `TEI/@xml:id` since
2022 (D7.3). The **ID archaeology** (`archaeology.py`) establishes the ID of every version from
four kinds of evidence, used in this order:

| `id_method` | evidence |
|---|---|
| `file` | a DraCor ID in the file: `TEI/@xml:id` or `<idno type="dracor">`. Only values with the corpus's ID prefix count; `tochange`, `?`, `fer001461`, a Wikidata or EEBO number are reported as `id-invalid` and ignored. |
| `legacy:<type>` | an older ID encoding in the file, e.g. `<idno type="DLINA-ID">88</idno>` → `ger000088`. Known types: `DLINA-ID` (ger), `RusDraCor` (rus), `GreekDraCor` (greek), `quadramaX` (cal), `Dramawebben` (swe), `vid` (EEBO volume ID, eng). Numeric types are converted by number if all learned pairs agree; others go through a table learned from versions that carry both. |
| `lineage` | the file's history: the same path in consecutive commits; a rename (same blob, same ID, or git's rename detection); or a file deleted and added again later (same path, same older ID, or same title and author). A version gets the ID of the text it belongs to. |
| `title` / `title-fuzzy` | `<title>` and author surnames from `<titleStmt>`, matched against versions with a known ID. Accepted only if exactly one ID matches. |

A chain of linked versions is a *text*. If no evidence gives a text an ID, its versions get
`kind = tei-unresolved` and are keyed by their latest path. Disagreements (two older IDs
pointing to different plays, a file switching to a different play at the same path, …) are
reported as special cases and never decided silently.

`ger000088` (Lessing, *Emilia Galotti*) across two renames and two ID encodings, from
`ids.csv`:

| key | path | from_seq | to_seq | id_in_file | id_method |
|---|---|---|---|---|---|
| ger000088 | data/1772-Lessing_Gotthold_Ephraim-Emilia_Galotti-lina.xml | 0 | 15 | | legacy:DLINA-ID |
| ger000088 | tei/1772-Lessing_Gotthold_Ephraim-Emilia_Galotti-lina.xml | 16 | 17 | | legacy:DLINA-ID |
| ger000088 | tei/lessing-emilia-galotti.xml | 18 | 818 | | legacy:DLINA-ID |
| ger000088 | tei/lessing-emilia-galotti.xml | 819 | | ger000088 | file |

Non-TEI files are keyed by their path.

## Files per corpus: `index/{corpus}/`

`{corpus}` is the DraCor corpus name (`ger`, `greek`, `shake`, …). All files are UTF-8 CSV with
a header row.

### `commits.csv`: every corpus version

| column | meaning |
|---|---|
| `seq` | main-line number from 0; empty for side-branch commits |
| `commit` | full commit hash |
| `date` | committer date (ISO 8601) |
| `first_parent` | `true` for main-line commits, `false` for side-branch commits |
| `merged_by` | side-branch commits: the main-line commit that merged them; empty if never merged |
| `base_seq` | side-branch commits: `seq` of the newest main-line ancestor, whose tree the rows in `side_commits.csv` apply to; empty if the commit has no main-line ancestor |

### `components.csv`: every file content on the main line

| column | meaning |
|---|---|
| `key` | DraCor ID for TEI files (see above); the path for other files and unresolved TEI files |
| `path` | path in the repository |
| `blob` | git blob hash |
| `from_seq`, `to_seq` | validity interval; empty `to_seq` = still valid at the newest commit |
| `kind` | `tei`, `tei-unresolved` or `other` |
| `id_in_file` | the ID value the file itself carries (may be an older ID, a placeholder, or empty) |
| `id_method` | `file`, `legacy:<type>`, `lineage`, `title`, `title-fuzzy`; empty for `other` |

Two simultaneously valid rows with the same key are a duplicate ID (known upstream, e.g. six
DutchDraCor pairs); they are reported, never merged.

### `side_commits.csv`: side-branch commits as differences

`commit, base_seq, change, path, blob, key, kind, id_in_file, id_method`: one row per path
where a side-branch commit differs from its base. `change` is `A` (added), `M` (modified) or
`D` (deleted; `blob` empty). TEI files get their key like main-line files: from a main-line
version with the same blob, else the ID in the file, an older ID, the same path in the base
tree, or the title. If nothing gives one, `kind` is `tei-unresolved` and the key is the path.

### `ids.csv`: filename/ID table

`key, path, from_seq, to_seq, id_in_file, id_method`: the TEI rows of `components.csv` with
consecutive versions of one play merged when path, `id_in_file` and method stay the same. It
answers "which file was this play, and what did the file call itself, when?", and the reverse
question from path to ID.

### `special_cases.csv`

`case, seq, commit, key, path, blob, detail`: everything the index noticed but didn't decide.
From the pass over the git history:

| case | meaning |
|---|---|
| `tei-without-id` | a TEI file version without a DraCor ID in the file |
| `id-assigned` / `id-lost` | a file gains / loses its DraCor ID at the same path |
| `id-change` | same path, different ID in the file |
| `rename-same-blob` | new path, same content |
| `rename-with-change` | new path and new content, same ID |
| `rename-similar` | new path and new content, linked by git's rename detection |
| `id-reappears` | an ID comes back after a gap |
| `duplicate-id` / `duplicate-id-resolved` | two files with the same ID in the file at the same time, and the commit where that ends |
| `kind-change` | a file at the same path changes between TEI / non-TEI |
| `id-encoding-conflict` | `@xml:id` and `<idno type="dracor">` disagree |
| `malformed-xml` | the TEI file can't be parsed (also TEI P4 files whose DTD entities can't be resolved); its ID was read by regex |
| `gitlink` | a submodule entry |
| `unhandled-diff-status` | an unexpected `git diff-tree` status |

From the ID archaeology (recomputed on every run):

| case | meaning |
|---|---|
| `id-invalid` | an ID value in the file that isn't a DraCor ID of this corpus |
| `id-from-legacy` | a text that never carried a DraCor ID, identified by its older ID |
| `id-from-title` | a text identified by title and author |
| `id-unresolved` | a text without any usable evidence, or with several candidates |
| `lineage-break` | same path, but different ID and different title: treated as two texts |
| `lineage-gap` | a file deleted and added again later, linked as one text |
| `lineage-ambiguous` | a file splits into two, or two files merge; the links are dropped |
| `legacy-id-ambiguous` | an older ID used by texts with different DraCor IDs (e.g. a reused DLINA number) |
| `legacy-id-conflict` | the older ID points to another play than the DraCor ID |
| `duplicate-id-inferred` | two texts get the same key at the same time through inference |

### `blobs.csv`

A cache of what was read from each XML blob: `blob, tei, malformed, xmlid, idno,
legacy_type, legacy_value, title, authors`. With it, later runs don't need the old blobs, so
they can work on a blobless clone. It also tells you the title and IDs of any blob without
fetching it.

### `meta.json`

Repository URL, the indexed branch (and a warning if it differs from the branch in the API's
repository URL), the newest indexed commit (`head`), the commit the DraCor API had loaded when
the index was built (`api_commit`), counts (commits, distinct TEI blobs, deduplication factor,
special cases by type), the archaeology statistics, and the state for incremental runs.

## `index/registry.json`

A copy of the [DraCor corpus registry](https://github.com/dracor-org/dracor-registry)
(`corpora.json`), pinned to the registry commit it was taken from (`source`, `commit`). Each
build refreshes it when the registry has changed. `lookup.py` uses it to find the corpus of a
DraCor ID by its prefix, and for the licence. The registry has no ID prefix field yet
([dracor-registry#3](https://github.com/dracor-org/dracor-registry/issues/3) proposed
`idPrefix`). Since the DraCor API never reads the registry, the prefix is now proposed for each
corpus's `corpus.xml` instead
([dracor-schema#213](https://github.com/dracor-org/dracor-schema/issues/213)). Until then the
prefix the index derives from the IDs at head is used (`dutch` → `dut`), plus earlier prefixes
listed in `archaeology.py` (`ep` → `eng`).

## Answering questions with the index

The index has no client. Everything below works with the CSV files alone: by hand, in a
spreadsheet, with a shell, or with any CSV-reading tool (Python's `csv` module, R, pandas,
DuckDB, …). `lookup.py` in this repository is a small Python helper that does the same.

The index never contains the texts themselves. It gives you the path and the blob; the text
is in the corpus repository on GitHub (repository URL in `meta.json`).

### Which file was play X in corpus version C?

You have a DraCor ID, e.g. `ger000088`, and a commit hash, e.g. `15a6661` (abbreviated hashes
of 7 or more characters are fine).

1. **Find the commit.** Open `index/ger/commits.csv` and find the row whose `commit` starts
   with your hash. If you don't know the corpus, search all `index/*/commits.csv`.
   ```
   seq,commit,date,first_parent,merged_by,base_seq
   1467,15a666187e4243368f29c6b7b3f4c461ec94a33c,2026-08-04T09:18:22+02:00,true,,
   ```
2. **Main-line commit** (`seq` is filled, here 1467): in `components.csv`, take the row with
   `key` = your ID and `from_seq ≤ seq ≤ to_seq` (an empty `to_seq` counts as "still valid").
   ```
   ger000088,tei/lessing-emilia-galotti.xml,51324cb412d328dc3b2ee5442bbdc9e15745368b,1418,,tei,ger000088,file
   ```
   So `ger000088` at `15a6661` is `tei/lessing-emilia-galotti.xml` with blob `51324cb…`.
   No row: the play wasn't in the corpus at that commit. Two rows: a duplicate ID (see
   `special_cases.csv`).
3. **Side-branch commit** (`seq` is empty): start from the files valid at its `base_seq`
   (step 2 with `seq` = `base_seq`, for all keys), then apply the rows of `side_commits.csv`
   for that commit: a row with `change` `A` or `M` sets its path to its blob, `D` removes the
   path. Then take the rows with your key. Example: GreekDraCor `65321f2` has `base_seq` 97
   and 76 difference rows, among them `M tei/euripides-bacchae.xml 967d2c8… greek000001`.
4. **Get the text.** Any of these gives exactly that file content:
   - `https://github.com/{org}/{repo}/blob/{commit}/{path}` (view), or
     `https://raw.githubusercontent.com/{org}/{repo}/{commit}/{path}` (raw file);
   - `git show {commit}:{path}` or `git cat-file -p {blob}` in a clone of the corpus;
   - `https://api.github.com/repos/{org}/{repo}/git/blobs/{blob}` (base64 content).

   With the blob hash you can also check that a copy of the file is that version:
   `git hash-object file.xml` must print the blob hash.

### Resolving a reference to a single play version

A play version can be referred to as `{DraCor ID}@{commit}`, e.g. `ger000088@15a6661` or
`https://dracor.org/id/ger000088@15a6661`. This is the form proposed for a versioned `/id/`
resolver (see [RESOLVER.md](RESOLVER.md) and the
[demonstrator](#use-in-the-dracor-api-demonstrator); not available on dracor.org). The corpus is found
from the ID prefix (see `registry.json` below). Only the DraCor ID counts here, or an older ID
the file itself carried at that commit, never file names.

The other form is `{corpus}/{play}@{commit}`:

- **corpus**: the corpus name (`ger`) or its repository name (`gerdracor`, `dracor-org/gerdracor`,
  `https://github.com/dracor-org/gerdracor`);
- **play**: the DraCor ID (`ger000088`), the play name, i.e. the file name without `.xml`
  (`lessing-emilia-galotti`), or a path in the repository (`tei/lessing-emilia-galotti.xml`);
- **commit**: the corpus version, 7 to 40 hex characters.

`lookup.py` resolves such references from the command line (`--json` for JSON output):

```
$ python lookup.py gerdracor/ger000088@15a6661
gerdracor/ger000088@15a6661: ok
  corpus      ger (https://github.com/dracor-org/gerdracor)
  commit      15a666187e4243368f29c6b7b3f4c461ec94a33c
  matched by  id
  key         ger000088  (tei, id_method file, ID in file: ger000088)
  path        tei/lessing-emilia-galotti.xml
  blob        51324cb412d328dc3b2ee5442bbdc9e15745368b
  github      https://github.com/dracor-org/gerdracor/blob/15a666187e4243368f29c6b7b3f4c461ec94a33c/tei/lessing-emilia-galotti.xml
  raw         https://raw.githubusercontent.com/dracor-org/gerdracor/15a666187e4243368f29c6b7b3f4c461ec94a33c/tei/lessing-emilia-galotti.xml
  blob_api    https://api.github.com/repos/dracor-org/gerdracor/git/blobs/51324cb412d328dc3b2ee5442bbdc9e15745368b
```

The same play version by name, `gerdracor/lessing-emilia-galotti@15a6661`, gives the same
path and blob (`matched by name`). The DraCor ID also works for the first commit of
GerDraCor (2016), when the file had another name and no DraCor ID yet:

```
$ python lookup.py gerdracor/ger000088@2f4e830a
  matched by  id
  key         ger000088  (tei, id_method legacy:DLINA-ID, ID in file: -)
  path        data/1772-Lessing_Gotthold_Ephraim-Emilia_Galotti-lina.xml
  blob        cf6b8faadc2eaa6ac491d67b8f8b80823ec052f9
```

So does today's name (`gerdracor/lessing-emilia-galotti@2f4e830a`, `matched by history`).
The DraCor ID is the most robust form of a reference: names and paths changed over time,
the ID key doesn't.

The play part is tried in this order, and `matched by` says which step matched:

| step | matches |
|---|---|
| `id` | a DraCor ID (the key) |
| `path` | a path in the repository |
| `name` | a file of that name (without `.xml`) at that commit |
| `history` | a play that had that file name at some other commit (renamed before or after) |
| `partial` | a file at that commit whose name contains the given words: `emilia-galotti` finds `lessing-emilia-galotti` |
| `in-file` | a file that carries the value as its ID at that commit (an older ID) |

If several files match (e.g. `gerdracor/galotti@15a6661` finds *Emilia Galotti* and Bodmer's
*Odoardo Galotti*), the result is `ambiguous` and lists them; nothing is picked. Other
results: `not-found` (no such play at that commit), `unknown-commit` (not a commit of the
corpus, or an abbreviation that fits several), `unknown-corpus`, `bad-reference`. The exit
code is 0 only if every reference resolved to exactly one file.

### Other questions

| question | how |
|---|---|
| What did the corpus contain at commit C? | all rows of `components.csv` valid at C's `seq` (`kind` = `tei` for plays); for a side-branch commit as in step 3 |
| Which play is the file `path` at commit C? | the row with that `path` valid at C's `seq`; its `key` is the DraCor ID |
| What happened to play X over time? | all rows with `key` = X in `ids.csv` (paths and IDs) or `components.csv` (every content change); `from_seq`/`to_seq` → dates via `commits.csv` |
| Which corpus version was current on date D? | the last main-line row of `commits.csv` with `date` ≤ D |
| Which play and versions is blob B? | rows with `blob` = B in `components.csv` and `side_commits.csv`; title and IDs in `blobs.csv` |
| I have data from the DraCor API. Which version is it? | the API reports the commit each play was loaded from (API version ≥ 1.1.0, D7.4); look that commit up as above |
| A file in an old commit carries an ID that isn't today's | rows with `id_in_file` = that value; their `key` is today's ID |
| How certain is an assignment? | `id_method`: `file` is read from the file itself; `legacy`, `lineage` and `title` are inferred (see `special_cases.csv` for disagreements) |

### Shell

```sh
# commit → seq (main line)
grep ',15a6661' index/ger/commits.csv | cut -d, -f1
# ger000088 at seq 1467
awk -F, -v k=ger000088 -v s=1467 '$1==k && $4<=s && ($5=="" || s<=$5)' index/ger/components.csv
# all plays at seq 1467
awk -F, -v s=1467 '$6=="tei" && $4<=s && ($5=="" || s<=$5) {print $1, $2}' index/ger/components.csv
```

Paths contain no commas in the current corpora; for general use, prefer a real CSV parser.

### Python (standard library only)

```python
import csv

def commit_row(corpus, commit):
    with open(f"index/{corpus}/commits.csv", newline="") as f:
        hits = [r for r in csv.DictReader(f) if r["commit"].startswith(commit)]
    return hits[0] if len(hits) == 1 else None

def files_at(corpus, seq):
    with open(f"index/{corpus}/components.csv", newline="") as f:
        return [r for r in csv.DictReader(f)
                if int(r["from_seq"]) <= seq and (not r["to_seq"] or seq <= int(r["to_seq"]))]

c = commit_row("ger", "15a6661")
[r for r in files_at("ger", int(c["seq"])) if r["key"] == "ger000088"]
```

### With `lookup.py`

```python
from lookup import VersionIndex

ix = VersionIndex("index/ger")
ix.lookup_at("ger000088", "15a6661")     # any commit, main line or side branch → [{path, blob, …}]
ix.tree_at("65321f2")                     # all files of any commit
seq = ix.resolve_commit("15a6661")        # main-line commit → seq; None for side-branch commits
ix.lookup("ger000088", seq)               # by seq
ix.lookup("ger000088", 0)                 # works before the file carried its DraCor ID
ix.lookup_in_file("ger000088", seq)       # by the ID written in the file at that commit
ix.id_at("tei/lessing-emilia-galotti.xml", seq)   # path → key
ix.valid_at(seq)                          # the whole main-line tree at that seq
ix.merged_by.get("<side commit>")         # the main-line commit that merged a side commit
```

Lookups return a list: empty if the play doesn't exist at that commit, more than one row for
a duplicate ID.

## Building and updating

Requires git and Python 3 (developed with 3.13); only the standard library is used. On the
development machine that is Homebrew Python (`/opt/homebrew/bin/python3.13`) in a local
`.venv`.

```sh
python build_index.py als              # one corpus; repository URL from the DraCor API
python build_index.py --all            # all corpora
python build_index.py ger --full       # rebuild from scratch instead of continuing
python build_index.py --all --cleanup  # delete each clone afterwards
```

- **Corpus list:** names, repository URLs and the loaded commits come from
  `https://dracor.org/api/v1/corpora`. A new corpus there is indexed on the next run.
- **Branch:** the branch whose main line contains the commit the DraCor API loads. That is
  normally the branch in the repository URL (`…#reboot`) or the default branch. If the API
  commit is on another branch, that branch is used and a warning is recorded (greek).
- **Updates are incremental.** A run continues from the last indexed main-line commit and
  reads only new files. The ID archaeology and the side-branch commits are recomputed on every
  run, because a new commit can change them (a file that gets its ID changes the key of its
  older versions). A corpus without new commits is reported as `up to date` and its files are
  not rewritten, so an unchanged corpus produces no diff.
- **Clones** go to `repos/{corpus}` (gitignored). A first index uses a full clone. Once
  `index/{corpus}/` exists, a missing clone is made blobless (commits and trees, no file
  contents), and only the XML files of new commits are fetched, in one batch.
- **Rewritten history:** if the main line changed since the last run (force push), the corpus
  is rebuilt from scratch.

### Automatic updates

`.github/workflows/update-index.yml` keeps the index in this repository up to date. The index
lives in its own repository, never in a corpus repository: every index commit would change
the history it describes. The workflow runs nightly, by hand (Actions → "Update versions
index" → Run workflow, optionally for named corpora), or when a corpus repository sends a
`repository_dispatch` of type `corpus-updated`. It updates all corpora, tests
the ones that changed, and commits `index/` only if the tests pass. It needs no secrets: the
corpus repositories are public, and the push uses the workflow's own `GITHUB_TOKEN`.

### Use in the DraCor API (demonstrator)

The branch [`feature_resolve_versions`](https://github.com/dracor-org/dracor-api/tree/feature_resolve_versions)
of dracor-api resolves `https://dracor.org/id/{id}@{commit}` and `/id/{corpus}@{commit}` from
this index (module `modules/resolver.xqm`; not merged, not deployed). The API loads the index
with an authorized request, which fetches `commits.csv`, `components.csv`, `side_commits.csv`
and `meta.json` of each corpus, plus `registry.json` for licences, and stores them as XML:

```sh
curl -u admin: -X POST "http://localhost:8080/exist/restxq/v1/id-index?corpora=ger,als"
```

By default it reads `https://raw.githubusercontent.com/dracor-org/dracor-version-index/main/index`
(tested). `source=` sets another location, e.g. a local
copy served with `python -m http.server`. Without `corpora=` it loads the corpora the API has.
In tests its answers agreed with `lookup.py` on about 2,700 random references across all
corpora.

## Tests

```sh
python test_index.py als                 # exhaustive
python test_index.py ger --sample 3000   # random (ID, commit) sample for large corpora
```

The tests check the index against the git repository in `repos/{corpus}` (a full or blobless
clone; a blobless one fetches the blobs it reads):

- the index reproduces `git ls-tree` for every checked commit;
- each (ID, commit) lookup matches `git rev-parse`, and the evidence for its ID is in the blob;
- an unchanged file keeps its blob over its interval;
- the blob differs at each change point;
- there is no hit before a play was added;
- every commit of `git rev-list --all` is listed;
- side-branch commit trees (base + differences) reproduce `git ls-tree` exactly;
- every TEI version has an ID or is reported as unresolved.

They also check against the DraCor Open Knowledge Graph on the EOSC EU Node, which pins one
commit per corpus, using fixtures in `tests/fixtures/`: per-play pins, play counts per corpus
version, and the number of plays at each corpus's pinned commit. Five corpora differ from the
graph for documented reasons (`known_differences` in `kg_pins_summary.json`): TEI files with
names DraCor doesn't load (dutch, eng, yi) and plays the graph leaves out (fre, ibs). These
checks pass as long as the difference stays exactly the known one.

## Known limitations

- The ID archaeology is evidence-based, not certain: `lineage`, `title` and inferred
  `legacy` assignments are inferences. `id_method` says which kind each one is, and the
  special cases list every disagreement.
- Known older ID encodings and earlier ID prefixes are configured in `archaeology.py`
  (`LEGACY_TYPES`, `HISTORIC_PREFIXES`). A new encoding has to be added there.
- TEI files that DraCor doesn't load (names that aren't `[a-z0-9-]+.xml`, drafts in other
  folders) are in the index, because they are in git. The index describes the repository,
  not what the API served.
- A few versions have no ID at all (unfinished drafts, one EEBO play with two candidate IDs);
  they are `tei-unresolved` and listed as `id-unresolved`.

## References

- Fischer, F., Börner, I., Göbel, M., Hechtl, A., et al. (2019). *Programmable Corpora:
  Introducing DraCor, an Infrastructure for the Research on European Drama.* DH2019.
  [doi:10.5281/zenodo.4284002](https://doi.org/10.5281/zenodo.4284002)
- Börner, I., Trilcke, P. (2023). *CLS INFRA D7.1 On Programmable Corpora.*
  [doi:10.5281/zenodo.7664964](https://doi.org/10.5281/zenodo.7664964)
- Börner, I., Trilcke, P. (2024). *CLS INFRA D7.3 On Versioning Living and Programmable
  Corpora.* [doi:10.5281/zenodo.11081934](https://doi.org/10.5281/zenodo.11081934)
- Börner, I., Trilcke, P., Skorinkin, D., Giovannini, L., et al. (2025). *CLS INFRA D7.4 Report
  on the Implementation of Programmable Corpora.*
  [doi:10.5281/zenodo.15301341](https://doi.org/10.5281/zenodo.15301341)
- DraCor Technical Reports and System Documentation:
  <https://dracor-org.github.io/dracor-tech-reports/>
