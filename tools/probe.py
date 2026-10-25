#!/usr/bin/env python3
"""Fetch each URL in tools/probe_urls.txt and record status, type, size and the first bytes (for endpoint discovery)."""
import json, sys, urllib.request, ssl, time
out = []
for line in open("tools/probe_urls.txt"):
    u = line.strip()
    if not u or u.startswith("#"): continue
    rec = {"url": u}
    try:
        req = urllib.request.Request(u, headers={"Accept": "application/fhir+json, application/json;q=0.9, */*;q=0.5", "User-Agent": "Medicare4Me-probe/1.0"})
        t = time.time()
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read(6000)
            rec.update(status=r.status, final=r.geturl(), ctype=r.headers.get("Content-Type"), length=r.headers.get("Content-Length"), secs=round(time.time()-t,1), head=body.decode("utf-8", "replace"))
    except Exception as e:
        rec.update(error=repr(e)[:500])
        if hasattr(e, "read"):
            try: rec["head"] = e.read(1500).decode("utf-8", "replace")
            except Exception: pass
    out.append(rec); print(json.dumps(rec)[:300], flush=True)
json.dump(out, open("probe_results.json", "w"), indent=1)
