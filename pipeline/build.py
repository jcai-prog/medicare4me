#!/usr/bin/env python3
"""Medicare4Me data build.

Inputs (CMS originals, unmodified):
  src/land/.../CY2027_Landscape_202609.csv          plans by county, premiums, MOOP, deductible
  src/p27/*.txt                                      CY2027 Part D PUF (Oct 2026): plans, formularies, cost sharing
  src/price/pricing.txt               2026 Q3 plan drug prices (latest public prices)
  src/price/formulary.txt + plan information (to map 2026 NDC -> RxCUI, flags)
  src/enr/.../SCC_Enrollment_MA_2026_09.csv          SSA <-> FIPS county codes
  src/geo/zcta_county.txt            ZIP (ZCTA) -> county
  src/stars/2027 ... Summary Ratings.csv             2027 Star Ratings
  src/rxnorm_current_concepts.txt                    NLM RxNorm names (SCD/SBD/GPCK/BPCK)

Output: site/data/*.json  (static shards; the browser does all cost math)
"""
import csv, json, os, re, statistics, collections, gzip, sys
import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("M4M_SRC", os.path.join(ROOT, "work", "src"))
OUT = os.environ.get("M4M_OUT", os.path.join(ROOT, "site", "data"))
os.makedirs(OUT, exist_ok=True)
for d in ("z", "c", "d"):
    os.makedirs(os.path.join(OUT, d), exist_ok=True)

LAND = f"{SRC}/land/landscape.csv"
P27 = f"{SRC}/p27"
S26 = f"{SRC}/price"
ENR = f"{SRC}/enr/scc.csv"
ZCTA = f"{SRC}/geo/zcta_county.txt"
STARS = f"{SRC}/stars/summary.csv"
RXN = f"{SRC}/rxnorm_current_concepts.txt"

db = duckdb.connect()
MAN = json.load(open(f"{SRC}/manifest.json")) if os.path.exists(f"{SRC}/manifest.json") else {}
def rd(path, delim="|"):
    return f"read_csv('{path}', delim='{delim}', header=true, all_varchar=true, quote='\"')"

def money(s):
    if s is None: return None
    s = s.strip()
    if not s or s.lower().startswith("not"): return None
    neg = s.startswith("(")
    v = float(re.sub(r"[^0-9.]", "", s) or 0)
    return -v if neg else v

def write(rel, obj):
    p = os.path.join(OUT, rel)
    with open(p, "w") as fh:
        json.dump(obj, fh, separators=(",", ":"))
    return os.path.getsize(p)

# ---------------------------------------------------------------- geography
STATE_ABBR = {}
loc = db.sql(f"select * from {rd(P27 + '/geo.txt')}").fetchall()
# COUNTY_CODE|STATENAME|COUNTY|...
ssa_info = {}  # ssa -> (state name, county)
for r in loc:
    ssa_info[r[0]] = (r[1], r[2])

def cnorm(n):
    n = n.lower().replace("&", "and").replace(".", "").replace("'", "")
    n = re.sub(r"\b(county|parish|borough|census area|city and borough|municipality|municipio)\b", "", n)
    n = n.replace("saint ", "st ").replace("ste ", "ste ")
    return re.sub(r"[^a-z0-9]", "", n)

fips2ssa = collections.defaultdict(set)
ssa2st = {}
with open(ENR, encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh):
        s, f = r["SSA Code"].strip(), r["FIPS Code"].strip()
        if s and f and s.isdigit():
            fips2ssa[f].add(s)
            ssa2st[s] = r["State"].strip()
            STATE_ABBR[ssa_info.get(s, ("", ""))[0]] = r["State"].strip()

zips = collections.defaultdict(list)
fips_name = {}
with open(ZCTA, encoding="utf-8-sig") as fh:
    for r in csv.DictReader(fh, delimiter="|"):
        z, f = r["GEOID_ZCTA5_20"], r["GEOID_COUNTY_20"]
        if not z or not f: continue
        fips_name[f] = r["NAMELSAD_COUNTY_20"]
        tot = float(r["AREALAND_ZCTA5_20"] or 0); part = float(r["AREALAND_PART"] or 0)
        zips[z].append((f, part / tot if tot else 0))

