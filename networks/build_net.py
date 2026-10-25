#!/usr/bin/env python3
"""Merge harvested network files into the site's static network shards (memory-lean: plan lists are interned).

Inputs : HARVEST_DIR/*.net.tsv (npi \t comma-separated plan IDs H1234-001-000), *.log.json
         SITE_DATA/c/*.json (plan keys present in the site, e.g. H1234_001_0)
Outputs: SITE_DATA/n/g.json          group id -> list of site plan keys (one contract's plans)
         SITE_DATA/n/<NN>.json       npi -> [group ids]   (NN = last two digits of the NPI)
         SITE_DATA/n/coverage.json   contract -> {providers, source, date, plans: [plan keys with complete data]}
"""
import json, os, sys, glob, collections, array

harvest, site = sys.argv[1], sys.argv[2]
plans = set()
for f in glob.glob(f"{site}/c/*.json"):
    plans |= set(json.load(open(f))["plans"])

def site_key(pid):
    c, p, s = pid.split("-")
    return f"{c}_{p}_{int(s)}"

# intern each distinct plan list -> {contract: tuple(site keys)}; then each list -> group ids
list_id, list_groups = {}, []
groups, gidx = [], {}
def groups_for(planlist):
    lid = list_id.get(planlist)
    if lid is not None: return lid
    byc = collections.defaultdict(list)
    for pid in planlist.split(","):
        if not pid: continue
        try: k = site_key(pid)
        except ValueError: continue
        if k in plans: byc[k.split("_")[0]].append(k)
    gl = []
    for c, lst in byc.items():
        sig = tuple(sorted(lst))
        if sig not in gidx: gidx[sig] = len(groups); groups.append(list(sig))
        gl.append(gidx[sig])
    list_id[planlist] = len(list_groups); list_groups.append(tuple(sorted(gl)))
    return list_id[planlist]

npi_lists = collections.defaultdict(set)     # npi -> set of interned list ids
for f in sorted(glob.glob(f"{harvest}/*.net.tsv")):
    with open(f) as fh:
        for line in fh:
            npi, _, ps = line.rstrip("\n").partition("\t")
            if len(npi) == 10 and ps: npi_lists[npi].add(groups_for(ps))

shards = collections.defaultdict(dict)
contract_npis = collections.Counter()
for npi, lids in npi_lists.items():
    gs = sorted({g for l in lids for g in list_groups[l]})
    if not gs: continue
    shards[npi[-2:]][npi] = gs
    for c in {groups[g][0].split("_")[0] for g in gs}: contract_npis[c] += 1
del npi_lists

os.makedirs(f"{site}/n", exist_ok=True)
for k, v in shards.items():
    json.dump(v, open(f"{site}/n/{k}.json", "w"), separators=(",", ":"))
json.dump(groups, open(f"{site}/n/g.json", "w"), separators=(",", ":"))

cov, bad = {}, set()
logs = []
for f in glob.glob(f"{harvest}/*.log.json"):
    try: logs.append(json.load(open(f)))
    except Exception: pass
for lg in logs: bad |= set(lg.get("incomplete_plans", []))
for lg in logs:
    if any(str(p).startswith("FAILED") for p in lg.get("problems", [])): continue
    for pid in lg.get("plans", []):
        if pid in bad: continue
        try: k = site_key(pid)
        except ValueError: continue
        c = k.split("_")[0]
        if k not in plans or not contract_npis.get(c): continue
        e = cov.setdefault(c, {"providers": contract_npis[c], "source": lg["url"], "date": lg.get("finished", "")[:10], "plans": []})
        if k not in e["plans"]: e["plans"].append(k)
json.dump(cov, open(f"{site}/n/coverage.json", "w"), separators=(",", ":"))
print(f"providers {sum(len(v) for v in shards.values())}, plan lists {len(list_groups)}, groups {len(groups)}, "
      f"contracts with networks {len(cov)}, plans with networks {sum(len(v['plans']) for v in cov.values())}")
