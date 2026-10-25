# Medicare4Me

A free 2027 Medicare Advantage plan shopper. A shopper enters a ZIP code, drugs and doctors. The tool lists every plan in their county with:
- estimated yearly cost (premiums plus drug costs under 2027 Part D rules);
- drug-by-drug coverage;
- whether each doctor is in the plan's network;
- the 2027 Star Rating.

It's independent and not affiliated with Medicare or the U.S. government. All data is public.

## How it stays free at any scale
- **Static site** on Cloudflare Pages. Every data source is precomputed into small JSON shards, and the browser does the math (`web/engine.js`). There's no server, database or API.
- **Private:** a shopper's ZIP code, drugs and doctors never leave their browser.
- **Rebuilt monthly by GitHub Actions** (`.github/workflows/build.yml`). Public repo, so the minutes are free.

## Pipeline
| Job | What it does | Sources |
|---|---|---|
| `plans` | Plans by county, premiums, MOOP, formularies, cost sharing, drug prices, Stars, ZIP → county; then `verify.py` checks against the originals | CMS MA Landscape; CMS monthly Part D formulary file (data.cms.gov); CMS quarterly Part D file with prices; CMS Star Ratings; Census ZCTA; NLM RxNorm |
| `discover` | Finds each insurer's public directory: MPF index files plus Plan-Net FHIR APIs | CMS National Provider Directory endpoint file; `networks/seeds.json`; same-insurer URL patterns |
| `harvest` ×20 | Streams every directory file or API and extracts NPI → 2027 plan IDs | Insurer files (CMS MPF format: machine-readable JSON or FHIR JSON); Plan-Net APIs |
| `doctors` | Doctor search index by state and last name | CMS NPPES monthly file; NUCC taxonomy |
| `assemble` | Merges network shards, writes the page, deploys to Cloudflare, saves the coverage report to the `results` branch | — |

Run it from the Actions tab (**build-and-deploy → Run workflow**). It also runs on the 2nd of each month.

## Secrets (Settings → Secrets and variables → Actions)
- `CLOUDFLARE_API_TOKEN`: token with Account → Cloudflare Pages → Edit
- `CLOUDFLARE_ACCOUNT_ID`

## Local build
```
python pipeline/fetch_sources.py   # needs open internet
python pipeline/build.py && python pipeline/verify.py && python pipeline/wrap.py
```

## Known limits
- Drug prices come from CMS's latest quarterly file. Until the 2027 prices are posted, these are 2026 plan prices, with CMS's 2027 negotiated-price discounts applied to the newly negotiated drugs.
- Network data covers the insurers whose directories are publicly reachable. `results/build/network_report.md` lists coverage and failures. Plans without data show "No network data from this insurer" and a Medicare.gov link.
- Directories can be wrong. The page tells shoppers to confirm with the doctor's office.
