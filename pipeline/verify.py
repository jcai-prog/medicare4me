#!/usr/bin/env python3
"""Independent checks of site/data against the CMS originals (plain csv module, no shared code with build.py)."""
import csv, json, os, random, re, collections, sys
H = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); S = os.environ.get("M4M_SRC", f"{H}/work/src"); D = f"{H}/site/data"
out = []
def rep(name, ok, detail=""):
    out.append((name, ok, detail)); print(("PASS " if ok else "FAIL ") + name + (" — " + detail if detail else ""))

def money(s):
    s = (s or "").strip()
    if not s or s.lower().startswith("not"): return None
    v = float(re.sub(r"[^0-9.]", "", s)); return -v if s.startswith("(") else v

# load outputs
counties, plans = {}, {}
for f in os.listdir(f"{D}/c"):
    j = json.load(open(f"{D}/c/{f}")); counties.update(j["counties"]); plans.update(j["plans"])
zipmap = {}
for f in os.listdir(f"{D}/z"): zipmap.update(json.load(open(f"{D}/z/{f}")))

# 1. landscape rows -> output rows (premiums, deductible, MOOP)
loc = {}
for r in csv.DictReader(open(f"{S}/p27/geo.txt", encoding="latin-1"), delimiter="|"):
    loc[r["COUNTY_CODE"]] = (r["STATENAME"], r["COUNTY"])
outrows = {}
for ssa, c in counties.items():
    for row in c["p"]:
        outrows[(c["st"], loc[ssa][1].lower(), row[0])] = row
n = miss = bad = 0; bads = []
seen_plans = set()
for r in csv.DictReader(open(f"{S}/land/landscape.csv", encoding="utf-8-sig")):
    if r["Contract Category Type"] == "PDP": continue
    n += 1
    cps = f'{r["Contract ID"]}_{r["Plan ID"]}_{int(r["Segment ID"])}'
    seen_plans.add(cps)
    o = outrows.get((r["State Territory Abbreviation"], r["County Name"].lower(), cps))
    if not o: miss += 1; continue
    exp = [money(r["Part C Premium"]), money(r["Part D Total Premium"]), money(r["Annual Part D Deductible Amount"]), money(r["In-Network Maximum Out-of-Pocket (MOOP) Amount"])]
    if exp != o[1:5]: bad += 1; bads.append((cps, exp, o[1:5]))
rep("Landscape MA plan-county rows present in output", miss == 0, f"{n} rows, {miss} missing")
rep("Premiums, deductible, MOOP match landscape exactly", bad == 0, f"{bad} mismatches {bads[:3]}")
rep("Output has no plans absent from landscape", set(plans) <= seen_plans, f"{len(plans)} plans")

# 2. PUF vs landscape: Part D premium and deductible
puf = {}
for r in csv.DictReader(open(f"{S}/p27/plan.txt", encoding="latin-1"), delimiter="|"):
    puf[f'{r["CONTRACT_ID"]}_{r["PLAN_ID"]}_{int(r["SEGMENT_ID"])}'] = (float(r["PREMIUM"] or 0), float(r["DEDUCTIBLE"] or 0), r["FORMULARY_ID"])
landD = {}
for ssa, c in counties.items():
    for row in c["p"]: landD[row[0]] = (row[2], row[3])
pm = dm = dsnp = 0; ex = []
for k, (pd, dd) in landD.items():
    if k in puf and pd is not None:
        if abs(puf[k][0] - pd) > 0.01:
            # D-SNPs: PUF lists the premium after Extra Help ($0); the landscape lists the full Part D premium
            if plans[k]["snp"] == "D" and puf[k][0] == 0: dsnp += 1
            else: pm += 1; ex.append((k, puf[k][0], pd))
        if dd is not None and abs(puf[k][1] - dd) > 0.01: dm += 1
rep("Part D premium: PUF vs landscape", pm == 0, f"{pm} unexplained of {len(landD)}; {dsnp} D-SNPs show $0 after Extra Help in the PUF {ex[:3]}")
rep("Part D deductible: PUF vs landscape", dm == 0, f"{dm} differ")

# 3. Stars
stars = {}
rows = list(csv.reader(open(f"{S}/stars/summary.csv", encoding="utf-8-sig")))
ix = {h.strip(): i for i, h in enumerate(rows[1])}
for r in rows[2:]:
    if r and r[0].strip(): stars[r[0].strip()] = r[ix["2027 Overall"]].strip()
sb = 0
for k, p in plans.items():
    raw = stars.get(k.split("_")[0])
    got = p["st"][0] if p.get("st") else None
    want = (float(raw) if re.fullmatch(r"[0-9.]+", raw or "") else raw) if raw is not None else None
    if got != want: sb += 1
rep("2027 overall Star Rating matches CMS table for every plan", sb == 0, f"{sb} mismatches")

# 4. Formulary: random sample of 3,000 raw rows -> drug shard tier/flags
form = json.load(open(f"{D}/meta.json"))["formularies"]; fidx = {f: i for i, f in enumerate(form)}
raw = list(csv.DictReader(open(f"{S}/p27/formulary.txt", encoding="latin-1"), delimiter="|"))
random.seed(7); fb = 0; bucket = {}
mins = collections.defaultdict(lambda: 99)
for r in raw: mins[(r["FORMULARY_ID"], r["RXCUI"])] = min(mins[(r["FORMULARY_ID"], r["RXCUI"])], int(r["TIER_LEVEL_VALUE"]))
for r in random.sample(raw, 3000):
    rx = int(r["RXCUI"]); b = rx % 256
    if b not in bucket: bucket[b] = json.load(open(f"{D}/d/{b}.json"))
    ent = [e for e in bucket[b][str(rx)]["f"] if e[0] == fidx[r["FORMULARY_ID"]]]
    if not ent or ent[0][1] != mins[(r["FORMULARY_ID"], r["RXCUI"])]: fb += 1
    elif r["PRIOR_AUTHORIZATION_YN"] == "Y" and not ent[0][2] & 1: fb += 1
rep("Formulary tiers and prior-auth flags (3,000 random rows)", fb == 0, f"{fb} mismatches")
rxs = {r["RXCUI"] for r in raw}
idx = {str(x[0]) for x in json.load(open(f"{D}/drugs.json"))}
rep("Every 2027 formulary drug is searchable", rxs <= idx, f"{len(rxs)} drugs, {len(rxs - idx)} missing")

# 5. Plan -> formulary link
fl = sum(1 for k, p in plans.items() if p["pd"] and form[p["f"]] != puf[k][2])
rep("Each drug plan points at its CMS formulary", fl == 0, f"{fl} mismatches")

# 6. ZIP spot checks
for z, want in [("07030", "Hudson"), ("90210", "Los Angeles"), ("33139", "Miami-Dade"), ("10001", "New York"), ("60601", "Cook")]:
    got = [r[1] for r in zipmap.get(z, [])]
    rep(f"ZIP {z} -> {want}", want in got, str(got))
json.dump(out, open(f"{H}/site/verify_report.json", "w"), indent=1)
sys.exit(0 if all(o[1] for o in out) else 1)
