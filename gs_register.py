#!/usr/bin/env python3
"""Genspark 注册编排 —— 除人机验证外全自动

用法:
  python gs_reg_flow.py --email <邮箱> --seq 6

流程:
  1. 写配置 → 启动驱动（自动到表单 + 填邮箱）
  2. 检测邮箱已填 → 立刻轮询收码（CLI 直查，不用会缓存的 MCP）
  3. 填码 → 点 Verify code → 填两次密码 → 点 Create
  4. 等待注册成功 → 提示跑提取

关键（踩过的坑）:
  - MCP mail_wait 会返回缓存 → 必须用 CLI `um.py code --address` 强制重查
  - 收码要在「邮箱已填」后立刻开始轮询，不要 sleep 等
  - 新邮件到 = 旧码作废，必须用最新 uid 的码
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time

BASE = os.environ.get("GS_BASE_DIR", os.path.dirname(os.path.abspath(__file__)))
DRIVER = os.path.join(BASE, "gs_reg_driver.py")
CMDFILE = os.path.join(BASE, "gs_cmd.txt")
UM_DIR = os.environ.get("UM_DIR", "")   # unified-mail CLI 目录（可选，用于自动收码）
PY312 = os.environ.get("GS_PYTHON", sys.executable)

ap = argparse.ArgumentParser()
ap.add_argument("--email", required=True)
ap.add_argument("--seq", required=True, help="账号序号（决定 profile/log/out 路径）")
ap.add_argument("--no-start", action="store_true", help="驱动已在跑，只做后续步骤")
args = ap.parse_args()

SEQ = args.seq
PROFILE = rf"{BASE}\genspark_reg{SEQ}_profile"
OUT = rf"{BASE}\genspark_reg{SEQ}"
LOG = rf"{BASE}\gs_reg{SEQ}.log"


def log(m):
    print(f"[flow] {m}", flush=True)


def sh(cmd, timeout=120):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       timeout=timeout, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def cmd(text):
    """写命令给驱动"""
    with open(CMDFILE, "w", encoding="utf-8") as f:
        f.write(text.rstrip("\n") + "\n")


def tail_log(n=1):
    if not os.path.exists(LOG):
        return ""
    with open(LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return "".join(lines[-n:])


def wait_log(pattern, timeout=180, label=""):
    """等日志出现 pattern"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if re.search(pattern, tail_log(200)):
            log(f"  ✅ {label or pattern}  ({int(time.time()-t0)}s)")
            return True
        time.sleep(2)
    log(f"  ❌ 超时 {label or pattern} ({timeout}s)")
    return False


def fetch_code(email, timeout=240, interval=8):
    """CLI 直查验证码（不用 MCP，避免缓存）"""
    log(f"  轮询收码: {email}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc, out = sh(f'python -X utf8 um.py code --address "{email}" '
                     f'--timeout 20 --raw', timeout=60)
        cwd_note = ""
        # 需要在 um 目录跑
        m = re.search(r'"value":\s*"(\d{6})"', out)
        if m:
            code = m.group(1)
            uid = re.search(r'"uid":\s*"([^"]+)"', out)
            log(f"  ✅ 收到验证码 {code}  uid={uid.group(1) if uid else '?'}  "
                f"({int(time.time()-t0)}s)")
            return code
        time.sleep(interval)
    log(f"  ❌ 收码超时 ({timeout}s)")
    return None


def fetch_code_um(email, timeout=240, interval=8):
    """在 um 目录里跑 CLI 收码"""
    log(f"  轮询收码（um CLI）: {email}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        rc, out = sh(f'cd /d "{UM_DIR}" && python -X utf8 um.py code '
                     f'--address "{email}" --timeout 20 --raw', timeout=90)
        m = re.search(r'"value":\s*"(\d{6})"', out)
        if m:
            code = m.group(1)
            uid = re.search(r'"uid":\s*"([^"]+)"', out)
            log(f"  ✅ 验证码 {code}  uid={uid.group(1) if uid else '?'}  "
                f"({int(time.time()-t0)}s)")
            return code
        time.sleep(interval)
    log(f"  ❌ 收码超时 ({timeout}s)")
    return None


# ============ 1. 启动驱动 ============
if not args.no_start:
    log(f"配置驱动: seq={SEQ} email={args.email}")
    src = open(DRIVER, encoding="utf-8").read()
    src = re.sub(r'PROFILE\s*=\s*r"[^"]*"', lambda m: f'PROFILE = r"{PROFILE}"', src)
    src = re.sub(r'OUT\s*=\s*r"[^"]*"', lambda m: f'OUT     = r"{OUT}"', src)
    src = re.sub(r'LOG\s*=\s*r"[^"]*"', lambda m: f'LOG     = r"{LOG}"', src)
    src = re.sub(r'EMAIL\s*=\s*"[^"]*"', lambda m: f'EMAIL   = "{args.email}"', src)
    open(DRIVER, "w", encoding="utf-8").write(src)
    log("✅ 驱动配置已更新")

    if os.path.exists(CMDFILE):
        os.remove(CMDFILE)
    log("启动驱动（浏览器窗口会打开）...")
    # Windows: DETACHED_PROCESS 让子进程独立于父进程，避免父进程退出时被杀
    DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [PY312, "gs_reg_driver.py", "open"],
        cwd=BASE, creationflags=DETACHED,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        env={**os.environ, "https_proxy": "", "http_proxy": ""})
    time.sleep(5)
    # 二次检查：确认驱动进程真的活着
    rc, ps = sh('powershell -NoProfile -Command "Get-CimInstance Win32_Process '
                '-Filter \\"Name=\'python.exe\'\\" | Where-Object { $_.CommandLine '
                '-like \'*gs_reg_driver4*\' } | Select-Object -ExpandProperty ProcessId"',
                timeout=30)
    alive = bool(ps.strip())
    log(f"驱动进程存活检查: {'✅ 活着' if alive else '❌ 未启动'}  pid={ps.strip()[:40]}")
    if not alive:
        log("❌ 驱动启动失败，退出")
        sys.exit(1)
    # 等邮箱填入
    if not wait_log(r"fill:#email\] ok", timeout=200, label="邮箱已填"):
        sys.exit(1)

