"""Genspark 网页端反代 —— 多账号轮转版

架构（标准网页端反代，浏览器不在链路里）：
  浏览器只用于登录取 cookie（gs_login.py）→ curl_cffi 纯 HTTP 转发

多账号轮转：
  读 accounts.json → 每个号一份 cookie + 独立 proxy
  轮转策略：round-robin + 失败自动切下一个号
  429/配额耗尽 → 冷却该号，切下一个

端点：
  POST /v1/chat/completions   (OpenAI 兼容，支持 stream)
  GET  /v1/models
  GET  /health
  GET  /state
"""
import json
import os
import secrets
import sys
import threading
import time
import uuid

from curl_cffi import requests as cffi
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# PyInstaller onefile: __file__ 指向临时解压目录，需要用 sys.executable 定位
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
MAP_FILE = os.environ.get("GS_ACCOUNTS", os.path.join(BASE, "accounts.json"))
PORT = int(os.environ.get("GS_PORT", "8899"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")
UPSTREAM = "https://www.genspark.ai/api/agent/ask_proxy"
REFERER = "https://www.genspark.ai/agents?type=ai_chat"

# 模型清单（2026-09-23 实测 53 个中 50 个可用）
MODELS = [
    # openai (23)
    "gpt-5", "gpt-5.1", "gpt-5.2", "gpt-5.4", "gpt-5.5", "gpt-5.6", "gpt-6",
    "gpt-5-pro", "gpt-5.1-high", "gpt-5.1-low", "gpt-5.1-medium",
    "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.4-pro", "gpt-5.5-pro",
    "gpt-5.6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-6-luna", "gpt-6-sol",
    # anthropic (10)
    "claude-4-5-haiku", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
    "claude-opus-5", "claude-opus-5-5", "claude-sonnet-4", "claude-sonnet-4-5",
    "claude-sonnet-4-6", "claude-sonnet-5",
    # google (6)
    "gemini-2.5-flash", "gemini-3.1-flash-lite-preview", "gemini-3.1-pro-preview",
    "gemini-3.6-flash", "gemini-3.7-flash", "gemini-3.8-flash",
    # genspark (11)
    "GLM-5.3", "deep-seek-v4-flash", "deep-seek-v4.1-flash", "glm-5p3",
    "glm-5p3-flash-baseten", "grok-4.5", "grok-4.6", "grok-4.7",
    "kimi-k3", "minimax-m3", "nemotron-3-ultra",
]
# 别名映射（用户可能用 API 风格的名字）
ALIAS = {
    "claude-haiku-4-5": "claude-4-5-haiku",
    "claude-opus-4-5": "claude-opus-4-6",
}

LOCK = threading.Lock()
_rr = 0


class Account:
    def __init__(self, d):
        self.seq = d.get("seq")
        self.email = d.get("email")
        self.cogen_id = d.get("cogen_id")
        cf = d.get("cookie_file")
        self.cookie_file = cf
        self.proxy = d.get("proxy") or d.get("proxy_default") or os.environ.get("GS_PROXY", "")
        self.cookie = ""
        self.cooldown_until = 0.0
        self.stats = {"ok": 0, "fail": 0, "throttle": 0}
        self._session = None
        self.load()

    def load(self):
        if not self.cookie_file or not os.path.exists(self.cookie_file):
            # 没有文件时保留已有 cookie（可能是直接赋值/导入的）
            if not self.cookie:
                self.cookie = ""
            return
        d = json.load(open(self.cookie_file, encoding="utf-8"))
        self.cookie = "; ".join(f"{c['name']}={c['value']}"
                                for c in d.get("cookies", []) if c.get("name"))

    @property
    def ready(self):
        return bool(self.cookie) and time.time() >= self.cooldown_until

    def cooldown(self, secs):
        self.cooldown_until = time.time() + secs

    def session(self):
        if self._session is None:
            self._session = cffi.Session(impersonate="chrome")
        return self._session

    def headers(self):
        rid = "|" + uuid.uuid4().hex + "." + uuid.uuid4().hex[:16]
        p = rid.lstrip("|").split(".")
        return {
            "User-Agent": UA, "Content-Type": "application/json",
            "Accept": "text/event-stream", "Origin": "https://www.genspark.ai",
            "Referer": REFERER, "request-id": rid,
            "traceparent": f"00-{p[0]}-{p[1]}-01", "Cookie": self.cookie,
        }

    @property
    def proxies(self):
        if not self.proxy:
            return None
        return {"https": self.proxy, "http": self.proxy}

    def to_dict(self):
        return {
            "seq": self.seq,
            "email": self.email,
            "cookie_file": self.cookie_file,
            "proxy": self.proxy,
        }


def load_accounts():
    if not os.path.exists(MAP_FILE):
        # 首次运行（如双击 exe）没有配置文件时，创建空配置而不是崩溃。
        # 管理面板可以从零添加账号。
        try:
            with open(MAP_FILE, "w", encoding="utf-8") as f:
                json.dump({"accounts": []}, f, indent=2, ensure_ascii=False)
            print(f"[init] 未找到 {MAP_FILE}，已创建空配置；可在管理面板添加账号", flush=True)
        except OSError as e:
            print(f"[init] 无法创建 {MAP_FILE}: {e}（本次以空账号运行）", flush=True)
        return []
    d = json.load(open(MAP_FILE, encoding="utf-8"))
    accts = []
    for a in d.get("accounts", []):
        if a.get("status") == "disabled":
            continue
        acc = Account(a)
        if acc.cookie:
            accts.append(acc)
    return accts


ACCOUNTS = load_accounts()
print(f"[init] 加载 {len(ACCOUNTS)} 个账号: "
      f"{[(a.seq, a.email[:22]) for a in ACCOUNTS]}", flush=True)


def save_accounts():
    """将当前 ACCOUNTS 列表写回 accounts.json。

    保留原文件顶层字段（channel/note/proxy_default 等）；
    已存在的账号条目按 seq 合并，保留 password/cogen_id/credits/note 等字段。
    """
    data = {"accounts": []}
    if os.path.exists(MAP_FILE):
        try:
            with open(MAP_FILE, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"accounts": []}
    old = {}
    for a in data.get("accounts", []):
        old.setdefault(a.get("seq"), a)
    merged = []
    for a in ACCOUNTS:
        entry = dict(old.get(a.seq) or {})
        entry.update(a.to_dict())
        merged.append(entry)
    data["accounts"] = merged
    tmp = MAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MAP_FILE)


ADMIN_PASSWORD = os.environ.get("GS_ADMIN_PASSWORD", "admin123")
TOKEN_TTL = 24 * 3600
# {token: expiry_timestamp}，进程内存存储，重启即失效
ADMIN_TOKENS = {}


def verify_admin(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未授权")
    token = authorization[7:]
    expiry = ADMIN_TOKENS.get(token)
    if expiry is None:
        raise HTTPException(status_code=401, detail="token 无效")
    if time.time() > expiry:
        ADMIN_TOKENS.pop(token, None)
        raise HTTPException(status_code=401, detail="token 已过期")
    return token


def find_account(seq):
    for a in ACCOUNTS:
        if a.seq == seq:
            return a
    return None


def pick():
    """round-robin 选可用账号"""
    global _rr
    with LOCK:
        ready = [a for a in ACCOUNTS if a.ready]
        if not ready:
            return None
        a = ready[_rr % len(ready)]
        _rr += 1
        return a


def build_body(payload):
    m = payload.get("model") or "claude-4-5-haiku"
    m = ALIAS.get(m, m)
    return {
        "ai_chat_model": m,
        "ai_chat_enable_search": False,
        "ai_chat_disable_personalization": False,
        "use_moa_proxy": False, "moa_models": [], "writingContent": None,
        "sas_ask_origin": "typed", "type": "ai_chat", "is_private": True,
        "messages": payload.get("messages") or [],
    }


def parse_sse(text):
    content, deltas, throttle, err = None, [], None, None
    for line in text.split("\n"):
        if not line.startswith("data: "):
            continue
        try:
            j = json.loads(line[6:])
        except Exception:
            continue
        t = j.get("type")
        if t == "message_field" and j.get("field_name") == "content":
            content = j.get("field_value")
        if t == "message_field_delta" and j.get("field_name") == "content":
            deltas.append(j.get("delta") or "")
        if t == "message_result" and isinstance(j.get("message"), dict):
            mc = j["message"].get("content") or ""
            if "too quickly" in mc or "Rate limit" in mc or "积分已用完" in mc:
                throttle = mc[:200]
            elif not content:
                content = mc
        if t == "error":
            err = json.dumps(j)[:300]
    return content, "".join(deltas), throttle, err


app = FastAPI()
START = time.time()

# 允许从 Genspark 页面（bookmarklet）跨域 POST cookie 回本地管理端
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.genspark.ai",
        "https://genspark.ai",
    ],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get("/health")
def health():
    return {
        "ok": True, "uptime_s": round(time.time() - START, 1),
        "accounts": [{
            "seq": a.seq, "email": a.email[:26],
            "ready": a.ready,
            "cooldown_left_s": max(0, round(a.cooldown_until - time.time())),
            "stats": a.stats,
        } for a in ACCOUNTS],
    }


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "genspark-web"} for m in MODELS]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    payload = await req.json()
    model = payload.get("model") or "claude-4-5-haiku"
    want_stream = bool(payload.get("stream"))
    body = build_body(payload)
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    # 尝试轮转（最多试 3 个号）
    last_err = None
    for attempt in range(3):
        acct = pick()
        if acct is None:
            return JSONResponse(
                {"error": {"message": "所有账号都在冷却中（配额耗尽）",
                           "type": "no_account"}}, status_code=429)
        s = acct.session()

        if not want_stream:
            try:
                r = s.post(UPSTREAM, headers=acct.headers(),
                           data=json.dumps(body), proxies=acct.proxies, timeout=120)
                t = r.text
            except Exception as e:
                acct.stats["fail"] += 1
                acct.cooldown(30)
                last_err = f"{type(e).__name__}: {e}"
                continue

            if r.status_code >= 400:
                acct.stats["fail"] += 1
                acct.cooldown(60)
                last_err = f"upstream_http_{r.status_code}"
                continue

            if "not login" in t:
                acct.stats["fail"] += 1
                acct.cooldown(300)
                last_err = "not_login"
                continue
            if "Rate limit" in t or "too quickly" in t:
                acct.stats["throttle"] += 1
                acct.cooldown(3600)
                last_err = "rate_limit"
                continue

            content, joined, throttle, err = parse_sse(t)
            if throttle:
                acct.stats["throttle"] += 1
                acct.cooldown(3600)
                last_err = "throttled"
                continue
            acct.stats["ok"] += 1
            return JSONResponse({
                "id": cid, "object": "chat.completion", "created": created,
                "model": model,
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": content or joined or ""}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                "x_genspark": {"account": acct.seq, "email": acct.email[:22],
                               "upstream_status": r.status_code, "raw_len": len(t)},
            })

        # 流式
        def gen(a=acct, b=body, i=cid, cr=created, mo=model):
            yield f'data: {json.dumps({"id": i, "object": "chat.completion.chunk", "created": cr, "model": mo, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})}\n\n'
            buf, emitted, failed = "", 0, False
            try:
                r = a.session().post(
                    UPSTREAM, headers=a.headers(), data=json.dumps(b),
                    proxies=a.proxies, timeout=120, stream=True)
                for chunk in r.iter_content(chunk_size=None):
                    if not chunk:
                        continue
                    buf += chunk.decode("utf-8", "replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line.startswith("data: "):
                            continue
                        try:
                            j = json.loads(line[6:])
                        except Exception:
                            continue
                        if j.get("type") == "message_field_delta" and j.get("field_name") == "content":
                            d = j.get("delta") or ""
                            if d:
                                emitted += 1
                                yield f'data: {json.dumps({"id": i, "object": "chat.completion.chunk", "created": cr, "model": mo, "choices": [{"index": 0, "delta": {"content": d}, "finish_reason": None}]})}\n\n'
                        elif j.get("type") == "message_result" and isinstance(j.get("message"), dict):
                            mc = j["message"].get("content") or ""
                            if ("too quickly" in mc or "Rate limit" in mc or "积分已用完" in mc) and emitted == 0:
                                failed = True
                                a.stats["throttle"] += 1
                                a.cooldown(3600)
                                yield f'data: {json.dumps({"error": {"message": mc[:200], "retry": True}})}\n\n'
            except Exception as e:
                failed = True
                a.stats["fail"] += 1
                a.cooldown(30)
                yield f'data: {json.dumps({"error": {"message": f"{type(e).__name__}: {e}", "retry": True}})}\n\n'
            finally:
                yield f'data: {json.dumps({"id": i, "object": "chat.completion.chunk", "created": cr, "model": mo, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'
                yield "data: [DONE]\n\n"
                if not failed:
                    a.stats["ok"] += 1

        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse({"error": {"message": f"所有账号都失败: {last_err}"}},
                        status_code=502)


# ---------- 管理后台 ----------

class LoginBody(BaseModel):
    password: str


class AccountCreateBody(BaseModel):
    seq: int
    email: str = ""
    cookie_file: str = ""
    cookie: str = ""
    proxy: str = ""


class CooldownBody(BaseModel):
    seconds: int = Field(ge=0)


def account_view(a):
    now = time.time()
    cooldown_left = max(0, round(a.cooldown_until - now))
    return {
        "seq": a.seq,
        "email": a.email,
        "cookie_file": a.cookie_file,
        "proxy": a.proxy,
        "status": "cooldown" if cooldown_left else ("ready" if a.ready else "no_cookie"),
        "ready": a.ready,
        "cooldown_left_s": cooldown_left,
        "stats": dict(a.stats),
    }


@app.post("/api/admin/login")
def admin_login(body: LoginBody):
    if not secrets.compare_digest(body.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="密码错误")
    token = secrets.token_hex(32)
    ADMIN_TOKENS[token] = time.time() + TOKEN_TTL
    return {"token": token}


@app.get("/api/admin/session")
def admin_session(token: str = Depends(verify_admin)):
    return {"ok": True, "expires_in_s": round(ADMIN_TOKENS[token] - time.time())}


@app.get("/api/admin/stats")
def admin_stats(_: str = Depends(verify_admin)):
    views = [account_view(a) for a in ACCOUNTS]
    return {
        "total_accounts": len(ACCOUNTS),
        "ready_accounts": sum(1 for v in views if v["ready"]),
        "cooldown_accounts": sum(1 for v in views if v["cooldown_left_s"] > 0),
        "total_ok": sum(a.stats["ok"] for a in ACCOUNTS),
        "total_fail": sum(a.stats["fail"] for a in ACCOUNTS),
        "total_throttle": sum(a.stats["throttle"] for a in ACCOUNTS),
        "accounts": views,
    }


@app.get("/api/admin/accounts")
def admin_list_accounts(_: str = Depends(verify_admin)):
    return {"accounts": [account_view(a) for a in ACCOUNTS]}


@app.post("/api/admin/accounts", status_code=201)
def admin_add_account(body: AccountCreateBody, _: str = Depends(verify_admin)):
    if find_account(body.seq) is not None:
        raise HTTPException(status_code=409, detail=f"seq {body.seq} 已存在")
    d = body.model_dump()
    raw_cookie = d.pop("cookie", "")
    acc = Account(d)
    if raw_cookie:
        # 直接传入 cookie 字符串：写成文件，同时赋值给运行中的账号
        n = 1
        while os.path.exists(os.path.join(BASE, f"cookies{n}.json")):
            n += 1
        fname = f"cookies{n}.json"
        fpath = os.path.join(BASE, fname)
        pairs = [{"name": p.split("=", 1)[0].strip(), "value": p.split("=", 1)[1].strip(),
                  "domain": ".genspark.ai", "path": "/"}
                 for p in raw_cookie.split(";") if "=" in p]
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump({"exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "source": "admin_panel_paste", "cookies": pairs},
                      f, ensure_ascii=False, indent=2)
        acc.cookie_file = fname
        acc.cookie = raw_cookie
    with LOCK:
        ACCOUNTS.append(acc)
        save_accounts()
    return account_view(acc)


@app.delete("/api/admin/accounts/{seq}")
def admin_delete_account(seq: int, _: str = Depends(verify_admin)):
    acc = find_account(seq)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"seq {seq} 不存在")
    with LOCK:
        ACCOUNTS.remove(acc)
        save_accounts()
    return {"ok": True, "deleted": seq}


@app.post("/api/admin/accounts/{seq}/cooldown")
def admin_cooldown_account(seq: int, body: CooldownBody,
                           _: str = Depends(verify_admin)):
    acc = find_account(seq)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"seq {seq} 不存在")
    acc.cooldown(body.seconds)
    return account_view(acc)


