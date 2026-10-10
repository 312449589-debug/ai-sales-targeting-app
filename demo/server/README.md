# AI 销售定向营销 App · 后端（demo/server）

Flask 后端：3 个 AI 代理接口（一句话整理画像卡 / 五层分群＋策略 / 按画像生成三版话术）＋
画像/话术/触达记录 CRUD，SQLite 单文件数据库，同时托管前端 `static/index.html`。

AI 走千问百炼 OpenAI 兼容模式（`https://dashscope.aliyuncs.com/compatible-mode/v1`，
model `qwen-plus`）。**Key 只从环境变量 `BAILIAN_API_KEY` 读，不写进代码。**

## 本地运行

```bash
cd demo/server
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# 把你的百炼 Key 填进 .env 的 BAILIAN_API_KEY（不要提交）
python app.py
```

浏览器打开 http://127.0.0.1:5000/ 即可使用（前端由 Flask 直接托管）。

## 接口验证

```bash
# 健康检查（Key 未配会返回 warn，属预期）
curl http://127.0.0.1:5000/api/health

# 画像 CRUD
curl -X POST http://127.0.0.1:5000/api/profiles \
  -H 'Content-Type: application/json' \
  -d '{"称呼":"测试","在意什么":"怕疼"}'
curl http://127.0.0.1:5000/api/profiles

# 一键载入 5 个示例客户
curl -X POST http://127.0.0.1:5000/api/seed

# AI 接口（需要真实 Key；Key 无效会返回 502 可读错误，属预期）
curl -X POST http://127.0.0.1:5000/api/ai/organize \
  -H 'Content-Type: application/json' \
  -d '{"text":"纹眉-35岁-怕疼-二次到店意向高"}'
```

## 目录说明

- `app.py`：全部后端逻辑（DB 建表、API、百炼代理、风险词、静态托管）
- `static/index.html`：前端单文件（零外部依赖）
- `requirements.txt`：flask / flask-cors / requests
- `.env.example`：环境变量模板（复制为 `.env` 后填 Key）
- `sales-app.service`：systemd unit（服务器部署用，WorkingDirectory 按实际改）
