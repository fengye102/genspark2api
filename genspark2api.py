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
import threading
import time
import uuid

from curl_cffi import requests as cffi
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

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
    "gpt-5.4-mini": "gpt-5.4-mini",
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
        self.load()

    def load(self):
        if not self.cookie_file or not os.path.exists(self.cookie_file):
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
        return {"https": self.proxy, "http": self.proxy}


def load_accounts():
    if not os.path.exists(MAP_FILE):
        raise RuntimeError(f"缺少 {MAP_FILE}")
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
        s = cffi.Session(impersonate="chrome")

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
            buf, emitted = "", 0
            try:
                r = cffi.Session(impersonate="chrome").post(
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
                                yield f'data: {json.dumps({"error": {"message": mc[:200]}})}\n\n'
            except Exception as e:
                yield f'data: {json.dumps({"error": {"message": f"{type(e).__name__}: {e}"}})}\n\n'
            finally:
                yield f'data: {json.dumps({"id": i, "object": "chat.completion.chunk", "created": cr, "model": mo, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})}\n\n'
                yield "data: [DONE]\n\n"
                a.stats["ok"] += 1

        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse({"error": {"message": f"所有账号都失败: {last_err}"}},
                        status_code=502)


if __name__ == "__main__":
    import uvicorn
    print(f"[main] serving on :{PORT}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
