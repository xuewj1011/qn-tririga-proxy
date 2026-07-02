"""
QN-TRIRIGA 反馈代理服务器
接收前端请求 → 翻译 → 写入飞书多维表格
"""
import os
import json
import time
import hashlib
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

# Load .env for local development
_DOTENV = os.path.join(os.environ.get("USERPROFILE", os.path.expanduser("~")), 
                       "AppData", "Local", "hermes", ".env")
if os.path.exists(_DOTENV):
    for line in open(_DOTENV, "r", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# DEBUG
print(f"Server starting... DEEPSEEK key loaded: {bool(os.environ.get('DEEPSEEK_API_KEY'))}", flush=True)
print(f"FEISHU_APP_SECRET: len={len(os.environ.get('FEISHU_APP_SECRET', ''))}, starts={repr(os.environ.get('FEISHU_APP_SECRET', '')[:8])}", flush=True)

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

# ═══════════ CONFIG ═══════════
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aaca763cbe399bfb")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# Bitables config
BITABLES = {
    "party-a": {
        "app_token": "UWarbCF92ak7GDseUuNclUDpnwe",
        "table_id": "tbldcDi4uh1JqzeZ",
        "fields": {"姓名": "fld9cluCRH", "问题描述": "fldiRzyUU3", "提交时间": "fldcU1Jwad"},
    },
    "party-b": {
        "app_token": "FMVJb4qR5azJlqswWj3cFpLbnIf",
        "table_id": "tblVqm4RDo9cl1zz",
        "fields": {"姓名": "fldpzg1pSx", "问题描述": "fldpyFFUQb", "提交时间": "fld4MERklh"},
    },
}

# ═══════════ HTTP SESSION (bypass system proxy) ═══════════
_session = requests.Session()
_session.trust_env = False  # don't use system proxy (127.0.0.1:7890)

# ═══════════ CACHE ═══════════
_token_cache = {"token": "", "expires_at": 0}
_translate_cache = {}


def get_feishu_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    resp = _session.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    ).json()
    if "tenant_access_token" not in resp:
        raise Exception(resp.get("msg", resp.get("error", "feishu auth failed")))
    token = resp["tenant_access_token"]
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + resp.get("expire", 7200)
    return token


def translate_text(text, target_lang="en"):
    """Bidirectional translation: zh<->en. target_lang: 'zh' or 'en'."""
    if not text.strip():
        return ""
    cache_key = hashlib.md5((text + "||" + target_lang).encode()).hexdigest()
    if cache_key in _translate_cache:
        return _translate_cache[cache_key]
    if target_lang == "en":
        system_prompt = "你是一个中英翻译器。只输出英文翻译，不要任何解释。如果输入已是英文，原样返回。"
        user_prompt = f"翻译成英文：{text}"
    else:
        system_prompt = "You are an EN->ZH translator. Output only the Chinese translation, no explanation. If input is already Chinese, return it as-is."
        user_prompt = f"Translate to Chinese: {text}"
    try:
        resp = _session.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.3,
            },
            timeout=15,
        ).json()
        result = resp["choices"][0]["message"]["content"].strip()
        _translate_cache[cache_key] = result
        return result
    except Exception:
        return text  # fallback to original


def name_to_pinyin(name):
    """Convert Chinese name to pinyin. Non-Chinese names returned as-is."""
    if not name.strip():
        return ""
    # Only convert if name contains CJK
    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in name)
    if not has_cjk:
        return name  # already English, don't touch
    cache_key = hashlib.md5(("pinyin:" + name).encode()).hexdigest()
    if cache_key in _translate_cache:
        return _translate_cache[cache_key]
    try:
        resp = _session.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": "Convert Chinese names to pinyin with proper capitalization. Output ONLY the pinyin, no explanation. Examples: 薛文娟→Xue Wenjuan, 张三→Zhang San, 欧阳锋→Ouyang Feng."},
                    {"role": "user", "content": name},
                ],
                "temperature": 0.1,
            },
            timeout=10,
        ).json()
        result = resp["choices"][0]["message"]["content"].strip()
        _translate_cache[cache_key] = result
        return result
    except Exception:
        return name  # fallback


# ═══════════ API ROUTES ═══════════

def _split_combined(val, idx=0):
    """Split 'part1\n---\npart2' and return part at idx. Falls back to full val."""
    parts = val.split("\n---\n", 1)
    if idx == 1 and len(parts) > 1:
        return parts[1]
    return parts[0]

