#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 销售定向营销 App · 后端（方案 B）
Flask + sqlite3 + requests，直调千问百炼 OpenAI 兼容接口，同时托管前端单文件。
API Key 只从环境变量 BAILIAN_API_KEY 读取，不写进代码。
"""
import json
import os
import re
import sqlite3
import time
from datetime import datetime

import requests
from flask import Flask, g, jsonify, request, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SALES_DB", os.path.join(BASE_DIR, "sales_app.db"))
BAILIAN_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
BAILIAN_MODEL = "qwen-plus"

# 风险词：只标红，不拦截（PRD R02 业务规则）
RISK_WORDS = ["最便宜", "保证", "根治", "100%", "全网最低",
              "包治", "零风险", "永久", "绝不反弹", "一次见效"]

# 五层分群判定树（PRD 第 7 章），写进 prompt 要点
SEGMENT_TREE = """判定树（从上到下，命中即定层）：
1. 流失（一票否决）：明确拒绝 / 拉黑 / 长期（90 天以上）无响应
2. 高价值：成交过（行为特征有购买记录）或高意向；价值优先于新近度
3. 沉睡：有过互动，但超过 30 天无互动
4. 新用户：无任何历史互动（首次录入）
5. 活跃：以上皆非，默认层（30 天内有互动）"""

PROMPT_ORGANIZE = """你是销售助手。把销售对客户的一句话描述整理成结构化画像卡，只输出 JSON，不要输出其他内容。
JSON 字段：
- 基本信息：对象，含 称呼（必填）、年龄、性别、职业
- 行为特征：数组，只装客观事实；必须包含消费行为类事实（买过什么、客单价带、购买频次），没有就写"待确认"
- 在意什么：一句话主观动机（怕什么、图什么）；注意"怕疼"这类是在意什么，不是行为特征
- 上次聊到哪：一句话；没有就写"待确认"
- 客户阶段：从 新用户/活跃/高价值/沉睡/流失 中初判一个
规则：信息缺失标"待确认"，不编造。"""

PROMPT_SEGMENT = """你是销售分层助手。根据客户画像判断客户阶段，只输出 JSON，不要输出其他内容。
%s
JSON 字段：
- 客户阶段：新用户 / 活跃 / 高价值 / 沉睡 / 流失 之一
- 策略一句话：针对该阶段给一句触达策略
- 打扰风险提示：一句，提醒销售别过度打扰（如沉睡客户唤醒最多跟两次）""" % SEGMENT_TREE

PROMPT_SCRIPTS = """你是销售话术助手。为指定客户生成三版话术：正式版（专业稳重）、亲切版（像熟人聊天）、玩梗版（轻松幽默带网感）。
每版必须显式用到至少 1 个画像信息点（基本信息/行为特征/在意什么/上次聊到哪）。
表达对象影响侧重点：技术型重参数和原理，业务型重效果和案例，老板型重 ROI 和决策效率。
只输出 JSON，不要输出其他内容。JSON 字段：
- versions：数组，每版含 版本（正式版/亲切版/玩梗版）、文本、信息点（数组，用到的画像信息点原文）"""

app = Flask(__name__, static_folder="static")
CORS(app)  # demo 阶段放开跨域


# ---------- DB ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        "称呼" TEXT NOT NULL,
        "基本信息" TEXT DEFAULT '{}',
        "行为特征" TEXT DEFAULT '[]',
        "在意什么" TEXT DEFAULT '',
        "上次聊到哪" TEXT DEFAULT '',
        "客户阶段" TEXT DEFAULT '新用户',
        "更新时间" TEXT
    );
    CREATE TABLE IF NOT EXISTS scripts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL,
        "表达对象" TEXT DEFAULT '',
        "三版" TEXT DEFAULT '[]',
        "选中版本" TEXT DEFAULT '',
        "生成时间" TEXT
    );
    CREATE TABLE IF NOT EXISTS touch_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id INTEGER NOT NULL,
        script_id INTEGER DEFAULT 0,
        "渠道" TEXT DEFAULT '',
        "客户反应" TEXT DEFAULT '',
        "发送时间" TEXT
    );
    """)
    db.commit()
    db.close()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def row_to_dict(row):
    return dict(row)


