"""清理没有被任何笔记引用的上传图片

图片是「文件 + 一条 UploadedImage 记录」，笔记正文里以 /media/... 的地址引用。
改笔记时删掉图片链接、或粘贴后没保存，文件就会变成没人引用的孤儿。

默认只列出不删除（安全做法），确认后加 --delete 才真删：

    python manage.py cleanup_media            # 预览
    python manage.py cleanup_media --delete   # 执行
"""

from django.core.management.base import BaseCommand

from notes.models import Note, UploadedImage


class Command(BaseCommand):
    help = '清理没有被任何笔记引用的上传图片（默认只预览，加 --delete 才删除）'

    def add_arguments(self, parser):
        parser.add_argument('--delete', action='store_true', help='真正删除（默认只列出）')

    def handle(self, *args, **options):
        orphans = []
        for img in UploadedImage.objects.select_related('user').iterator():
            if not img.image:
                orphans.append(img)
                continue
            # 正文里还出现这个地址 → 有人用，跳过
            if Note.objects.filter(content__contains=img.image.url).exists():
                continue
            orphans.append(img)

        if not orphans:
            self.stdout.write(self.style.SUCCESS('没有发现孤立图片'))
            return

        total_bytes = 0
        for img in orphans:
            size = 0
            try:
                if img.image and img.image.storage.exists(img.image.name):
                    size = img.image.size
            except OSError:
                size = 0
            total_bytes += size
            self.stdout.write(f'  {img.image.name if img.image else "(无文件)"}  {size / 1024:.0f} KB')

        self.stdout.write(f'共 {len(orphans)} 张，约 {total_bytes / 1024 / 1024:.1f} MB')

        if not options['delete']:
            self.stdout.write(self.style.WARNING('当前是预览，未删除任何文件。确认后加 --delete 执行。'))
            return

        for img in orphans:
            img.delete()      # post_delete 信号负责删磁盘文件
        self.stdout.write(self.style.SUCCESS(f'已删除 {len(orphans)} 张孤立图片'))
