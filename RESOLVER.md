# Specification: the DraCor ID resolver with versions, `https://dracor.org/id/{id}@{commit}`

**Status: draft specification with a working demonstrator.** The demonstrator is the dracor-api
branch [`feature_resolve_versions`](https://github.com/dracor-org/dracor-api/tree/feature_resolve_versions)
(module `modules/resolver.xqm`; not merged, not deployed). This document describes what it
does. Decisions taken with Ingo on 2026-11-23 are marked *(decided)*; everything else is a
proposal. `lookup.py` in this repository is the reference implementation of the lookup step (ID
+ commit → file); in tests the demonstrator agreed with it on about 2,700 random references
across all 24 corpora.

## 1. Overview

| request | resolves |
|---|---|
| `/id/{id}`, e.g. `/id/ger000088` | the play as loaded in DraCor, as before (section 2) |
| `/id/{corpusname}`, e.g. `/id/ger` | the corpus as loaded in DraCor (section 3) |
| `/id/{id}@{commit}`, e.g. `/id/ger000088@15a6661` | a play version (section 5) |
| `/id/{corpusname}@{commit}`, e.g. `/id/ger@15a6661` | a corpus version (section 6) |

`https://dracor.org/id/…` is passed by the web server to the API route `GET /api/v1/id/…`
(`resolver:resolve`). All targets are chosen by **exact match** on the `Accept` header, as
before, and optionally by a profile in the `Accept-Profile` header (section 7).

The resolver lives in its own module, `modules/resolver.xqm` *(decided)*. The earlier endpoint
`api:id-to-url` in `modules/api.xqm` is commented out there; `dutil:id-to-url` in
`modules/util.xqm` is no longer called.

## 2. Plays: `/id/{id}` (unchanged)

The resolver finds the loaded play whose `TEI/@xml:id` is `{id}` and answers `303 See Other`:

| `Accept` | redirect to |
|---|---|
| `application/json` | API play endpoint `/api/v1/corpora/{corpus}/plays/{play}` |
| `application/rdf+xml` | `/api/v1/corpora/{corpus}/plays/{play}/rdf` |
| anything else | the play page `/{corpus}/{play}` |

`404` (empty body) if no loaded play has that ID and no corpus has that name. Targets are on
the answering server's host (configured API base), so staging stays on staging.

**This behaviour does not change** *(decided)*. The only addition is the repository profile
(section 7). Tested: 48 requests (every play of the test corpus, an unknown and an empty ID, six
`Accept` values) give the same status and `Location` as dracor-api `main`.

## 3. Corpora: `/id/{corpusname}`

The corpus URIs `https://dracor.org/id/{corpusname}` are already used as identifiers (DTS
collection IDs, `dc:identifier` in the OAI-PMH records) but did not resolve
([dracor-api#245](https://github.com/dracor-org/dracor-api/issues/245)). The corpus is found by
its name, as in `corpus.xml` (`dutil:get-corpus`). Note: by name (`/id/dutch`), not by ID prefix
(`dut`).

| `Accept` | response |
|---|---|
| `application/json` | `303` → API corpus endpoint `/api/v1/corpora/{corpusname}` |
| `application/tei+xml` or `application/xml` | `200`, the `corpus.xml` stored in the database, `Content-Type` as requested, `Content-Profile: <https://dracor.org/schema.rng>` |
| anything else | `303` → the corpus page `/{corpusname}` |

A play ID takes precedence over a corpus name; the two can't collide (IDs end in digits).

## 4. Syntax of versions

```
https://dracor.org/id/{id}@{commit}
https://dracor.org/id/{corpusname}@{commit}
https://dracor.org/api/v1/id/…@{commit}       (the API route behind it)
```

- **`{id}`**: a DraCor play ID (letters + digits), e.g. `ger000088`. **`{corpusname}`**: a
  corpus name (letters), e.g. `ger`.
- **`{commit}`**: a commit of the corpus repository (a corpus version). **Full (40) and
  abbreviated (≥ 7) hex hashes are both accepted** and resolved directly, without a redirect
  to the full form *(decided)*. Any commit counts: main line or side branch, released or not.
- The `@` reaches the RESTXQ path parameter unchanged (tested); the resolver splits at it.

## 5. Play versions: `/id/{id}@{commit}`

### 5.1 Resolution

All data comes from this index (section 9).

1. **Corpus.** The letters before the digits of `{id}` are its prefix. Find the corpus whose
   prefix equals it exactly (`ro` ≠ `rom`, `ger` ≠ `gersh`). Prefixes come from the index
   (`meta.json` → `id_archaeology.prefix`, e.g. `dutch` → `dut`) and the earlier prefixes of
   `archaeology.py` (`ep` → `eng`, repeated in `$resolver:former-prefixes`). Once corpora
   declare their prefix in `corpus.xml`
   ([dracor-schema#213](https://github.com/dracor-org/dracor-schema/issues/213)), that should
   come first and the index serve as fallback (earlier prefixes, corpora not loaded). No corpus
   → `unknown-corpus`.
2. **Commit.** Find the commit among the corpus's commits: exact match on a full hash, or the
   commits starting with an abbreviation. None → `unknown-commit`; several →
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

### 5.2 Responses

**The commit DraCor has loaded for that play** (the `commit` of the play endpoint, from
`git.xml`): exactly the response of `/id/{id}` (section 2) *(decided)*, including
`application/json` → `303` to the API play endpoint. The version record is requested with the
profile `dog` (section 7).

**Any other commit:** the play page and API can't show other states, so the targets are the
exact file contents on GitHub *(decided)*:

| `Accept` | response |
|---|---|
| `application/json` | `200`, version record (5.3) |
| `application/tei+xml` or `application/xml` | `303` → `https://raw.githubusercontent.com/{org}/{repo}/{commit}/{path}` |
| `application/rdf+xml` | `406`, error `not-acceptable` |
| anything else (browsers) | `303` → `https://github.com/{org}/{repo}/blob/{commit}/{path}` |

`{commit}` in these URLs is always the full hash. File paths are not percent-encoded yet (open).

### 5.3 Version record

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

- `id_in_file`: the ID the file itself carried at that commit, `null` if none (early
  versions). `id_method` tells how the ID was established (`file`: in the file itself;
  `legacy:…`, `lineage`, `title`: inferred by the index, see README).
- `loaded`: whether this is the commit DraCor currently has loaded for the play. If `true`,
  `urls` also contains `api` (the play endpoint) and `page` (the play page).
- `license`: from the registry copy in the index (`registry.json`), `null` where the registry
  has none.
- To be decided: a play title (has to be read from the TEI file), corpus title and DOI, and a
  version number for the record format, since external configs (DOG) depend on it.

`python lookup.py --profile dog ger000088@15a6661` prints the same record (without `loaded` and
the `api`/`page` URLs) and the error bodies of section 8.

## 6. Corpus versions: `/id/{corpusname}@{commit}`

The corpus is found by name in the index, the commit as in 5.1 step 2.

**The commit DraCor has loaded for the corpus** (the `commit` of the corpus endpoint): exactly
the response of `/id/{corpusname}` (section 3).

**Any other commit:**

| `Accept` | response |
|---|---|
| `application/json` | `200`, corpus version record |
| `application/tei+xml` or `application/xml` | `303` → raw `corpus.xml` at that commit on GitHub |
| anything else | `303` → the repository at that commit, `https://github.com/{org}/{repo}/tree/{commit}` |

Corpus version record: `corpus`, `commit`, `date`, `loaded`, `license`, `urls` (`github`: the
tree, `raw`: `corpus.xml`). This resolves corpus versions that DraCor doesn't have loaded,
which is useful with or without a profile.

## 7. Profiles (`Accept-Profile`)

Requested with a header, using W3C Content Negotiation by Profile *(decided: header, not a
query parameter)*, e.g. `Accept-Profile: <https://dracor.org/profiles/dog>`. An unknown profile
is ignored (the `Accept` rules apply), as the W3C draft allows. **All profile URIs are
placeholders** until DraCor has a policy for its URIs
([dracor-api#434](https://github.com/dracor-org/dracor-api/issues/434)); a profile URI needn't
resolve, but it can't change once external configs use it.

| profile | applies to | effect |
|---|---|---|
| `https://dracor.org/profiles/repository` | all four forms | the page view (any `Accept` other than JSON, RDF, XML) goes to the repository instead of dracor.org: plays → the TEI file on GitHub at the loaded or requested commit; corpora → the repository (for a corpus version other than the loaded one, the tree at that commit). |
| `https://dracor.org/profiles/dog` | versions | always JSON, never a redirect: `200` and the version record (5.3, or the corpus version record) for every resolved commit, the loaded one included, or the error body (section 8); `Content-Profile: <https://dracor.org/profiles/dog>`. For the CLARIN Digital Object Gateway (section 10). |
| `https://dracor.org/schema.rng` | response only | sent as `Content-Profile` with `corpus.xml` (section 3): the document follows the DraCor schema. |

- **No `Link` header** on any response, unless it is real FAIR Signposting (section 10).
- New JSON and XML responses carry `Vary: Accept, Accept-Profile`. The redirects of sections 2
  and 3 are unchanged and have none.
- Suggested, not implemented: the OAI-PMH record of a corpus as another target (Dublin Core or
  CMDI, chosen by profile). The endpoint returns a full OAI-PMH envelope as `text/xml`, and
  whether CLARIN accepts a GetRecord URL as the CMDI self link is still open, so this waits.

## 8. Errors

Every error of a versioned request is a JSON body with the HTTP status, an error code and a
message *(decided)*:

```json
{"status": 404, "error": "unknown-commit",
 "message": "Commit 1234567 is not in the version index of corpus ger (index up to f4bb969, 2026-11-08). It may be newer than the index."}
```

| HTTP | `error` | when |
|---|---|---|
| 400 | `bad-request` | `{commit}` not 7–40 hex characters, or `{id}` neither letters + digits nor letters |
| 404 | `unknown-corpus` | no corpus in the index has that ID prefix or name |
| 404 | `unknown-commit` | the commit is not in the index: wrong, or **newer than the last index load** *(decided: 404; the message says how far the index goes)* |
| 404 | `ambiguous-commit` | the abbreviation fits several commits of the corpus |
| 404 | `not-found` | the play is not in that corpus version |
| 300 | `duplicate-id` | several files carry the ID at that commit; the body lists them (`files`) with their records |
| 406 | `not-acceptable` | RDF requested for a version other than the loaded one |

Unversioned requests keep the plain `404` without a body (section 2).

## 9. Data, loading and refresh

The resolver needs, per corpus, `commits.csv`, `components.csv`, `side_commits.csv` (rows of
kind `tei` and `tei-unresolved`), `meta.json` (prefix, repository, head), plus `registry.json`
(licences).

**Loading** (implemented): `POST /api/v1/id-index`, authorized like the corpus endpoints.

```sh
curl -u admin: -X POST "https://…/api/v1/id-index?corpora=ger,als"
```

- `corpora=`: comma-separated corpus names; default: the corpora the API has.
- `source=`: base URL of the index; default
  `https://raw.githubusercontent.com/dracor-org/dracor-version-index/main/index` (tested).
- It converts each corpus to one XML document, `/db/dracor/versions/{corpus}.xml` (prefixes,
  licence, commits, file intervals, side-branch changes), with a range index on hashes, IDs and
  corpus names. A reload replaces the document. All 24 corpora take about 18 seconds.
- Files are decoded as UTF-8 whatever charset the server names.

**Refresh** (open): nothing reloads the index yet, so a commit is `unknown-commit` until the
next load. Proposal: reload a corpus's index whenever the corpus is loaded, plus a nightly
reload of all corpora after the index workflow has run (the index is updated nightly).

**Caching** is handled by nginx, not by the resolver *(decided)*. Responses for a full hash
never change; for an abbreviated hash they could only change if a later commit made the
abbreviation ambiguous; error responses (in particular `unknown-commit`) change when the index
is reloaded.

**Hosts:** redirects for the loaded commit go to the answering server's own host, as `/id/{id}`
does; redirects for other commits go to GitHub.

## 10. CLARIN: Digital Object Gateway (DOG)

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

  For the resolver: eXist's RESTXQ answers `HEAD` with `405` (as for `/id/{id}` today), which
  DOG treats as "no signposting" and continues with the config. A `303` would do the same. This
  works only as long as **no `Link` header** is sent there.
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

**Tested end to end (2026-11-23), before the demonstrator existed:** DOGlib `d14b7c2` in a
`python:3.11-slim` container against a local stand-in of the resolver (`lookup.version_record` behind a small HTTP server, same
config with host `127.0.0.1:8765`):

| identifier | `fetch` → referenced file |
|---|---|
| `…/id/ger000088@15a6661` | `https://raw.githubusercontent.com/dracor-org/gerdracor/15a6661…/tei/lessing-emilia-galotti.xml`, licence CC0 |
| `…/id/ger000088@2f4e830a` (2016, before the file had its DraCor ID) | `…/gerdracor/2f4e830a…/data/1772-Lessing_Gotthold_Ephraim-Emilia_Galotti-lina.xml` |
| `…/id/greek000001@65321f2` (side-branch commit) | `…/greekdracor/65321f2…/tei/euripides-bacchae.xml`, licence CC BY-SA 3.0 US |

Requests the stand-in received per `fetch`: `HEAD` without `Accept-Profile` (answered `303`,
signposting skipped), then `GET` with `Accept: */*` and
`Accept-Profile: <https://dracor.org/profiles/dog>` (answered `200` with the record). DOG sends
`Accept: */*`, so the profile must take precedence over `Accept`, as specified in section 7.

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
- Repeat this test against the demonstrator instead of the stand-in.
- `description` and `collection_title` should use a play title once the record has one.
- An identifier like `https://dracor.org/id/ger000088` (without `@{commit}`) doesn't match the
  regex, so DOG would not resolve it with this config. Whether unversioned IDs should go through
  DOG too is a separate question.