# ---------- 百炼调用 ----------
def strip_code_fence(text):
    """剥掉 AI 返回的 ```json 包裹（踩坑点：直接 parse 会崩）"""
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```$", t, re.S)
    return m.group(1).strip() if m else t


def call_bailian(system_prompt, user_prompt):
    key = os.environ.get("BAILIAN_API_KEY", "").strip()
    if not key:
        return None, ("后端未配置 BAILIAN_API_KEY，请在服务器 .env 中配置后重启", 503)
    body = {
        "model": BAILIAN_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    try:
        r = requests.post(
            BAILIAN_URL,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json=body, timeout=60)
    except Exception as e:
        return None, ("调用百炼接口网络异常：%s" % e, 502)
    if r.status_code != 200:
        return None, ("百炼接口返回 %s：%s" % (r.status_code, r.text[:200]), 502)
    try:
        content = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return None, ("百炼返回格式异常：%s" % e, 502)
    try:
        return json.loads(strip_code_fence(content)), None
    except Exception:
        return None, ("AI 返回不是合法 JSON：%s" % content[:200], 502)


def find_risk_words(text):
    return [w for w in RISK_WORDS if w in text]


# ---------- 基础 ----------
@app.route("/api/health")
def health():
    key_ok = bool(os.environ.get("BAILIAN_API_KEY", "").strip())
    return jsonify({
        "ok": True,
        "bailian": "ok" if key_ok else "warn: 未配置 BAILIAN_API_KEY，AI 接口不可用",
        "time": now_str(),
    })


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ---------- profiles ----------
@app.route("/api/profiles", methods=["GET"])
def list_profiles():
    db = get_db()
    rows = db.execute('SELECT * FROM profiles ORDER BY id DESC').fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/profiles", methods=["POST"])
def create_profile():
    data = request.get_json(force=True)
    if not data.get("称呼"):
        return jsonify({"error": "称呼必填"}), 400
    db = get_db()
    cur = db.execute(
        'INSERT INTO profiles ("称呼","基本信息","行为特征","在意什么","上次聊到哪","客户阶段","更新时间")'
        ' VALUES (?,?,?,?,?,?,?)',
        (data.get("称呼"),
         json.dumps(data.get("基本信息", {}), ensure_ascii=False),
         json.dumps(data.get("行为特征", []), ensure_ascii=False),
         data.get("在意什么", ""), data.get("上次聊到哪", ""),
         data.get("客户阶段", "新用户"), now_str()))
    db.commit()
    row = db.execute('SELECT * FROM profiles WHERE id=?', (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(row)), 201


@app.route("/api/profiles/<int:pid>", methods=["GET"])
def get_profile(pid):
    db = get_db()
    row = db.execute('SELECT * FROM profiles WHERE id=?', (pid,)).fetchone()
    if not row:
        return jsonify({"error": "客户不存在"}), 404
    return jsonify(row_to_dict(row))


@app.route("/api/profiles/<int:pid>", methods=["PUT"])
def update_profile(pid):
    data = request.get_json(force=True)
    fields, vals = [], []
    for k in ["称呼", "基本信息", "行为特征", "在意什么", "上次聊到哪", "客户阶段"]:
        if k in data:
            v = data[k]
            if k in ("基本信息", "行为特征"):
                v = json.dumps(v, ensure_ascii=False)
            fields.append('"%s"=?' % k)
            vals.append(v)
    if not fields:
        return jsonify({"error": "没有可更新的字段"}), 400
    fields.append('"更新时间"=?')
    vals.append(now_str())
    vals.append(pid)
    db = get_db()
    cur = db.execute('UPDATE profiles SET %s WHERE id=?' % ",".join(fields), vals)
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "客户不存在"}), 404
    row = db.execute('SELECT * FROM profiles WHERE id=?', (pid,)).fetchone()
    return jsonify(row_to_dict(row))


@app.route("/api/profiles/<int:pid>", methods=["DELETE"])
def delete_profile(pid):
    db = get_db()
    row = db.execute('SELECT id FROM profiles WHERE id=?', (pid,)).fetchone()
    if row is None:
        return jsonify({"error": "客户不存在"}), 404
    # 级联删除：该客户的话术与触达记录一并清除，不留孤儿数据
    db.execute('DELETE FROM touch_logs WHERE profile_id=?', (pid,))
    db.execute('DELETE FROM scripts WHERE profile_id=?', (pid,))
    db.execute('DELETE FROM profiles WHERE id=?', (pid,))
    db.commit()
    return jsonify({"ok": True})


# ---------- scripts ----------
@app.route("/api/scripts", methods=["GET"])
def list_scripts():
    db = get_db()
    pid = request.args.get("profile_id")
    if pid:
        rows = db.execute('SELECT * FROM scripts WHERE profile_id=? ORDER BY id DESC', (pid,)).fetchall()
    else:
        rows = db.execute('SELECT * FROM scripts ORDER BY id DESC LIMIT 50').fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/scripts", methods=["POST"])
def create_script():
    data = request.get_json(force=True)
    if not data.get("profile_id"):
        return jsonify({"error": "缺少 profile_id"}), 400
    db = get_db()
    cur = db.execute(
        'INSERT INTO scripts (profile_id,"表达对象","三版","选中版本","生成时间") VALUES (?,?,?,?,?)',
        (data["profile_id"], data.get("表达对象", ""),
         json.dumps(data.get("三版", []), ensure_ascii=False),
         data.get("选中版本", ""), now_str()))
    db.commit()
    row = db.execute('SELECT * FROM scripts WHERE id=?', (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(row)), 201


@app.route("/api/scripts/<int:sid>/select", methods=["POST"])
def select_script(sid):
    data = request.get_json(force=True)
    db = get_db()
    cur = db.execute('UPDATE scripts SET "选中版本"=? WHERE id=?',
                     (data.get("选中版本", ""), sid))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "话术不存在"}), 404
    row = db.execute('SELECT * FROM scripts WHERE id=?', (sid,)).fetchone()
    return jsonify(row_to_dict(row))


# ---------- logs ----------
@app.route("/api/logs", methods=["GET"])
def list_logs():
    db = get_db()
    rows = db.execute(
        'SELECT l.*, p."称呼" AS 客户称呼 FROM touch_logs l'
        ' LEFT JOIN profiles p ON l.profile_id=p.id'
        ' ORDER BY l."发送时间" DESC LIMIT 200').fetchall()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/logs", methods=["POST"])
def create_log():
    data = request.get_json(force=True)
    if not data.get("profile_id"):
        return jsonify({"error": "缺少 profile_id"}), 400
    db = get_db()
    # 兼容旧库：没有"话术版本"列时自动加上
    try:
        db.execute('ALTER TABLE touch_logs ADD COLUMN "话术版本" TEXT DEFAULT \'\'')
    except Exception:
        pass
    cur = db.execute(
        'INSERT INTO touch_logs (profile_id,script_id,"话术版本","渠道","客户反应","发送时间") VALUES (?,?,?,?,?,?)',
        (data["profile_id"], data.get("script_id", 0), data.get("话术版本", ""),
         data.get("渠道", ""), data.get("客户反应", ""), now_str()))
    db.commit()
    row = db.execute('SELECT * FROM touch_logs WHERE id=?', (cur.lastrowid,)).fetchone()
    return jsonify(row_to_dict(row)), 201


# ---------- seed ----------
SEED_PROFILES = [
    {"称呼": "张姐",
     "基本信息": {"年龄": "38岁", "性别": "女", "职业": "企业行政"},
     "行为特征": ["买过热玛吉", "客单价3万档", "半年购2次", "二次到店意向高"],
     "在意什么": "怕疼，做决定慢，需要安全感，不喜欢被催单",
     "上次聊到哪": "上次咨询超声炮但嫌贵",
     "客户阶段": "高价值"},
    {"称呼": "小美",
     "基本信息": {"年龄": "26岁", "性别": "女", "职业": "新媒体运营"},
     "行为特征": ["买过小气泡", "客单价800档", "月购1次", "领过3次优惠券"],
     "在意什么": "图便宜，喜欢团购，怕被推销高价项目",
     "上次聊到哪": "上周问双11有没有满减",
     "客户阶段": "活跃"},
    {"称呼": "王女士",
     "基本信息": {"年龄": "45岁", "性别": "女", "职业": "教师"},
     "行为特征": ["买过纹眉", "客单价5000档", "一年购1次"],
     "在意什么": "在意恢复期，不想请假，怕影响上班",
     "上次聊到哪": "三个月前咨询过祛斑，之后没再联系",
     "客户阶段": "沉睡"},
    {"称呼": "Lisa",
     "基本信息": {"年龄": "30岁", "性别": "女", "职业": "待确认"},
     "行为特征": ["刚加微信", "无购买记录"],
     "在意什么": "待确认",
     "上次聊到哪": "刚加好友，还没聊过",
     "客户阶段": "新用户"},
    {"称呼": "陈阿姨",
     "基本信息": {"年龄": "52岁", "性别": "女", "职业": "退休"},
     "行为特征": ["咨询过但未成交", "明确拒绝推销"],
     "在意什么": "反感被打扰，只信熟人推荐",
     "上次聊到哪": "上个月明确说不需要，别再联系",
     "客户阶段": "流失"},
]


@app.route("/api/seed", methods=["POST"])
def seed():
    db = get_db()
    db.execute('DELETE FROM touch_logs')
    db.execute('DELETE FROM scripts')
    db.execute('DELETE FROM profiles')
    for p in SEED_PROFILES:
        db.execute(
            'INSERT INTO profiles ("称呼","基本信息","行为特征","在意什么","上次聊到哪","客户阶段","更新时间")'
            ' VALUES (?,?,?,?,?,?,?)',
            (p["称呼"], json.dumps(p["基本信息"], ensure_ascii=False),
             json.dumps(p["行为特征"], ensure_ascii=False),
             p["在意什么"], p["上次聊到哪"], p["客户阶段"], now_str()))
    db.commit()
    n = db.execute('SELECT COUNT(*) c FROM profiles').fetchone()["c"]
    return jsonify({"ok": True, "seeded": n})


# ---------- AI 代理 ----------
def get_profile_obj(data):
    """segment/scripts 接口接受 {profile_id} 或 {profile}"""
    if data.get("profile"):
        return data["profile"]
    pid = data.get("profile_id")
    if not pid:
        return None
    db = get_db()
    row = db.execute('SELECT * FROM profiles WHERE id=?', (pid,)).fetchone()
    return row_to_dict(row) if row else None


@app.route("/api/ai/organize", methods=["POST"])
def ai_organize():
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "请输入一句话描述"}), 400
    result, err = call_bailian(PROMPT_ORGANIZE, "客户描述：%s" % text)
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify(result)


@app.route("/api/ai/segment", methods=["POST"])
def ai_segment():
    data = request.get_json(force=True)
    profile = get_profile_obj(data)
    if not profile:
        return jsonify({"error": "缺少客户画像（profile_id 或 profile）"}), 400
    user = "客户画像：%s" % json.dumps(profile, ensure_ascii=False)
    result, err = call_bailian(PROMPT_SEGMENT, user)
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify(result)


@app.route("/api/ai/scripts", methods=["POST"])
def ai_scripts():
    data = request.get_json(force=True)
    profile = get_profile_obj(data)
    if not profile:
        return jsonify({"error": "缺少客户画像（profile_id 或 profile）"}), 400
    audience = data.get("audience", "亲切型")
    user = ("客户画像：%s\n表达对象：%s（技术型重参数原理，业务型重效果案例，老板型重 ROI 决策效率）"
            % (json.dumps(profile, ensure_ascii=False), audience))
    result, err = call_bailian(PROMPT_SCRIPTS, user)
    if err:
        return jsonify({"error": err[0]}), err[1]
    # 风险词标红：后端匹配命中词，前端负责标红展示（只标红不拦截）
    for v in result.get("versions", []):
        v["风险词"] = find_risk_words(v.get("文本", ""))
    return jsonify(result)


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
