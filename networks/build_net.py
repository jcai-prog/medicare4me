#!/usr/bin/env python3
"""Merge harvested network files into the site's static network shards.

Inputs : HARVEST_DIR/*.net.tsv (npi \t plan IDs H1234-001-000,...), *.who.tsv, *.log.json
         SITE_DATA/c/*.json (plan keys present in the site, e.g. H1234_001_0)
Outputs: SITE_DATA/n/g.json          group id -> list of site plan keys (a group = one contract's plans a doctor is in)
         SITE_DATA/n/<NN>.json       npi -> [group ids]   (NN = last two digits of the NPI)
         SITE_DATA/n/coverage.json   contracts with network data, source URL, provider count, harvest date
"""
import json, os, sys, glob, collections

harvest, site = sys.argv[1], sys.argv[2]
plans = set()
for f in glob.glob(f"{site}/c/*.json"):
    plans |= set(json.load(open(f))["plans"])

def site_key(pid):                     # H1234-001-000 -> H1234_001_0
    c, p, s = pid.split("-")
    return f"{c}_{p}_{int(s)}"

npi_plans = collections.defaultdict(set)
for f in glob.glob(f"{harvest}/*.net.tsv"):
    for line in open(f):
        npi, _, ps = line.rstrip("\n").partition("\t")
        for pid in ps.split(","):
            if not pid: continue
            k = site_key(pid)
            if k in plans: npi_plans[npi].add(k)

groups, gidx = [], {}
shards = collections.defaultdict(dict)
contract_npis = collections.Counter()
for npi, ks in npi_plans.items():
    byc = collections.defaultdict(list)
    for k in ks: byc[k.split("_")[0]].append(k)
    gl = []
    for c, lst in byc.items():
        sig = tuple(sorted(lst))
        if sig not in gidx: gidx[sig] = len(groups); groups.append(list(sig))
        gl.append(gidx[sig]); contract_npis[c] += 1
    shards[npi[-2:]][npi] = sorted(gl)

os.makedirs(f"{site}/n", exist_ok=True)
for k, v in shards.items():
    json.dump(v, open(f"{site}/n/{k}.json", "w"), separators=(",", ":"))
json.dump(groups, open(f"{site}/n/g.json", "w"), separators=(",", ":"))

cov = {}
for f in glob.glob(f"{harvest}/*.log.json"):
    lg = json.load(open(f))
    for pid in lg.get("plans", []):
        c = pid.split("-")[0]
        if contract_npis.get(c):
            cov.setdefault(c, {"providers": contract_npis[c], "source": lg["url"], "date": lg.get("finished", "")[:10]})
json.dump(cov, open(f"{site}/n/coverage.json", "w"), indent=0)
print(f"providers {len(npi_plans)}, groups {len(groups)}, contracts with networks {len(cov)}")
