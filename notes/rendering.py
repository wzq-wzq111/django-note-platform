"""
Markdown 渲染 — 统一入口

为什么需要这一层：
    Python-Markdown 默认**放行原始 HTML**，笔记正文里写 <script> 会被原样输出，
    模板再用 |safe 渲染就是存储型 XSS（任何注册用户发一篇公开笔记即可对访客执行脚本）。

做法：
    渲染（markdown）与放行（bleach 白名单）分开，先转成 HTML，再按标签/属性白名单清洗。
    详情页、PDF 导出、目录都走这里，保证"网页看到的"和"导出的"用同一套规则。
"""

import bleach
import markdown

# 详情页与 PDF 共用的扩展：围栏代码块 / 代码高亮 / 表格 / 目录锚点
MARKDOWN_EXTENSIONS = ['fenced_code', 'codehilite', 'tables', 'toc']

# 白名单：只放开 Markdown 真正会生成、且展示需要的标签。
# script / iframe / object / embed / form / style 一律不在名单内。
ALLOWED_TAGS = {
    'a', 'abbr', 'b', 'blockquote', 'br', 'caption', 'code', 'col', 'colgroup',
    'dd', 'del', 'div', 'dl', 'dt', 'em', 'figcaption', 'figure',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'img', 'input', 'ins',
    'kbd', 'li', 'mark', 'ol', 'p', 'pre', 's', 'span', 'strong', 'sub', 'sup',
    'table', 'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'u', 'ul',
}

# 属性白名单：class 是 codehilite / 任务列表要用的；其余按标签单独限制。
ALLOWED_ATTRIBUTES = {
    '*': ['class'],
    'a': ['href', 'title', 'rel'],
    'img': ['src', 'alt', 'title', 'width', 'height', 'loading'],
    'ol': ['start'],
    'th': ['align', 'colspan', 'rowspan'],
    'td': ['align', 'colspan', 'rowspan'],
    'input': ['type', 'checked', 'disabled'],
}

# 允许的链接协议；不带协议的相对路径（如 /media/uploads/x.png）本身安全，bleach 会放行
ALLOWED_PROTOCOLS = {'http', 'https', 'mailto'}


def clean_html(html: str) -> str:
    """按白名单清洗一段 HTML（markdown 输出、目录都过这里）"""
    return bleach.clean(
        html or '',
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )


def render(content: str, with_toc: bool = False):
    """Markdown 正文 → (安全 HTML, 目录 HTML)。

    用 Markdown 类而不是 markdown() 函数，才能在 convert 之后拿到 toc；
    markdown 3.10 在没有标题时 md.toc 会返回空的 <ul></ul>，
    所以用 md.toc_tokens 判断是否真的有标题。
    """
    md = markdown.Markdown(extensions=MARKDOWN_EXTENSIONS)
    html = md.convert(content) if content else ''
    toc = md.toc if md.toc_tokens else ''
    return clean_html(html), (clean_html(toc) if with_toc else '')
