"""图片处理 —— 上传压缩 + 列表缩略图

为什么要有这个模块（2026-11-01 实测终端日志）：
    首页两张笔记配图分别是 1.47 MB 和 2.35 MB，
    而 CSS 里 .note-thumb 的实际显示尺寸只有 130×90 像素。
    也就是说浏览器要下载几 MB 的原图，再缩成 130px 显示，
    首屏慢的根源在这里，不是 Django 渲染慢。

做法：
    1. 上传时把长边压到 1600（详情页放大看也够清晰），JPEG 质量 85；
    2. 同时另存一张长边 480 的小图给列表卡片用（480 够 2x 屏）；
    3. 本来就不大的图（≤300KB 且尺寸没超标）原样保留，避免无意义的二次压缩。
"""

import os
from io import BytesIO

from django.conf import settings
from django.core.files.base import ContentFile
from PIL import Image, ImageOps

MAX_UPLOAD_EDGE = 1600       # 详情页用的长边上限
THUMB_EDGE = 480             # 列表卡片用的长边（显示 130px，480 给高分屏留余量）
JPEG_QUALITY = 85
THUMB_QUALITY = 82
SMALL_ENOUGH = 300 * 1024    # 小于这个体积且尺寸不超标就原样保留
THUMB_SUFFIX = '_thumb'
SKIP_FORMATS = {'GIF'}       # 动图重编码会丢帧，原样保留

# 缩略图统一存 JPEG：列表是 130×90 的小图，体积优先；
# 透明区域用白底合成，避免深色页面透出卡片底色
THUMB_BG = (255, 255, 255)


def _flatten(img):
    """把带透明通道的图合成到白底，转成 RGB（JPEG 不支持透明）"""
    if img.mode == 'P':
        img = img.convert('RGBA')
    if img.mode not in ('RGBA', 'LA'):
        return img.convert('RGB')
    background = Image.new('RGB', img.size, THUMB_BG)
    background.paste(img, mask=img.split()[-1])
    return background


def _scale(img, max_edge):
    """按长边等比缩放（只缩小，不放大）"""
    if max(img.size) <= max_edge:
        return img
    ratio = max_edge / max(img.size)
    size = (max(1, round(img.width * ratio)), max(1, round(img.height * ratio)))
    return img.resize(size, Image.LANCZOS)


def optimize_upload(uploaded_file):
    """压缩上传的图片。

    返回 (ContentFile, 说明)；不需要处理时 ContentFile 为 None，
    调用方应当原样保存 form 里那份文件。
    """
    try:
        uploaded_file.seek(0)
        data = uploaded_file.read()
        uploaded_file.seek(0)
        img = Image.open(BytesIO(data))
        img.load()
    except Exception:
        return None, '无法读取，按原文件保存'

    fmt = (img.format or 'JPEG').upper()
    if fmt in SKIP_FORMATS:
        return None, '动图不压缩'

    too_big = max(img.size) > MAX_UPLOAD_EDGE
    # 尺寸没超标、体积也不大：重编码只会掉画质，不动它
    if not too_big and len(data) <= SMALL_ENOUGH:
        return None, '体积已足够小'

    img = ImageOps.exif_transpose(img)   # 手机竖拍的照片按 EXIF 摆正，否则会躺着
    if too_big:
        img = _scale(img, MAX_UPLOAD_EDGE)

    buf = BytesIO()
    if fmt == 'PNG':
        if img.mode not in ('RGB', 'RGBA', 'L', 'P'):
            img = img.convert('RGBA')
        img.save(buf, 'PNG', optimize=True)
        ext = '.png'
    elif fmt == 'WEBP':
        img.save(buf, 'WEBP', quality=JPEG_QUALITY, method=6)
        ext = '.webp'
    else:
        img = _flatten(img)
        img.save(buf, 'JPEG', quality=JPEG_QUALITY, optimize=True, progressive=True)
        ext = '.jpg'

    new_data = buf.getvalue()
    # 压完反而更大（原图已经是极致压缩），那就别折腾
    if len(new_data) >= len(data) and not too_big:
        return None, '压缩无收益'

    return ContentFile(new_data, name=f'upload{ext}'), f'{len(data) // 1024} KB → {len(new_data) // 1024} KB'


def thumb_rel_path(rel_path):
    """uploads/2026/08/abc.png → uploads/2026/08/abc_thumb.jpg"""
    base, _ = os.path.splitext(rel_path)
    return f'{base}{THUMB_SUFFIX}.jpg'


def thumb_abs_path(rel_path):
    return os.path.join(str(settings.MEDIA_ROOT), thumb_rel_path(rel_path).replace('/', os.sep))


def make_thumbnail(source_rel):
    """为 media 下的一张原图生成缩略图，返回缩略图相对路径；失败返回空字符串。

    只为「列表卡片」服务，所以失败也不抛异常 —— 卡片回退显示原图即可。
    """
    src = os.path.join(str(settings.MEDIA_ROOT), source_rel.replace('/', os.sep))
    try:
        img = Image.open(src)
        img.load()
    except Exception:
        return ''

    img = ImageOps.exif_transpose(img)
    img = _scale(img, THUMB_EDGE)
    img = _flatten(img)

    dst = thumb_abs_path(source_rel)
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        img.save(dst, 'JPEG', quality=THUMB_QUALITY, optimize=True, progressive=True)
    except OSError:
        return ''
    return thumb_rel_path(source_rel)


def thumb_url(image_url):
    """把原图 URL 换成对应的缩略图 URL；缩略图不存在则返回空字符串。

    外链图片（正文里贴的外部地址）直接返回空，由调用方回退原图。
    """
    if not image_url:
        return ''
    media = settings.MEDIA_URL or '/media/'
    if not image_url.startswith(media):
        return ''
    rel = image_url[len(media):]
    dst = thumb_abs_path(rel)
    if not os.path.exists(dst):
        return ''
    return media + thumb_rel_path(rel)
