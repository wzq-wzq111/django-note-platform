# 📝 个人笔记平台

基于 Django 的 Web 笔记平台：Markdown 写作、图片上传、历史版本、统计图表、REST API、AI 辅助写作。

## 功能一览

- 🔐 注册登录（自动登录、会话管理）
- 📝 笔记增删改查：Markdown 渲染、目录自动生成、置顶、浏览量
- 📁 笔记本与标签管理、全文搜索
- 🖼 图片上传：Pillow 校验 + 5MB 限制 + UUID 重命名；超过 1600px 自动压缩，
  并另存 480px 缩略图给列表卡片（实测首页图片下载量从 3.9MB 降到 25KB）
- 🌍 公开 / 私有权限：首页展示公开笔记，私有笔记仅作者可见
- 🕐 历史版本：django-reversion 记录每次保存，支持任意两版对比（红绿高亮 diff）
- 📊 个人中心统计：近 30 天创作趋势、标签分布、浏览量排行（Chart.js）
- 📄 中文 PDF 导出（xhtml2pdf + 中文字体，中文文件名不乱码）
- 🔌 REST API + Swagger 在线文档（DRF + drf-spectacular，对象级权限过滤）
- 🤖 AI 辅助写作：接入 DeepSeek 大模型，一键总结 / 大纲扩写 / Markdown 格式修正
- 🛡 安全：Markdown 输出经 bleach 白名单清洗（防存储型 XSS）、登录失败限流、对象级权限校验
- ✅ 131 条自动化测试持续全绿

## 技术栈

Python · Django 6 · Django REST Framework · django-reversion · SQLite · Bootstrap 5 · Chart.js · xhtml2pdf · openai SDK · bleach

## 本地运行

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 初始化数据库
python manage.py migrate

# 3. （可选）配置密钥，用于 AI 辅助写作与语义搜索
set LLM_API_KEY=sk-你的DeepSeek密钥
set EMBED_API_KEY=你的智谱密钥

# 4. 启动
python manage.py runserver
```

浏览器打开 http://127.0.0.1:8000/ ，注册账号即可使用。

## 部署前必读

默认是开发配置（`DEBUG=True`、允许任意 Host、SECRET_KEY 用内置开发值）。
上线前必须设置以下环境变量，否则程序会按下面的规则处理：

| 变量 | 说明 |
|---|---|
| `DJANGO_DEBUG=0` | 关闭调试，同时开启 HTTPS 跳转、Secure Cookie、HSTS |
| `SECRET_KEY` | **必填**，未设置会直接启动失败（不会悄悄用弱密钥上线） |
| `DJANGO_ALLOWED_HOSTS` | **必填**，逗号分隔的域名；未设置时为空列表，所有请求被拒 |

```bash
# 生成密钥
python -c "import secrets;print(secrets.token_urlsafe(50))"
```

其余运维命令：

```bash
python manage.py rebuild_index     # 重建语义搜索向量索引
python manage.py cleanup_media     # 预览没有被笔记引用的图片
python manage.py cleanup_media --delete   # 确认后执行清理
python manage.py make_thumbnails   # 给上传功能之前的老图片补列表缩略图（不动原图）
python manage.py compress_images --dry-run   # 预览老原图能压掉多少（不动文件）
python manage.py compress_images   # 压缩老原图：长边缩到 1600，不透明 PNG 转 JPEG
```

`compress_images` 会把大图缩到长边 1600、JPEG 质量 85；`alpha` 全不透明
（或根本没有 alpha 通道）的 PNG 会转成 JPEG —— 实测 2.4 MB 的图能降到 244 KB。
转格式时它会**同步改写三处引用**：`UploadedImage.image.name`、笔记正文地址、
reversion 历史快照里的地址，都改成功才删旧 png。执行前请先备份 `media/`。

## 常见问题

**语义搜索页提示「已回退关键词搜索」**

`chromadb` 的 Rust 绑定依赖 MSVC 运行库，报错形如
`ImportError: DLL load failed while importing chromadb_rust_bindings`。

先确认 DLL 是否真的在（有些人「已安装」但文件不在）：

```powershell
Test-Path "$env:WINDIR\System32\msvcp140.dll",
          "$env:WINDIR\System32\vcruntime140.dll",
          "$env:WINDIR\System32\vcruntime140_1.dll"
```

三个都是 `True` 就跳过安装。缺失时装一次（**需要管理员权限**）：

```powershell
winget install Microsoft.VCRedist.2015+.x64
```

装完仍缺失（安装程序只报「已安装」不做修复，常见于安装包缓存损坏），
直接对已注册的 MSI 产品做修复安装，比重新下载安装包更可靠。
先查到这两个产品的缓存包路径（文件名每台机器不同，修复后还会变）：

```powershell
Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Installer\UserData\S-1-5-18\Products' |
  ForEach-Object {
    $ip = Join-Path $_.PSPath 'InstallProperties'
    if (Test-Path $ip) {
      $p = Get-ItemProperty $ip
      if ($p.DisplayName -like '*2022 X64*Runtime*') { "$($p.DisplayName)`n  $($p.LocalPackage)" }
    }
  }
```

再对查到的两个路径（Minimum / Additional）依次执行：

```powershell
msiexec /i "C:\Windows\Installer\<Minimum 的包>" REINSTALL=ALL REINSTALLMODE=vomus /qn
msiexec /i "C:\Windows\Installer\<Additional 的包>" REINSTALL=ALL REINSTALLMODE=vomus /qn
```

`REINSTALLMODE` 里的 `v` 会重新缓存安装包，所以执行后旧文件名会变成新名字——
这是正常的，不算丢文件，注册表里的 `LocalPackage` 会自动指向新缓存。

装完执行 `python manage.py rebuild_index` 把已有笔记补进索引。

**搜不到任何结果**

语义搜索有相关度门槛（`notes/embeddings.py` 里的 `MAX_DISTANCE`）。
向量检索不会返回"查无结果"，不设门槛时搜「今天天气」也会列出一堆笔记。
如果换成余弦距离空间，这个阈值需要重新标定。

## 运行测试

```bash
python manage.py test notes accounts
```

测试跑在独立环境里，不会碰真实数据：

- 数据库用 Django 临时测试库
- 向量库改到 `%TEMP%\chroma_test_*` 下（`settings.CHROMA_DIR`），
  否则测试批量建笔记会把真实笔记的向量和 `is_public` 覆盖掉
- 不调用外部向量化接口（`settings.EMBEDDINGS_ENABLED = False`），整套测试不依赖网络

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
