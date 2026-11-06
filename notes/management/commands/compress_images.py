"""压缩历史上传的原图（上传时就压缩的逻辑只对新图生效，老图还是原样）

背景（2026-11-01 实测）：
    详情页一张 2844×1280 的 JPEG 1.5 MB、一张 1202×1201 的 PNG 2.4 MB。
    其中 PNG 其实是全不透明的位图（alpha 全 255），用 PNG 存照片等于白烧体积，
    转 JPEG q85 后只要 243 KB —— 小 10 倍，肉眼看不出差别。

    python manage.py compress_images --dry-run    # 先看收益，不动文件
    python manage.py compress_images              # 执行

安全性：
    · 只处理 media/uploads 下、非 _thumb 的图片；
    · 每个文件先写 .tmp 再 os.replace，中途断电不会留下半个坏文件；
    · 转格式（PNG→JPEG）会同步改写 UploadedImage.image.name、笔记正文里的地址、
      以及历史版本快照里的地址，都成功后才会删掉旧 png，任何一步失败就整套回滚；
    · 动手前请先备份 media/（本命令不会自动备份）。

为什么默认允许 PNG→JPEG：
    全部像素的 alpha 都是 255（即完全不透明）时，PNG 的透明通道毫无意义。
    这时转 JPEG 体积降一个数量级；加 --keep-png 可禁用此行为。
"""

import json
import os
import shutil
from io import BytesIO

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from PIL import Image, ImageOps
from reversion.models import Version

from notes import images
from notes.models import Note, UploadedImage

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.bmp')


def _is_opaque(img):
    """判断图片是否完全没有透明像素 —— 没有的话 PNG 的 alpha 通道就是白搭"""
    if img.mode in ('RGB', 'L'):
        return True                      # 这两个模式根本没有 alpha 通道
    if img.mode in ('RGBA', 'LA'):
        return img.split()[-1].getextrema() == (255, 255)
    if img.mode == 'P':
        return 'transparency' not in img.info
    return False                         # CMYK 之类少见模式，不去猜


def _encode(img, fmt, quality):
    """按目标格式编码，返回 (bytes, 扩展名)"""
    buf = BytesIO()
    if fmt == 'PNG':
        if img.mode not in ('RGB', 'RGBA', 'L', 'P'):
            img = img.convert('RGBA')
        img.save(buf, 'PNG', optimize=True)
        return buf.getvalue(), '.png'
    if fmt == 'WEBP':
        img.save(buf, 'WEBP', quality=quality, method=6)
        return buf.getvalue(), '.webp'
    img = images._flatten(img)
    img.save(buf, 'JPEG', quality=quality, optimize=True, progressive=True)
    return buf.getvalue(), '.jpg'