@app.post("/api/admin/accounts/{seq}/reset")
def admin_reset_account(seq: int, _: str = Depends(verify_admin)):
    acc = find_account(seq)
    if acc is None:
        raise HTTPException(status_code=404, detail=f"seq {seq} 不存在")
    acc.cooldown_until = 0.0
    acc.load()  # cookie 文件可能已更新，顺便重载
    return account_view(acc)


# ---------- 浏览器获取 Cookie（轻量版：新标签页 + fetch 回传） ----------

class CookieImportBody(BaseModel):
    cookies: list = []
    email: str = ""


@app.get("/api/admin/genspark-login-url")
def admin_genspark_login_url(_: str = Depends(verify_admin)):
    """返回 Genspark 登录页 URL，前端用 window.open 打开。"""
    return {"url": "https://www.genspark.ai/agents?type=ai_chat"}


@app.post("/api/admin/import-cookie")
def admin_import_cookie(body: CookieImportBody, _: str = Depends(verify_admin)):
    """接收浏览器端 POST 回来的 cookie 列表，保存为 cookiesN.json 文件。"""
    if not body.cookies:
        raise HTTPException(status_code=400, detail="cookie 列表为空")
    # 检查关键 cookie
    names = {c.get("name") for c in body.cookies if isinstance(c, dict)}
    if "session_id" not in names:
        raise HTTPException(status_code=400, detail="缺少 session_id，请确认已在 Genspark 登录")
    # 找一个不冲突的文件名
    n = 1
    while os.path.exists(os.path.join(BASE, f"cookies{n}.json")):
        n += 1
    fname = f"cookies{n}.json"
    fpath = os.path.join(BASE, fname)
    cookie_data = {
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "browser_tab_import",
        "cookies": body.cookies,
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(cookie_data, f, ensure_ascii=False, indent=2)
    return {"ok": True, "cookie_file": fname, "count": len(body.cookies)}


# PyInstaller onefile 时 static/ 在临时解压目录里
_STATIC_CANDIDATES = [
    os.path.join(BASE, "static"),
    os.path.join(getattr(sys, '_MEIPASS', ''), "static"),
]
STATIC_DIR = next((p for p in _STATIC_CANDIDATES if os.path.isdir(p)),
                  _STATIC_CANDIDATES[0])
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def admin_page():
    html_path = os.path.join(STATIC_DIR, "admin.html")
    if os.path.exists(html_path):
        with open(html_path, encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("<h1>管理面板未安装</h1>", status_code=404)


if __name__ == "__main__":
    import uvicorn
    print(f"[main] serving on :{PORT}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    except OSError as e:
        # 常见：端口已被占用（已在运行的实例）。双击时给出可读提示再退出。
        print(f"[fatal] 启动失败: {e}", flush=True)
        print(f"[fatal] 端口 {PORT} 可能已被占用（是否已有实例在运行？）", flush=True)
        sys.exit(1)
