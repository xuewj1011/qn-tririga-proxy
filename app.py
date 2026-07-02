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
        "fields": {"姓名": "fld9cluCRH", "问题描述": "fldiRzyUU3", "提交时间": "fldcU1Jwad", "问题分类": "fld0vfnqGm"},
    },
    "party-b": {
        "app_token": "FMVJb4qR5azJlqswWj3cFpLbnIf",
        "table_id": "tblVqm4RDo9cl1zz",
        "fields": {"姓名": "fldpzg1pSx", "问题描述": "fldpyFFUQb", "提交时间": "fld4MERklh", "问题分类": "fld5MNVLg3"},
    },
}

# ═══════════ HTTP SESSION (bypass system proxy) ═══════════
_session = requests.Session()
_session.trust_env = False  # don't use system proxy (127.0.0.1:7890)

# ═══════════ CACHE ═══════════
_token_cache = {"token": "", "expires_at": 0}
_translate_cache = {}
_summary_cache = {"data": None, "expires_at": 0}
_feedback_cache = {"party-a": {"data": None, "expires_at": 0}, "party-b": {"data": None, "expires_at": 0}}
_reply_cache = {}  # keyed by feedback_id


def invalidate_summary_cache():
    _summary_cache["data"] = None
    _summary_cache["expires_at"] = 0


def invalidate_feedback_cache(party=None):
    if party:
        _feedback_cache[party]["data"] = None
        _feedback_cache[party]["expires_at"] = 0
    else:
        for p in _feedback_cache:
            _feedback_cache[p]["data"] = None
            _feedback_cache[p]["expires_at"] = 0


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
        return text


def name_to_pinyin(name):
    """Convert Chinese name to pinyin. Keeps ASCII names as-is."""
    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in name)
    if not has_cjk:
        return name
    try:
        from pypinyin import pinyin, Style
        parts = pinyin(name, style=Style.NORMAL)
        return " ".join([p[0] for p in parts]).title()
    except ImportError:
        return name


def _split_combined(val, idx=0):
    """Split 'part1\n---\npart2' and return part at idx. Falls back to full val."""
    val = val.replace("\r\n", "\n")
    parts = val.split("\n---\n", 1)
    if idx == 1 and len(parts) > 1:
        return parts[1]
    return parts[0]


def _fetch_feedback_list(cfg, token):
    """Fetch and parse feedback records from a bitable."""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg['app_token']}/tables/{cfg['table_id']}/records"
    resp = _session.get(url, headers={"Authorization": f"Bearer {token}"}).json()
    records = []
    if resp.get("code") == 0:
        for item in resp.get("data", {}).get("items", []):
            f = item.get("fields", {})
            rid = item.get("record_id", "")
            records.append({
                "id": rid,
                "name": _split_combined(f.get("姓名", "")),
                "name_en": _split_combined(f.get("姓名", ""), 1),
                "text_zh": _split_combined(f.get("问题描述", "")),
                "text_en": _split_combined(f.get("问题描述", ""), 1),
                "time": f.get("提交时间", ""),
                "category": f.get("问题分类", ""),
            })
    return records


# ═══════════ REPLIES BITABLE ═══════════
REPLY_TABLE = {
    "app_token": "FoqGbaOIxabI10shm9rc7HKwnCf",
    "table_id": "tbluv6vaiIoFQp1o",
}

# ═══════════ API ROUTES ═══════════