# ---------------------------------------------------------------- stars
stars = {}
with open(STARS, encoding="utf-8-sig") as fh:
    rows = list(csv.reader(fh))
hdr = rows[1]
ix = {h.strip(): i for i, h in enumerate(hdr)}
for r in rows[2:]:
    if not r or not r[0].strip(): continue
    def sv(col):
        v = r[ix[col]].strip()
        try: return float(v)
        except ValueError: return v  # "Plan too new to be measured" etc.
    stars[r[0].strip()] = [sv("2027 Overall"), sv("2027 Part C Summary"), sv("2027 Part D Summary")]

# ---------------------------------------------------------------- landscape (MA plans only)
land = db.sql(f"""select * from {rd(LAND, ',')}
  where "Contract Category Type" <> 'PDP' """).df()
name2ssa = {}
for s, (stn, cn) in ssa_info.items():
    name2ssa[(STATE_ABBR.get(stn, stn), cnorm(cn))] = s

plans = {}           # cps -> plan record (county-independent fields)
county_plans = collections.defaultdict(list)  # ssa -> [(cps, premC, premD, ...)]
unmatched = collections.Counter()
land_cols = list(land.columns)
L = {c: i for i, c in enumerate(land_cols)}
for row in land.itertuples(index=False, name=None):
    g = lambda c: row[L[c]]
    st, cn = g("State Territory Abbreviation"), g("County Name")
    ssa = name2ssa.get((st, cnorm(cn)))
    if not ssa:
        unmatched[(st, cn)] += 1
        continue
    cid, pid, seg = g("Contract ID"), g("Plan ID"), g("Segment ID")
    cps = f"{cid}_{pid}_{int(seg)}"
    if cps not in plans:
        plans[cps] = {
            "n": g("Plan Name"), "o": g("Organization Marketing Name"), "par": g("Parent Organization Name"),
            "t": g("Plan Type"), "snp": {"Not Applicable": "", "Dual-Eligible": "D", "Chronic or Disabling Condition": "C", "Institutional": "I"}.get(g("SNP Type"), "?"),
            "pd": 1 if g("Part D Coverage Indicator") == "Yes" else 0,
            "cat": g("Contract Category Type"),
            "sanc": 1 if g("Sanctioned Plan") == "Yes" else 0,
            "st": stars.get(cid),
        }
    county_plans[ssa].append([cps, money(g("Part C Premium")), money(g("Part D Total Premium")),
                              money(g("Annual Part D Deductible Amount")), money(g("In-Network Maximum Out-of-Pocket (MOOP) Amount"))])
print("landscape MA plan-county rows:", sum(len(v) for v in county_plans.values()), "plans", len(plans),
      "unmatched counties", len(unmatched), list(unmatched.items())[:10], file=sys.stderr)

# ---------------------------------------------------------------- 2027 PUF plan info -> formulary, PUF premium/deductible (check)
pinfo = db.sql(f"""select CONTRACT_ID, PLAN_ID, SEGMENT_ID, FORMULARY_ID, PREMIUM, DEDUCTIBLE, COUNTY_CODE, PLAN_SUPPRESSED_YN
  from {rd(P27 + '/plan.txt')}""").fetchall()
formulary_of = {}
puf_counties = collections.defaultdict(set)
puf_prem = {}
for c_, p_, s_, f_, prem, ded, cc, sup in pinfo:
    cps = f"{c_}_{p_}_{int(s_)}"
    formulary_of[cps] = f_
    puf_counties[cps].add(cc)
    puf_prem[cps] = (float(prem or 0), float(ded or 0), sup)