# ============ 2. 立刻收码 ============
log("开始收码（此时你应已过人机验证并点了 Send verification code）")
code = fetch_code_um(args.email, timeout=300)
if not code:
    log("❌ 没收到验证码，退出")
    sys.exit(1)

# ============ 3. 填码 → Verify → 密码 → Create ============
log(f"填入验证码 {code}")
cmd(f"type=#emailVerificationCode|{code}")
time.sleep(20)

log("点 Verify code")
cmd("click=Verify code")
time.sleep(28)

log("填两次密码")
cmd("password")
if not wait_log(r"fill:#reenterPassword\] ok", timeout=180, label="密码已填"):
    log("⚠️ 密码填入异常，继续尝试")

time.sleep(3)
log("点 Create")
cmd("create")

# ============ 4. 等注册成功 ============
if wait_log(r"state:after_create\] url=https://www\.genspark\.ai/",
            timeout=120, label="注册成功"):
    log("🎉 注册成功！")
else:
    log("⚠️ 未检测到成功跳转，请检查浏览器窗口")
    log("最后日志:")
    print(tail_log(8))
    sys.exit(1)

# ============ 5. 自动提取 cookie ============
log("停驱动释放 profile 锁...")
cmd("quit")
time.sleep(12)
if os.path.exists(CMDFILE):
    os.remove(CMDFILE)

log("提取 cookie...")
EXPORT = os.path.join(BASE, f"gs_export{SEQ}.py")
COOKIE_FILE = os.path.join(BASE, f"gs_cookies{SEQ}.json")
# 用占位符模板生成（避免正则替换的路径转义坑）
tpl = open(os.path.join(BASE, "gs_export_template.py"), encoding="utf-8").read()
tpl = (tpl.replace("{{PROFILE}}", PROFILE)
          .replace("{{ACCOUNT}}", str(SEQ))
          .replace("{{EMAIL}}", args.email)
          .replace("{{COOKIE_FILE}}", COOKIE_FILE))
open(EXPORT, "w", encoding="utf-8").write(tpl)
log(f"生成 {EXPORT}")

# 用干净 env 直接跑（不依赖 shell 的 unset）
env = {k: v for k, v in os.environ.items()
       if k.lower() not in ("https_proxy", "http_proxy", "all_proxy",
                            "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")}
try:
    pr = subprocess.run([PY312, EXPORT], cwd=BASE, env=env,
                        capture_output=True, text=True, timeout=300,
                        encoding="utf-8", errors="replace")
    out = (pr.stdout or "") + (pr.stderr or "")
except Exception as e:
    out = f"EXC {type(e).__name__}: {e}"
log("提取输出:")
for line in out.strip().split("\n")[-8:]:
    log(f"  {line}")

# 读回 cogen_id
cogen = None
if os.path.exists(COOKIE_FILE):
    try:
        cj = json.load(open(COOKIE_FILE, encoding="utf-8"))
        cogen = cj.get("cogen_id")
        log(f"✅ cookie 已导出: gs_cookies{SEQ}.json  cogen_id={cogen}")
    except Exception as e:
        log(f"⚠️ 读 cookie 失败: {e}")
else:
    log(f"❌ 未生成 {COOKIE_FILE}")

# 读密码
pwd = None
if os.path.exists(LOG):
    mm = re.search(r"\[password\]\s+(\S+)", open(LOG, encoding="utf-8", errors="replace").read())
    if mm:
        pwd = mm.group(1)
        log(f"密码: {pwd}")

# ============ 6. 写入映射文件 ============
if cogen:
    mapf = os.path.join(BASE, "genspark-ipmap.json")
    mp = json.load(open(mapf, encoding="utf-8"))
    mp["accounts"] = [a for a in mp["accounts"] if a.get("seq") != int(SEQ)]
    mp["accounts"].append({
        "seq": int(SEQ), "email": args.email, "password": pwd,
        "cogen_id": cogen, "credits": 100,
        "cookie_file": COOKIE_FILE, "proxy": os.environ.get("GS_PROXY", ""),
        "status": "active",
        "note": f"{time.strftime('%Y-%m-%d')} 注册（编排脚本）",
    })
    mp["accounts"].sort(key=lambda a: a.get("seq", 0))
    json.dump(mp, open(mapf, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    log(f"✅ 已写入映射文件（当前 {len(mp['accounts'])} 个账号）")

log("完成。如需入池，重启反代: genspark_rp3.py")
