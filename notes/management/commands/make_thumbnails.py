"""为已有图片补生成列表缩略图

新上传的图片会自动生成缩略图；这个命令是给「功能上线前就传好的老图片」补图用的。
只读原图、只新写 _thumb.jpg，不改动任何原文件，所以默认就是安全的：

    python manage.py make_thumbnails            # 只补缺失的
    python manage.py make_thumbnails --force    # 全部重新生成
"""

import os

from django.conf import settings
from django.core.management.base import BaseCommand

from notes import images


class Command(BaseCommand):
    help = '为 media/uploads 下已有图片补生成列表缩略图（不改动原图）'

    def add_arguments(self, parser):
        parser.add_argument('--force', action='store_true', help='已存在缩略图也重新生成')

    def handle(self, *args, **options):
        uploads = os.path.join(str(settings.MEDIA_ROOT), 'uploads')
        if not os.path.isdir(uploads):
            self.stdout.write(self.style.WARNING(f'没有找到图片目录：{uploads}'))
            return

        sources = []
        for root, _dirs, files in os.walk(uploads):
            for name in files:
                if images.THUMB_SUFFIX in name:
                    continue
                if os.path.splitext(name)[1].lower() in ('.jpg', '.jpeg', '.png', '.webp', '.bmp', '.gif'):
                    sources.append(os.path.join(root, name))

        created = skipped = failed = 0
        saved_bytes = 0
        for path in sorted(sources):
            rel = os.path.relpath(path, str(settings.MEDIA_ROOT)).replace(os.sep, '/')
            dst = images.thumb_abs_path(rel)

            if os.path.exists(dst) and not options['force']:
                skipped += 1
                continue

            result = images.make_thumbnail(rel)
            if not result:
                failed += 1
                self.stdout.write(self.style.WARNING(f'  跳过（无法读取）：{rel}'))
                continue

            created += 1
            before = os.path.getsize(path)
            after = os.path.getsize(dst)
            saved_bytes += max(0, before - after)
            self.stdout.write(f'  {rel}  {before / 1024:.0f} KB → {after / 1024:.0f} KB')

        self.stdout.write(
            f'共 {len(sources)} 张：新生成 {created}，已有跳过 {skipped}，失败 {failed}；'
            f'列表页可省下约 {saved_bytes / 1024 / 1024:.1f} MB 下载量'
        )
        if failed:
            self.stdout.write(self.style.WARNING(f'{failed} 张图片无法读取，已跳过'))
