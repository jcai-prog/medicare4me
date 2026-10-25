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
    # size each network and learn how the server pages (offset "_skip"/"_offset" can be split across jobs)
    info = {}
    for net in nets:
        try:
            b = get(f"{base}/PractitionerRole?network={urllib.parse.quote(net)}&_count={PAGE}")
            nxt = next((l["url"] for l in b.get("link", []) if l.get("relation") == "next"), "")
            q = urllib.parse.parse_qs(urllib.parse.urlparse(nxt).query)
            off = "_skip" if "_skip" in q else "_offset" if "_offset" in q else None
            step = int(q[off][0]) if off else None
            info[net] = {"total": b.get("total"), "offset": off, "step": step}
        except Exception as ex:
            info[net] = {"error": repr(ex)[:200]}
    json.dump({"networks": {k: sorted(v) for k, v in nets.items()}, "info": info},
              open(f"{outdir}/{key}.plans.json", "w"), indent=0)
    print(f"{key}: {n} InsurancePlans, {kept} {YEAR} MA plans, {len(nets)} networks, "
          f"{sum((i.get('total') or 0) for i in info.values()):,} roles", flush=True)

PAGE = int(os.environ.get("M4M_PAGE", "100"))
THREADS = int(os.environ.get("M4M_THREADS", "4"))

def parse_bundle(b, net, npi_nets, who, prac, pending):
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

def do_crawl(base, key, outdir, shard, budget):
    import concurrent.futures, threading
    i, n = map(int, shard.split("/"))
    plan = json.load(open(f"{outdir}/{key}.plans.json"))
    netmap, info = plan["networks"], plan.get("info", {})
    end = time.time() + budget
    npi_nets = collections.defaultdict(set); who = {}; prac = {}; pending = []
    lock = threading.Lock()
    log = {"key": f"{key}-{i}", "url": base, "problems": [], "incomplete": [], "pages": 0}
    # work items: (network, skip) pages for offset servers split across all jobs; whole networks otherwise
    items, cursor_nets = [], []
    for net in sorted(netmap):
        inf = info.get(net, {})
        if inf.get("offset") and inf.get("total"):
            step = inf.get("step") or PAGE
            for k, skip in enumerate(range(0, inf["total"], step)):
                items.append((net, inf["offset"], skip, step))
        else:
            cursor_nets.append(net)
    mine = [it for k, it in enumerate(items) if k % n == i]
    cursor_mine = cursor_nets[i::n]
    log["offset_pages"] = len(mine); log["cursor_networks"] = len(cursor_mine)
    inc = "&_include=PractitionerRole:practitioner"
    def fetch_page(it):
        net, off, skip, step = it
        if time.time() > end: return it, None
        try:
            b = get(f"{base}/PractitionerRole?network={urllib.parse.quote(net)}&_count={step}&{off}={skip}{inc}")
            loc = (collections.defaultdict(set), {}, {}, [])
            parse_bundle(b, net, *loc)
            return it, loc
        except Exception as ex:
            return it, ex
    with concurrent.futures.ThreadPoolExecutor(THREADS) as ex:
        for it, res in ex.map(fetch_page, mine):
            if res is None: log["incomplete"].append(f"{it[0]}@{it[2]}"); continue
            if isinstance(res, Exception): log["problems"].append(f"{it[0]}@{it[2]}: {res!r}"[:200]); continue
            a, w, p, pe = res
            for k, v in a.items(): npi_nets[k] |= v
            for k, v in w.items(): who.setdefault(k, v)
            prac.update(p); pending += pe; log["pages"] += 1
    for net in cursor_mine:
        try:
            for b in pages(f"{base}/PractitionerRole?network={urllib.parse.quote(net)}&_count={PAGE}{inc}", end):
                if b is None: log["incomplete"].append(net); break
                parse_bundle(b, net, npi_nets, who, prac, pending); log["pages"] += 1
        except Exception as ex2:
            log["problems"].append(f"{net}: {ex2!r}"[:300])
    for pid, net, tax in pending:
        npi = prac.get(pid)
        if npi: npi_nets[npi].add(net)
    with open(f"{outdir}/{key}-{i}.net.tsv", "w") as fh:
        for npi, ns in npi_nets.items():
            plans = sorted({p for x in ns for p in netmap.get(x, [])})
            if plans: fh.write(f"{npi}\t{','.join(plans)}\n")
    with open(f"{outdir}/{key}-{i}.who.tsv", "w") as fh:
        for npi, w in who.items():
            if npi in npi_nets: fh.write(f"{npi}\t{w[0]}\t{w[1]}\t\t{','.join(sorted(w[3]))[:120]}\n")
    bad_nets = {x.split("@")[0] for x in log["incomplete"]} | {x.split(":")[0].split("@")[0] for x in log["problems"]}
    log["incomplete_plans"] = sorted({p for x in bad_nets for p in netmap.get(x, [])})
    log["providers"] = len(npi_nets)
    log["plans"] = sorted({p for x in netmap for p in netmap[x]})
    log["finished"] = time.strftime("%FT%TZ", time.gmtime())
    log["problems"] = log["problems"][:50]; log["incomplete_count"] = len(log["incomplete"]); log["incomplete"] = log["incomplete"][:50]
    json.dump(log, open(f"{outdir}/{key}-{i}.log.json", "w"), indent=1)
    print(f"{key}-{i}: {len(npi_nets)} providers, {log['pages']} pages, incomplete {log['incomplete_count']}", flush=True)

if __name__ == "__main__":
    mode, base, key, outdir = sys.argv[1:5]; os.makedirs(outdir, exist_ok=True)
    if mode == "plan": do_plan(base.rstrip("/"), key, outdir)
    else:
        shard = sys.argv[sys.argv.index("--shard") + 1]
        budget = float(sys.argv[sys.argv.index("--budget") + 1]) if "--budget" in sys.argv else 5 * 3600
        do_crawl(base.rstrip("/"), key, outdir, shard, budget)
