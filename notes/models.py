"""
笔记应用 — 数据模型
"""

import os
import uuid

from django.db import models
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
import reversion  # 历史版本：记录笔记每次保存的内容


class Notebook(models.Model):
    """笔记本 = 文件夹"""
    name = models.CharField("名称", max_length=100)
    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="所属用户")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "笔记本"
        verbose_name_plural = "笔记本"
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.name} ({self.user.username})"


@reversion.register()  # Note 的 follow=['tags'] 要求 Tag 也注册，否则加标签时 reversion 报错
class Tag(models.Model):
    """标签"""
    name = models.CharField("标签名", max_length=50, unique=True)
    slug = models.SlugField(max_length=50, unique=True)

    class Meta:
        verbose_name = "标签"
        verbose_name_plural = "标签"

    def __str__(self):
        return self.name


@reversion.register(follow=['tags'])  # 注册历史版本；follow 让标签的增删也随版本记录
class Note(models.Model):
    """笔记"""
    title = models.CharField("标题", max_length=200)
    content = models.TextField("内容", blank=True)
    notebook = models.ForeignKey(
        Notebook, on_delete=models.SET_NULL, null=True, blank=True,
        verbose_name="所属笔记本"
    )
    author = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="作者")
    tags = models.ManyToManyField(Tag, blank=True, verbose_name="标签")

    is_public = models.BooleanField("公开", default=False)
    is_pinned = models.BooleanField("置顶", default=False)
    view_count = models.PositiveIntegerField("浏览量", default=0)

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "笔记"
        verbose_name_plural = "笔记"
        ordering = ['-is_pinned', '-updated_at']

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('note_detail', args=[self.pk])

    def content_preview(self):
        import re
        # 先整段去掉图片/链接语法（如 ![名字](地址)），避免图片名和路径残留进摘要
        text = re.sub(r'!?\[[^\]]*\]\([^)]*\)', '', self.content)
        text = re.sub(r'[#*_~`>\[\]()!|-]', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        # 省略号按清洗后的实际长度判断：原来拿原文长度比，
        # 一篇 150 字但大半是图片语法的笔记，摘要只有 20 字也会被加上 "..."
        return text[:120] + ('...' if len(text) > 120 else '')

    def first_image(self):
        """提取正文里第一张图片的地址（详情页、PDF 用的都是原图）"""
        import re
        match = re.search(r'!\[[^\]]*\]\(([^)\s]+)', self.content)
        return match.group(1) if match else ''

    def thumb_image(self):
        """列表卡片用的小图：优先用 _thumb.jpg，没有（老图 / 外链图）回退原图。

        模板里卡片只显示 130×90 像素，没必要让浏览器下载几 MB 的原图。
        """
        from . import images
        url = self.first_image()
        return images.thumb_url(url) or url


def note_image_path(instance, filename):
    """生成图片保存路径：uploads/年/月/uuid.扩展名
    用 uuid 代替原始文件名，避免中文、空格、括号等特殊字符破坏 Markdown 语法，
    也避免两个用户上传同名文件互相覆盖。"""
    ext = os.path.splitext(filename)[1].lower() or '.jpg'
    return f'uploads/{timezone.now():%Y/%m}/{uuid.uuid4().hex}{ext}'


class UploadedImage(models.Model):
    """笔记里上传的图片"""
    image = models.ImageField("图片", upload_to=note_image_path)
    user = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="上传用户")
    uploaded_at = models.DateTimeField("上传时间", auto_now_add=True)

    class Meta:
        verbose_name = "上传的图片"
        verbose_name_plural = "上传的图片"
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.user.username} 上传于 {self.uploaded_at:%Y-%m-%d}"
