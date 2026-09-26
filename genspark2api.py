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
import logging
import os
import re
import secrets
import sys
import threading
import time
import uuid
from collections import deque

from curl_cffi import requests as cffi
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (HTMLResponse, JSONResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# PyInstaller onefile: __file__ 指向临时解压目录，需要用 sys.executable 定位
if getattr(sys, 'frozen', False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
# 数据目录：accounts.json / config.json / cookies*.json / logs / 抓号 profile。
# Linux/容器部署时用 GS_DATA_DIR 指向挂载卷即可持久化，代码目录保持只读。
DATA = os.environ.get("GS_DATA_DIR", BASE)
os.makedirs(DATA, exist_ok=True)
MAP_FILE = os.environ.get("GS_ACCOUNTS", os.path.join(DATA, "accounts.json"))
PORT = int(os.environ.get("GS_PORT", "8899"))
# 监听地址：默认仅本机回环（Windows 双击场景安全）；容器/服务器用 GS_HOST=0.0.0.0
HOST = os.environ.get("GS_HOST", "127.0.0.1")
VERSION = "1.2.0"

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

# ---------- 运行时日志：内存环形缓冲，管理后台可查看 ----------
_RUNTIME_LOGS = deque(maxlen=500)
_RTLOG_LOCK = threading.Lock()
_rtlog_id = 0


def rlog(level, msg):
    """打印到控制台，同时写入运行时日志环形缓冲。"""
    global _rtlog_id
    line = f"[{level}] {msg}"
    print(line, flush=True)
    with _RTLOG_LOCK:
        _rtlog_id += 1
        _RUNTIME_LOGS.append({
            "id": _rtlog_id,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": str(msg),
        })


class _RuntimeLogHandler(logging.Handler):
    """把 uvicorn / 第三方库的 logging 输出也接进环形缓冲。"""

    def emit(self, record):
        global _rtlog_id
        try:
            msg = self.format(record)
        except Exception:
            return
        with _RTLOG_LOCK:
            _rtlog_id += 1
            _RUNTIME_LOGS.append({
                "id": _rtlog_id,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname.lower(),
                "message": msg,
            })


logging.getLogger().addHandler(_RuntimeLogHandler())

# ---------- 请求日志：内存环形缓冲 + JSONL 持久化 ----------
LOG_DIR = os.path.join(DATA, "logs")
LOG_FILE = os.path.join(LOG_DIR, "requests.jsonl")
REQUEST_LOGS = deque(maxlen=1000)   # 最新在最左
_REQLOG_LOCK = threading.Lock()


def _load_request_logs():
    if not os.path.exists(LOG_FILE):
        return
    try:
        with open(LOG_FILE, encoding="utf-8") as f:
            lines = f.readlines()[-REQUEST_LOGS.maxlen:]
        for line in lines:
            try:
                REQUEST_LOGS.append(json.loads(line))
            except Exception:
                continue
        # 文件里是时间正序，翻转为最新在前
        REQUEST_LOGS.reverse()
    except Exception:
        pass


def record_request(model, account=None, stream=False, status="ok",
                   latency_ms=0, attempts=1, error=""):
    entry = {
        "id": uuid.uuid4().hex[:12],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ts": time.time(),
        "model": model,
        "account_seq": getattr(account, "seq", None),
        "email": (getattr(account, "email", "") or "")[:26],
        "stream": bool(stream),
        "status": status,           # ok / fail / throttle / no_account
        "latency_ms": round(latency_ms),
        "attempts": attempts,
        "error": (error or "")[:200],
    }
    with _REQLOG_LOCK:
        REQUEST_LOGS.appendleft(entry)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


_load_request_logs()


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
        path = self.cookie_file
        if path and not os.path.isabs(path):
            path = os.path.join(DATA, path)
        if not path or not os.path.exists(path):
            # 没有文件时保留已有 cookie（可能是直接赋值/导入的）
            if not self.cookie:
                self.cookie = ""
            return
        d = json.load(open(path, encoding="utf-8"))
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


def _next_cookie_fname():
    n = 1
    while os.path.exists(os.path.join(DATA, f"cookies{n}.json")):
        n += 1
    return f"cookies{n}.json"


def _write_cookie_file(pairs, source):
    """把 cookie 列表写到下一个空闲的 cookiesN.json，返回文件名。"""
    fname = _next_cookie_fname()
    with open(os.path.join(DATA, fname), "w", encoding="utf-8") as f:
        json.dump({"exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "source": source, "cookies": pairs},
                  f, ensure_ascii=False, indent=2)
    return fname


def _prune_cookie_files():
    """删除没有账号引用的 cookiesN.json（只认数字编号文件名，避免误删）。"""
    referenced = {a.cookie_file for a in ACCOUNTS if a.cookie_file}
    removed = 0
    for name in os.listdir(DATA):
        if not name.startswith("cookies") or not name.endswith(".json"):
            continue
        stem = name[len("cookies"):-len(".json")]
        if not stem.isdigit() or name in referenced:
            continue
        try:
            os.remove(os.path.join(DATA, name))
            removed += 1
        except OSError:
            pass
    return removed


def load_accounts():
    if not os.path.exists(MAP_FILE):
        # 首次运行（如双击 exe）没有配置文件时，创建空配置而不是崩溃。
        # 管理面板可以从零添加账号。
        try:
            with open(MAP_FILE, "w", encoding="utf-8") as f:
                json.dump({"accounts": []}, f, indent=2, ensure_ascii=False)
            rlog("init", f"未找到 {MAP_FILE}，已创建空配置；可在管理面板添加账号")
        except OSError as e:
            rlog("init", f"无法创建 {MAP_FILE}: {e}（本次以空账号运行）")
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
rlog("init", f"加载 {len(ACCOUNTS)} 个账号: "
             f"{[(a.seq, a.email[:22]) for a in ACCOUNTS]}")
_pruned = _prune_cookie_files()
if _pruned:
    rlog("init", f"清理 {_pruned} 个未被账号引用的 cookie 文件")


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


# ---- 管理员密码：优先环境变量，其次配置文件，最后默认值 ----
_CONFIG_FILE = os.path.join(DATA, "config.json")


def _load_config():
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg):
    tmp = _CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CONFIG_FILE)


def _load_password():
    env = os.environ.get("GS_ADMIN_PASSWORD")
    if env:
        return env
    return _load_config().get("admin_password", "admin123")


ADMIN_PASSWORD = _load_password()
TOKEN_TTL = 24 * 3600
# {token: expiry_timestamp}，进程内存存储，重启即失效
ADMIN_TOKENS = {}

# ---- 运行时设置：管理后台可改，持久化在 config.json 的 settings ----
DEFAULT_SETTINGS = {
    "default_model": "claude-4-5-haiku",  # 请求未指定模型时使用
    "request_timeout_s": 120,             # 上游请求超时（秒）
    "retry_attempts": 3,                  # 失败时最多换号重试次数
    "cooldown_fail_s": 60,                # 普通失败冷却时长
    "cooldown_throttle_s": 3600,          # 限流冷却时长
    "cooldown_not_login_s": 300,          # cookie 失效冷却时长
}


def _load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        saved = _load_config().get("settings") or {}
        for k in DEFAULT_SETTINGS:
            if k in saved:
                s[k] = saved[k]
    except Exception:
        pass
    return s


SETTINGS = _load_settings()


def _persist_settings():
    cfg = _load_config()
    cfg["settings"] = SETTINGS
    _save_config(cfg)

# ---- API 密钥：调用 /v1/* 时用作 Bearer 凭证，持久化在 config.json 的 api_keys ----
# 密钥为对象：{id, name, key, enabled, created_at, last_used_at}
# 兼容旧格式（纯字符串列表），加载时自动迁移。
def _new_key(name="默认密钥"):
    return {
        "id": secrets.token_hex(4),
        "name": name,
        "key": "sk-" + secrets.token_hex(24),
        "enabled": True,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_used_at": None,
    }


def _migrate_key(item):
    if isinstance(item, dict) and item.get("key"):
        item.setdefault("id", secrets.token_hex(4))
        item.setdefault("name", "默认密钥")
        item.setdefault("enabled", True)
        item.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        item.setdefault("last_used_at", None)
        return item
    if isinstance(item, str) and item:
        return {"id": secrets.token_hex(4), "name": "默认密钥", "key": item,
                "enabled": True,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "last_used_at": None}
    return None


def _init_api_keys():
    env = os.environ.get("GS_API_KEY")
    if env:
        return [{"id": secrets.token_hex(4), "name": "环境变量密钥", "key": env,
                 "enabled": True,
                 "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "last_used_at": None}]
    cfg = _load_config()
    raw = cfg.get("api_keys")
    keys = []
    if isinstance(raw, list):
        keys = [k for k in (_migrate_key(i) for i in raw) if k]
    if not keys:
        keys = [_new_key()]
    cfg["api_keys"] = keys
    try:
        _save_config(cfg)
    except Exception:
        pass
    return keys


API_KEYS = _init_api_keys()
_KEYS_DIRTY = {"dirty": False, "last_flush": 0.0}


def _mark_key_used(record):
    """更新 last_used_at；写入限频，最多每分钟落盘一次。"""
    record["last_used_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _KEYS_DIRTY["dirty"] = True
    if time.time() - _KEYS_DIRTY["last_flush"] > 60:
        _flush_keys()


def _flush_keys():
    if not _KEYS_DIRTY["dirty"]:
        return
    _KEYS_DIRTY["dirty"] = False
    _KEYS_DIRTY["last_flush"] = time.time()
    if os.environ.get("GS_API_KEY"):
        return  # 环境变量密钥不落盘
    try:
        cfg = _load_config()
        cfg["api_keys"] = API_KEYS
        _save_config(cfg)
    except Exception:
        pass


def verify_api_key(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "缺少 API 密钥（Authorization: Bearer <key>）。"
                                         "请到管理后台「API 密钥」页生成。",
                              "type": "invalid_api_key"}})
    key = authorization[7:].strip()
    for rec in API_KEYS:
        if rec.get("enabled") and secrets.compare_digest(key, rec["key"]):
            _mark_key_used(rec)
            return key
    if any(secrets.compare_digest(key, rec["key"]) for rec in API_KEYS):
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "API 密钥已被禁用",
                              "type": "invalid_api_key"}})
    raise HTTPException(
        status_code=401,
        detail={"error": {"message": "API 密钥无效",
                          "type": "invalid_api_key"}})
    return key


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
    m = payload.get("model") or SETTINGS["default_model"]
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
def models(_: str = Depends(verify_api_key)):
    return {"object": "list", "data": [
        {"id": m, "object": "model", "owned_by": "genspark-web"} for m in MODELS]}


