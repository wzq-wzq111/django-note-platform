"""
笔记视图 — 列表、详情、创建、编辑、删除、搜索
"""

import os
import reversion
import markdown
from datetime import timedelta
from reversion.models import Version
from difflib import SequenceMatcher
from urllib.parse import quote
from xhtml2pdf import pisa
from xhtml2pdf.files import pisaFileObject as _pisaFileObject

# Windows 修补：xhtml2pdf 把字体复制进 NamedTemporaryFile 后只返回路径，
# 而该临时文件句柄仍被 xhtml2pdf 持有（系统标记为"关闭即删除"），
# reportlab 再次打开同一路径会被系统拒绝（Permission denied）。
# 这里改成：本地字体文件直接返回原始路径，绕开临时文件复制。
def _get_named_file_direct(self):
    path = getattr(self.instance, 'path', None)
    if isinstance(path, str) and os.path.isfile(path):
        return path
    f = self.instance.get_named_tmp_file()
    return f.name if f else None

_pisaFileObject.getNamedFile = _get_named_file_direct

from django.shortcuts import render, redirect, get_object_or_404
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db.models import Q, F, Count, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.http import require_POST
from .models import Note, Notebook, Tag, UploadedImage
from .forms import ImageUploadForm
from . import ai


def home(request):
    """首页 — 公开笔记"""
    query = request.GET.get('q', '')
    notes = Note.objects.filter(is_public=True).select_related('author')
    if query:
        notes = notes.filter(Q(title__icontains=query) | Q(content__icontains=query))
    notes = notes[:30]
    tags = Tag.objects.all()
    return render(request, 'notes/home.html', {'notes': notes, 'tags': tags, 'query': query})


@login_required
def my_notes(request):
    """我的笔记"""
    notebook_id = request.GET.get('notebook')
    query = request.GET.get('q', '')
    notes = Note.objects.filter(author=request.user)
    if notebook_id:
        notes = notes.filter(notebook_id=notebook_id)
    if query:
        notes = notes.filter(Q(title__icontains=query) | Q(content__icontains=query))
    notebooks = Notebook.objects.filter(user=request.user)
    return render(request, 'notes/my_notes.html', {'notes': notes, 'notebooks': notebooks, 'query': query})


def note_detail(request, pk):
    """笔记详情"""
    note = get_object_or_404(Note.objects.select_related('author', 'notebook'), pk=pk)
    if not note.is_public and note.author != request.user:
        return render(request, 'notes/403.html', status=403)

    # 浏览量 +1：用 queryset.update 直接写 SQL，不触发 save 信号，
    # 这样装 django-reversion 后浏览详情页不会产生历史版本
    Note.objects.filter(pk=note.pk).update(view_count=F('view_count') + 1)

    # 用 Markdown 类而不是 markdown() 函数，convert 之后才能拿到目录（toc）
    md = markdown.Markdown(extensions=['fenced_code', 'codehilite', 'tables', 'toc'])
    html = md.convert(note.content) if note.content else ''
    # 注意：markdown 3.10 在笔记没有标题时 md.toc 会返回空的 <ul></ul> 而不是空字符串，
    # 所以用 md.toc_tokens（标题列表）判断是否真的有标题，避免显示空目录卡片
    toc = md.toc if md.toc_tokens else ''
    return render(request, 'notes/note_detail.html', {'note': note, 'html': html, 'toc': toc})


@login_required
def note_create(request):
    """创建笔记"""
    if request.method == 'POST':
        title = request.POST.get('title', '').strip()
        content = request.POST.get('content', '').strip()
        if not title:
            return render(request, 'notes/note_form.html', {'error': '请输入标题', 'notebooks': Notebook.objects.filter(user=request.user)})
        nb_id = request.POST.get('notebook')
        # 用版本块包住"创建 + 加标签"：历史版本功能会记录这次创建
        with reversion.create_revision():
            note = Note.objects.create(title=title, content=content, author=request.user,
                                       is_public=request.POST.get('is_public') == 'on',
                                       notebook=Notebook.objects.filter(id=nb_id).first() if nb_id else None)
            tag_names = request.POST.get('tags', '')
            for name in tag_names.split(','):
                name = name.strip()
                if name:
                    tag, _ = Tag.objects.get_or_create(name=name, defaults={'slug': name})
                    note.tags.add(tag)
            reversion.set_user(request.user)      # 记录是谁保存的
            reversion.set_comment('创建笔记')
        return redirect('note_detail', pk=note.pk)
    return render(request, 'notes/note_form.html', {'notebooks': Notebook.objects.filter(user=request.user)})


