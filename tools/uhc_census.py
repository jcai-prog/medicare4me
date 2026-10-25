#!/usr/bin/env python3
"""Census of a Plan-Net server's InsurancePlans: period years, identifier systems, sample MA IDs."""
import json, sys, collections, urllib.request, re
base = sys.argv[1]; out = {"years": collections.Counter(), "systems": collections.Counter(), "ma_ids": collections.Counter(), "samples": []}
url = f"{base}/InsurancePlan?_count=200"
while url:
    b = json.load(urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/fhir+json", "User-Agent": "Medicare4Me/1.0"}), timeout=120))
    for e in b.get("entry", []):
        r = e["resource"]; y = (r.get("period") or {}).get("start", "none")[:4]; out["years"][y] += 1
        for i in r.get("identifier", []):
            out["systems"][i.get("system", "?")] += 1
            if re.match(r"^[HR]\d{4}", str(i.get("value", ""))): out["ma_ids"][y] += 1
            if len(out["samples"]) < 12 and re.match(r"^[HR]\d{4}", str(i.get("value", ""))): out["samples"].append([y, i.get("value"), r.get("name"), [n.get("display") for n in r.get("network", [])][:2]])
    url = next((l["url"] for l in b.get("link", []) if l.get("relation") == "next"), None)
json.dump(out, open("uhc_census.json", "w"), indent=1, default=dict); print(json.dumps(out, default=dict)[:3000])
