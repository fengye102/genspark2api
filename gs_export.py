"""Export session cookies from a browser profile after signup/login.

Reads the cookies from a persistent browser profile and writes them to a JSON
file the bridge can load. Also reports the account's own metadata (cogen_id,
credit balance) by calling the site's own endpoints inside the logged-in page.

Usage:
  python gs_export.py --profile <dir> --account <n> --email <addr> --out <file.json>
"""
import argparse
import json
import os
import time

import cloakbrowser

def _sanitize_proxy(url):
    """Remove credentials from proxy URL for safe storage."""
    if not url:
        return url
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    if p.password or p.username:
        host = p.hostname or ""
        if p.port:
            host += f":{p.port}"
        return urlunparse((p.scheme, host, p.path, p.params, p.query, p.fragment))
    return url

ap = argparse.ArgumentParser()
ap.add_argument("--profile", required=True, help="browser profile directory")
ap.add_argument("--account", default="", help="account label/index")
ap.add_argument("--email", default="", help="account email (recorded only)")
ap.add_argument("--out", required=True, help="output JSON path")
ap.add_argument("--proxy", default=os.environ.get("GS_PROXY", ""),
                help="egress proxy used by the browser")
ap.add_argument("--headless", action="store_true")
args = ap.parse_args()

br = cloakbrowser.launch_persistent_context(
    user_data_dir=args.profile, headless=args.headless, stealth_args=True,
    proxy=args.proxy or None, viewport={"width": 1440, "height": 900})
pg = br.pages[0] if br.pages else br.new_page()
if "genspark.ai" not in (pg.url or ""):
    pg.goto("https://www.genspark.ai/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(7)
print(f"url: {pg.url}")

r = pg.evaluate("""async () => {
  const r = await fetch('/api/is_login', {credentials:'include'});
  return await r.json();
}""")
d = r.get("data", {}) or {}
print(f"is_login = {d.get('is_login')}")
print(f"cogen_id = {d.get('cogen_id')}")
print(f"email    = {d.get('cogen_email')}")

try:
    r2 = pg.evaluate("""async () => {
      const r = await fetch('/api/credit_audit/billing_cycle', {credentials:'include'});
      return await r.json();
    }""")
    print(f"credits  = {r2.get('data', {}).get('remaining')}")
except Exception:
    pass

cks = br.cookies()
out = {
    "account": args.account,
    "email": args.email,
    "cogen_id": d.get("cogen_id"),
    "proxy": _sanitize_proxy(args.proxy),
    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "user_agent": pg.evaluate("() => navigator.userAgent"),
    "cookies": [{"name": c.get("name"), "value": c.get("value"),
                 "domain": c.get("domain"), "path": c.get("path"),
                 "httpOnly": c.get("httpOnly"), "secure": c.get("secure"),
                 "sameSite": c.get("sameSite")} for c in cks],
}
os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

names = {c["name"] for c in out["cookies"]}
print(f"\nOK exported {len(out['cookies'])} cookies -> {args.out}")
print(f"key cookies: {sorted(names & {'session_id', 'c1', 'c2'})}")
br.close()