@app.post("/v1/chat/completions")
async def chat(req: Request, _: str = Depends(verify_api_key)):
    payload = await req.json()
    model = payload.get("model") or SETTINGS["default_model"]
    want_stream = bool(payload.get("stream"))
    body = build_body(payload)
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    timeout = int(SETTINGS["request_timeout_s"])
    t0 = time.time()

    # 尝试轮转（最多试 3 个号）
    last_err = None
    last_acct = None
    attempts = 0
    for attempt in range(int(SETTINGS["retry_attempts"])):
        acct = pick()
        if acct is None:
            record_request(model, None, want_stream, "no_account",
                           (time.time() - t0) * 1000, attempts,
                           "所有账号都在冷却中")
            return JSONResponse(
                {"error": {"message": "所有账号都在冷却中（配额耗尽）",
                           "type": "no_account"}}, status_code=429)
        attempts += 1
        last_acct = acct
        s = acct.session()

        if not want_stream:
            try:
                r = s.post(UPSTREAM, headers=acct.headers(),
                           data=json.dumps(body), proxies=acct.proxies,
                           timeout=timeout)
                t = r.text
            except Exception as e:
                acct.stats["fail"] += 1
                acct.cooldown(int(SETTINGS["cooldown_fail_s"]))
                last_err = f"{type(e).__name__}: {e}"
                continue

            if r.status_code >= 400:
                acct.stats["fail"] += 1
                acct.cooldown(int(SETTINGS["cooldown_fail_s"]))
                last_err = f"upstream_http_{r.status_code}"
                continue

            if "not login" in t:
                acct.stats["fail"] += 1
                acct.cooldown(int(SETTINGS["cooldown_not_login_s"]))
                last_err = "not_login"
                continue
            if "Rate limit" in t or "too quickly" in t:
                acct.stats["throttle"] += 1
                acct.cooldown(int(SETTINGS["cooldown_throttle_s"]))
                last_err = "rate_limit"
                continue

            content, joined, throttle, err = parse_sse(t)
            if throttle:
                acct.stats["throttle"] += 1
                acct.cooldown(int(SETTINGS["cooldown_throttle_s"]))
                last_err = "throttled"
                continue
            acct.stats["ok"] += 1
            record_request(model, acct, False, "ok",
                           (time.time() - t0) * 1000, attempts)
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
        def gen(a=acct, b=body, i=cid, cr=created, mo=model, at=attempts):
            yield f'data: {json.dumps({"id": i, "object": "chat.completion.chunk", "created": cr, "model": mo, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]})}\n\n'
            buf, emitted, failed = "", 0, False
            status, err_msg = "ok", ""
            try:
                r = a.session().post(
                    UPSTREAM, headers=a.headers(), data=json.dumps(b),
                    proxies=a.proxies, timeout=timeout, stream=True)
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
                                status, err_msg = "throttle", mc[:200]
                                a.stats["throttle"] += 1
                                a.cooldown(int(SETTINGS["cooldown_throttle_s"]))
                                yield f'data: {json.dumps({"error": {"message": mc[:200], "retry": True}})}\n\n'
            except Exception as e:
                failed = True
                status, err_msg = "fail", f"{type(e).__name__}: {e}"
                a.stats["fail"] += 1
                a.cooldown(int(SETTINGS["cooldown_fail_s"]))
                yield f'data: {json.dumps({"error": {"message": f"{type(e).__name__}: {e}", "retry": True}})}\n\n'
            finally:
                yield f'data: {json.dumps({"id": i, "object": "chat.completion.chunk", "created": cr, "model": mo, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'
                yield "data: [DONE]\n\n"
                if not failed:
                    a.stats["ok"] += 1
                record_request(mo, a, True, status,
                               (time.time() - t0) * 1000, at, err_msg)

        return StreamingResponse(gen(), media_type="text/event-stream")

    record_request(model, last_acct, want_stream, "fail",
                   (time.time() - t0) * 1000, attempts,
                   f"所有账号都失败: {last_err}")
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


