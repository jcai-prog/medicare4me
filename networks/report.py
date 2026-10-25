#!/usr/bin/env python3
"""Coverage report: which insurers' doctor networks made it into the site, and what failed."""
import json, glob, sys, collections
h, cov_path, contracts_path = sys.argv[1:4]
cov = json.load(open(cov_path)) if cov_path else {}
contracts = json.load(open(contracts_path))
byp = collections.defaultdict(list)
for c, v in contracts.items(): byp[v["parent"]].append(c)
print(f"# Doctor network coverage\n\n{len(cov)} of {len(contracts)} MA contracts have network data.\n")
print("| Parent organization | Contracts with data | Providers (max per contract) |\n|---|---|---|")
for p, cs in sorted(byp.items(), key=lambda x: -len(x[1])):
    have = [c for c in cs if c in cov]
    print(f"| {p} | {len(have)} of {len(cs)} | {max([cov[c]['providers'] for c in have], default=0):,} |")
print("\n## Source problems\n")
for f in sorted(glob.glob(f"{h}/*.log.json")):
    lg = json.load(open(f))
    if lg.get("problems") or lg.get("incomplete"):
        print(f"- `{lg['key']}`: {'; '.join(lg.get('problems', []))[:300]} {'incomplete networks: ' + str(len(lg['incomplete'])) if lg.get('incomplete') else ''}")
