"""模型信号 — 让「删记录」和「删文件」保持一致

UploadedImage 存的是文件 + 一条数据库记录。
删记录时 Django 不会去动磁盘上的文件，导致 media 目录只增不减。
每张原图旁边还有一张列表卡片用的 _thumb.jpg，也要一起清掉。
"""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import UploadedImage
from .images import thumb_rel_path


def _remove(storage, name):
    """删文件；文件本来就不在也不报错"""
    if not name:
        return
    try:
        if storage.exists(name):
            storage.delete(name)
    except OSError:
        pass


@receiver(post_delete, sender=UploadedImage)
def delete_image_file(sender, instance, **kwargs):
    """删除图片记录时，同步删除磁盘上的原图和缩略图"""
    if not instance.image:
        return
    storage = instance.image.storage
    name = instance.image.name
    _remove(storage, name)
    _remove(storage, thumb_rel_path(name))
