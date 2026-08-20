# 📝 个人笔记平台

基于 Django 的 Web 笔记平台：Markdown 写作、图片上传、历史版本、统计图表、REST API、AI 辅助写作。

## 功能一览

- 🔐 注册登录（自动登录、会话管理）
- 📝 笔记增删改查：Markdown 渲染、目录自动生成、置顶、浏览量
- 📁 笔记本与标签管理、全文搜索
- 🖼 图片上传：Pillow 校验 + 5MB 限制 + UUID 重命名防冲突；列表卡片缩略图
- 🌍 公开 / 私有权限：首页展示公开笔记，私有笔记仅作者可见
- 🕐 历史版本：django-reversion 记录每次保存，支持任意两版对比（红绿高亮 diff）
- 📊 个人中心统计：近 30 天创作趋势、标签分布、浏览量排行（Chart.js）
- 📄 中文 PDF 导出（xhtml2pdf + 中文字体，中文文件名不乱码）
- 🔌 REST API + Swagger 在线文档（DRF + drf-spectacular，对象级权限过滤）
- 🤖 AI 辅助写作：接入 DeepSeek 大模型，一键总结 / 大纲扩写 / Markdown 格式修正
- ✅ 58 条自动化测试持续全绿

## 技术栈

Python · Django 6 · Django REST Framework · django-reversion · SQLite · Bootstrap 5 · Chart.js · xhtml2pdf · openai SDK

## 本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 初始化数据库
python manage.py migrate

# 3. （可选）配置大模型密钥，用于 AI 辅助写作
set LLM_API_KEY=sk-你的DeepSeek密钥

# 4. 启动
python manage.py runserver
```

浏览器打开 http://127.0.0.1:8000/ ，注册账号即可使用。

## 运行测试

```bash
python manage.py test notes accounts
```

## 目录结构

```
├── accounts/     # 注册登录应用
├── notes/        # 笔记应用（模型/视图/API/AI/测试）
├── config/       # Django 项目配置
├── templates/    # HTML 模板
├── static/       # CSS / JS
├── dev-logs/     # 开发日志
└── docs/         # 需求、技术选型、执行计划文档
```
