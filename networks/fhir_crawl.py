#!/usr/bin/env python3
"""Crawl an insurer's public Plan-Net FHIR provider directory API (CMS Interoperability rule, no login).

Step 1 (plan):  python fhir_crawl.py plan  BASE_URL KEY OUTDIR
    Pages through InsurancePlan, keeps 2027 Medicare Advantage plans (identifier/alias holds an MA plan ID),
    and writes OUTDIR/KEY.plans.json  {network id: [plan IDs]}.
Step 2 (crawl): python fhir_crawl.py crawl BASE_URL KEY OUTDIR --shard i/n [--budget SECONDS]
    For its share of networks, pages PractitionerRole?network=<id>&_include=PractitionerRole:practitioner
    and writes OUTDIR/KEY-i.net.tsv, KEY-i.who.tsv, KEY-i.log.json (same format as harvest.py).
"""
import json, os, re, sys, time, urllib.request, urllib.parse, collections, gzip

YEAR = os.environ.get("M4M_YEAR", "2027")
UA = {"User-Agent": "Medicare4Me-directory-harvester/1.0 (+https://github.com/jcai-prog/medicare4me)",
      "Accept": "application/fhir+json, application/json;q=0.9", "Accept-Encoding": "gzip"}
PID = re.compile(r"([HR]\d{4})[-_ ]?(\d{3})[-_ ]?(\d{3})(?:[-_ ]?(\d{4}))?$")

def get(url, tries=6):
    delay = 2
    for i in range(tries):
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120)
            data = r.read()
            if r.headers.get("Content-Encoding") == "gzip": data = gzip.decompress(data)
            return json.loads(data)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                ra = e.headers.get("Retry-After")
                time.sleep(float(ra) if ra and ra.replace(".", "").isdigit() else delay); delay = min(delay * 2, 60); continue
            raise
        except Exception:
            time.sleep(delay); delay = min(delay * 2, 60)
    raise RuntimeError(f"gave up: {url}")

def pages(url, budget_end=None):
    while url:
        if budget_end and time.time() > budget_end: yield None; return
        b = get(url)
        yield b
        url = next((l["url"] for l in b.get("link", []) if l.get("relation") == "next"), None)

def rid(ref):
    return (ref or "").rstrip("/").rsplit("/", 1)[-1]

def plan_ids(res):
    vals = [i.get("value", "") for i in res.get("identifier", [])] + list(res.get("alias", []))
    out = set()
    for v in vals:
        m = PID.search(str(v).strip().upper())
        if m and (not m.group(4) or m.group(4) == YEAR):
            out.add(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    return out

def do_plan(base, key, outdir):
    nets = collections.defaultdict(set); n = kept = 0
    for b in pages(f"{base}/InsurancePlan?_count=200"):
        for e in b.get("entry", []):
            r = e.get("resource", {}); n += 1
            start = (r.get("period") or {}).get("start", "")
            if start and not start.startswith(YEAR): continue
            ids = plan_ids(r)
            if not ids: continue
            kept += 1
            refs = [rid(x.get("reference")) for x in r.get("network", [])]
            for p in r.get("plan", []): refs += [rid(x.get("reference")) for x in p.get("network", [])]
            for net in refs:
                nets[net] |= ids
    json.dump({k: sorted(v) for k, v in nets.items()}, open(f"{outdir}/{key}.plans.json", "w"), indent=0)
    print(f"{key}: {n} InsurancePlans, {kept} {YEAR} MA plans, {len(nets)} networks", flush=True)

def do_crawl(base, key, outdir, shard, budget):
    i, n = map(int, shard.split("/"))
    netmap = json.load(open(f"{outdir}/{key}.plans.json"))
    mine = sorted(netmap)[i::n]
    end = time.time() + budget
    npi_nets = collections.defaultdict(set); who = {}; prac = {}
    log = {"key": f"{key}-{i}", "url": base, "networks": len(mine), "problems": [], "incomplete": []}
    pending = []  # (practitioner id, network)
    for net in mine:
        url = f"{base}/PractitionerRole?network={urllib.parse.quote(net)}&_include=PractitionerRole:practitioner&_count=200"
        try:
            for b in pages(url, end):
                if b is None: log["incomplete"].append(net); break
                for e in b.get("entry", []):
                    r = e.get("resource", {}); t = r.get("resourceType")
                    if t == "Practitioner":
                        npi = next((x["value"] for x in r.get("identifier", []) if re.fullmatch(r"\d{10}", str(x.get("value", "")))), None)
                        if npi:
                            prac[r["id"]] = npi
                            nm = (r.get("name") or [{}])[0]
                            zips = {str(a.get("postalCode", ""))[:5] for a in r.get("address", []) if a.get("postalCode")}
                            who.setdefault(npi, [nm.get("family", ""), " ".join(nm.get("given", [])[:1]), set(), zips])
                    elif t == "PractitionerRole":
                        npi = next((x["value"] for x in r.get("identifier", []) if re.fullmatch(r"\d{10}", str(x.get("value", "")))), None)
                        pid = rid((r.get("practitioner") or {}).get("reference"))
                        tax = {c.get("code") for s in r.get("specialty", []) for c in s.get("coding", []) if c.get("code")}
                        if npi: npi_nets[npi].add(net)
                        else: pending.append((pid, net, tax))
                        if npi and npi in who: who[npi][2] |= tax
        except Exception as ex:
            log["problems"].append(f"{net}: {ex!r}"[:300])
        if time.time() > end:
            log["incomplete"] += [x for x in mine if x not in log["incomplete"] and x not in npi_nets]; break
    for pid, net, tax in pending:
        npi = prac.get(pid)
        if npi:
            npi_nets[npi].add(net)
            if npi in who: who[npi][2] |= tax
    with open(f"{outdir}/{key}-{i}.net.tsv", "w") as fh:
        for npi, ns in npi_nets.items():
            plans = sorted({p for x in ns for p in netmap.get(x, [])})
            if plans: fh.write(f"{npi}\t{','.join(plans)}\n")
    with open(f"{outdir}/{key}-{i}.who.tsv", "w") as fh:
        for npi, w in who.items():
            if npi in npi_nets: fh.write(f"{npi}\t{w[0]}\t{w[1]}\t{','.join(sorted(w[2]))[:200]}\t{','.join(sorted(w[3]))[:120]}\n")
    log["providers"] = len(npi_nets)
    log["plans"] = sorted({p for x in mine for p in netmap.get(x, [])})
    log["finished"] = time.strftime("%FT%TZ", time.gmtime())
    json.dump(log, open(f"{outdir}/{key}-{i}.log.json", "w"), indent=1)
    print(f"{key}-{i}: {len(npi_nets)} providers, incomplete networks {len(log['incomplete'])}", flush=True)

if __name__ == "__main__":
    mode, base, key, outdir = sys.argv[1:5]; os.makedirs(outdir, exist_ok=True)
    if mode == "plan": do_plan(base.rstrip("/"), key, outdir)
    else:
        shard = sys.argv[sys.argv.index("--shard") + 1]
        budget = float(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else 5 * 3600
        do_crawl(base.rstrip("/"), key, outdir, shard, budget)