class PasswordBody(BaseModel):
    old_password: str
    new_password: str


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


@app.post("/api/admin/change-password")
def admin_change_password(body: PasswordBody, _: str = Depends(verify_admin)):
    global ADMIN_PASSWORD
    if not secrets.compare_digest(body.old_password, ADMIN_PASSWORD):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少 6 位")
    ADMIN_PASSWORD = body.new_password
    cfg = _load_config()
    cfg["admin_password"] = body.new_password
    _save_config(cfg)
    ADMIN_TOKENS.clear()  # 旧 token 全部失效，强制重新登录
    return {"ok": True}


@app.get("/api/admin/stats")
def admin_stats(_: str = Depends(verify_admin)):
    views = [account_view(a) for a in ACCOUNTS]
    with _REQLOG_LOCK:
        logs = list(REQUEST_LOGS)
    total_req = len(logs)
    ok_req = sum(1 for r in logs if r["status"] == "ok")
    return {
        "total_accounts": len(ACCOUNTS),
        "ready_accounts": sum(1 for v in views if v["ready"]),
        "cooldown_accounts": sum(1 for v in views if v["cooldown_left_s"] > 0),
        "total_ok": sum(a.stats["ok"] for a in ACCOUNTS),
        "total_fail": sum(a.stats["fail"] for a in ACCOUNTS),
        "total_throttle": sum(a.stats["throttle"] for a in ACCOUNTS),
        # 请求日志聚合（供仪表盘 + 图表）
        "total_requests": total_req,
        "success_requests": ok_req,
        "success_rate": round(ok_req / total_req * 100, 1) if total_req else 0.0,
        "recent_requests": logs[:10],
        "uptime_s": round(time.time() - START, 1),
        "version": VERSION,
        "accounts": views,
    }


