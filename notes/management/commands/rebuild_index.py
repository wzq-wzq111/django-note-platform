"""
管理命令：重建语义搜索索引

用法：python manage.py rebuild_index
首次启用语义搜索、或向量库数据丢失时全量重建。

要点：
1. 先清空集合。笔记删掉后它的向量不会自动消失，残留条目会让搜索命中
   已不存在的笔记（id 对不上就直接被过滤掉，看起来像"搜不到"）。
2. 重建后核对条数。index_note 内部吞异常（保存笔记时不能因为向量库坏了就失败），
   所以命令里必须自己核对，否则向量库坏了也会打印"重建完成"。
"""
from django.core.management.base import BaseCommand, CommandError

from notes import embeddings
from notes.models import Note


class Command(BaseCommand):
    help = '把全部笔记重新写入向量库（语义搜索索引）'

    def handle(self, *args, **options):
        notes = list(Note.objects.all())

        try:
            collection = embeddings.get_collection()
            existing = collection.get().get('ids') or []
            if existing:
                collection.delete(ids=existing)
        except Exception as exc:
            raise CommandError(f'向量库不可用（{type(exc).__name__}: {exc}）') from exc

        self.stdout.write(f'已清空旧索引 {len(existing)} 条')

        failed = [note.pk for note in notes if not embeddings.index_note(note)]

        total = collection.count()
        if failed:
            raise CommandError(
                f'{len(failed)} 篇笔记未能写入向量库（id={failed[:10]}，详见日志），当前索引 {total} 条'
            )
        self.stdout.write(self.style.SUCCESS(f'索引重建完成，共 {total} 篇笔记'))
