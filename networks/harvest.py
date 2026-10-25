#!/usr/bin/env python3
"""Harvest Medicare Advantage provider networks from insurers' public Medicare Plan Finder (MPF) directory files.

Each MA organization posts, at a public URL it registers with CMS, an index file that lists its provider
files. CMS accepts two formats (CMS MPF Provider Directory Technical Implementation Guide):
  * machine-readable JSON  (MAPROVIDERS.JSON: [{npi, type, plans:[{maPlanId, year}], name, specialty, addresses}])
  * FHIR-based JSON        (Da Vinci PDex Plan-Net resources: InsurancePlan with the MA plan ID and its
                            network; PractitionerRole pointing at a network; Practitioner with the NPI)
Both are parsed as streams, so multi-gigabyte files work in a small runner.

Usage: harvest.py SOURCES_JSON OUTDIR [--shard i/n]
Output per source: OUTDIR/<key>.net.tsv   npi \t comma-separated 2027 plan IDs (H1234-001-000)
                   OUTDIR/<key>.who.tsv   npi \t family \t given \t taxonomy codes \t zip5 list
                   OUTDIR/<key>.log.json  counts and problems
"""
import gzip, io, json, os, re, sys, time, urllib.request, urllib.parse, collections, hashlib
import ijson

YEAR = os.environ.get("M4M_YEAR", "2027")
PLAN_RE = re.compile(r"^([HR]\d{4})[-_ ]?(\d{3})[-_ ]?(\d{1,3})$")
UA = {"User-Agent": "Medicare4Me-directory-harvester/1.0 (+https://github.com/jcai-prog/medicare4me)",
      "Accept": "application/json, application/fhir+json;q=0.9, */*;q=0.5", "Accept-Encoding": "gzip"}

def norm_plan(s):
    m = PLAN_RE.match((s or "").strip().upper())
    return f"{m.group(1)}-{m.group(2)}-{int(m.group(3)):03d}" if m else None

def open_url(url, tries=4):
    last = None
    for i in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180)
            if r.headers.get("Content-Encoding") == "gzip" or url.endswith(".gz"):
                return gzip.GzipFile(fileobj=r)
            return r
        except Exception as e:
            last = e; time.sleep(5 * (i + 1))
    raise last

