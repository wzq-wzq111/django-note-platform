"""
笔记视图 — 列表、详情、创建、编辑、删除、搜索
"""

import os
import re
import json
import logging
import reversion
from io import BytesIO
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
from django.core.cache import cache
from django.db.models import Q, F, Count, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse, HttpResponse, HttpResponseForbidden
from django.template.loader import render_to_string
from django.utils import timezone
from django.utils.html import escape
from django.views.decorators.http import require_POST
from .models import Note, Notebook, Tag, UploadedImage
from .forms import ImageUploadForm, NoteForm
from . import ai, embeddings, rendering, images

logger = logging.getLogger(__name__)

# PDF 缓存时长：笔记改动会生成新的缓存键，这里只是一个兜底上限
PDF_CACHE_SECONDS = 60 * 60

# 送给大模型的正文上限：笔记可能上万字，直接整篇塞进去会超上下文直接报错
AI_INPUT_LIMIT = 8000


def _visible_notes(user):
    """当前用户可见的笔记集合：公开的 + 自己的（用于搜索类页面，避免两次查询）"""
    if user.is_authenticated:
        return Note.objects.filter(Q(is_public=True) | Q(author=user))
    return Note.objects.filter(is_public=True)


def home(request):
    """首页 — 公开笔记"""
    query = request.GET.get('q', '')
    notes = Note.objects.filter(is_public=True).select_related('author')
    if query:
        notes = notes.filter(Q(title__icontains=query) | Q(content__icontains=query))
    notes = notes[:30]
    # 首页标签云只展示"确实有公开笔记在用"的标签，避免泄露别人的私有标签名
    tags = Tag.objects.filter(note__is_public=True).distinct()
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

    # 浏览量 +1：① 用 queryset.update 直接写 SQL，不触发 save 信号，
    #    这样装 django-reversion 后浏览详情页不会产生历史版本；
    # ② 按会话去重，同一个人反复刷新不再重复计数（原来刷新一次 +1，数字可以被刷）。
    viewed = request.session.get('viewed_notes') or []
    if note.pk not in viewed:
        Note.objects.filter(pk=note.pk).update(view_count=F('view_count') + 1)
        note.view_count += 1
        # 只保留最近 500 条，避免 session 无限膨胀
        request.session['viewed_notes'] = (viewed + [note.pk])[-500:]

    # 渲染与清洗统一走 notes.rendering：markdown 默认放行原始 HTML，
    # 直接 |safe 输出会形成存储型 XSS，必须在渲染后按白名单清洗。
    html, toc = rendering.render(note.content, with_toc=True)
    return render(request, 'notes/note_detail.html', {'note': note, 'html': html, 'toc': toc})


# ===== 笔记表单相关的三个小工具（创建/编辑共用，避免两份校验各写一遍）=====

def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _initial_from_note(note):
    """把笔记转成模板回显用的字典；新建时返回空表单"""
    if note is None:
        return {'title': '', 'content': '', 'tags': '', 'notebook': None, 'is_public': False}
    return {
        'title': note.title,
        'content': note.content,
        'tags': ','.join(note.tags.values_list('name', flat=True)),
        'notebook': note.notebook_id,
        'is_public': note.is_public,
    }


def _initial_from_post(post):
    """校验失败时用用户填过的内容回显，别让人白写一遍"""
    return {
        'title': post.get('title', ''),
        'content': post.get('content', ''),
        'tags': post.get('tags', ''),
        'notebook': _to_int(post.get('notebook')),
        'is_public': post.get('is_public') == 'on',
    }


def _form_error(form):
    """取第一条错误，给模板顶部的提示条用"""
    for errors in form.errors.values():
        if errors:
            return errors[0]
    return ''


def _sync_tags(note, names):
    """按名字同步标签：不存在的自动创建（与 API 行为保持一致）"""
    tags = []
    for name in names:
        tag, _ = Tag.objects.get_or_create(name=name, defaults={'slug': name})
        tags.append(tag)
    note.tags.set(tags)


@login_required
def note_create(request):
    """创建笔记"""
    if request.method == 'POST':
        form = NoteForm(request.POST, user=request.user)
        if form.is_valid():
            # 用版本块包住"创建 + 加标签"：历史版本功能会记录这次创建
            with reversion.create_revision():
                note = form.save(commit=False)
                note.author = request.user
                note.save()
                _sync_tags(note, form.cleaned_data['tags'])
                reversion.set_user(request.user)      # 记录是谁保存的
                reversion.set_comment('创建笔记')
            embeddings.index_note(note)   # 写入向量库（失败只记日志，不影响保存）
            return redirect('note_detail', pk=note.pk)
        return render(request, 'notes/note_form.html', {
            'form': form, 'initial': _initial_from_post(request.POST),
            'notebooks': Notebook.objects.filter(user=request.user),
            'error': _form_error(form),
        })
    form = NoteForm(user=request.user)
    return render(request, 'notes/note_form.html', {
        'form': form, 'initial': _initial_from_note(None),
        'notebooks': Notebook.objects.filter(user=request.user),
    })