fids = sorted(set(formulary_of.values()))
fidx = {f: i for i, f in enumerate(fids)}
missing_form = [k for k, v in plans.items() if v["pd"] and k not in formulary_of]
print("MA-PD plans without PUF formulary:", len(missing_form), missing_form[:10], file=sys.stderr)
for k, v in plans.items():
    if k in formulary_of:
        v["f"] = fidx[formulary_of[k]]
        v["sup"] = 1 if puf_prem[k][2] == "Y" else 0

# ---------------------------------------------------------------- cost sharing (2027)
def num(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0
CH = ["PREF", "NONPREF", "MAIL_PREF", "MAIL_NONPREF"]
cost = collections.defaultdict(lambda: {})
for r in db.sql(f"select * from {rd(P27 + '/cost.txt')}").fetchall():
    (c_, p_, s_, lvl, tier, days) = r[:6]
    cps = f"{c_}_{p_}_{int(s_)}"
    if days not in ("1", "2"): continue  # 1 = one-month supply, 2 = three-month supply
    ch = []
    for k in range(4):
        b = 6 + 4 * k
        ch.append([int(r[b] or 0), num(r[b + 1]), num(r[b + 2]), num(r[b + 3])])
    spec, ded_applies = r[22], r[23]
    t = cost[cps].setdefault(tier, {"sp": 1 if spec == "Y" else 0, "da": 1 if ded_applies == "Y" else 0})
    t[f"{lvl}_{days}"] = ch
insulin = collections.defaultdict(dict)
for r in db.sql(f"select * from {rd(P27 + '/insulin.txt')}").fetchall():
    c_, p_, s_, tier, days = r[:5]
    if days not in ("1", "2"): continue
    cps = f"{c_}_{p_}_{int(s_)}"
    cop = [None if (v or "").strip() == "" else float(v) for v in r[5:9]]
    coi = [None if (v or "").strip() == "" else float(v) for v in r[9:13]]
    insulin[cps][f"{tier}_{days}"] = [cop, coi]

# ---------------------------------------------------------------- drug names
rxname = {}
with open(RXN) as fh:
    next(fh)
    for line in fh:
        p = line.rstrip("\n").split("|", 2)
        if len(p) == 3: rxname[p[0]] = (p[1], p[2])

# ---------------------------------------------------------------- 2027 formulary by drug
f27 = db.sql(f"""select FORMULARY_ID, RXCUI, min(TIER_LEVEL_VALUE) tier,
     max(QUANTITY_LIMIT_YN) ql, max(PRIOR_AUTHORIZATION_YN) pa, max(STEP_THERAPY_YN) st, max(SELECTED_DRUG_YN) sel,
     max(case when QUANTITY_LIMIT_YN='Y' then try_cast(QUANTITY_LIMIT_AMOUNT as double) end) qla,
     max(case when QUANTITY_LIMIT_YN='Y' then try_cast(QUANTITY_LIMIT_DAYS as double) end) qld
   from {rd(P27 + '/formulary.txt')} group by 1,2""").fetchall()
drugs = collections.defaultdict(lambda: {"f": [], "sel": 0, "ql30": []})
for fid, rx, tier, ql, pa, st, sel, qla, qld in f27:
    flags = (1 if pa == "Y" else 0) | (2 if st == "Y" else 0) | (4 if ql == "Y" else 0)
    e = [fidx[fid], int(tier), flags]
    if ql == "Y" and qla and qld:
        e += [qla, int(qld)]
        drugs[rx]["ql30"].append(round(qla * 30 / qld, 2))
    drugs[rx]["f"].append(e)
    if sel == "Y": drugs[rx]["sel"] = 1

# ---------------------------------------------------------------- 2026 prices -> (contract, rxcui) median unit cost (30-day)
print("pricing aggregation...", file=sys.stderr)
db.sql(f"""create table n2r as select distinct NDC, RXCUI from {rd(S26 + '/formulary.txt')}""")
db.sql(f"""create table sel26 as select distinct RXCUI from {rd(S26 + '/formulary.txt')} where SELECTED_DRUG_YN='Y'""")
db.sql(f"""create table pr as select p.CONTRACT_ID c, n.RXCUI r, median(try_cast(p.UNIT_COST as double)) u, count(*) k
   from {rd(S26 + '/pricing.txt')} p join n2r n on p.NDC = n.NDC
   where p.DAYS_SUPPLY = '30' and try_cast(p.UNIT_COST as double) > 0
   group by 1, 2""")
nat = dict(db.sql("select r, median(u) from pr group by 1").fetchall())
price = collections.defaultdict(dict)
for c_, r_, u, k in db.sql("select c, r, u, k from pr").fetchall():
    price[r_][c_] = round(u, 4)
sel26 = {r[0] for r in db.sql("select RXCUI from sel26").fetchall()}

# 2027 negotiated-price discounts (CMS IPAY 2027 infographic: % below 2024 list price, 30-day supply)
MFP27 = [  # order matters: more specific first
    ("umeclidinium", 0.73), ("vilanterol", 0.83), ("deutetrabenazine", 0.38), ("acalabrutinib", 0.40),
    ("palbociclib", 0.50), ("sitagliptin", 0.85), ("linaclotide", 0.75), ("nintedanib", 0.50),
    ("apremilast", 0.65), ("semaglutide", 0.71), ("pomalidomide", 0.60), ("linagliptin", 0.84),
    ("cariprazine", 0.44), ("rifaximin", 0.63), ("enzalutamide", 0.48),
]
mfp_applied = []

# ---------------------------------------------------------------- drug shards
ORAL = re.compile(r"\b(Oral Tablet|Oral Capsule|Tablet|Capsule|Chewable|Disintegrating|Sublingual)\b", re.I)
def unit_label(nm):
    n = nm.lower()
    if re.search(r"tablet|capsule|lozenge|troche|wafer", n): return "tablets/capsules"
    if re.search(r"\bml\b|injection|solution|suspension|syrup|elixir|pen|syringe|cartridge", n): return "mL"
    if re.search(r"inhal|aerosol|powder", n): return "units"
    if re.search(r"patch|transdermal", n): return "patches"
    if re.search(r"cream|ointment|gel|lotion", n): return "grams"
    return "units"

index = []
buckets = collections.defaultdict(dict)
NB = 256
for rx, d in drugs.items():
    tty, nm = rxname.get(rx, ("?", f"RxCUI {rx}"))
    ql = d["ql30"]
    if ORAL.search(nm):
        q = 30          # shopper sets their own count; 30 = one a day
    elif ql:
        q = collections.Counter(ql).most_common(1)[0][0]
    else:
        q = 1
    p = price.get(rx, {})
    pm = nat.get(rx)
    adj = None
    if d["sel"] and rx not in sel26:
        low = nm.lower()
        for ing, disc in MFP27:
            if ing in low:
                adj = disc; break
        if adj is not None:
            mfp_applied.append((rx, nm, adj))
            p = {k: round(v * (1 - adj), 4) for k, v in p.items()}
            pm = pm * (1 - adj) if pm else None
    rec = {"n": nm, "u": unit_label(nm), "q": q, "f": d["f"], "p": p, "pm": round(pm, 4) if pm else None}
    if "insulin" in nm.lower(): rec["ins"] = 1
    if d["sel"]: rec["sel"] = 1
    if adj is not None: rec["mfp"] = adj
    buckets[int(rx) % NB][rx] = rec
    index.append([int(rx), nm, tty])
index.sort(key=lambda x: x[1].lower())
dsz = [write(f"d/{b}.json", v) for b, v in buckets.items()]
print("drug buckets", len(dsz), "max KB", max(dsz) // 1024, "MFP-adjusted", len(mfp_applied), file=sys.stderr)
write("drugs.json", index)

# ---------------------------------------------------------------- county shards (state-chunked)
county_files = {}
by_state = collections.defaultdict(list)
for ssa in county_plans:
    by_state[ssa2st.get(ssa) or STATE_ABBR.get(ssa_info[ssa][0], "XX")].append(ssa)
LIMIT = 900_000
for st, ssas in by_state.items():
    ssas.sort()
    chunk, cplans, size, n = {}, {}, 0, 0
    chunks = []
    cur = {"counties": {}, "plans": {}}
    for ssa in ssas:
        rows = county_plans[ssa]
        add = {}
        for row in rows:
            cps = row[0]
            if cps not in cur["plans"]:
                pr_ = dict(plans[cps])
                pr_["cs"] = cost.get(cps)
                if cps in insulin: pr_["ins"] = insulin[cps]
                add[cps] = pr_
        cur["counties"][ssa] = {"name": ssa_info[ssa][1], "st": st, "p": rows}
        cur["plans"].update(add)
        if len(json.dumps(cur, separators=(",", ":"))) > LIMIT and len(cur["counties"]) > 1:
            # move this county into a fresh chunk
            del cur["counties"][ssa]
            for k in add: cur["plans"].pop(k, None)
            chunks.append(cur)
            cur = {"counties": {ssa: {"name": ssa_info[ssa][1], "st": st, "p": rows}}, "plans": add}
    chunks.append(cur)
    for i, ch in enumerate(chunks):
        fn = f"{st}{i}"
        write(f"c/{fn}.json", ch)
        for s in ch["counties"]: county_files[s] = fn
csz = [os.path.getsize(os.path.join(OUT, "c", f)) for f in os.listdir(os.path.join(OUT, "c"))]
print("county files", len(csz), "max KB", max(csz) // 1024, file=sys.stderr)

# ---------------------------------------------------------------- zip shards (by first 2 digits)
zb = collections.defaultdict(dict)
nozip = 0
for z, parts in zips.items():
    out = []
    for f, share in sorted(parts, key=lambda x: -x[1]):
        for ssa in sorted(fips2ssa.get(f, [])):
            if ssa in county_files:
                out.append([ssa, ssa_info[ssa][1], ssa2st.get(ssa, ""), county_files[ssa], round(share, 3)])
    if out: zb[z[:2]][z] = out
    else: nozip += 1
for k, v in zb.items(): write(f"z/{k}.json", v)
print("zip files", len(zb), "zips without plans", nozip, file=sys.stderr)

# ---------------------------------------------------------------- meta
meta = {
    "year": 2027, "prices_file": MAN.get("prices", "").rsplit("/", 1)[-1], "prices_year": MAN.get("prices_year"), "oop_cap": 2400, "insulin_cap": 35, "formularies": fids,
    "built": __import__("datetime").date.today().isoformat(),
    "sources": {
        "landscape": "CMS CY2027 MA Landscape (Sep 2026 release, ReadMe 09/30/2026)",
        "puf": "CMS Monthly Prescription Drug Plan Formulary and Pharmacy Network Information: " + MAN.get("puf", "").rsplit("/", 1)[-1],
        "prices": "CMS Quarterly Prescription Drug Plan Formulary, Pharmacy Network, and Pricing Information: " + MAN.get("prices", "").rsplit("/", 1)[-1],
        "stars": "CMS 2027 Star Ratings data tables (Oct 8, 2026)",
        "rxnorm": "NLM RxNorm via RxNav, current concepts (Oct 24, 2026)",
        "geo": "Census 2020 ZCTA-to-county relationship file; CMS SSA/FIPS crosswalk (MA enrollment by county, Sep 2026)",
        "mfp": "CMS negotiated prices for initial price applicability year 2027 (infographic)",
    },
    "mfp_applied": [[r, n, a] for r, n, a in mfp_applied],
}
write("meta.json", meta)

# diagnostics for verification
json.dump({"unmatched": [[a, b, n] for (a, b), n in unmatched.items()], "missing_form": missing_form},
          open(os.path.join(OUT, "..", "build_diag.json"), "w"), indent=1)
print("done", file=sys.stderr)
