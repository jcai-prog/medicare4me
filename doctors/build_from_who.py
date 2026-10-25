#!/usr/bin/env python3
"""Fallback doctor search index from the harvested insurer directories (used when the NPPES file is unavailable).
Only clinicians listed in at least one harvested network are searchable this way.

Usage: build_from_who.py HARVEST_DIR ZCTA_COUNTY_FILE NUCC_CSV SITE_DATA
"""
import csv, glob, json, os, re, sys, collections
h, zcta, nucc, site = sys.argv[1:5]
ST = {"01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE","11":"DC","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA","20":"KS","21":"KY","22":"LA","23":"ME","24":"MD","25":"MA","26":"MI","27":"MN","28":"MS","29":"MO","30":"MT","31":"NE","32":"NV","33":"NH","34":"NJ","35":"NM","36":"NY","37":"NC","38":"ND","39":"OH","40":"OK","41":"OR","42":"PA","44":"RI","45":"SC","46":"SD","47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA","54":"WV","55":"WI","56":"WY","72":"PR","66":"GU","78":"VI"}
zst = {}
for r in csv.DictReader(open(zcta, encoding="utf-8-sig"), delimiter="|"):
    if r["GEOID_ZCTA5_20"] and r["GEOID_COUNTY_20"]: zst.setdefault(r["GEOID_ZCTA5_20"], ST.get(r["GEOID_COUNTY_20"][:2]))
tax_name = {}
if os.path.exists(nucc) and os.path.getsize(nucc) > 1000:
    for r in csv.DictReader(open(nucc, encoding="utf-8-sig", errors="replace")):
        if r.get("Code"): tax_name[r["Code"].strip()] = (r.get("Display Name") or r.get("Classification") or "").strip()
who = {}
for f in glob.glob(f"{h}/*.who.tsv"):
    for line in open(f, encoding="utf-8", errors="replace"):
        p = line.rstrip("\n").split("\t")
        if len(p) < 5 or not re.fullmatch(r"\d{10}", p[0]) or not p[1].strip(): continue
        w = who.setdefault(p[0], [p[1].strip().title(), p[2].strip().title(), set(), set()])
        w[2] |= {t for t in p[3].split(",") if t}; w[3] |= {z for z in p[4].split(",") if re.fullmatch(r"\d{5}", z)}
out = collections.defaultdict(list); used = set()
for npi, (last, first, tax, zips) in who.items():
    states = {zst.get(z) for z in zips if zst.get(z)}
    t = sorted(tax)[0] if tax else ""
    used.add(t)
    for st in states:
        z = sorted(x for x in zips if zst.get(x) == st)[0]
        letter = re.sub(r"[^A-Z]", "", last.upper())[:1] or "_"
        out[(st, letter)].append([npi, last, first, "", t, "", z])
os.makedirs(f"{site}/doc", exist_ok=True)
for (st, letter), rows in out.items():
    os.makedirs(f"{site}/doc/{st}", exist_ok=True)
    rows.sort(key=lambda x: (x[1], x[2]))
    json.dump(rows, open(f"{site}/doc/{st}/{letter}.json", "w"), separators=(",", ":"))
json.dump({t: tax_name.get(t, "") for t in sorted(used) if t}, open(f"{site}/doc/tax.json", "w"), separators=(",", ":"))
print(f"clinicians {len(who)}, index files {len(out)}")
