from django.apps import AppConfig


class NotesConfig(AppConfig):
    name = 'notes'

    def ready(self):
        # 注册信号：删除图片记录时同步删除磁盘文件
        from . import signals  # noqa: F401
