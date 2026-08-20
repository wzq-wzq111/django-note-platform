"""
笔记应用 — 表单
"""

from django import forms


class ImageUploadForm(forms.Form):
    """图片上传的校验表单。
    只负责校验，不在模板里渲染——渲染部分在 note_form.html 里手工写。
    作用：forms.ImageField 会用 Pillow 真正打开文件检查是不是图片，
    手写视图里 Model.objects.create() 是不会做这个校验的。"""
    image = forms.ImageField(label="图片")