@login_required
def note_edit(request, pk):
    """编辑笔记（标题必填等规则与创建完全一致，由 NoteForm 统一保证）"""
    note = get_object_or_404(Note, pk=pk, author=request.user)
    if request.method == 'POST':
        form = NoteForm(request.POST, instance=note, user=request.user)
        if form.is_valid():
            # 用版本块包住"保存 + 标签改动"：每次编辑都产生一个历史版本
            with reversion.create_revision():
                note = form.save(commit=False)
                note.save()
                _sync_tags(note, form.cleaned_data['tags'])
                reversion.set_user(request.user)
                reversion.set_comment('编辑保存')
            embeddings.index_note(note)   # 更新向量库（失败只记日志）
            return redirect('note_detail', pk=note.pk)
        return render(request, 'notes/note_form.html', {
            'form': form, 'note': note, 'initial': _initial_from_post(request.POST),
            'notebooks': Notebook.objects.filter(user=request.user),
            'error': _form_error(form),
        })
    form = NoteForm(instance=note, user=request.user)
    return render(request, 'notes/note_form.html', {
        'form': form, 'note': note, 'initial': _initial_from_note(note),
        'notebooks': Notebook.objects.filter(user=request.user),
    })


def _cleanup_note_images(author, content):
    """笔记删除后，清掉它引用过、且没有其他笔记再引用的图片。

    原来删除笔记只删记录，media 目录里的图片永远留着，时间一长就是一堆垃圾。
    这里只删「确认没人再引用」的：其他笔记正文里还出现同一个地址就保留。
    """
    for rel_path in set(re.findall(r'/media/([^)\s"\']+)', content or '')):
        img = UploadedImage.objects.filter(user=author, image=rel_path).first()
        if img is None:
            continue
        if Note.objects.filter(content__contains=f'/media/{rel_path}').exists():
            continue
        img.delete()      # 磁盘文件由 post_delete 信号负责删


@login_required
def note_delete(request, pk):
    """删除笔记"""
    note = get_object_or_404(Note, pk=pk, author=request.user)
    if request.method == 'POST':
        author, content = note.author, note.content
        note.delete()
        _cleanup_note_images(author, content)   # 顺便清理不再被引用的图片
        embeddings.remove_note(pk)   # 同步从向量库删除（失败只记日志）
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

    # 先压缩再存：原图动辄几 MB，而列表卡片只显示 130×90 像素
    optimized, reason = images.optimize_upload(file)
    logger.info('图片上传 %s：%s', file.name, reason)
    obj = UploadedImage.objects.create(
        user=request.user,
        image=optimized if optimized is not None else form.cleaned_data['image'],
    )
    images.make_thumbnail(obj.image.name)   # 另存一张小图给列表卡片
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

    # 渲染 1~3 秒主要花在"把中文字体重嵌进 PDF"上，内容没变就没必要重复做。
    # 缓存键带上 updated_at：笔记一改就是新键，不会返回旧内容。
    cache_key = f'note_pdf:{note.pk}:{int(note.updated_at.timestamp())}'
    pdf_bytes = cache.get(cache_key)
    if pdf_bytes is None:
        # 与详情页共用同一套渲染 + 清洗规则，保证导出的内容与网页一致
        html_content, _ = rendering.render(note.content)
        html = render_to_string('notes/note_pdf.html', {'note': note, 'html_content': html_content})
        buffer = BytesIO()
        status = pisa.CreatePDF(html, dest=buffer, encoding='utf-8', link_callback=media_link_callback)
        if status.err:
            return HttpResponse('PDF 生成失败，请重试', status=500)
        pdf_bytes = buffer.getvalue()
        cache.set(cache_key, pdf_bytes, PDF_CACHE_SECONDS)

    response = HttpResponse(pdf_bytes, content_type='application/pdf')
    # RFC 5987 编码：filename*=UTF-8''... 让中文标题下载时文件名不乱码
    response['Content-Disposition'] = 'attachment; filename*=UTF-8\'\'%s.pdf' % quote(note.title)
    response['Content-Length'] = str(len(pdf_bytes))
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
            {'role': 'user', 'content': f'请用 3~5 句话总结下面这篇笔记的要点：\n\n{note.content[:AI_INPUT_LIMIT]}'},
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
                f'大纲：\n{content[:AI_INPUT_LIMIT]}'
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
                f'原文：\n{content[:AI_INPUT_LIMIT]}'
            )},
        ])
    except ai.LlmError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    return JsonResponse({'result': result})