@app.route("/api/feedback/<party>", methods=["GET"])
def get_feedback(party):
    # Return cached data if fresh (30s TTL)
    now = time.time()
    fc = _feedback_cache.get(party)
    if fc and fc["data"] is not None and now < fc["expires_at"]:
        return jsonify(fc["data"])

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
            records.append({
                "id": rid,
                "name": _split_combined(f.get("姓名", "")),
                "name_en": _split_combined(f.get("姓名", ""), 1),
                "text_zh": _split_combined(f.get("问题描述", "")),
                "text_en": _split_combined(f.get("问题描述", ""), 1),
                "time": f.get("提交时间", ""),
                "category": f.get("问题分类", ""),
            })
        # Cache result
        _feedback_cache[party]["data"] = records
        _feedback_cache[party]["expires_at"] = now + 30
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
    src_lang = data.get("src_lang", "zh")
    category = data.get("category", "").strip()
    if not name or not text:
        return jsonify({"error": "name and text required"}), 400

    if src_lang not in ("zh", "en"):
        has_cjk = any('\u4e00' <= c <= '\u9fff' for c in text)
        src_lang = "zh" if has_cjk else "en"

    text_zh = text if src_lang == "zh" else ""
    text_en = text if src_lang == "en" else ""

    if os.environ.get("DEEPSEEK_API_KEY"):
        if src_lang == "zh" and not text_en:
            text_en = translate_text(text, "en")
        elif src_lang == "en" and not text_zh:
            text_zh = translate_text(text, "zh")

    name_zh = name
    name_en = name_to_pinyin(name) if os.environ.get("DEEPSEEK_API_KEY") else name
    name_combined = f"{name_zh}\n---\n{name_en}" if name_en != name_zh else name_zh

    token = get_feishu_token()
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg['app_token']}/tables/{cfg['table_id']}/records"
    ts = int(time.time() * 1000)
    combined = f"{text_zh}\n---\n{text_en}" if text_en else text_zh
    fields = {
        "姓名": name_combined,
        "问题描述": combined,
        "提交时间": ts,
        "问题分类": category if category else "未分类",
    }
    resp = _session.post(url, headers={"Authorization": f"Bearer {token}"}, json={"fields": fields}).json()
    if resp.get("code") != 0:
        return jsonify({"error": resp.get("msg", "write failed")}), 500
    invalidate_summary_cache()
    invalidate_feedback_cache(party)
    return jsonify({
        "id": resp["data"]["record"]["record_id"],
        "name": name_zh,
        "name_en": name_en,
        "text_zh": text_zh,
        "text_en": text_en,
        "time": ts,
        "category": category,
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
    invalidate_summary_cache()
    invalidate_feedback_cache(party)
    return jsonify({"ok": True})


@app.route("/api/translate", methods=["POST"])
def api_translate():
    data = request.json or {}
    text = data.get("text", "").strip()
    target_lang = data.get("target_lang", "en")
    if not text:
        return jsonify({"error": "text required"}), 400
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return jsonify({"error": "DEEPSEEK not configured"}), 503
    result = translate_text(text, target_lang)
    return jsonify({"original": text, "target_lang": target_lang, "translated": result})


@app.route("/api/replies/<party>/<feedback_id>", methods=["GET"])
def get_replies(party, feedback_id):
    if not REPLY_TABLE["app_token"] or not REPLY_TABLE["table_id"]:
        return jsonify({"error": "reply table not configured"}), 503

    # Return cached if fresh (30s TTL)
    now = time.time()
    rc = _reply_cache.get(feedback_id)
    if rc and rc["data"] is not None and now < rc["expires_at"]:
        return jsonify(rc["data"])

    try:
        token = get_feishu_token()
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{REPLY_TABLE['app_token']}"
               f"/tables/{REPLY_TABLE['table_id']}/records"
               f"?filter=CurrentValue.[关联反馈ID]=\"{feedback_id}\""
               f"&page_size=100")
        resp = _session.get(url, headers={"Authorization": f"Bearer {token}"}).json()
        if resp.get("code") != 0:
            return jsonify({"error": resp.get("msg", "feishu error")}), 500
        replies = []
        for item in resp.get("data", {}).get("items", []):
            f = item.get("fields", {})
            replies.append({
                "id": item.get("record_id", ""),
                "feedback_id": feedback_id,
                "party": f.get("回复方", ""),
                "replier": _split_combined(f.get("回复者", "")),
                "replier_en": _split_combined(f.get("回复者", ""), 1),
                "text": _split_combined(f.get("回复内容", "")),
                "text_en": _split_combined(f.get("回复内容", ""), 1),
                "time": f.get("回复时间", 0),
            })
        _reply_cache[feedback_id] = {"data": replies, "expires_at": now + 30}
        return jsonify(replies)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/replies/<party>/<feedback_id>", methods=["POST"])
def add_reply(party, feedback_id):
    if not REPLY_TABLE["app_token"] or not REPLY_TABLE["table_id"]:
        return jsonify({"error": "reply table not configured"}), 503
    data = request.json or {}
    replier = data.get("replier", "").strip()
    text = data.get("text", "").strip()
    if not replier or not text:
        return jsonify({"error": "replier and text required"}), 400

    # Detect source language and translate
    has_cjk = any('\u4e00' <= c <= '\u9fff' for c in text)
    src_lang = "zh" if has_cjk else "en"
    text_zh = text if src_lang == "zh" else ""
    text_en = text if src_lang == "en" else ""

    if os.environ.get("DEEPSEEK_API_KEY"):
        if src_lang == "zh" and not text_en:
            text_en = translate_text(text, "en")
        elif src_lang == "en" and not text_zh:
            text_zh = translate_text(text, "zh")

    replier_zh = replier
    replier_en = name_to_pinyin(replier) if os.environ.get("DEEPSEEK_API_KEY") else replier
    replier_combined = f"{replier_zh}\n---\n{replier_en}" if replier_en != replier_zh else replier_zh
    text_combined = f"{text_zh}\n---\n{text_en}" if text_en else text_zh

    try:
        token = get_feishu_token()
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{REPLY_TABLE['app_token']}"
               f"/tables/{REPLY_TABLE['table_id']}/records")
        ts = int(time.time() * 1000)
        fields = {
            "关联反馈ID": feedback_id,
            "回复方": party,
            "回复者": replier_combined,
            "回复内容": text_combined,
            "回复时间": ts,
        }
        resp = _session.post(url, headers={"Authorization": f"Bearer {token}"}, json={"fields": fields}).json()
        if resp.get("code") != 0:
            return jsonify({"error": resp.get("msg", "write failed")}), 500
        invalidate_summary_cache()
        # Invalidate reply cache for this feedback
        _reply_cache.pop(feedback_id, None)
        return jsonify({
            "id": resp["data"]["record"]["record_id"],
            "feedback_id": feedback_id,
            "party": party,
            "replier": replier_zh,
            "text": text_zh,
            "time": ts,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/replies/<party>/<feedback_id>/<reply_id>", methods=["DELETE"])
def delete_reply(party, feedback_id, reply_id):
    if not REPLY_TABLE["app_token"] or not REPLY_TABLE["table_id"]:
        return jsonify({"error": "reply table not configured"}), 503
    try:
        token = get_feishu_token()
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{REPLY_TABLE['app_token']}"
               f"/tables/{REPLY_TABLE['table_id']}/records/{reply_id}")
        resp = _session.delete(url, headers={"Authorization": f"Bearer {token}"}).json()
        if resp.get("code") != 0:
            return jsonify({"error": resp.get("msg", "delete failed")}), 500
        invalidate_summary_cache()
        _reply_cache.pop(feedback_id, None)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/summary", methods=["GET"])
def get_summary():
    """Aggregate all feedback from both parties with their replies."""
    # Return cached data if still fresh (30s TTL)
    now = time.time()
    if _summary_cache["data"] is not None and now < _summary_cache["expires_at"]:
        return jsonify(_summary_cache["data"])

    try:
        token = get_feishu_token()
        all_items = []

        # Batch-fetch all replies once, then group by feedback_id
        all_replies = {}
        if REPLY_TABLE["app_token"] and REPLY_TABLE["table_id"]:
            reply_url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{REPLY_TABLE['app_token']}"
                         f"/tables/{REPLY_TABLE['table_id']}/records?page_size=500")
            reply_resp = _session.get(reply_url, headers={"Authorization": f"Bearer {token}"}).json()
            if reply_resp.get("code") == 0:
                for ri in reply_resp.get("data", {}).get("items", []):
                    rf = ri.get("fields", {})
                    fid = rf.get("关联反馈ID", "")
                    if fid not in all_replies:
                        all_replies[fid] = []
                    all_replies[fid].append({
                        "id": ri.get("record_id", ""),
                        "party": rf.get("回复方", ""),
                        "replier": _split_combined(rf.get("回复者", "")),
                        "replier_en": _split_combined(rf.get("回复者", ""), 1),
                        "text": _split_combined(rf.get("回复内容", "")),
                        "text_en": _split_combined(rf.get("回复内容", ""), 1),
                        "time": rf.get("回复时间", 0),
                    })

        for party, cfg in BITABLES.items():
            url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{cfg['app_token']}"
                   f"/tables/{cfg['table_id']}/records?page_size=500")
            resp = _session.get(url, headers={"Authorization": f"Bearer {token}"}).json()
            if resp.get("code") != 0:
                continue

            party_label = "甲方" if party == "party-a" else "乙方"
            for item in resp.get("data", {}).get("items", []):
                f = item.get("fields", {})
                record_id = item.get("record_id", "")
                feedback_item = {
                    "id": record_id,
                    "party": party,
                    "party_label": party_label,
                    "name": _split_combined(f.get("姓名", "")),
                    "name_en": _split_combined(f.get("姓名", ""), 1),
                    "text": _split_combined(f.get("问题描述", "")),
                    "text_en": _split_combined(f.get("问题描述", ""), 1),
                    "time": f.get("提交时间", 0),
                    "category": f.get("问题分类", ""),
                    "replies": all_replies.get(record_id, []),
                }
                all_items.append(feedback_item)

        all_items.sort(key=lambda x: x.get("time", 0), reverse=True)
        _summary_cache["data"] = all_items
        _summary_cache["expires_at"] = now + 30  # 30s cache
        return jsonify(all_items)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    response = app.make_response(app.send_static_file("index.html"))
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