def urls_in(obj, base):
    """Collect every http(s) or relative .json URL in an index document."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items(): out += urls_in(v, base)
    elif isinstance(obj, list):
        for v in obj: out += urls_in(v, base)
    elif isinstance(obj, str):
        s = obj.strip()
        if s.startswith("http") or re.search(r"\.(json|ndjson)(\.gz)?(\?|$)", s, re.I):
            out.append(urllib.parse.urljoin(base, s))
    return out

class Acc:
    def __init__(self):
        self.net_plans = collections.defaultdict(set)     # network id -> plans
        self.file_plans = set()                            # plans named anywhere in the source
        self.role_nets = collections.defaultdict(set)      # npi -> network ids (FHIR)
        self.npi_plans = collections.defaultdict(set)      # npi -> plans (machine-readable JSON)
        self.who = {}                                      # npi -> [family, given, taxonomies, zips]
        self.prac_npi = {}                                 # Practitioner id -> npi
        self.role_prac = []                                # (practitioner ref, networks, taxonomy) when role lacks NPI
        self.counts = collections.Counter()

    def person(self, npi, family=None, given=None, tax=None, zips=None):
        w = self.who.setdefault(npi, ["", "", set(), set()])
        if family and not w[0]: w[0] = family
        if given and not w[1]: w[1] = given
        if tax: w[2].update(tax)
        if zips: w[3].update(zips)

def npi_of(identifiers):
    for i in identifiers or []:
        v = str(i.get("value", "")).strip()
        if ("npi" in str(i.get("system", "")).lower() or re.fullmatch(r"\d{10}", v)) and re.fullmatch(r"\d{10}", v):
            return v
    return None

def ref_id(ref):
    return (ref or "").rsplit("/", 1)[-1]

def zips_of(addrs):
    out = set()
    for a in addrs or []:
        z = str(a.get("postalCode") or a.get("zip") or "")[:5]
        if re.fullmatch(r"\d{5}", z): out.add(z)
    return out

def take_fhir(res, acc):
    t = res.get("resourceType")
    acc.counts[t] += 1
    if t == "InsurancePlan":
        per = res.get("period") or {}
        st, en = str(per.get("start", ""))[:4], str(per.get("end", ""))[:4]
        # directory files are per contract year; only skip plans clearly for another year
        if (st and st > YEAR) or (en and en < YEAR): acc.counts["plan_other_year"] += 1; return
        ids = [norm_plan(i.get("value")) for i in res.get("identifier", [])] + [norm_plan(a) for a in res.get("alias", [])]
        ids = [p for p in ids if p]
        nets = [ref_id(n.get("reference")) for n in res.get("network", [])]
        for pl in res.get("plan", []):
            nets += [ref_id(n.get("reference")) for n in pl.get("network", [])]
            ids += [norm_plan(i.get("value")) for i in pl.get("identifier", []) if norm_plan(i.get("value"))]
        for p in ids:
            acc.file_plans.add(p)
            for n in nets: acc.net_plans[n].add(p)
    elif t == "PractitionerRole":
        nets = set()
        for ext in res.get("extension", []):
            if "network-reference" in ext.get("url", ""):
                nets.add(ref_id((ext.get("valueReference") or {}).get("reference")))
        for n in res.get("network", []) if isinstance(res.get("network"), list) else []:
            nets.add(ref_id(n.get("reference")))
        tax = {c.get("code") for s in res.get("specialty", []) for c in s.get("coding", []) if c.get("code")}
        npi = npi_of(res.get("identifier"))
        pref = (res.get("practitioner") or {}).get("reference")
        if not npi and pref:
            npi = acc.prac_npi.get(ref_id(pref))
            if not npi:
                acc.role_prac.append((ref_id(pref), nets, tax)); return
        if not npi and pref and re.fullmatch(r"\d{10}", ref_id(pref)): npi = ref_id(pref)
        if not npi: acc.counts["role_no_npi"] += 1; return
        acc.role_nets[npi].update(nets or {"__file__"})
        acc.person(npi, tax=tax)
    elif t == "Practitioner":
        npi = npi_of(res.get("identifier")) or (res.get("id") if re.fullmatch(r"\d{10}", str(res.get("id"))) else None)
        if not npi: return
        acc.prac_npi[res.get("id")] = npi
        nm = (res.get("name") or [{}])[0]
        acc.person(npi, nm.get("family"), " ".join(nm.get("given", [])[:1]), None, zips_of(res.get("address")))

def take_mr(item, acc):
    """Machine-readable JSON provider entry."""
    acc.counts["mr_entry"] += 1
    npi = str(item.get("npi", "")).strip()
    if not re.fullmatch(r"\d{10}", npi): acc.counts["mr_bad_npi"] += 1; return
    if str(item.get("type", "")).lower().startswith("fac"): acc.counts["mr_facility"] += 1
    tax, zips = set(item.get("specialty") or []), zips_of(item.get("addresses"))
    for p in item.get("plans", []):
        if not isinstance(p, dict): continue
        yr = p.get("year", YEAR)
        yrs = {str(y) for y in yr} if isinstance(yr, list) else {str(yr)}
        if YEAR not in yrs: acc.counts["mr_other_year"] += 1; continue
        pid = norm_plan(p.get("maPlanId") or p.get("planId") or p.get("maPlanID"))
        if pid: acc.npi_plans[npi].add(pid); acc.file_plans.add(pid)
        tax |= set(p.get("specialty") or []); zips |= zips_of(p.get("addresses"))
    nm = item.get("name") or {}
    if isinstance(nm, dict):
        acc.person(npi, nm.get("last"), nm.get("first"), tax, zips)

def sniff(stream):
    head = stream.read(4096)
    return head, io.BufferedReader(_Chain(head, stream), 1 << 20)

class _Chain(io.RawIOBase):
    def __init__(self, head, rest): self.head, self.rest = head, rest
    def readable(self): return True
    def readinto(self, b):
        if self.head:
            n = min(len(b), len(self.head)); b[:n] = self.head[:n]; self.head = self.head[n:]; return n
        d = self.rest.read(len(b)); b[:len(d)] = d; return len(d)

def parse_file(url, acc, log):
    s = open_url(url)
    head, s = sniff(s)
    txt = head.lstrip()[:200].decode("utf-8", "replace")
    if txt.startswith("["):
        for item in ijson.items(s, "item", use_float=True):
            if isinstance(item, dict) and "resourceType" in item: take_fhir(item, acc)
            elif isinstance(item, dict): take_mr(item, acc)
    elif txt.startswith("{") and '"resourceType"' in head.decode("utf-8", "replace")[:2000] and "\n{" in head.decode("utf-8", "replace")[:4000] and '"entry"' not in head.decode("utf-8", "replace")[:600]:
        for line in io.TextIOWrapper(s, encoding="utf-8"):   # NDJSON
            line = line.strip()
            if line: take_fhir(json.loads(line), acc)
    elif txt.startswith("{"):
        for res in ijson.items(s, "entry.item.resource", use_float=True): take_fhir(res, acc)
    else:
        log["problems"].append(f"unknown format {url}: {txt[:80]!r}")

def finish(acc, log):
    # resolve roles that referenced practitioners before the Practitioner resource was seen
    for pref, nets, tax in acc.role_prac:
        npi = acc.prac_npi.get(pref)
        if npi: acc.role_nets[npi].update(nets or {"__file__"}); acc.person(npi, tax=tax)
        else: acc.counts["role_unresolved"] += 1
    out = collections.defaultdict(set)
    for npi, nets in acc.role_nets.items():
        for n in nets:
            plans = acc.net_plans.get(n) if n != "__file__" else None
            if plans: out[npi] |= plans
            elif n == "__file__" or not acc.net_plans:   # no network links in the source: all plans it names
                out[npi] |= acc.file_plans; acc.counts["fallback_file_plans"] += 1
            else: acc.counts["unknown_network"] += 1
    for npi, plans in acc.npi_plans.items(): out[npi] |= plans
    log["providers"] = len(out); log["plans"] = sorted(acc.file_plans)
    return out

def harvest(src, outdir):
    key = src["key"]
    log = {"key": key, "url": src["url"], "problems": [], "started": time.strftime("%FT%TZ", time.gmtime())}
    acc = Acc()
    try:
        files = [src["url"]]
        if src.get("kind", "mpf") == "mpf":
            head, s = sniff(open_url(src["url"]))
            doc = None
            if head.lstrip()[:1] == b"{" and b'"resourceType"' not in head[:300]:
                raw = s.read(20_000_000)          # an index file (lists provider files); small
                try: doc = json.loads(raw)
                except Exception as e: log["problems"].append(f"index not JSON: {e!r}"[:200])
            if isinstance(doc, dict) and ("resourceType" in doc and doc.get("resourceType") == "Bundle"):
                files = [src["url"]]
            elif doc is not None:
                files = [u for u in urls_in(doc, src["url"]) if u != src["url"]] or [src["url"]]
            log["files"] = files
        for u in files:
            parse_file(u, acc, log)
        out = finish(acc, log)
        with open(f"{outdir}/{key}.net.tsv", "w") as fh:
            for npi, plans in out.items(): fh.write(f"{npi}\t{','.join(sorted(plans))}\n")
        with open(f"{outdir}/{key}.who.tsv", "w") as fh:
            for npi, w in acc.who.items():
                if npi in out: fh.write(f"{npi}\t{w[0]}\t{w[1]}\t{','.join(sorted(w[2]))[:200]}\t{','.join(sorted(w[3]))[:120]}\n")
    except Exception as e:
        log["problems"].append(f"FAILED: {e!r}"[:600])
    log["counts"] = dict(acc.counts); log["finished"] = time.strftime("%FT%TZ", time.gmtime())
    json.dump(log, open(f"{outdir}/{key}.log.json", "w"), indent=1)
    print(key, log.get("providers"), len(log.get("plans", [])), log["problems"][:2], flush=True)

if __name__ == "__main__":
    srcs = json.load(open(sys.argv[1])); outdir = sys.argv[2]; os.makedirs(outdir, exist_ok=True)
    if len(sys.argv) > 4 and sys.argv[3] == "--shard":
        i, n = map(int, sys.argv[4].split("/")); srcs = [s for j, s in enumerate(srcs) if j % n == i]
    for s in srcs: harvest(s, outdir)
