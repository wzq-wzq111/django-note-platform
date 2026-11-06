"""
笔记应用 — 表单

为什么把笔记表单收在这里：
    原来创建/编辑两个视图各自手工读 request.POST 再拼参数，
    结果"标题必填"只在创建里校验、编辑页能存空标题；
    "笔记本只能选自己的"只有 API 序列化器校验、网页端能写进别人的笔记本。
    校验写在视图里就会漏，写进 Form 才是单一来源。
"""

from django import forms

from .models import Note, Notebook


class ImageUploadForm(forms.Form):
    """图片上传的校验表单。
    只负责校验，不在模板里渲染——渲染部分在 note_form.html 里手工写。
    作用：forms.ImageField 会用 Pillow 真正打开文件检查是不是图片，
    手写视图里 Model.objects.create() 是不会做这个校验的。"""
    image = forms.ImageField(label="图片")


class NoteForm(forms.ModelForm):
    """笔记创建 / 编辑共用的表单。

    tags 用「逗号分隔的字符串」而不是 ModelMultipleChoiceField：
    前端是自由输入框，标签允许临时新建，用字符串更贴合实际交互。
    """

    tags = forms.CharField(required=False, label='标签')

    class Meta:
        model = Note
        fields = ['title', 'content', 'notebook', 'is_public']

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        # 笔记本下拉框只列自己的：即使前端被改成别人的 id，表单也不会接受
        if user is not None:
            self.fields['notebook'].queryset = Notebook.objects.filter(user=user)
            self.fields['notebook'].required = False
        # required=False 是故意的：交给 clean_title 统一报「请输入标题」，
        # 否则空白标题会先被字段层拦成 Django 默认的"该字段是必填项。"
        self.fields['title'].required = False

    def clean_title(self):
        # 两个视图共用这一处校验：去掉首尾空白后不能为空
        title = (self.cleaned_data.get('title') or '').strip()
        if not title:
            raise forms.ValidationError('请输入标题')
        return title

    def clean_content(self):
        return (self.cleaned_data.get('content') or '').strip()

    def clean_tags(self):
        """把 "Python, Django，，Python" 整理成 ['Python', 'Django']（去空白、去空项、去重、限长）"""
        names = []
        for raw in (self.cleaned_data.get('tags') or '').split(','):
            name = raw.strip()[:50]
            if name and name not in names:
                names.append(name)
        return names
