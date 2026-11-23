# Specification: versioned DraCor IDs, `https://dracor.org/id/{id}@{commit}`

**Status: draft specification, not implemented.** Decisions taken with Ingo on 2026-11-23 are
marked *(decided)*; everything else is a proposal. This repository implements the resolution
step (ID + commit → file) in `lookup.py`; the resolver itself is meant to be implemented in
XQuery in [dracor-api](https://github.com/dracor-org/dracor-api), extending the existing ID
resolver.

## 1. Today's resolver (unchanged)

`https://dracor.org/id/{id}` is passed by the web server to the API route
`GET /api/v1/id/{id}` (`api:id-to-url` in `modules/api.xqm`, logic in `dutil:id-to-url` in
`modules/util.xqm`). It finds the loaded play whose `TEI/@xml:id` is `{id}` and answers
`303 See Other`, choosing the target by exact match on the `Accept` header (values split at
commas):

| `Accept` | redirect to |
|---|---|
| `application/json` | API play endpoint `/api/v1/corpora/{corpus}/plays/{play}` |
| `application/rdf+xml` | `/api/v1/corpora/{corpus}/plays/{play}/rdf` |
| anything else | the play page `/{corpus}/{play}` |

`404` if no loaded play has that ID.

**This behaviour does not change** *(decided)*. The versioned form is purely additive.

## 2. Syntax

```
https://dracor.org/id/{id}@{commit}
https://dracor.org/api/v1/id/{id}@{commit}       (the API route behind it)
```

- **`{id}`**: a DraCor play ID, e.g. `ger000088`. **Plays only**; corpora and characters are
  not resolved *(decided)*.
- **`{commit}`**: a commit of the corpus repository (a corpus version). **Full (40) and
  abbreviated (≥ 7) hex hashes are both accepted** and resolved directly, without a redirect
  to the full form *(decided)*. Any commit counts: main line or side branch, released or not.

## 3. Resolution

Input: `{id}`, `{commit}`. All data comes from this index (section 7).

1. **Corpus.** The letters before the digits of `{id}` are its prefix. Find the corpus whose
   prefix equals it exactly (`ro` ≠ `rom`, `ger` ≠ `gersh`):
   - from the DraCor corpus registry, field `idPrefix` or `formerIdPrefixes`, once it exists
     ([dracor-registry#3](https://github.com/dracor-org/dracor-registry/issues/3));
   - until then from the prefix the index derives (`meta.json` → `id_archaeology.prefix`, e.g.
     `dutch` → `dut`) and the earlier prefixes in `archaeology.py` (`ep` → `eng`).

   No corpus → error `unknown-corpus`.
2. **Commit.** Find the commit in the corpus's `commits.csv`: exact match on a full hash, or
   the single commit starting with an abbreviation. None → `unknown-commit`; several →
   `ambiguous-commit`.
3. **Files at that commit.**
   - Main-line commit (`seq` set): all rows of `components.csv` with
     `from_seq ≤ seq ≤ to_seq` (empty `to_seq` = still valid).
   - Side-branch commit (`seq` empty): the files at its `base_seq` as above, then the rows of
     `side_commits.csv` for that commit: `A`/`M` sets `path` to that row, `D` removes `path`.
4. **The play.** Among those files, the rows whose `key` equals `{id}`. If there are none, the
   rows whose `id_in_file` equals `{id}` (an older ID the file carried at that commit). Name
   or path matching is never used for IDs.
   - none → `not-found`;
   - several → `duplicate-id` (known upstream, e.g. DutchDraCor);
   - one → resolved: `path`, `blob`, the full commit hash.

`lookup.py` is the reference implementation (`VersionIndex.resolve_name` with steps
`("id", "in-file")`, `resolve_ref`).

## 4. Responses

### 4.1 Resolved, and `{commit}` is the commit DraCor has loaded for that play

"The same as latest": the API knows the commit each play was loaded from (`commit` in the play
endpoint, from `git.xml`). If the resolved full commit equals it, the response is exactly that
of `/id/{id}` (section 1): `303` to the play page, the API play endpoint or its RDF, on the
host of the server that answers *(decided)*. So on staging it stays on staging.

`application/json` keeps that meaning too (`303` to the API play endpoint). The version
record is requested with the profile `dog` (4.5) instead.

### 4.2 Resolved, any other commit

The play page and API can't show other states, so the targets are the exact file contents on
GitHub *(decided)*:

| `Accept` (exact match, as today) | response |
|---|---|
| `application/json` | `200`, version record (4.3); the API can't serve other states, so there is no endpoint to redirect to |
| `application/tei+xml` or `application/xml` | `303` → `https://raw.githubusercontent.com/{org}/{repo}/{commit}/{path}` |
| `application/rdf+xml` | `406`, error `not-acceptable` (no RDF exists for other versions) |
| anything else (browsers) | `303` → `https://github.com/{org}/{repo}/blob/{commit}/{path}` |

`{commit}` in these URLs is always the full hash.

Proposal: `application/tei+xml` / `application/xml` also go to the raw GitHub file in 4.1, since
that is the exact git content (the API's TEI output is re-serialised by eXist).

### 4.3 Version record (profile `dog`)

```json
{
  "id": "ger000088",
  "corpus": "ger",
  "commit": "15a666187e4243368f29c6b7b3f4c461ec94a33c",
  "date": "2026-08-04T09:18:22+02:00",
  "path": "tei/lessing-emilia-galotti.xml",
  "blob": "51324cb412d328dc3b2ee5442bbdc9e15745368b",
  "id_in_file": "ger000088",
  "id_method": "file",
  "loaded": false,
  "license": {"name": "CC0", "url": "https://creativecommons.org/share-your-work/public-domain/cc0/"},
  "urls": {
    "github": "https://github.com/dracor-org/gerdracor/blob/15a666187e4243368f29c6b7b3f4c461ec94a33c/tei/lessing-emilia-galotti.xml",
    "raw": "https://raw.githubusercontent.com/dracor-org/gerdracor/15a666187e4243368f29c6b7b3f4c461ec94a33c/tei/lessing-emilia-galotti.xml"
  }
}
```

- `id_method` tells how the ID was established for that version (`file`: in the file itself;
  `legacy:…`, `lineage`, `title`: inferred by the index, see README).
- `loaded`: whether this is the commit DraCor currently has loaded for the play. If `true`,
  `urls` also contains `api` (the play endpoint) and `page` (the play page).
- To be decided: a play title (has to be read from the TEI file; the index stores only a
  normalised title for matching), corpus title and DOI from the registry, and a version number
  for the record format, since external configs (DOG) depend on it.

`python lookup.py --profile dog ger000088@15a6661` prints this record (without `loaded` and
the `api`/`page` URLs, which need the DraCor API), and the error bodies of 4.4.

### 4.4 Errors

Every error is a JSON body with the HTTP status, an error code and a message *(decided)*:

```json
{"status": 404, "error": "unknown-commit",
 "message": "Commit 15a6661 is not in the version index of corpus ger (index up to 9f3c2e1, 2026-11-22). It may be newer than the index."}
```

| HTTP | `error` | when |
|---|---|---|
| 400 | `bad-request` | `{commit}` not 7–40 hex characters, or `{id}` not letters + digits |
| 404 | `unknown-corpus` | no corpus has that ID prefix |
| 404 | `unknown-commit` | the commit is not in the index: wrong, or **newer than the last index update** *(decided: 404; the message says how far the index goes)* |
| 404 | `ambiguous-commit` | the abbreviation fits several commits of the corpus |
| 404 | `not-found` | the play is not in that corpus version |
| 300 | `duplicate-id` | several files carry the ID at that commit; the body lists them with their URLs |
| 406 | `not-acceptable` | RDF requested for a version other than the loaded one |

### 4.5 Profile `dog`: always the version record

The version record is requested with a profile in a request header *(decided: header, not a
query parameter)*, using W3C Content Negotiation by Profile:

```
GET /api/v1/id/ger000088@15a6661
Accept-Profile: <https://dracor.org/profiles/dog>
```

With that profile the resolver ignores `Accept` and always answers with JSON: `200` and the
version record (4.3) for every resolved commit, the loaded one included, or the error body
(4.4), with `Content-Type: application/json` and `Content-Profile:
<https://dracor.org/profiles/dog>`. It never redirects. DOG gets the same structure for every
version, and nothing depends on which commit DraCor has loaded.

- The profile URI `https://dracor.org/profiles/dog` is a placeholder; to be decided. It
  should be outside `/id/` and ideally dereference to a description of the record.
- An unknown profile in `Accept-Profile` is ignored (the normal `Accept` rules apply), as W3C
  conneg-by-profile allows.
- The profile applies to the versioned form only. `/id/{id}` stays unchanged.
- **No `Link` header** on any `@{commit}` response, unless it is real FAIR Signposting
  (section 8).

## 5. Caching

Handled by nginx, not by the resolver *(decided)*. Responses for a full hash never change;
for an abbreviated hash they could only change if a later commit made the abbreviation
ambiguous; error responses (in particular `unknown-commit`) change when the index is updated.

## 6. Hosts

- Redirects for the loaded commit (4.1) go to the answering server's own host, as `/id/{id}`
  does (configured API base).
- Redirects for other commits (4.2) go to GitHub.

## 7. Data the resolver needs

From this index, per corpus: `commits.csv`, `components.csv` (rows of kind `tei` and
`tei-unresolved` suffice), `side_commits.csv` (same), `meta.json` (prefix, repository), plus
`registry.json`. Open: how this data gets into eXist and in what form (e.g. converted to XML
and stored in a collection when corpora are loaded, or fetched from the index repository), and
how often it is refreshed. The index is updated incrementally (nightly in the draft workflow),
so a brand-new commit is `unknown-commit` until the next update.

## 8. CLARIN: Digital Object Gateway (DOG)

DOG resolves identifiers of repositories with a repository config in
[DOGlib](https://github.com/clarin-eric/DOGlib) (`doglib/static/repo_configs/*.json`). Checked
against the DOGlib source (master `d14b7c2`, 2025-11-10):

- **Matching** (`repos.py`, `RegRepo.match_pid`): for a URL, the config's `host_netloc`
  (without scheme) must be a substring of the identifier. `https://dracor.org` thus also
  matches `staging.dracor.org`.
- **Request URL** (`get_request_url`): the `url.regex` is matched against the identifier, its
  group `record_id` is put into `url.format` (`$api` → `api.base`, `$record_id` → the value) by
  plain string replacement.
- **Custom headers** (`get_headers`): the request carries the config's `url.headers` (or
  `api.headers`) as given. So `Accept-Profile` can be set in the config.
- **Fetching** (`doglib.py`, `_fetch`, `identify`): DOG first sends a `HEAD` to the request URL
  **without** the custom headers and looks for a `Link` header (FAIR Signposting). If there is
  one, it takes the first link and parses its target as a Zenodo-like record instead of using
  the config. If the `HEAD` fails or has no `Link`, DOG sends `GET` with the custom headers,
  following redirects, and requires a final `200`. Its user agent is
  `CLARIN-DOG: https://www.clarin.eu/dog`.

  For the resolver: the header-less `HEAD` gets the normal answer (`303` to GitHub). DOG doesn't
  follow redirects on that `HEAD`, treats the `303` as "no signposting" and continues with the
  config. This works only as long as **no `Link` header** is sent there.
- **Parsing** (`parsers.py`, `JSONParser`): `ref_file.path` (required) and the optional
  `description`, `license`, `collection_title`, `reverse_pid` and `authors` are JSONPath
  expressions on the response.
- **Config schema** (`schema/repo_config_validation_schema.json`): required are `name`,
  `host_name`, `host_netloc`, `parser` (with `type`); `api` needs `base`; `url` has `format` and
  `regex`.

DraCor config (draft):

```json
{
  "repository": {
    "id": "DRACOR",
    "name": "DraCor",
    "host_name": "dracor",
    "host_netloc": "https://dracor.org",
    "api": {"base": "https://dracor.org/api/v1/id/$record_id"},
    "url": {
      "regex": "^.*/id/(?P<record_id>[a-z]+[0-9]+@[0-9a-fA-F]{7,40})$",
      "format": "$api",
      "headers": {"Accept-Profile": "<https://dracor.org/profiles/dog>"}
    },
    "parser": {
      "type": "json",
      "config": {
        "license": "$.license.name",
        "items_root": "",
        "description": "$.path",
        "collection_title": "$.id",
        "reverse_pid": "$.id",
        "ref_file": {"path": "$.urls.raw"}
      }
    },
    "test_examples": {"url": "https://dracor.org/id/ger000088@15a666187e4243368f29c6b7b3f4c461ec94a33c"}
  }
}
```

**Tested end to end (2026-11-23):** DOGlib `d14b7c2` in a `python:3.11-slim` container against
a local stand-in of the resolver (`lookup.version_record` behind a small HTTP server, same
config with host `127.0.0.1:8765`):

| identifier | `fetch` → referenced file |
|---|---|
| `…/id/ger000088@15a6661` | `https://raw.githubusercontent.com/dracor-org/gerdracor/15a6661…/tei/lessing-emilia-galotti.xml`, licence CC0 |
| `…/id/ger000088@2f4e830a` (2016, before the file had its DraCor ID) | `…/gerdracor/2f4e830a…/data/1772-Lessing_Gotthold_Ephraim-Emilia_Galotti-lina.xml` |
| `…/id/greek000001@65321f2` (side-branch commit) | `…/greekdracor/65321f2…/tei/euripides-bacchae.xml`, licence CC BY-SA 3.0 US |

Requests the resolver received per `fetch`: `HEAD` without `Accept-Profile` (answered `303`,
signposting skipped), then `GET` with `Accept: */*` and
`Accept-Profile: <https://dracor.org/profiles/dog>` (answered `200` with the record). DOG sends
`Accept: */*`, so the profile must take precedence over `Accept`, as specified in 4.5.

Bugs in DOGlib found on the way (not DraCor's), reported with proposed fixes as
[DOGlib#59](https://github.com/clarin-eric/DOGlib/issues/59) and
[DOGlib#60](https://github.com/clarin-eric/DOGlib/issues/60); fix in
[DOGlib#61](https://github.com/clarin-eric/DOGlib/pull/61):
- `JSONParser.identify` uses `self.descriptions_path` (doesn't exist; the attribute is
  `description_path`), so `identify` fails for every repository with a JSON parser.
- `curl.RequestError.__init__` calls `super.__init__` instead of `super().__init__`, so every
  non-`200` response raises a `TypeError` instead of `RequestError`. For a `404` from the
  resolver (e.g. unknown commit), DOG's caller gets that `TypeError`.

Open:
- `description` and `collection_title` should use a play title once the record has one.
- An identifier like `https://dracor.org/id/ger000088` (without `@{commit}`) doesn't match the
  regex, so DOG would not resolve it with this config. Whether unversioned IDs should go through
  DOG too is a separate question.
