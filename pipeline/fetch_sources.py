#!/usr/bin/env python3
"""Lay out every input build.py needs under work/src, downloading the large CMS files fresh.

Committed in sources/ (small, change once a year): CY2027 MA landscape, 2027 Star Ratings summary,
MA enrollment by county (SSA/FIPS crosswalk), Census ZCTA-to-county file.
Downloaded each run: latest CY2027 monthly Part D formulary file and the latest quarterly file with drug
prices (2027 when CMS posts it, otherwise the newest 2026), from the data.cms.gov catalog; RxNorm names
from NLM RxNav.
"""
import io, json, os, re, shutil, sys, urllib.request, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.environ.get("M4M_SRC", f"{ROOT}/work/src")
DL = os.environ.get("M4M_DL", f"{ROOT}/work/dl")
YEAR = os.environ.get("M4M_YEAR", "2027")
UA = {"User-Agent": "Medicare4Me-build/1.0 (+https://github.com/jcai-prog/medicare4me)"}
for d in ("land", "enr", "geo", "stars", "p27", "price"): os.makedirs(f"{SRC}/{d}", exist_ok=True)
os.makedirs(DL, exist_ok=True)

def fetch(url, path):
    if os.path.exists(path) and os.path.getsize(path) > 0: return path
    print("download", url, flush=True)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=600) as r, open(path + ".part", "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(path + ".part", path); return path

def one_member(zpath, pattern, dest):
    z = zipfile.ZipFile(zpath)
    name = [n for n in z.namelist() if re.search(pattern, n, re.I)][0]
    with z.open(name) as s, open(dest, "wb") as d: shutil.copyfileobj(s, d, 1 << 20)
    return name

S = f"{ROOT}/sources"
one_member(f"{S}/cy2027-landscape-202609-1.zip", r"Landscape_\d+\.csv$", f"{SRC}/land/landscape.csv")
one_member(f"{S}/ma-enrollment-state-county-contract-september-2026-full-version.zip", r"\.csv$", f"{SRC}/enr/scc.csv")
shutil.copy(f"{S}/tab20_zcta520_county20_natl.txt", f"{SRC}/geo/zcta_county.txt")
shutil.copy(f"{S}/stars-2027-summary.csv", f"{SRC}/stars/summary.csv")

# ---- Part D files from the data.cms.gov catalog
cat = json.load(urllib.request.urlopen(urllib.request.Request("https://data.cms.gov/data.json", headers=UA), timeout=300))
def latest(title_re, file_re):
    best = None
    for d in cat["dataset"]:
        if not re.search(title_re, d.get("title", "")): continue
        for x in d.get("distribution", []):
            u = x.get("downloadURL") or x.get("accessURL") or ""
            m = re.search(file_re, u)
            if m and (best is None or m.group(1) > best[0]): best = (m.group(1), u)
    return best
puf = latest(r"^Monthly Prescription Drug Plan Formulary", rf"/{YEAR}_(\d{{8}})[^/]*\.zip$")
spuf = latest(r"^Quarterly Prescription Drug Plan Formulary", rf"/SPUF_{YEAR}_(\d{{8}})\.zip$") or \
       latest(r"^Quarterly Prescription Drug Plan Formulary", rf"/SPUF_{int(YEAR) - 1}_(\d{{8}})\.zip$")
print("PUF", puf, "\nSPUF", spuf, flush=True)

def inner(zpath, pat, dest):
    """The CMS zips hold one zip per file; pull the text file out of the inner zip matching pat."""
    z = zipfile.ZipFile(zpath)
    iname = [n for n in z.namelist() if re.search(pat, n, re.I) and "sample" not in n.lower()][0]
    with z.open(iname) as f: iz = zipfile.ZipFile(io.BytesIO(f.read()))
    tname = [n for n in iz.namelist() if n.lower().endswith(".txt") and "sample" not in n.lower()][0]
    with iz.open(tname) as s, open(dest, "wb") as d: shutil.copyfileobj(s, d, 1 << 20)
    return iname

pz = fetch(puf[1], f"{DL}/{puf[1].rsplit('/', 1)[1]}")
for pat, name in [(r"^geographic locator", "geo.txt"), (r"^plan information", "plan.txt"), (r"^beneficiary cost", "cost.txt"),
                  (r"^insulin beneficiary", "insulin.txt"), (r"^basic drugs formulary", "formulary.txt")]:
    inner(pz, pat, f"{SRC}/p27/{name}")
sz = fetch(spuf[1], f"{DL}/{spuf[1].rsplit('/', 1)[1]}")
inner(sz, r"^basic drugs formulary", f"{SRC}/price/formulary.txt")
inner(sz, r"^pricing file", f"{SRC}/price/pricing.txt")

# ---- RxNorm names (NLM RxNav, free, no key)
rx = json.load(urllib.request.urlopen(urllib.request.Request(
    "https://rxnav.nlm.nih.gov/REST/allconcepts.json?tty=SCD+SBD+GPCK+BPCK", headers=UA), timeout=300))
with open(f"{SRC}/rxnorm_current_concepts.txt", "w") as f:
    f.write("RXCUI|TTY|NAME\n")
    for c in rx["minConceptGroup"]["minConcept"]: f.write(f"{c['rxcui']}|{c['tty']}|{c['name']}\n")

json.dump({"puf": puf[1], "prices": spuf[1], "prices_year": re.search(r"SPUF_(\d{4})", spuf[1]).group(1)},
          open(f"{SRC}/manifest.json", "w"), indent=1)
print("sources ready", flush=True)
