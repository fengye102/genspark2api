"""Genspark cookie 提取模板 —— 占位符由编排脚本 gs_reg_flow.py 替换"""
import os, time, json
import cloakbrowser

PROFILE = r"{{PROFILE}}"
ACCOUNT = "{{ACCOUNT}}"
EMAIL = "{{EMAIL}}"
COOKIE_FILE = r"{{COOKIE_FILE}}"

br = cloakbrowser.launch_persistent_context(
    user_data_dir=PROFILE, headless=False, stealth_args=True,
    proxy=os.environ.get("GS_PROXY") or None, viewport={"width": 1440, "height": 900})
pg = br.pages[0] if br.pages else br.new_page()
if "genspark.ai" not in (pg.url or ""):
    pg.goto("https://www.genspark.ai/", wait_until="domcontentloaded", timeout=60000)
    time.sleep(7)
print(f"url: {pg.url}")

r = pg.evaluate("""async () => {
  const r = await fetch('/api/is_login', {credentials:'include'});
  return await r.json();
}""")
d = r.get("data", {})
print(f"is_login = {d.get('is_login')}")
print(f"cogen_id = {d.get('cogen_id')}")
print(f"email    = {d.get('cogen_email')}")

r2 = pg.evaluate("""async () => {
  const r = await fetch('/api/credit_audit/billing_cycle', {credentials:'include'});
  return await r.json();
}""")
print(f"余额 = {r2.get('data',{}).get('remaining')}")

cks = br.cookies()
out = {
    "account": ACCOUNT, "email": EMAIL,
    "cogen_id": d.get("cogen_id"), "proxy": os.environ.get("GS_PROXY", ""),
    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "user_agent": pg.evaluate("() => navigator.userAgent"),
    "cookies": [{"name": c.get("name"), "value": c.get("value"),
                 "domain": c.get("domain"), "path": c.get("path"),
                 "httpOnly": c.get("httpOnly"), "secure": c.get("secure"),
                 "sameSite": c.get("sameSite")} for c in cks],
}
json.dump(out, open(COOKIE_FILE, "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
names = {c["name"] for c in out["cookies"]}
print(f"\nOK 导出 {len(out['cookies'])} 个 cookie -> {COOKIE_FILE}")
print(f"关键 cookie: {sorted(names & {'session_id','c1','c2'})}")
br.close()