@app.get("/api/admin/accounts")
def admin_list_accounts(_: str = Depends(verify_admin)):
    return {"accounts": [account_view(a) for a in ACCOUNTS]}


# ---------- API 密钥管理 ----------

def _persist_api_keys():
    if os.environ.get("GS_API_KEY"):
        return  # 环境变量密钥不落盘
    _KEYS_DIRTY["dirty"] = False
    _KEYS_DIRTY["last_flush"] = time.time()
    cfg = _load_config()
    cfg["api_keys"] = API_KEYS
    _save_config(cfg)


def _key_view(rec):
    k = rec["key"]
    return {
        "id": rec["id"],
        "name": rec.get("name", ""),
        "prefix": k[:10],
        "suffix": k[-4:],
        "enabled": bool(rec.get("enabled", True)),
        "created_at": rec.get("created_at"),
        "last_used_at": rec.get("last_used_at"),
    }


def _find_key(kid):
    for rec in API_KEYS:
        if rec["id"] == kid:
            return rec
    return None


@app.get("/api/admin/keys")
def admin_list_keys(_: str = Depends(verify_admin)):
    # 出于安全只返回前缀/后缀，完整密钥仅创建时展示一次
    return {"keys": [_key_view(k) for k in API_KEYS]}


class KeyCreateBody(BaseModel):
    name: str = ""


class KeyPatchBody(BaseModel):
    name: str = None
    enabled: bool = None


@app.post("/api/admin/keys", status_code=201)
def admin_create_key(body: KeyCreateBody = None, _: str = Depends(verify_admin)):
    name = (body.name if body else "") or f"密钥 {len(API_KEYS) + 1}"
    rec = _new_key(name)
    API_KEYS.append(rec)
    _persist_api_keys()
    # 完整 key 只在这一个响应里出现一次
    return {"key": rec["key"], "id": rec["id"], "name": rec["name"]}


@app.patch("/api/admin/keys/{kid}")
def admin_patch_key(kid: str, body: KeyPatchBody, _: str = Depends(verify_admin)):
    rec = _find_key(kid)
    if rec is None:
        raise HTTPException(status_code=404, detail="密钥不存在")
    if body.enabled is False and rec.get("enabled") \
            and sum(1 for k in API_KEYS if k.get("enabled")) <= 1:
        raise HTTPException(status_code=400, detail="至少保留一把启用的密钥")
    if body.name is not None:
        rec["name"] = body.name.strip() or rec["name"]
    if body.enabled is not None:
        rec["enabled"] = bool(body.enabled)
    _persist_api_keys()
    return _key_view(rec)


@app.delete("/api/admin/keys/{kid}")
def admin_delete_key(kid: str, _: str = Depends(verify_admin)):
    rec = _find_key(kid)
    if rec is None:
        raise HTTPException(status_code=404, detail="密钥不存在")
    if len(API_KEYS) <= 1:
        raise HTTPException(status_code=400, detail="至少保留一把密钥")
    API_KEYS.remove(rec)
    _persist_api_keys()
    return {"ok": True, "removed": _key_view(rec)}


