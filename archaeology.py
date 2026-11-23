"""ID archaeology: give every TEI file version in a corpus history its DraCor ID.

The raw pass (build_index.Indexer) records, per file version, what the file itself says.
Many versions carry no usable DraCor ID: early commits before IDs existed, older
encodings, placeholders, typos. This module resolves an ID for every TEI interval from
four kinds of evidence, in this order of precedence:

  1. file     a valid DraCor ID in the file (TEI/@xml:id or <idno type="dracor">);
              valid = prefix of the IDs used at head (e.g. ger, dut, eng) + digits.
  2. legacy   an older ID encoding in the file (<idno type="DLINA-ID">171</idno> etc.),
              translated by a table learned from versions that carry both, or, for
              numeric encodings whose learned pairs all agree, by number (171 -> ger000171).
  3. lineage  the file's history: same path in consecutive commits, or a recorded rename
              (same blob, same ID, or git rename detection), back or forward to a version
              whose ID is known.
  4. title    <title> + author surnames in <titleStmt>, matched against versions with a
              known ID; accepted only if exactly one ID matches.

A "text" is a chain of linked intervals (one file through renames and edits). Its key is
the latest ID it carries; earlier IDs of the same text stay visible in id_in_file.
Disagreements and unresolvable texts are reported as special cases, never decided silently.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict

# Older ID encodings found in corpus histories (<idno type="...">).
# vid = EEBO volume ID: carried by both the TCP files and the EarlyPrint-derived files in eng.
LEGACY_TYPES = ("DLINA-ID", "RusDraCor", "GreekDraCor", "quadramaX", "Dramawebben", "vid")
# Earlier DraCor ID schemes that count as real IDs although head uses another prefix.
HISTORIC_PREFIXES = {"eng": ("ep",)}
RENAME_CASES = ("rename-same-blob", "rename-with-change", "rename-similar")
ARCH_CASES = ("id-invalid", "lineage-break", "lineage-gap", "lineage-ambiguous", "legacy-id-ambiguous",
              "legacy-id-conflict", "id-from-legacy", "id-from-title", "id-unresolved",
              "duplicate-id-inferred")


def head_prefix(intervals) -> str | None:
    """The ID prefix used by the files valid at head (most common one)."""
    c = Counter()
    for iv in intervals:
        if iv["to_seq"] is None and iv["kind"] == "tei":
            m = re.match(r"[a-z]+", iv["key"])
            if m:
                c[m.group(0)] += 1
    return c.most_common(1)[0][0] if c else None


def _span_overlap(a, b, last):
    a1 = a[1] if a[1] is not None else last
    b1 = b[1] if b[1] is not None else last
    return a[0] <= b1 and b[0] <= a1


def resolve(corpus: str, intervals: list[dict], blob_meta: dict, cases: list[dict], last_seq: int):
    """Return ({id(iv): {'key','kind','id_in_file','id_method','text'}}, new_cases)."""
    prefix = head_prefix(intervals)
    prefixes = ([prefix] if prefix else []) + list(HISTORIC_PREFIXES.get(corpus, ()))
    valid_re = re.compile("(" + "|".join(map(re.escape, prefixes)) + r")\d+") if prefixes else None
    out_cases: list[dict] = []

    def case(kind, seq, key="", path="", blob="", detail=""):
        out_cases.append({"case": kind, "seq": seq, "key": key, "path": path, "blob": blob,
                          "detail": detail})

    tei = [iv for iv in intervals if iv["kind"] in ("tei", "tei-noid")]
    tei.sort(key=lambda iv: (iv["from_seq"], iv["path"]))
    n = len(tei)
    info = []
    invalid_seen = set()
    for iv in tei:
        m = blob_meta.get(iv["blob"], {})
        raw = [v for v in (m.get("xmlid"), m.get("idno")) if v]
        valid = [v for v in raw if valid_re and valid_re.fullmatch(v)]
        direct = valid[0] if valid else None
        in_file = direct or (raw[0] if raw else "")
        for v in raw:
            if v not in valid and (iv["path"], v) not in invalid_seen:
                invalid_seen.add((iv["path"], v))
                case("id-invalid", iv["from_seq"], v, iv["path"], iv["blob"],
                     f"'{v}' is not a {prefix}… DraCor ID; treated as no ID")
        info.append({"direct": direct, "in_file": in_file,
                     "legacy": (m.get("legacy_type"), m.get("legacy_value"))
                     if m.get("legacy_value") else None,
                     "title": m.get("title") or "", "authors": m.get("authors") or ""})

    # ---- links between intervals
    by_start = {(iv["path"], iv["from_seq"]): i for i, iv in enumerate(tei)}
    by_end = {(iv["path"], iv["to_seq"]): i for i, iv in enumerate(tei) if iv["to_seq"] is not None}
    succ, pred = defaultdict(set), defaultdict(set)

    def similar_title(a, b):
        ta, tb = info[a]["title"], info[b]["title"]
        if not ta or not tb or ta == tb or ta in tb or tb in ta:  # subtitle split off or added
            return True
        wa, wb = set(ta.split()), set(tb.split())
        short, long_ = sorted((ta, tb), key=len)
        return (len(wa & wb) / min(len(wa), len(wb)) >= .5
                or difflib.SequenceMatcher(None, short, long_[:len(short)]).ratio() >= .85  # typo fix
                or difflib.SequenceMatcher(None, ta, tb).ratio() >= .6)

    for i, iv in enumerate(tei):
        if iv["to_seq"] is None:
            continue
        j = by_start.get((iv["path"], iv["to_seq"] + 1))
        if j is None:
            continue
        di, dj = info[i]["direct"], info[j]["direct"]
        if di and dj and di != dj and not similar_title(i, j):
            case("lineage-break", tei[j]["from_seq"], dj, iv["path"], tei[j]["blob"],
                 f"ID {di} -> {dj} and title '{info[i]['title']}' -> '{info[j]['title']}': "
                 f"treated as a different text at the same path")
            continue
        succ[i].add(j)
        pred[j].add(i)
    for c in cases:
        if c["case"] not in RENAME_CASES or not c["detail"].startswith("from "):
            continue
        old = c["detail"][5:].split(" (", 1)[0]
        seq = int(c["seq"])
        i, j = by_end.get((old, seq - 1)), by_start.get((c["path"], seq))
        if i is not None and j is not None:
            succ[i].add(j)
            pred[j].add(i)
    for i in range(n):
        if len(succ[i]) > 1:
            case("lineage-ambiguous", tei[i]["to_seq"] + 1, info[i]["direct"] or "", tei[i]["path"],
                 tei[i]["blob"], "continues as " + " | ".join(sorted(tei[j]["path"] for j in succ[i]))
                 + "; links dropped")
            for j in succ[i]:
                pred[j].discard(i)
            succ[i] = set()
    for j in range(n):
        if len(pred[j]) > 1:
            case("lineage-ambiguous", tei[j]["from_seq"], info[j]["direct"] or "", tei[j]["path"],
                 tei[j]["blob"], "continues " + " | ".join(sorted(tei[i]["path"] for i in pred[j]))
                 + "; links dropped")
            for i in pred[j]:
                succ[i].discard(j)
            pred[j] = set()

    # ---- texts = chains of linked intervals
    text_of = [-1] * n
    texts: list[list[int]] = []
    for i in range(n):
        if text_of[i] >= 0 or pred[i]:
            continue
        chain, k = [], i
        while True:
            text_of[k] = len(texts)
            chain.append(k)
            nxt = [j for j in succ[k] if text_of[j] < 0]
            if not nxt:
                break
            k = nxt[0]
        texts.append(chain)
    for i in range(n):  # cycles cannot occur (seq increases), but be safe
        if text_of[i] < 0:
            text_of[i] = len(texts)
            texts.append([i])

    # ---- gap links: a file deleted and added again later (same path or same older ID,
    # similar title) continues the same text. Only unique candidates are linked.
    first_at_path = defaultdict(list)
    first_legacy = defaultdict(list)
    first_sig = defaultdict(list)
    for t, chain in enumerate(texts):
        f = chain[0]
        first_at_path[tei[f]["path"]].append(t)
        if info[f]["legacy"]:
            first_legacy[info[f]["legacy"]].append(t)
        if info[f]["title"]:
            first_sig[(info[f]["title"], info[f]["authors"])].append(t)
    gap_next, gap_prev = {}, {}
    for a, chain in enumerate(texts):
        last = chain[-1]
        e = tei[last]["to_seq"]
        if e is None:
            continue
        cands = [b for b in first_at_path[tei[last]["path"]] if tei[texts[b][0]]["from_seq"] > e + 1]
        if cands:
            b = min(cands, key=lambda b: tei[texts[b][0]]["from_seq"])
            cands = [b] if similar_title(last, texts[b][0]) else []
            via = "same path"
        if not cands and info[last]["legacy"]:
            cands = [b for b in first_legacy[info[last]["legacy"]] if tei[texts[b][0]]["from_seq"] > e + 1
                     and similar_title(last, texts[b][0])]
            via = f"{info[last]['legacy'][0]}={info[last]['legacy'][1]}"
        if not cands and info[last]["title"]:
            s = (info[last]["title"], info[last]["authors"])
            cands = [b for b in first_sig[s] if tei[texts[b][0]]["from_seq"] > e + 1]
            via = "same title and author"
        if len(cands) == 1 and cands[0] not in gap_prev:
            b = cands[0]
            di = [info[i]["direct"] for i in chain if info[i]["direct"]]
            dj = [info[i]["direct"] for i in texts[b] if info[i]["direct"]]
            if di and dj and di[-1] != dj[0] and not via.startswith("same path") \
                    and di[-1].startswith(prefix) and dj[0].startswith(prefix):
                pass  # an older ID agreeing but DraCor IDs differing: leave apart
            else:
                gap_next[a], gap_prev[b] = b, a
                f = texts[b][0]
                case("lineage-gap", tei[f]["from_seq"], "", tei[f]["path"], tei[f]["blob"],
                     f"continues {tei[last]['path']} (valid until seq {e}) via {via}")
    if gap_next:
        merged = []
        for t in range(len(texts)):
            if t in gap_prev:
                continue
            chain, k = list(texts[t]), t
            while k in gap_next:
                k = gap_next[k]
                chain += texts[k]
            merged.append(chain)
        texts = merged
        for t, chain in enumerate(texts):
            for i in chain:
                text_of[i] = t

    key = [None] * len(texts)
    how = [None] * len(texts)
    for t, chain in enumerate(texts):
        ids = [info[i]["direct"] for i in chain if info[i]["direct"]]
        if ids:
            key[t], how[t] = ids[-1], "file"

    def span(t):
        return (tei[texts[t][0]]["from_seq"], tei[texts[t][-1]]["to_seq"])

    # ---- legacy table
    pairs = defaultdict(set)
    for t, chain in enumerate(texts):
        if key[t]:
            for i in chain:
                if info[i]["legacy"]:
                    pairs[info[i]["legacy"]].add(key[t])
    table = {}
    for lv, ks in pairs.items():
        if len(ks) == 1:
            table[lv] = next(iter(ks))
        else:
            case("legacy-id-ambiguous", None, " | ".join(sorted(ks)), "", "",
                 f"{lv[0]}={lv[1]} belongs to texts with different IDs")
    numeric_rule = {}
    for typ in LEGACY_TYPES:
        ps = [(v, k) for (ty, v), k in table.items() if ty == typ]
        if len(ps) >= 3 and all(v.isdigit() and re.fullmatch(rf"{prefix}0*{int(v)}", k) for v, k in ps):
            numeric_rule[typ] = True

    def legacy_id(lv):
        if lv in table:
            return table[lv]
        if numeric_rule.get(lv[0]) and lv[1].isdigit():
            return f"{prefix}{int(lv[1]):06d}"
        return None

    for t, chain in enumerate(texts):
        lids = {legacy_id(info[i]["legacy"]) for i in chain if info[i]["legacy"]} - {None}
        if key[t] and lids - {key[t]}:
            i = chain[-1]
            case("legacy-id-conflict", tei[i]["from_seq"], key[t], tei[i]["path"], tei[i]["blob"],
                 f"older encoding points to {sorted(lids - {key[t]})}; kept {key[t]}")
        elif not key[t] and len(lids) == 1:
            key[t], how[t] = lids.pop(), "legacy"
            i = chain[0]
            case("id-from-legacy", tei[i]["from_seq"], key[t], tei[i]["path"], tei[i]["blob"],
                 f"text has no DraCor ID in any version; {info[i]['legacy'][0]}="
                 f"{info[i]['legacy'][1]}; {len(chain)} intervals")
        elif not key[t] and len(lids) > 1:
            i = chain[0]
            case("legacy-id-conflict", tei[i]["from_seq"], "", tei[i]["path"], tei[i]["blob"],
                 f"older encodings point to {sorted(lids)}; left unresolved")

    # ---- titles
    sig_keys = defaultdict(set)
    title_keys = defaultdict(set)
    spans_of_key = defaultdict(list)
    for t, chain in enumerate(texts):
        if key[t] and how[t] in ("file", "legacy"):
            spans_of_key[key[t]].append(span(t))
            for i in chain:
                if info[i]["title"]:
                    sig_keys[(info[i]["title"], info[i]["authors"])].add(key[t])
                    title_keys[info[i]["title"]].add(key[t])
    for t, chain in enumerate(texts):
        if key[t]:
            continue
        sigs = {(info[i]["title"], info[i]["authors"]) for i in chain if info[i]["title"]}
        cand, method = set(), "title"
        for s in sigs:
            cand |= sig_keys.get(s, set())
        if not cand:
            for s in sigs:
                cand |= title_keys.get(s[0], set())
        if not cand and sigs:
            best = {}
            for (tt, aa), ks in sig_keys.items():
                for s in sigs:
                    if s[1] and aa and not set(s[1].split("|")) & set(aa.split("|")):
                        continue
                    r = difflib.SequenceMatcher(None, s[0], tt).ratio()
                    for k in ks:
                        best[k] = max(best.get(k, 0), r)
            top = sorted(best.items(), key=lambda kv: -kv[1])
            if top and top[0][1] >= .85 and (len(top) == 1 or top[1][1] < top[0][1] - .1):
                cand, method = {top[0][0]}, f"title-fuzzy"
        if len(cand) > 1:  # drop IDs whose own text exists at the same time as this one
            free = {k for k in cand if not any(_span_overlap(span(t), sp, last_seq) for sp in spans_of_key[k])}
            if free:
                cand = free
        i = chain[-1]
        if len(cand) == 1:
            key[t], how[t] = next(iter(cand)), method
            case("id-from-title", tei[chain[0]]["from_seq"], key[t], tei[i]["path"], tei[i]["blob"],
                 f"{method}: '{info[i]['title']}' / {info[i]['authors'] or '-'}; "
                 f"paths {' | '.join(sorted({tei[x]['path'] for x in chain}))}")
        else:
            case("id-unresolved", tei[chain[0]]["from_seq"], tei[i]["path"], tei[i]["path"], tei[i]["blob"],
                 f"title '{info[i]['title']}' / {info[i]['authors'] or '-'}; "
                 + (f"candidates {sorted(cand)}" if cand else "no candidate")
                 + f"; valid seq {span(t)[0]}-{'' if span(t)[1] is None else span(t)[1]}")

    # ---- per-interval result
    res = {}
    for t, chain in enumerate(texts):
        for i in chain:
            iv = tei[i]
            if key[t] is None:
                r = {"key": tei[chain[-1]]["path"], "kind": "tei-unresolved", "id_method": ""}
            else:
                d, lv = info[i]["direct"], info[i]["legacy"]
                if d == key[t]:
                    m = "file"
                elif how[t] in ("title", "title-fuzzy"):
                    m = how[t]
                elif not d and lv and legacy_id(lv) == key[t]:
                    m = f"legacy:{lv[0]}"
                else:
                    m = "lineage"
                r = {"key": key[t], "kind": "tei", "id_method": m}
            r.update(id_in_file=info[i]["in_file"], text=t)
            res[id(iv)] = r

    # ---- duplicates created by inference (raw duplicates are reported by the raw pass)
    by_key = defaultdict(list)
    for t in range(len(texts)):
        if key[t]:
            by_key[key[t]].append(t)
    for k, ts in by_key.items():
        for a in range(len(ts)):
            for b in range(a + 1, len(ts)):
                ta, tb = ts[a], ts[b]
                if how[ta] == how[tb] == "file":
                    continue
                for i in texts[ta]:
                    for j in texts[tb]:
                        ia, jb = tei[i], tei[j]
                        if _span_overlap((ia["from_seq"], ia["to_seq"]), (jb["from_seq"], jb["to_seq"]), last_seq) \
                                and not (info[i]["direct"] == k and info[j]["direct"] == k):
                            case("duplicate-id-inferred", max(ia["from_seq"], jb["from_seq"]), k,
                                 f"{ia['path']} | {jb['path']}", "",
                                 f"two texts carry {k} at the same time ({how[ta]}/{how[tb]})")
                            break
                    else:
                        continue
                    break
    stats = {"prefix": prefix, "texts": len(texts),
             "texts_by_method": dict(Counter(how[t] or "unresolved" for t in range(len(texts)))),
             "intervals_by_method": dict(Counter(r["id_method"] or "unresolved" for r in res.values())),
             "legacy_table": len(table), "legacy_numeric_rule": sorted(numeric_rule)}
    return res, out_cases, stats