@app.route("/api/feedback/<party>", methods=["GET"])
def get_feedback(party):
    try:
        cfg = BITABLES.get(party)
        if not cfg:
            return jsonify({"error": "invalid party"}), 400
        token = get_feishu_token()
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg['app_token']}/tables/{cfg['table_id']}/records"
        resp = _session.get(url, headers={"Authorization": f"Bearer {token}"}).json()
        if resp.get("code") != 0:
            return jsonify({"error": resp.get("msg", "feishu error")}), 500
        records = []
        for item in resp.get("data", {}).get("items", []):
            f = item.get("fields", {})
            rid = item.get("record_id", "")
            # split combined field "zh\n---\nen" into text_zh / text_en
            combined = f.get("问题描述", "")
            parts = combined.split("\n---\n", 1)
            text_zh = parts[0]
            text_en = parts[1] if len(parts) > 1 else ""
            # fallback: try to extract ms timestamp from record_id if no time field
            t = f.get("提交时间", "")
            if not t and rid:
                try: t = int(rid[3:16]) if len(rid) > 16 else 0
                except: t = 0
            records.append({
                "id": rid,
                "name": _split_combined(f.get("姓名", "")),
                "name_en": _split_combined(f.get("姓名", ""), 1),
                "text_zh": text_zh,
                "text_en": text_en,
                "time": t,
            })
        return jsonify(records)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/feedback/<party>", methods=["POST"])
def add_feedback(party):
    cfg = BITABLES.get(party)
    if not cfg:
        return jsonify({"error": "invalid party"}), 400
    data = request.json or {}
    name = data.get("name", "").strip()
    text = data.get("text", "").strip()
    src_lang = data.get("src_lang", "zh")  # "zh" or "en"
    if not name or not text:
        return jsonify({"error": "name and text required"}), 400

    # Detect source language if not specified
    if src_lang not in ("zh", "en"):
        # simple heuristic: if has CJK chars, assume Chinese
        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in text)
        src_lang = "zh" if has_cjk else "en"

    # Translate: name NEVER translated, only problem description
    text_zh = text if src_lang == "zh" else ""
    text_en = text if src_lang == "en" else ""

    if os.environ.get("DEEPSEEK_API_KEY"):
        if src_lang == "zh" and not text_en:
            text_en = translate_text(text, "en")
        elif src_lang == "en" and not text_zh:
            text_zh = translate_text(text, "zh")

    # Name: Chinese → pinyin for EN mode; English names stay as-is
    name_zh = name
    name_en = name_to_pinyin(name) if os.environ.get("DEEPSEEK_API_KEY") else name
    name_combined = f"{name_zh}\n---\n{name_en}" if name_en != name_zh else name_zh

    # Write to Feishu — store combined
    token = get_feishu_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg['app_token']}/tables/{cfg['table_id']}/records"
    ts = int(time.time() * 1000)
    combined = f"{text_zh}\n---\n{text_en}" if text_en else text_zh
    fields = {
        "姓名": name_combined,
        "问题描述": combined,
        "提交时间": ts,
    }
    resp = _session.post(url, headers={"Authorization": f"Bearer {token}"}, json={"fields": fields}).json()
    if resp.get("code") != 0:
        return jsonify({"error": resp.get("msg", "write failed")}), 500
    return jsonify({
        "id": resp["data"]["record"]["record_id"],
        "name": name_zh,
        "name_en": name_en,
        "text_zh": text_zh,
        "text_en": text_en,
        "time": ts,
    })


@app.route("/api/feedback/<party>/<record_id>", methods=["DELETE"])
def delete_feedback(party, record_id):
    cfg = BITABLES.get(party)
    if not cfg:
        return jsonify({"error": "invalid party"}), 400
    token = get_feishu_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg['app_token']}/tables/{cfg['table_id']}/records/{record_id}"
    resp = _session.delete(url, headers={"Authorization": f"Bearer {token}"}).json()
    if resp.get("code") != 0:
        return jsonify({"error": resp.get("msg", "delete failed")}), 500
    return jsonify({"ok": True})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    """On-demand bidirectional translation endpoint."""
    data = request.json or {}
    text = data.get("text", "").strip()
    target_lang = data.get("target_lang", "en")  # "zh" or "en"
    if not text:
        return jsonify({"error": "text required"}), 400
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return jsonify({"error": "DEEPSEEK not configured"}), 503
    result = translate_text(text, target_lang)
    return jsonify({"original": text, "target_lang": target_lang, "translated": result})


@app.route("/")
def index():
    return app.send_static_file("index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