@app.post("/api/admin/accounts", status_code=201)
def admin_add_account(body: AccountCreateBody, _: str = Depends(verify_admin)):
    if find_account(body.seq) is not None:
        raise HTTPException(status_code=409, detail=f"seq {body.seq} 已存在")
    d = body.model_dump()
    raw_cookie = d.pop("cookie", "")
    if d.get("cookie_file") and not raw_cookie:
        # 复用已存在的 cookiesN.json（如一键抓取的结果），不重复写盘
        if not os.path.exists(os.path.join(DATA, d["cookie_file"])):
            raise HTTPException(status_code=400,
                                detail=f"Cookie 文件不存在: {d['cookie_file']}")
        acc = Account(d)
        if not acc.cookie:
            raise HTTPException(status_code=400,
                                detail=f"Cookie 文件为空或缺少有效条目: {d['cookie_file']}")
    else:
        d["cookie_file"] = ""
        acc = Account(d)
        if raw_cookie:
            # 直接传入 cookie 字符串：写成文件，同时赋值给运行中的账号
            pairs = [{"name": p.split("=", 1)[0].strip(), "value": p.split("=", 1)[1].strip(),
                      "domain": ".genspark.ai", "path": "/"}
                     for p in raw_cookie.split(";") if "=" in p]
            acc.cookie_file = _write_cookie_file(pairs, "admin_panel_paste")
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
        removed = _prune_cookie_files()
    return {"ok": True, "deleted": seq, "cookie_files_removed": removed}


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


# ---------- 账号导入 / 导出 ----------

@app.get("/api/admin/accounts/export")
def admin_export_accounts(_: str = Depends(verify_admin)):
    """导出全部账号（含 cookie，可迁移到其他机器）。"""
    return {
        "version": VERSION,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "accounts": [{
            "seq": a.seq,
            "email": a.email,
            "proxy": a.proxy,
            "cookie": a.cookie,
        } for a in ACCOUNTS],
    }


class AccountImportBody(BaseModel):
    accounts: list = []


@app.post("/api/admin/accounts/import")
def admin_import_accounts(body: AccountImportBody, _: str = Depends(verify_admin)):
    """批量导入账号。已存在的 seq 跳过，返回导入结果。"""
    imported, skipped, failed = 0, 0, []
    for item in body.accounts:
        if not isinstance(item, dict):
            failed.append({"item": str(item)[:50], "reason": "格式错误"})
            continue
        seq = item.get("seq")
        cookie = item.get("cookie") or ""
        if seq is None or not cookie:
            failed.append({"seq": seq, "reason": "缺少 seq 或 cookie"})
            continue
        if find_account(seq) is not None:
            skipped += 1
            continue
        try:
            # 复用添加逻辑：写 cookie 文件 + 建账号
            pairs = [{"name": p.split("=", 1)[0].strip(),
                      "value": p.split("=", 1)[1].strip(),
                      "domain": ".genspark.ai", "path": "/"}
                     for p in cookie.split(";") if "=" in p]
            fname = _write_cookie_file(pairs, "admin_import")
            acc = Account({"seq": seq, "email": item.get("email", ""),
                           "cookie_file": fname,
                           "proxy": item.get("proxy", "")})
            with LOCK:
                ACCOUNTS.append(acc)
            imported += 1
        except Exception as e:
            failed.append({"seq": seq, "reason": str(e)[:100]})
    if imported:
        with LOCK:
            save_accounts()
    return {"ok": True, "imported": imported, "skipped": skipped,
            "failed": failed}


# ---------- 请求日志 ----------

@app.get("/api/admin/logs")
def admin_list_logs(model: str = "", status: str = "", account: str = "",
                    q: str = "", limit: int = 50, offset: int = 0,
                    _: str = Depends(verify_admin)):
    with _REQLOG_LOCK:
        logs = list(REQUEST_LOGS)
    if model:
        logs = [r for r in logs if r.get("model") == model]
    if status:
        logs = [r for r in logs if r.get("status") == status]
    if account:
        logs = [r for r in logs if str(r.get("account_seq")) == account
                or account.lower() in (r.get("email") or "").lower()]
    if q:
        ql = q.lower()
        logs = [r for r in logs
                if ql in (r.get("model") or "").lower()
                or ql in (r.get("email") or "").lower()
                or ql in (r.get("error") or "").lower()]
    total = len(logs)
    limit = max(1, min(limit, 200))
    return {"items": logs[offset:offset + limit], "total": total,
            "limit": limit, "offset": offset}


@app.delete("/api/admin/logs")
def admin_clear_logs(_: str = Depends(verify_admin)):
    with _REQLOG_LOCK:
        n = len(REQUEST_LOGS)
        REQUEST_LOGS.clear()
    try:
        if os.path.exists(LOG_FILE):
            os.remove(LOG_FILE)
    except Exception:
        pass
    return {"ok": True, "deleted": n}


