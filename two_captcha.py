"""2captcha solver for Genspark's signup image CAPTCHA.

Verified 2026-09-23 against the live signup flow.

Key findings from testing:
  - **Read the image from `img.src` (a `data:image/jpeg;base64,...` URL).**
    Do NOT screenshot the element by coordinates — a screenshot includes the
    surrounding background and the distorted glyphs get misread.
  - Answer is stable for the same image (3/3 identical across retries).
  - Case may vary between submissions; Genspark's CAPTCHA is not case sensitive.
  - Typical solve time: 6-20 s.
  - Cost: $0.001 per solve (billing is applied with a ~30 s lag).
  - `min_len` / `max_len` hints are not honored by the solver.

Usage:
    from two_captcha import solve_from_page
    answer = solve_from_page(page)          # reads image, submits, polls
"""
import base64
import json
import os
import time
import urllib.parse
import urllib.request

API_KEY = os.environ.get("TWOCAPTCHA_KEY", "")
PROXY = os.environ.get("TWOCAPTCHA_PROXY", "") or None
IN_URL = "https://2captcha.com/in.php"
RES_URL = "https://2captcha.com/res.php"

_opener = None
if PROXY:
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))


def _open(req_or_url, timeout=90):
    if _opener:
        return _opener.open(req_or_url, timeout=timeout)
    return urllib.request.urlopen(req_or_url, timeout=timeout)


def _retry(fn, tries=4, wait=4):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:
            last = e
            time.sleep(wait)
    raise last


def _post(url, data):
    body = urllib.parse.urlencode(data).encode()
    r = urllib.request.Request(url, data=body,
                               headers={"Content-Type": "application/x-www-form-urlencoded"},
                               method="POST")
    return _retry(lambda: _open(r).read().decode())


def _get(url, params):
    return _retry(lambda: _open(url + "?" + urllib.parse.urlencode(params)).read().decode())


GET_SRC_JS = """() => {
  const i = document.getElementById('captchaControlChallengeCode-img');
  return i ? i.src : null;
}"""


def read_captcha_src(page):
    """Return the CAPTCHA image as a data URL, searching every frame."""
    frames = [page.main_frame] + [f for f in page.frames if f != page.main_frame]
    for fr in frames:
        try:
            src = fr.evaluate(GET_SRC_JS)
        except Exception:
            continue
        if src and "," in src:
            return src
    return None


def solve_b64(b64_body, log=print, timeout=200):
    """Submit a base64 image. Returns (answer, captcha_id, info)."""
    if not API_KEY:
        return None, None, "TWOCAPTCHA_KEY is not set"
    t0 = time.time()
    resp = _post(IN_URL, {
        "key": API_KEY, "method": "base64", "body": b64_body, "json": 1,
        "regsense": 1,          # case sensitive (Genspark itself is not)
        "numeric": 4,           # must contain both digits and letters
        "lang": "en",
        "textinstructions": "Type ALL characters exactly as shown. Case matters.",
    })
    try:
        j = json.loads(resp)
    except Exception:
        return None, None, f"non-JSON submit response: {resp[:100]}"
    if j.get("status") != 1:
        return None, None, f"submit failed: {j.get('request')}"
    cid = j["request"]

    while time.time() - t0 < timeout:
        time.sleep(5)
        try:
            j = json.loads(_get(RES_URL, {"key": API_KEY, "action": "get",
                                          "id": cid, "json": 1}))
        except Exception:
            continue
        if j.get("status") == 1:
            return j["request"], cid, time.time() - t0
        if j.get("request") == "CAPCHA_NOT_READY":
            continue
        return None, cid, f"error: {j.get('request')}"
    return None, cid, "timeout"


def solve_from_page(page, save_dir=None, log=print, timeout=200):
    """Read the CAPTCHA image from the page and solve it."""
    src = read_captcha_src(page)
    if not src:
        return None, None, "no CAPTCHA image found (captchaControlChallengeCode-img)"
    b64 = src.split(",", 1)[1]
    if save_dir:
        try:
            os.makedirs(save_dir, exist_ok=True)
            raw = base64.b64decode(b64)
            p = os.path.join(save_dir, time.strftime("cap_%H%M%S") + ".jpg")
            open(p, "wb").write(raw)
            log(f"[2captcha] image saved {p} ({len(raw)} B)")
        except Exception:
            pass
    return solve_b64(b64, log=log, timeout=timeout)


def report(captcha_id, good):
    """Report the answer back to the platform (improves future accuracy)."""
    if not API_KEY or not captcha_id:
        return None
    try:
        return _get(RES_URL, {"key": API_KEY,
                              "action": "reportgood" if good else "reportbad",
                              "id": captcha_id})
    except Exception:
        return None


def balance():
    """Current account balance (USD)."""
    if not API_KEY:
        return None
    try:
        j = json.loads(_get(RES_URL, {"key": API_KEY, "action": "getbalance", "json": 1}))
        return j.get("request")
    except Exception:
        return None