@login_required
def note_edit(request, pk):
    """编辑笔记"""
    note = get_object_or_404(Note, pk=pk, author=request.user)
    if request.method == 'POST':
        nb_id = request.POST.get('notebook')
        # 用版本块包住"保存 + 标签改动"：每次编辑都产生一个历史版本
        with reversion.create_revision():
            note.title = request.POST.get('title', '').strip()
            note.content = request.POST.get('content', '').strip()
            note.is_public = request.POST.get('is_public') == 'on'
            note.notebook = Notebook.objects.filter(id=nb_id).first() if nb_id else None
            note.save()
            note.tags.clear()
            for name in request.POST.get('tags', '').split(','):
                name = name.strip()
                if name:
                    tag, _ = Tag.objects.get_or_create(name=name, defaults={'slug': name})
                    note.tags.add(tag)
            reversion.set_user(request.user)
            reversion.set_comment('编辑保存')
        return redirect('note_detail', pk=note.pk)
    return render(request, 'notes/note_form.html', {'note': note, 'notebooks': Notebook.objects.filter(user=request.user)})


@login_required
def note_delete(request, pk):
    """删除笔记"""
    note = get_object_or_404(Note, pk=pk, author=request.user)
    if request.method == 'POST':
        note.delete()
        return redirect('my_notes')
    return render(request, 'notes/note_confirm_delete.html', {'note': note})


@login_required          # 装饰器从下往上套：login_required 必须在外层，未登录先跳登录页
@require_POST            # 只允许 POST 请求，GET 访问会得到 405
def upload_image(request):
    """图片上传接口：保存图片文件，返回图片地址（JSON）。
    前端 JS 拿到地址后拼成 Markdown 图片语法插进内容框。"""
    if 'image' not in request.FILES:
        return JsonResponse({'error': '没有收到图片文件'}, status=400)

    file = request.FILES['image']
    # 大小限制：服务器端必须查（前端检查只是提前提示，可以被绕过）
    if file.size > 5 * 1024 * 1024:
        return JsonResponse({'error': '图片不能超过 5MB'}, status=400)

    form = ImageUploadForm(request.POST, request.FILES)  # 用 Pillow 校验文件确实是图片
    if not form.is_valid():
        return JsonResponse({'error': '文件不是有效的图片'}, status=400)

    obj = UploadedImage.objects.create(user=request.user, image=form.cleaned_data['image'])
    # file.name 是原始文件名（比如"截图.png"），留给前端做图片的替换文字
    return JsonResponse({'url': obj.image.url, 'name': file.name})


def build_diff(old_text, new_text):
    """逐行比较两个版本，生成红绿高亮 HTML（红=旧版本有，绿=新版本有）"""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    result = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, old_lines, new_lines).get_opcodes():
        if tag == 'equal':
            for line in old_lines[i1:i2]:
                result.append(f'<div>{escape(line) or "&nbsp;"}</div>')
        elif tag == 'delete':       # 只有旧版本有 → 红色（被删除）
            for line in old_lines[i1:i2]:
                result.append(f'<div class="diff-del">- {escape(line) or "&nbsp;"}</div>')
        elif tag == 'insert':       # 只有新版本有 → 绿色（新增）
            for line in new_lines[j1:j2]:
                result.append(f'<div class="diff-ins">+ {escape(line) or "&nbsp;"}</div>')
        elif tag == 'replace':      # 同一位置被改动 → 红旧行 + 绿新行
            for line in old_lines[i1:i2]:
                result.append(f'<div class="diff-del">- {escape(line) or "&nbsp;"}</div>')
            for line in new_lines[j1:j2]:
                result.append(f'<div class="diff-ins">+ {escape(line) or "&nbsp;"}</div>')
    return ''.join(result)


