#!/usr/bin/env python3
"""Assemble the page: inline the cost engine, write site/index.html (full document, Cloudflare Pages)
and art/index.html (fragment, artifact preview)."""
import os, re
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.makedirs(f"{here}/site", exist_ok=True)
app = open(f"{here}/web/app.html").read().replace("/*__ENGINE__*/", open(f"{here}/web/engine.js").read())
head_end = app.index("</style>") + len("</style>")
head, body = app[:head_end], app[head_end:]
full = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        + head + '\n</head>\n<body>\n' + body + '\n</body>\n</html>\n')
open(f"{here}/site/index.html", "w").write(full)
open(f"{here}/site/_headers", "w").write("/data/*\n  Cache-Control: public, max-age=86400\n  Access-Control-Allow-Origin: *\n/*\n  X-Content-Type-Options: nosniff\n  Referrer-Policy: strict-origin-when-cross-origin\n")
open(f"{here}/site/robots.txt", "w").write("User-agent: *\nAllow: /\n")
os.makedirs(f"{here}/art", exist_ok=True)
open(f"{here}/art/index.html", "w").write(app)
print("ok", len(full))
