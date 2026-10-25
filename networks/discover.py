#!/usr/bin/env python3
"""Find each 2027 MA contract's public Medicare Plan Finder (MPF) provider directory index.

Sources of candidate URLs:
  1. networks/seeds.json            curated URLs (one per contract, plan, or a {C}-template for an organization)
  2. CMS National Provider Directory Endpoint file (directory.cms.gov), which lists some plans' MPF/FHIR URLs
Each URL that embeds a contract ID becomes a template; the template is tried for every other contract of
the same parent organization (insurers host all their contracts the same way). Every candidate is fetched
and kept only if it returns JSON.

Usage: discover.py CONTRACTS_JSON OUT_SOURCES_JSON [--npd PATH_TO_03-Endpoint.ndjson.zst]
CONTRACTS_JSON: {"H1234": {"parent": "...", "plans": ["H1234-001-000", ...]}, ...}
"""
import json, re, sys, os, io, urllib.request, concurrent.futures, collections, time

UA = {"User-Agent": "Medicare4Me-directory-harvester/1.0 (+https://github.com/jcai-prog/medicare4me)",
      "Accept": "application/json, application/fhir+json;q=0.9, */*;q=0.5"}
C_RE = re.compile(r"([HhRr]\d{4})")
P_RE = re.compile(r"([HhRr]\d{4})-(\d{3})-(\d{3})")

def npd_urls(path):
    import zstandard
    out = []
    with open(path, "rb") as fh:
        rdr = io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(fh), encoding="utf-8")
        for line in rdr:
            if "direct-project" in line: continue
            if not re.search(r"mpf|manifest|index\.json|plan-?net|provider-?directory|providerdirectory", line, re.I): continue
            try: o = json.loads(line)
            except Exception: continue
            a = o.get("address", "")
            if a.startswith("http"): out.append(a)
    return sorted(set(out))

def probe(url):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45)
        head = r.read(2048).decode("utf-8", "replace").lstrip()
        ok = r.status == 200 and head[:1] in "{["
        return url, ok, head[:200]
    except Exception as e:
        return url, False, repr(e)[:200]

def main():
    contracts = json.load(open(sys.argv[1]))
    out_path = sys.argv[2]
    npd = sys.argv[sys.argv.index("--npd") + 1] if "--npd" in sys.argv else None
    seeds = json.load(open(os.path.join(os.path.dirname(__file__), "seeds.json")))
    by_parent = collections.defaultdict(list)
    for c, v in contracts.items(): by_parent[v["parent"]].append(c)

    raw = [s["url"] for s in seeds.get("mpf", [])]
    if npd and os.path.exists(npd):
        raw += npd_urls(npd)
    templates, literal = set(), set()
    for u in raw:
        m = C_RE.search(u.split("//", 1)[-1])
        if "{C}" in u or "{c}" in u or "{P}" in u:
            templates.add(u); continue
        if not m: literal.add(u); continue
        c = m.group(1).upper()
        if c not in contracts: literal.add(u); continue
        pm = P_RE.search(u)
        if pm:   # plan-level URL: template over every plan of every contract of the parent
            templates.add(u.replace(pm.group(0), "{P}"))
            templates.add(u.replace(pm.group(0), "{C}"))      # some hosts also answer at contract level
        else:
            lower = m.group(1).islower()
            templates.add(u.replace(m.group(1), "{c}" if lower else "{C}"))
        literal.add(u)  # keep the original URL too
    # expand templates over the parent organization's contracts (or all contracts for organization-free templates)
    cands = {}
    for t in templates:
        m = C_RE.search(t.split("//", 1)[-1])
        if "{" not in t:
            c = m.group(1).upper() if m else None
            cands[t] = [c] if c else []
            continue
        # which parent does this template belong to? any raw URL that matches it
        parents = set()
        rx = re.escape(t).replace(r"\{C\}", r"([HR]\d{4})").replace(r"\{c\}", r"([hr]\d{4})").replace(r"\{P\}", r"([HR]\d{4})-\d{3}-\d{3}")
        for u in raw:
            mm = re.fullmatch(rx, u)
            if mm and mm.group(1).upper() in contracts: parents.add(contracts[mm.group(1).upper()]["parent"])
        for s in seeds.get("mpf", []):
            if s["url"] == t and s.get("parent"): parents.add(s["parent"])
        for p in parents:
            for c in by_parent[p]:
                if "{P}" in t:
                    for pid in contracts[c]["plans"]: cands[t.replace("{P}", pid)] = [c]
                else:
                    cands[t.replace("{C}", c).replace("{c}", c.lower())] = [c]
    for u in literal: cands.setdefault(u, [])
    print(f"{len(raw)} raw URLs, {len(templates)} templates, {len(cands)} candidates", flush=True)

    found = []
    with concurrent.futures.ThreadPoolExecutor(24) as ex:
        for url, ok, head in ex.map(probe, list(cands)):
            if ok: found.append({"url": url, "contracts": cands[url], "head": head})
    # one source per URL; a contract may have several (plan-level manifests)
    srcs, seen = [], set()
    for f in sorted(found, key=lambda x: x["url"]):
        url = f["url"]
        kind = "mpf"
        if re.search(r"/metadata(\?.*)?$", url) or ('"CapabilityStatement"' in f["head"]):
            url, kind = re.sub(r"/metadata(\?.*)?$", "", url).rstrip("/"), "fhir"   # a Plan-Net API, not an MPF file
        if url in seen: continue
        seen.add(url)
        key = re.sub(r"[^A-Za-z0-9]+", "_", url.split("//", 1)[-1])[-90:]
        srcs.append({"key": key, "url": url, "kind": kind, "contracts": f["contracts"]})
    for s in seeds.get("fhir", []):
        if s["url"] in seen: continue
        srcs.append({"key": s["key"], "url": s["url"], "kind": "fhir", "parent": s.get("parent"), "contracts": by_parent.get(s.get("parent"), [])})
    covered = {c for s in srcs for c in s["contracts"]}
    json.dump(srcs, open(out_path, "w"), indent=1)
    print(f"sources {len(srcs)}; contracts covered {len(covered)} of {len(contracts)}", flush=True)

if __name__ == "__main__":
    main()
