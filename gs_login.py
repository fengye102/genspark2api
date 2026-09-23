#!/usr/bin/env python3
"""Genspark 登录 + cookie 导出（对照知识库 kuku_login.py 的标准做法）

用途：浏览器只负责登录，导出 cookie 给 genspark_rp2.py 做纯 HTTP 反代。
     浏览器不在反代链路里。

用法：
  python gs_login.py            # 打开窗口，你手动登录，然后创建 .proceed 文件
  python gs_login.py --auto     # 已有登录态时直接导出（不等待）
"""
import argparse
import json
import os
import sys
import time

import cloakbrowser

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "genspark_reg_profile")
OUT_FILE = os.path.join(BASE_DIR, "gs_cookies.json")
PROCEED_FILE = os.path.join(BASE_DIR, ".proceed")
TARGET = "https://www.genspark.ai/agents?type=ai_chat"

# 安全：拒绝使用系统 Chrome profile
assert "User Data" not in PROFILE_DIR, "refusing to use a system Chrome profile"

ap = argparse.ArgumentParser()
ap.add_argument("--auto", action="store_true", help="不等待，直接导出当前 cookie")
ap.add_argument("--timeout", type=int, default=600)
args = ap.parse_args()


def log(m):
    print(f"[gs-login] {m}", flush=True)


os.makedirs(PROFILE_DIR, exist_ok=True)
browser = cloakbrowser.launch_persistent_context(
    user_data_dir=PROFILE_DIR,
    headless=False,
    stealth_args=True,
    viewport={"width": 1440, "height": 900},
)
page = browser.pages[0] if browser.pages else browser.new_page()
log(f"opening {TARGET}")
page.goto(TARGET, wait_until="domcontentloaded", timeout=60000)
time.sleep(8)

if os.path.exists(PROCEED_FILE):
    os.remove(PROCEED_FILE)

if not args.auto:
    log("=== 请在浏览器窗口完成登录 ===")
    log(f"登录后（能看到聊天界面），创建文件 {PROCEED_FILE} 或等待自动检测")
    log(f"超时 {args.timeout}s")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if os.path.exists(PROCEED_FILE):
            log("检测到 .proceed")
            break
        # 自动检测登录态
        try:
            logged = page.evaluate("""async () => {
              try {
                const r = await fetch('/api/is_login', {credentials:'include'});
                const j = await r.json();
                return !!(j.data && j.data.is_login);
              } catch(e) { return false; }
            }""")
            if logged:
                log("✅ 自动检测到登录态")
                break
        except Exception:
            pass
        time.sleep(3)

# 导出 cookie（Playwright 格式，含 httpOnly）
cookies = browser.cookies()
out = {
    "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    "source": TARGET,
    "user_agent": page.evaluate("() => navigator.userAgent"),
    "cookies": [
        {"name": c.get("name"), "value": c.get("value"),
         "domain": c.get("domain"), "path": c.get("path"),
         "httpOnly": c.get("httpOnly"), "secure": c.get("secure"),
         "sameSite": c.get("sameSite")}
        for c in cookies
    ],
}
json.dump(out, open(OUT_FILE, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
log(f"✅ 已导出 {len(out['cookies'])} 个 cookie -> {OUT_FILE}")

# 关键 cookie 检查
names = {c["name"] for c in out["cookies"]}
need = {"session_id", "c1", "c2"}
log(f"关键 cookie: {sorted(need & names)}  缺失: {sorted(need - names)}")
if "session_id" not in names:
    log("⚠️ 缺 session_id —— 可能未登录")

browser.close()
log("浏览器已关闭（反代不需要浏览器常驻）")