class Command(BaseCommand):
    help = '压缩 media/uploads 下的历史原图（大图缩到 1600、不透明 PNG 转 JPEG）'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='只统计收益，不改动任何文件')
        parser.add_argument('--keep-png', action='store_true', help='不把不透明的 PNG 转成 JPEG')
        parser.add_argument('--quality', type=int, default=images.JPEG_QUALITY, help='JPEG 质量，默认 85')
        parser.add_argument('--max-edge', type=int, default=images.MAX_UPLOAD_EDGE,
                            help=f'长边上限，默认 {images.MAX_UPLOAD_EDGE}')

    # ---------- 采集 ----------
    def _collect(self):
        uploads = os.path.join(str(settings.MEDIA_ROOT), 'uploads')
        found = []
        if not os.path.isdir(uploads):
            return found
        for root, _dirs, files in os.walk(uploads):
            for name in files:
                if images.THUMB_SUFFIX in name:
                    continue
                if os.path.splitext(name)[1].lower() not in IMAGE_EXTS:
                    continue
                path = os.path.join(root, name)
                rel = os.path.relpath(path, str(settings.MEDIA_ROOT)).replace(os.sep, '/')
                found.append((path, rel))
        return sorted(found, key=lambda x: x[1])

    # ---------- 单张规划 ----------
    def _plan(self, path, rel, max_edge, quality, keep_png):
        """返回 dict(action, ...)；不需要处理返回 None"""
        size = os.path.getsize(path)
        try:
            img = Image.open(path)
            img.load()
        except Exception:
            return {'rel': rel, 'action': 'skip', 'reason': '无法读取', 'before': size}

        fmt = (img.format or 'JPEG').upper()
        if fmt in images.SKIP_FORMATS:
            return {'rel': rel, 'action': 'skip', 'reason': '动图', 'before': size}

        too_big = max(img.size) > max_edge
        if not too_big and size <= images.SMALL_ENOUGH:
            return {'rel': rel, 'action': 'skip', 'reason': '已经够小', 'before': size}

        work = ImageOps.exif_transpose(img)
        if too_big:
            work = images._scale(work, max_edge)

        # 完全不透明的 PNG：透明通道没信息量，转 JPEG 体积差一个数量级
        opaque_png = fmt == 'PNG' and not keep_png and _is_opaque(work)
        target_fmt = 'JPEG' if opaque_png or fmt not in ('PNG', 'WEBP') else fmt

        data, ext = _encode(work, target_fmt, quality)
        # 收益不到 5% 就别动：重编码一定掉画质，为几个 KB 不值
        if len(data) >= size * 0.95:
            return {'rel': rel, 'action': 'skip', 'reason': '压缩无收益', 'before': size}

        rel_ext = os.path.splitext(rel)[1].lower()
        return {
            'rel': rel,
            'path': path,
            'action': 'compress',
            'before': size,
            'after': len(data),
            'data': data,
            'ext': ext,
            'rename': rel_ext != ext,
            'new_rel': os.path.splitext(rel)[0] + ext,
        }

    # ---------- 落盘 ----------
    def _rewrite_history(self, old_url, new_url):
        """把旧版本快照里出现过的旧地址替换成新地址"""
        for version in Version.objects.filter(serialized_data__contains=old_url):
            data = json.loads(version.serialized_data)
            changed = False
            for obj in data:
                fields = obj.get('fields', {})
                if old_url in fields.get('content', ''):
                    fields['content'] = fields['content'].replace(old_url, new_url)
                    changed = True
            if changed:
                version.serialized_data = json.dumps(data, ensure_ascii=False)
                version.save(update_fields=['serialized_data'])

    def _write(self, plan):
        """写文件 + 改数据库引用，失败回滚到本次改动之前"""
        old_rel, new_rel = plan['rel'], plan['new_rel']
        old_abs = plan['path']
        new_abs = os.path.join(str(settings.MEDIA_ROOT), new_rel.replace('/', os.sep))
        tmp_abs = new_abs + '.tmp'

        with open(tmp_abs, 'wb') as fh:
            fh.write(plan['data'])
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_abs, new_abs)

        try:
            with transaction.atomic():
                if plan['rename']:
                    # 1) UploadedImage 记录
                    for rec in UploadedImage.objects.filter(image__endswith=os.path.basename(old_rel)):
                        rec.image.name = new_rel
                        rec.save(update_fields=['image'])
                    # 2) 笔记正文里的地址
                    old_url = settings.MEDIA_URL + old_rel
                    new_url = settings.MEDIA_URL + new_rel
                    for note in Note.objects.filter(content__contains=old_url):
                        note.content = note.content.replace(old_url, new_url)
                        note.save(update_fields=['content', 'updated_at'])
                    # 3) 历史版本里的地址
                    #    不一起改的话，将来点「恢复这个版本」图片就 404 了 ——
                    #    这是文件改名，不是内容修改，历史快照理应跟着走
                    self._rewrite_history(old_url, new_url)
        except Exception:
            # 数据库没改成：把新文件撤掉，原文件仍在（同格式时它已被覆盖，靠备份）
            if os.path.exists(new_abs) and os.path.exists(old_abs) and old_abs != new_abs:
                os.remove(new_abs)
            raise

        if plan['rename']:
            # 三处都改成功后才删旧文件
            if os.path.exists(old_abs) and old_abs != new_abs:
                os.remove(old_abs)
            # 缩略图名只跟主文件名有关（xxx_thumb.jpg），转格式不改变它，无需搬动
            old_thumb = images.thumb_abs_path(old_rel)
            new_thumb = images.thumb_abs_path(new_rel)
            if old_thumb != new_thumb and os.path.exists(old_thumb):
                shutil.move(old_thumb, new_thumb)

        images.make_thumbnail(new_rel)      # 缩略图跟着新图重生成

    def handle(self, *args, **options):
        dry = options['dry_run']
        plans = []
        for path, rel in self._collect():
            p = self._plan(path, rel, options['max_edge'], options['quality'], options['keep_png'])
            if p:
                plans.append(p)

        todo = [p for p in plans if p['action'] == 'compress']
        skips = [p for p in plans if p['action'] == 'skip']

        for p in skips:
            self.stdout.write(f'  跳过  {p["rel"]}  （{p["reason"]}）')

        before_total = after_total = 0
        for p in todo:
            before_total += p['before']
            after_total += p['after']
            arrow = '→ ' + p['new_rel'].split('/')[-1] if p['rename'] else ''
            self.stdout.write(
                f'  {p["rel"]}  {p["before"] / 1024:.0f} KB → {p["after"] / 1024:.0f} KB  {arrow}'
            )

        if not todo:
            self.stdout.write(self.style.SUCCESS('没有需要压缩的图片'))
            return

        saved = before_total - after_total
        self.stdout.write(
            f'共 {len(todo)} 张：{before_total / 1024 / 1024:.2f} MB → '
            f'{after_total / 1024 / 1024:.2f} MB，省 {saved / 1024 / 1024:.2f} MB'
            f'（{saved / before_total:.0%}）'
        )

        if dry:
            self.stdout.write(self.style.WARNING('当前是 --dry-run，未改动任何文件。去掉参数才会真正执行。'))
            return

        failed = 0
        for p in todo:
            try:
                self._write(p)
            except Exception as exc:      # noqa: BLE001 —— 一张失败不该中断整批
                failed += 1
                self.stdout.write(self.style.ERROR(f'  失败：{p["rel"]} —— {exc}'))

        if failed:
            self.stdout.write(self.style.ERROR(f'完成，但有 {failed} 张失败（原文件未删，请检查上面的报错）'))
        else:
            self.stdout.write(self.style.SUCCESS(f'完成，{len(todo)} 张已压缩'))