@login_required
def note_history(request, pk):
    """历史版本列表（仅作者可见，非作者直接 404）"""
    note = get_object_or_404(Note, pk=pk, author=request.user)
    versions = Version.objects.get_for_object(note).select_related('revision__user')
    return render(request, 'notes/note_history.html', {'note': note, 'versions': versions})


@login_required
def note_history_compare(request, pk):
    """对比两个历史版本（?v1=&v2= 传版本号；缺省时对比最新两个版本）"""
    note = get_object_or_404(Note, pk=pk, author=request.user)
    versions = list(Version.objects.get_for_object(note).select_related('revision__user'))
    v1_id, v2_id = request.GET.get('v1'), request.GET.get('v2')
    if v1_id and v2_id:
        # 在版本列表里找选中的两个版本（版本号不合法则视为没选）
        v1 = next((v for v in versions if str(v.pk) == v1_id), None)
        v2 = next((v for v in versions if str(v.pk) == v2_id), None)
    elif len(versions) >= 2:
        v1, v2 = versions[1], versions[0]   # 默认：次新 vs 最新
    else:
        v1 = v2 = None
    if not v1 or not v2:
        return render(request, 'notes/note_history.html',
                      {'note': note, 'versions': versions, 'error': '至少需要两个版本才能对比'})
    title_diff = build_diff(v1.field_dict['title'], v2.field_dict['title'])
    content_diff = build_diff(v1.field_dict['content'], v2.field_dict['content'])
    return render(request, 'notes/note_history_compare.html',
                  {'note': note, 'v1': v1, 'v2': v2,
                   'title_diff': title_diff, 'content_diff': content_diff})


def media_link_callback(uri, rel):
    """xhtml2pdf 的 link_callback：把 /media/ 开头的网址换成磁盘路径，
    这样 PDF 才能把笔记里的本地图片嵌进去"""
    if uri.startswith('/media/'):
        return str(settings.MEDIA_ROOT / uri[len('/media/'):])
    return uri


def note_pdf(request, pk):
    """导出笔记为 PDF：权限与查看详情页一致（公开所有人可看，私有仅作者）"""
    note = get_object_or_404(Note, pk=pk)
    if not note.is_public and note.author != request.user:
        return HttpResponseForbidden('无权导出这篇笔记')

    # 用与详情页相同的 Markdown 配置渲染，保证 PDF 内容与网页一致
    md = markdown.Markdown(extensions=['fenced_code', 'codehilite', 'tables'])
    html_content = md.convert(note.content) if note.content else ''

    html = render_to_string('notes/note_pdf.html', {'note': note, 'html_content': html_content})

    response = HttpResponse(content_type='application/pdf')
    # RFC 5987 编码：filename*=UTF-8''... 让中文标题下载时文件名不乱码
    response['Content-Disposition'] = 'attachment; filename*=UTF-8\'\'%s.pdf' % quote(note.title)
    # 渲染 1~3 秒是正常耗时；失败时返回提示页而不是崩溃
    status = pisa.CreatePDF(html, dest=response, encoding='utf-8', link_callback=media_link_callback)
    if status.err:
        return HttpResponse('PDF 生成失败，请重试', status=500)
    return response


@require_POST            # 只允许 POST 请求，GET 访问会得到 405
def note_ai_summarize(request, pk):
    """AI 总结笔记：权限与详情页一致（公开笔记所有人可用，私有仅作者）"""
    note = get_object_or_404(Note, pk=pk)
    if not note.is_public and note.author != request.user:
        return JsonResponse({'error': '没有权限使用这个功能'}, status=403)
    if not note.content.strip():
        return JsonResponse({'error': '笔记没有内容，无法总结'}, status=400)
    try:
        result = ai.llm_chat([
            {'role': 'system', 'content': ai.SYSTEM_PROMPT},
            {'role': 'user', 'content': f'请用 3~5 句话总结下面这篇笔记的要点：\n\n{note.content}'},
        ])
    except ai.LlmError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    return JsonResponse({'result': result})