@login_required
@require_POST
def note_ai_tags(request):
    """AI 智能标签推荐：根据笔记内容推荐 3~5 个标签（要求模型只输出 JSON 数组）"""
    content = request.POST.get('content', '').strip()
    if not content:
        return JsonResponse({'error': '请先写一些内容再推荐标签'}, status=400)
    try:
        result = ai.llm_chat([
            {'role': 'system', 'content': '你是标签推荐助手。只输出 JSON 数组，不要输出任何其他文字。'},
            {'role': 'user', 'content': f'为下面这篇笔记推荐 3~5 个标签，每个不超过 6 个字：\n\n{content[:AI_INPUT_LIMIT]}'},
        ])
    except ai.LlmError as exc:
        return JsonResponse({'error': str(exc)}, status=400)
    try:
        tags = json.loads(result)
        if not isinstance(tags, list):
            raise ValueError('不是数组')
        # 清洗：转字符串、去空白、去空项、最多 5 个
        tags = [str(t).strip() for t in tags if str(t).strip()][:5]
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({'error': '标签解析失败，请重试一次'}, status=400)
    return JsonResponse({'tags': tags})


def note_semantic_search(request):
    """语义搜索页：按「意思」搜笔记（向量相似检索），向量服务不可用时回退关键词搜索。

    权限与首页一致：匿名只看公开笔记，登录者额外可见自己的笔记。
    """
    query = request.GET.get('q', '').strip()
    results, fallback = [], False
    if query:
        try:
            ids = embeddings.search_similar(embeddings.embed_text(query))
        except Exception:
            # 向量服务不可用 → 关键词搜索兜底，功能不中断（但日志里必须留痕）
            logger.warning('语义搜索不可用，已回退关键词搜索', exc_info=True)
            fallback = True
            results = list(_visible_notes(request.user).filter(
                Q(title__icontains=query) | Q(content__icontains=query)
            ).select_related('author')[:10])
        else:
            # 一次查询拿「公开的 + 自己的」：原来分两次查再相加，
            # 自己的公开笔记会同时命中两边，导致结果重复。
            order = {pk: i for i, pk in enumerate(ids)}
            notes = _visible_notes(request.user).filter(pk__in=ids).select_related('author')
            # 按向量相似度从高到低排序
            results = sorted(notes, key=lambda n: order.get(n.pk, 999))[:10]
    return render(request, 'notes/semantic_search.html',
                  {'results': results, 'query': query, 'fallback': fallback})


@login_required
@require_POST
def note_stats_ask(request):
    """自然语言查询统计：LLM 把问题转成结构化参数，后端映射到写死的查询模板。

    安全设计：模型只输出参数 JSON，绝不生成/执行 SQL；
    参数经白名单校验，数字全部来自数据库真实查询。
    """
    question = request.POST.get('question', '').strip()
    if not question:
        return JsonResponse({'error': '请先输入你的问题'}, status=400)

    try:
        result = ai.llm_chat([
            {'role': 'system', 'content': (
                '你是数据查询助手。把用户关于笔记统计的问题转成 JSON，只输出 JSON，'
                '不要输出任何其他文字。格式：'
                '{"time_range": "本月|近7天|近30天|全部", '
                '"dimension": "标签|笔记本|全部", "metric": "篇数|浏览量", "tag": "可选标签名"}'
            )},
            {'role': 'user', 'content': question},
        ])
        params = json.loads(result)
    except (ai.LlmError, json.JSONDecodeError, AttributeError):
        return JsonResponse({'error': '问题理解失败，请换个问法重试'}, status=400)

    # 白名单校验：非法值回退默认（全部 + 篇数）
    time_range = params.get('time_range', '全部') if isinstance(params, dict) else '全部'
    dimension = params.get('dimension', '全部') if isinstance(params, dict) else '全部'
    metric = params.get('metric', '篇数') if isinstance(params, dict) else '篇数'
    tag = str((params.get('tag') or '').strip()) if isinstance(params, dict) else ''
    if time_range not in {'本月', '近7天', '近30天', '全部'}:
        time_range = '全部'
    if dimension not in {'标签', '笔记本', '全部'}:
        dimension = '全部'
    if metric not in {'篇数', '浏览量'}:
        metric = '篇数'

    # ---- 写死的查询模板：参数只来自上面的白名单值 ----
    notes = Note.objects.filter(author=request.user)
    if time_range == '本月':
        start = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        notes = notes.filter(created_at__gte=start)
    elif time_range == '近7天':
        notes = notes.filter(created_at__gte=timezone.now() - timedelta(days=7))
    elif time_range == '近30天':
        notes = notes.filter(created_at__gte=timezone.now() - timedelta(days=30))
    if dimension == '标签' and tag:
        notes = notes.filter(tags__name=tag)
    elif dimension == '笔记本' and tag:
        notes = notes.filter(notebook__name=tag)

    if metric == '篇数':
        value, unit = notes.count(), '篇'
    else:
        value, unit = notes.aggregate(total=Sum('view_count'))['total'] or 0, '次'

    # 回答由后端拼装，数字来自数据库
    dim_str = {'标签': f'带「{tag}」标签的', '笔记本': f'在「{tag}」笔记本里的', '全部': ''}[dimension]
    answer = f'你在{time_range}共写了{dim_str}笔记 {value} {unit}'
    return JsonResponse({'answer': answer})


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
    rest = tag_rows[8:].aggregate(total=Sum('count'))['total'] or 0
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