@app.get("/api/admin/logs/stats")
def admin_log_stats(hours: int = 24, _: str = Depends(verify_admin)):
    hours = max(1, min(hours, 24 * 30))
    now = time.time()
    cutoff = now - hours * 3600
    with _REQLOG_LOCK:
        logs = [r for r in REQUEST_LOGS if r.get("ts", 0) >= cutoff]
    total = len(logs)
    ok = sum(1 for r in logs if r["status"] == "ok")
    lat = sorted(r.get("latency_ms", 0) for r in logs if r["status"] == "ok")

    def pct(p):
        if not lat:
            return 0
        return lat[min(len(lat) - 1, int(len(lat) * p))]

    # 按小时分桶
    bucket_s = 3600 if hours <= 48 else 86400
    first = int(cutoff // bucket_s) * bucket_s
    nbuckets = int((now - first) // bucket_s) + 1
    series = [{"at": time.strftime("%m-%d %H:00" if bucket_s == 3600 else "%m-%d",
                                   time.localtime(first + i * bucket_s)),
               "requests": 0, "ok": 0, "error": 0}
              for i in range(nbuckets)]
    idx = {first + i * bucket_s: i for i in range(nbuckets)}
    by_model, by_account = {}, {}
    for r in logs:
        b = idx.get(int(r.get("ts", 0) // bucket_s) * bucket_s)
        if b is not None:
            series[b]["requests"] += 1
            if r["status"] == "ok":
                series[b]["ok"] += 1
            else:
                series[b]["error"] += 1
        m = r.get("model") or "unknown"
        d = by_model.setdefault(m, {"key": m, "count": 0, "ok": 0, "error": 0})
        d["count"] += 1
        d["ok" if r["status"] == "ok" else "error"] += 1
        a = r.get("email") or (f"#{r['account_seq']}" if r.get("account_seq")
                               else "未知")
        d = by_account.setdefault(a, {"key": a, "count": 0, "ok": 0,
                                      "error": 0})
        d["count"] += 1
        d["ok" if r["status"] == "ok" else "error"] += 1
    return {
        "window": {"hours": hours},
        "totals": {
            "requests": total,
            "ok": ok,
            "error": total - ok,
            "success_rate": round(ok / total * 100, 1) if total else 0.0,
        },
        "latency": {
            "avg_ms": round(sum(lat) / len(lat)) if lat else 0,
            "p50_ms": pct(0.5),
            "p95_ms": pct(0.95),
        },
        "models": sorted(by_model.values(), key=lambda x: -x["count"])[:10],
        "accounts": sorted(by_account.values(), key=lambda x: -x["count"])[:10],
        "series": series,
    }


# ---------- 运行时日志 ----------

@app.get("/api/admin/runtime-logs")
def admin_runtime_logs(after: int = 0, limit: int = 200, level: str = "",
                       q: str = "", _: str = Depends(verify_admin)):
    with _RTLOG_LOCK:
        logs = list(_RUNTIME_LOGS)
    if after:
        logs = [r for r in logs if r["id"] > after]
    if level:
        logs = [r for r in logs if r["level"] == level]
    if q:
        ql = q.lower()
        logs = [r for r in logs if ql in r["message"].lower()]
    limit = max(1, min(limit, 500))
    return {"items": logs[-limit:], "total": len(_RUNTIME_LOGS)}


# ---------- 运行时设置 ----------

@app.get("/api/admin/settings")
def admin_get_settings(_: str = Depends(verify_admin)):
    return {"settings": dict(SETTINGS), "defaults": dict(DEFAULT_SETTINGS),
            "models": MODELS}


class SettingsPatchBody(BaseModel):
    default_model: str = None
    request_timeout_s: int = None
    retry_attempts: int = None
    cooldown_fail_s: int = None
    cooldown_throttle_s: int = None
    cooldown_not_login_s: int = None


@app.patch("/api/admin/settings")
def admin_patch_settings(body: SettingsPatchBody,
                         _: str = Depends(verify_admin)):
    if body.default_model is not None:
        m = ALIAS.get(body.default_model, body.default_model)
        if m not in MODELS:
            raise HTTPException(status_code=400,
                                detail=f"未知模型: {body.default_model}")
        SETTINGS["default_model"] = m
    for field, lo, hi in (("request_timeout_s", 10, 600),
                          ("retry_attempts", 1, 10),
                          ("cooldown_fail_s", 0, 86400),
                          ("cooldown_throttle_s", 0, 86400),
                          ("cooldown_not_login_s", 0, 86400)):
        v = getattr(body, field)
        if v is not None:
            if not (lo <= v <= hi):
                raise HTTPException(status_code=400,
                                    detail=f"{field} 需在 {lo}~{hi} 之间")
            SETTINGS[field] = v
    _persist_settings()
    return {"settings": dict(SETTINGS)}


# ---------- 系统信息 / 访问信息 ----------

@app.get("/api/admin/info")
def admin_info(_: str = Depends(verify_admin)):
    base = f"http://127.0.0.1:{PORT}"
    return {
        "version": VERSION,
        "port": PORT,
        "uptime_s": round(time.time() - START, 1),
        "model_count": len(MODELS),
        "account_count": len(ACCOUNTS),
        "base_url": base,
        "endpoints": {
            "chat_completions": f"{base}/v1/chat/completions",
            "models": f"{base}/v1/models",
            "health": f"{base}/health",
        },
        "data_files": {
            "accounts": MAP_FILE,
            "config": _CONFIG_FILE,
            "request_logs": LOG_FILE,
        },
    }


# ---------- 模型测试 ----------

class TestChatBody(BaseModel):
    model: str = ""
    content: str = "你好，请用一句话介绍自己"
    account_seq: int = None


@app.post("/api/admin/test-chat")
def admin_test_chat(body: TestChatBody, _: str = Depends(verify_admin)):
    """从后台直接发一条测试请求到上游，验证账号/模型可用性。"""
    model = ALIAS.get(body.model, body.model) or SETTINGS["default_model"]
    if model not in MODELS:
        raise HTTPException(status_code=400, detail=f"未知模型: {body.model}")
    acct = find_account(body.account_seq) if body.account_seq else pick()
    if acct is None:
        raise HTTPException(status_code=400,
                            detail="没有可用账号（可能都在冷却）")
    payload = {"model": model,
               "messages": [{"role": "user", "content": body.content}]}
    t0 = time.time()
    try:
        r = acct.session().post(
            UPSTREAM, headers=acct.headers(), data=json.dumps(build_body(payload)),
            proxies=acct.proxies, timeout=int(SETTINGS["request_timeout_s"]))
    except Exception as e:
        return {"ok": False, "account_seq": acct.seq, "email": acct.email,
                "model": model,
                "error": f"{type(e).__name__}: {e}",
                "latency_ms": round((time.time() - t0) * 1000)}
    latency = round((time.time() - t0) * 1000)
    if r.status_code >= 400:
        return {"ok": False, "account_seq": acct.seq, "email": acct.email,
                "model": model, "error": f"上游 HTTP {r.status_code}",
                "latency_ms": latency}
    if "not login" in r.text:
        return {"ok": False, "account_seq": acct.seq, "email": acct.email,
                "model": model, "error": "cookie 已失效（not login）",
                "latency_ms": latency}
    content, joined, throttle, err = parse_sse(r.text)
    if throttle:
        return {"ok": False, "account_seq": acct.seq, "email": acct.email,
                "model": model, "error": f"限流: {throttle}",
                "latency_ms": latency}
    return {"ok": True, "account_seq": acct.seq, "email": acct.email,
            "model": model, "content": content or joined or "",
            "latency_ms": latency}


# ---------- 自动登录获取 Cookie（cloakbrowser） ----------
# session_id 是 httpOnly，JS 读不到，只能用真实浏览器登录后由 Playwright 提取。
# 点面板按钮 → 后端启动 cloakbrowser 打开 Genspark → 用户登录 → 检测到登录态后
# 自动导出 cookie 存为 cookiesN.json，前端轮询拿到结果自动填进表单。
_CAPTURE = {"status": "idle", "cookie": "", "cookie_file": "", "email": "", "error": ""}
_CAPTURE_LOCK = threading.Lock()


def _reset_capture():
    with _CAPTURE_LOCK:
        _CAPTURE.update({"status": "idle", "cookie": "", "cookie_file": "",
                         "email": "", "error": ""})


def _run_login_capture():
    """后台线程：启动 cloakbrowser，等用户登录，导出 cookie。"""
    try:
        import cloakbrowser
    except ImportError:
        with _CAPTURE_LOCK:
            _CAPTURE.update({"status": "error",
                             "error": "未安装 cloakbrowser（pip install cloakbrowser）"})
        return

    profile = os.path.join(DATA, "gs_login_profile")
    os.makedirs(profile, exist_ok=True)
    browser = None
    try:
        with _CAPTURE_LOCK:
            _CAPTURE["status"] = "waiting"
        browser = cloakbrowser.launch_persistent_context(
            user_data_dir=profile, headless=False, stealth_args=True,
            viewport={"width": 1280, "height": 840},
        )
        # 清掉上次残留登录态，保证每次添加账号都从登出状态开始，
        # 否则 profile 里的旧 session 会让浏览器一打开就是已登录、瞬间抓取关闭。
        try:
            browser.clear_cookies()
        except Exception:
            pass
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto("https://www.genspark.ai/agents?type=ai_chat",
                  wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)

        # 轮询登录态，最多 10 分钟（注册新号可能需要更久）
        deadline = time.time() + 600
        logged = False
        while time.time() < deadline:
            try:
                logged = page.evaluate("""async () => {
                  try {
                    const r = await fetch('/api/is_login', {credentials:'include'});
                    const j = await r.json();
                    return !!(j.data && j.data.is_login);
                  } catch(e) { return false; }
                }""")
            except Exception:
                logged = False
            if logged:
                break
            time.sleep(3)

        if not logged:
            with _CAPTURE_LOCK:
                _CAPTURE.update({"status": "error", "error": "等待登录超时（10 分钟）"})
            return

        # 导出全部 cookie（含 httpOnly）
        cookies = browser.cookies()
        names = {c.get("name") for c in cookies}
        if "session_id" not in names:
            with _CAPTURE_LOCK:
                _CAPTURE.update({"status": "error",
                                 "error": "已登录但未找到 session_id cookie"})
            return

        # 取登录邮箱
        email = ""
        try:
            email = page.evaluate("""async () => {
              try {
                const r = await fetch('/api/user/info', {credentials:'include'});
                const j = await r.json();
                return (j.data && (j.data.email || j.data.user_email)) || '';
              } catch(e) { return ''; }
            }""")
        except Exception:
            pass

        # 写成 cookiesN.json（保留浏览器侧附加字段，便于排查）
        out_cookies = [
            {"name": c.get("name"), "value": c.get("value"),
             "domain": c.get("domain"), "path": c.get("path"),
             "httpOnly": c.get("httpOnly"), "secure": c.get("secure"),
             "sameSite": c.get("sameSite")}
            for c in cookies
        ]
        fname = _write_cookie_file(out_cookies, "cloakbrowser_auto_login")

        cookie_str = "; ".join(f"{c['name']}={c['value']}"
                               for c in out_cookies if c.get("name"))
        with _CAPTURE_LOCK:
            _CAPTURE.update({"status": "done", "cookie": cookie_str,
                             "cookie_file": fname, "email": email or ""})
    except Exception as e:
        with _CAPTURE_LOCK:
            _CAPTURE.update({"status": "error", "error": str(e)})
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass


@app.post("/api/admin/start-login-capture")
def admin_start_login_capture(_: str = Depends(verify_admin)):
    """启动一次自动登录取 cookie。"""
    with _CAPTURE_LOCK:
        if _CAPTURE["status"] == "waiting":
            return {"ok": True, "status": "waiting"}
    _reset_capture()
    with _CAPTURE_LOCK:
        _CAPTURE["status"] = "waiting"
    threading.Thread(target=_run_login_capture, daemon=True).start()
    return {"ok": True, "status": "waiting"}


@app.get("/api/admin/login-capture-status")
def admin_login_capture_status(_: str = Depends(verify_admin)):
    """前端轮询登录捕获进度。"""
    with _CAPTURE_LOCK:
        return dict(_CAPTURE)


# ---------- 手动导入 Cookie ----------

class CookieImportBody(BaseModel):
    cookies: list = []
    email: str = ""


@app.get("/api/admin/genspark-login-url")
def admin_genspark_login_url(_: str = Depends(verify_admin)):
    """返回 Genspark 登录页 URL，前端用 window.open 打开。"""
    return {"url": "https://www.genspark.ai/agents?type=ai_chat"}


@app.get("/api/admin/latest-import-cookie")
def admin_latest_import_cookie(since: float = 0, _: str = Depends(verify_admin)):
    """返回最近一次 bookmarklet 导入的 cookie（前端加账号弹窗轮询用）。"""
    best = None
    try:
        names = os.listdir(DATA)
    except OSError:
        names = []
    for name in names:
        if not (name.startswith("cookies") and name.endswith(".json")):
            continue
        stem = name[len("cookies"):-len(".json")]
        if not stem.isdigit():
            continue
        path = os.path.join(DATA, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < since:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, path, name)
    if not best:
        return {"ok": False}
    try:
        d = json.load(open(best[1], encoding="utf-8"))
    except Exception:
        return {"ok": False}
    if d.get("source") != "browser_tab_import":
        return {"ok": False}
    pairs = d.get("cookies", [])
    if not any(c.get("name") == "session_id" for c in pairs if isinstance(c, dict)):
        return {"ok": False}
    cookie = "; ".join(f"{c['name']}={c['value']}"
                       for c in pairs if c.get("name"))
    return {"ok": True, "cookie_file": best[2], "cookie": cookie,
            "mtime": best[0], "count": len(pairs)}


@app.post("/api/admin/import-cookie")
def admin_import_cookie(body: CookieImportBody, _: str = Depends(verify_admin)):
    """接收浏览器端 POST 回来的 cookie 列表，保存为 cookiesN.json 文件。"""
    if not body.cookies:
        raise HTTPException(status_code=400, detail="cookie 列表为空")
    # 检查关键 cookie
    names = {c.get("name") for c in body.cookies if isinstance(c, dict)}
    if "session_id" not in names:
        raise HTTPException(status_code=400, detail="缺少 session_id，请确认已在 Genspark 登录")
    fname = _write_cookie_file(body.cookies, "browser_tab_import")
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
    rlog("main", f"serving on {HOST}:{PORT} (v{VERSION})")
    # Windows 双击场景：启动后自动打开本机浏览器进入后台。
    # 服务器/容器（GS_HOST=0.0.0.0 或 GS_NO_BROWSER=1）不打开。
    if HOST in ("127.0.0.1", "localhost", "::1") \
            and os.environ.get("GS_NO_BROWSER") != "1":
        import webbrowser
        threading.Timer(
            1.2, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}/")).start()
    try:
        uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
    except OSError as e:
        # 常见：端口已被占用（已在运行的实例）。双击时给出可读提示再退出。
        rlog("fatal", f"启动失败: {e}")
        rlog("fatal", f"端口 {PORT} 可能已被占用（是否已有实例在运行？）")
        sys.exit(1)