@login_required
@require_POST
def note_ai_expand(request):
    """AI 扩写：把内容框里的大纲/要点扩写成完整正文（登录即可，编辑页和新建页共用）"""
    content = request.POST.get('content', '').strip()
    if not content:
        return JsonResponse({'error': '请先在内容框里写下大纲或要点'}, status=400)
    try:
        result = ai.llm_chat([
            {'role': 'system', 'content': ai.SYSTEM_PROMPT},
            {'role': 'user', 'content': (
                '请把下面这份大纲扩写成完整的 Markdown 笔记正文：'
                '保留大纲的标题层级，把每个要点展开成通顺的段落。\n\n'
                f'大纲：\n{content}'
            )},
        ])
    except ai.LlmError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    return JsonResponse({'result': result})


@login_required
@require_POST
def note_ai_fix(request):
    """AI 修正 Markdown 格式：把不规范的 Markdown 改规范（不改变内容意思）"""
    content = request.POST.get('content', '').strip()
    if not content:
        return JsonResponse({'error': '请先在内容框里写一些内容'}, status=400)
    try:
        result = ai.llm_chat([
            {'role': 'system', 'content': ai.SYSTEM_PROMPT},
            {'role': 'user', 'content': (
                '请修正下面内容的 Markdown 语法：补齐标题井号、列表符号、代码块围栏，'
                '统一空行和缩进。不要改动文字内容本身，只修正格式。\n\n'
                f'原文：\n{content}'
            )},
        ])
    except ai.LlmError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    return JsonResponse({'result': result})


@login_required
def profile(request):
    """个人中心：统计数字 + 三张图表（创作趋势 / 标签分布 / 浏览量排行）"""
    notes = Note.objects.filter(author=request.user)

    # ① 最近 30 天创作趋势：按天分组统计，没有创作的天数补 0
    start = timezone.now().date() - timedelta(days=29)
    daily = (notes.filter(created_at__date__gte=start)
                  .annotate(day=TruncDate('created_at'))
                  .values('day')
                  .annotate(count=Count('id'))
                  .order_by('day'))
    day_counts = {row['day']: row['count'] for row in daily}
    trend_labels, trend_data = [], []
    for i in range(30):
        day = start + timedelta(days=i)
        trend_labels.append(day.strftime('%m-%d'))
        trend_data.append(day_counts.get(day, 0))

    # ② 标签分布：数量 Top 8，剩下的合并成"其他"
    tag_rows = (Tag.objects.filter(note__author=request.user)
                           .annotate(count=Count('note'))
                           .order_by('-count'))
    tag_labels = [t.name for t in tag_rows[:8]]
    tag_data = [t.count for t in tag_rows[:8]]
    rest = tag_rows[8:].aggregate(total=Count('id'))['total'] or 0
    if rest:
        tag_labels.append('其他')
        tag_data.append(rest)

    # ③ 浏览量 Top 10（标题截断 12 字；横向条形图数据反转让第一名在最上）
    top_notes = notes.exclude(view_count=0).order_by('-view_count')[:10]
    top_labels = [n.title[:12] + ('…' if len(n.title) > 12 else '') for n in top_notes]
    top_data = [n.view_count for n in top_notes]

    # 用户信息卡：笔记总数 + 总浏览量
    context = {
        'note_count': notes.count(),
        'total_views': notes.aggregate(total=Sum('view_count'))['total'] or 0,
        'trend_labels': trend_labels, 'trend_data': trend_data,
        'tag_labels': tag_labels, 'tag_data': tag_data,
        'top_labels': top_labels, 'top_data': top_data,
    }
    return render(request, 'notes/profile.html', context)


@login_required
def notebook_create(request):
    """创建笔记本"""
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        if name:
            Notebook.objects.create(name=name, user=request.user)
    return redirect('my_notes')
