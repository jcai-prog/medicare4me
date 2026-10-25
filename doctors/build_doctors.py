#!/usr/bin/env python3
"""Doctor search index from the CMS NPPES monthly file (every NPI in the U.S.).

Usage: build_doctors.py NPPES_ZIP NUCC_CSV SITE_DATA
Output: SITE_DATA/doc/<ST>/<A>.json  [[npi, last, first, credential, taxonomy, city, zip5], ...]
        (state of the practice location; A = first letter of last name)
        SITE_DATA/doc/tax.json       taxonomy code -> display name (NUCC)
Kept: individual clinicians (entity type 1), active NPIs, whose primary taxonomy is a physician, nurse
practitioner, physician assistant, podiatrist, optometrist, chiropractor, psychologist, therapist or similar
group a Medicare enrollee would look up by name.
"""
import csv, io, json, os, re, sys, zipfile, collections

zpath, nucc, site = sys.argv[1], sys.argv[2], sys.argv[3]
KEEP = ("20", "363L", "363A", "213E", "152W", "111N", "103T", "1041", "2251", "2255", "235", "225X", "133V", "231H", "237")

tax_name = {}
if os.path.exists(nucc):
    with open(nucc, encoding="utf-8-sig", errors="replace") as fh:
        for r in csv.DictReader(fh):
            code = r.get("Code", "").strip()
            disp = (r.get("Display Name") or r.get("Specialization") or r.get("Classification") or "").strip()
            if code: tax_name[code] = disp

z = zipfile.ZipFile(zpath)
name = [n for n in z.namelist() if re.match(r"npidata_pfile_\d+-\d+\.csv$", n.split("/")[-1])][0]
out = collections.defaultdict(list)
used_tax = set()
n = 0
with z.open(name) as fh:
    rdr = csv.reader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace"))
    h = next(rdr); ix = {c: i for i, c in enumerate(h)}
    T = [ix[f"Healthcare Provider Taxonomy Code_{k}"] for k in range(1, 16)]
    S = [ix[f"Healthcare Provider Primary Taxonomy Switch_{k}"] for k in range(1, 16)]
    E, NPI, LN, FN, CR = ix["Entity Type Code"], ix["NPI"], ix["Provider Last Name (Legal Name)"], ix["Provider First Name"], ix["Provider Credential Text"]
    CI, ST, ZP = ix["Provider Business Practice Location Address City Name"], ix["Provider Business Practice Location Address State Name"], ix["Provider Business Practice Location Address Postal Code"]
    DD = ix["NPI Deactivation Date"]; RD = ix["NPI Reactivation Date"]
    for r in rdr:
        if r[E] != "1" or (r[DD] and not r[RD]): continue
        tax = ""
        for t, s in zip(T, S):
            if r[s] == "Y": tax = r[t]; break
        if not tax: tax = r[T[0]]
        if not tax.startswith(KEEP): continue
        st = r[ST].strip().upper()
        if len(st) != 2: continue
        last = r[LN].strip().title(); first = r[FN].strip().title()
        if not last: continue
        letter = re.sub(r"[^A-Z]", "", last.upper())[:1] or "_"
        cred = re.sub(r"[^A-Za-z.,\- ]", "", r[CR]).strip()[:12]
        out[(st, letter)].append([r[NPI], last, first, cred, tax, r[CI].strip().title(), r[ZP][:5]])
        used_tax.add(tax); n += 1

os.makedirs(f"{site}/doc", exist_ok=True)
for (st, letter), rows in out.items():
    os.makedirs(f"{site}/doc/{st}", exist_ok=True)
    rows.sort(key=lambda x: (x[1], x[2]))
    json.dump(rows, open(f"{site}/doc/{st}/{letter}.json", "w"), separators=(",", ":"))
json.dump({t: tax_name.get(t, t) for t in sorted(used_tax)}, open(f"{site}/doc/tax.json", "w"), separators=(",", ":"))
print(f"clinicians {n}, files {len(out)}, taxonomy codes {len(used_tax)} (named {sum(1 for t in used_tax if t in tax_name)})")
