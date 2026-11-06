"""
笔记应用 — 单元测试

覆盖：模型、目录生成、权限、笔记增删改、图片上传、历史版本、
PDF 导出、个人中心、REST API。

运行：python manage.py test notes
"""
import io
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from PIL import Image
import reversion
from reversion.models import Version
from rest_framework.test import APITestCase

from .models import Note, Notebook, Tag, UploadedImage, note_image_path
from . import ai, embeddings, images, rendering


class ModelTests(TestCase):
    """模型方法：content_preview、note_image_path"""

    def setUp(self):
        self.user = User.objects.create_user('tester', 't@t.com', 'test12345')

    def test_content_preview_strips_markdown_symbols(self):
        # markdown 符号（# * ` > 等）应被去掉
        note = Note.objects.create(title='符号', content='# 标题\n**加粗** 和 `代码`', author=self.user)
        preview = note.content_preview()
        self.assertNotIn('#', preview)
        self.assertNotIn('*', preview)
        self.assertNotIn('`', preview)
        self.assertIn('标题', preview)

    def test_content_preview_truncates_over_120(self):
        # 内容超过 120 字符：截断到 120 + 省略号 '...'
        note = Note.objects.create(title='长文', content='a' * 150, author=self.user)
        preview = note.content_preview()
        self.assertEqual(len(preview), 123)
        self.assertTrue(preview.endswith('...'))

    def test_content_preview_short_has_no_ellipsis(self):
        # 恰好 120 字符不省略（条件是 > 120）
        note = Note.objects.create(title='短文', content='a' * 120, author=self.user)
        self.assertEqual(len(note.content_preview()), 120)

    def test_content_preview_strips_image_syntax(self):
        # 图片语法要整段去掉，不能残留图片名和路径
        note = Note.objects.create(
            title='带图', author=self.user,
            content='看图\n![截图名字](/media/uploads/abc.png)\n结尾',
        )
        preview = note.content_preview()
        self.assertNotIn('截图名字', preview)
        self.assertNotIn('/media/', preview)
        self.assertIn('看图', preview)
        self.assertIn('结尾', preview)

    def test_content_preview_ellipsis_uses_cleaned_length(self):
        # 原文 200+ 字但大半是图片语法，清洗后正文很短 → 不该加省略号
        note = Note.objects.create(
            title='多图', author=self.user,
            content='短文' + '![图](/media/uploads/x.png)' * 10,
        )
        self.assertNotIn('...', note.content_preview())

    def test_first_image_returns_url(self):
        # 正文里有图片语法 → 返回第一张图片的地址
        note = Note.objects.create(
            title='带图', author=self.user,
            content='文字\n![第一张](/media/uploads/a.png)\n![第二张](/media/uploads/b.png)',
        )
        self.assertEqual(note.first_image(), '/media/uploads/a.png')

    def test_first_image_empty_when_no_image(self):
        note = Note.objects.create(title='无图', content='纯文字', author=self.user)
        self.assertEqual(note.first_image(), '')

    def test_note_image_path_format(self):
        # 路径格式：uploads/年/月/32位uuid.扩展名
        img = UploadedImage(user=self.user)
        path = note_image_path(img, '照片.PNG')
        self.assertRegex(path, r'^uploads/\d{4}/\d{2}/[0-9a-f]{32}\.png$')

    def test_note_image_path_no_extension_defaults_jpg(self):
        img = UploadedImage(user=self.user)
        self.assertTrue(note_image_path(img, '无扩展名').endswith('.jpg'))


class TocTests(TestCase):
    """目录生成：有标题显示、无标题隐藏"""

    def setUp(self):
        self.user = User.objects.create_user('tester', 't@t.com', 'test12345')
        self.client.force_login(self.user)

    def test_toc_shown_when_has_headings(self):
        note = Note.objects.create(title='有目录', content='# 一级\n\n## 二级', author=self.user)
        response = self.client.get(reverse('note_detail', args=[note.pk]))
        self.assertEqual(response.status_code, 200)
        toc = response.context['toc']
        self.assertTrue(toc)
        self.assertIn('href', toc)
        self.assertIn('一级', toc)

    def test_toc_empty_when_no_headings(self):
        note = Note.objects.create(title='无目录', content='普通文字', author=self.user)
        response = self.client.get(reverse('note_detail', args=[note.pk]))
        self.assertEqual(response.context['toc'], '')


class NotePermissionTests(TestCase):
    """查看/编辑权限：私有笔记只有作者能看，公开所有人能看"""

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@o.com', 'test12345')
        self.other = User.objects.create_user('other', 'x@x.com', 'test12345')
        self.private_note = Note.objects.create(title='私有笔记', content='秘密', author=self.owner)
        self.public_note = Note.objects.create(title='公开笔记', content='公开', author=self.owner, is_public=True)

    def test_private_note_anonymous_403(self):
        response = self.client.get(reverse('note_detail', args=[self.private_note.pk]))
        self.assertEqual(response.status_code, 403)

    def test_private_note_other_user_403(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('note_detail', args=[self.private_note.pk]))
        self.assertEqual(response.status_code, 403)

    def test_private_note_owner_200(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('note_detail', args=[self.private_note.pk]))
        self.assertEqual(response.status_code, 200)

    def test_public_note_anonymous_200(self):
        response = self.client.get(reverse('note_detail', args=[self.public_note.pk]))
        self.assertEqual(response.status_code, 200)

    def test_my_notes_anonymous_redirects_with_next(self):
        response = self.client.get(reverse('my_notes'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/my/', response.url)

    def test_edit_other_note_404(self):
        # 编辑/删除他人笔记用 get_object_or_404 过滤作者 → 404 而非 403
        self.client.force_login(self.other)
        response = self.client.get(reverse('note_edit', args=[self.private_note.pk]))
        self.assertEqual(response.status_code, 404)

    def test_delete_other_note_404(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('note_delete', args=[self.private_note.pk]))
        self.assertEqual(response.status_code, 404)


class NoteCrudTests(TestCase):
    """笔记增删改：创建、空标题、编辑、删除、笔记本"""

    def setUp(self):
        self.user = User.objects.create_user('tester', 't@t.com', 'test12345')
        self.client.force_login(self.user)

    def test_create_note(self):
        response = self.client.post(reverse('note_create'), {
            'title': '新建笔记', 'content': '内容', 'tags': '甲,乙',
        })
        note = Note.objects.get(title='新建笔记')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('note_detail', args=[note.pk]))
        self.assertEqual(note.author, self.user)
        self.assertEqual({t.name for t in note.tags.all()}, {'甲', '乙'})

    def test_create_empty_title_shows_error(self):
        response = self.client.post(reverse('note_create'), {'title': '  '})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['error'], '请输入标题')
        self.assertFalse(Note.objects.exists())

    def test_edit_note_replaces_tags(self):
        note = Note.objects.create(title='旧标题', content='旧内容', author=self.user)
        note.tags.add(Tag.objects.create(name='旧标签', slug='旧标签'))
        response = self.client.post(reverse('note_edit', args=[note.pk]), {
            'title': '新标题', 'content': '新内容', 'tags': '新标签',
        })
        self.assertEqual(response.status_code, 302)
        note.refresh_from_db()
        self.assertEqual(note.title, '新标题')
        self.assertEqual(note.content, '新内容')
        self.assertEqual([t.name for t in note.tags.all()], ['新标签'])

    def test_delete_note_post(self):
        note = Note.objects.create(title='待删', content='', author=self.user)
        response = self.client.post(reverse('note_delete', args=[note.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Note.objects.filter(pk=note.pk).exists())

    def test_delete_note_get_shows_confirm(self):
        # GET 只显示确认页，不删除
        note = Note.objects.create(title='待删', content='', author=self.user)
        response = self.client.get(reverse('note_delete', args=[note.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Note.objects.filter(pk=note.pk).exists())

    def test_notebook_create(self):
        response = self.client.post(reverse('notebook_create'), {'name': '测试本'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('my_notes'))
        self.assertTrue(Notebook.objects.filter(name='测试本', user=self.user).exists())

    def test_notebook_create_get_redirects(self):
        response = self.client.get(reverse('notebook_create'))
        self.assertEqual(response.status_code, 302)


class UploadImageTests(TestCase):
    """图片上传：成功、无文件、超限、假图、未登录、GET 方法"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 测试图片存进临时目录，不污染真实 media 目录
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls._override = override_settings(MEDIA_ROOT=cls._tmp_dir.name)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        cls._tmp_dir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user('tester', 't@t.com', 'test12345')
        self.client.force_login(self.user)

    def _make_png_bytes(self):
        buf = io.BytesIO()
        Image.new('RGB', (10, 10), color='red').save(buf, format='PNG')
        return buf.getvalue()

    def test_upload_png_success(self):
        response = self.client.post(reverse('upload_image'), {
            'image': SimpleUploadedFile('photo.png', self._make_png_bytes(), content_type='image/png'),
        })
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data['url'].startswith('/media/uploads/'))
        self.assertEqual(data['name'], 'photo.png')
        self.assertEqual(UploadedImage.objects.count(), 1)

    def test_upload_without_file_400(self):
        response = self.client.post(reverse('upload_image'))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], '没有收到图片文件')

    def test_upload_too_large_400(self):
        # 先查大小再验图片，所以传任意 5MB+ 字节即可
        big = SimpleUploadedFile('big.png', b'\x00' * (5 * 1024 * 1024 + 1))
        response = self.client.post(reverse('upload_image'), {'image': big})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], '图片不能超过 5MB')

    def test_upload_fake_image_400(self):
        fake = SimpleUploadedFile('fake.png', b'not an image', content_type='image/png')
        response = self.client.post(reverse('upload_image'), {'image': fake})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], '文件不是有效的图片')

    def test_upload_anonymous_redirects(self):
        self.client.logout()
        response = self.client.post(reverse('upload_image'))
        self.assertEqual(response.status_code, 302)

    def test_upload_get_405(self):
        response = self.client.get(reverse('upload_image'))
        self.assertEqual(response.status_code, 405)


class HistoryTests(TestCase):
    """历史版本：留档、块外保存不留档、页面权限、对比、浏览不产生版本"""

    def setUp(self):
        self.user = User.objects.create_user('tester', 't@t.com', 'test12345')
        self.other = User.objects.create_user('other', 'x@x.com', 'test12345')
        self.client.force_login(self.user)
        self.client.post(reverse('note_create'), {'title': '版本笔记', 'content': '第一版'})
        self.note = Note.objects.get(title='版本笔记')

    def test_versions_recorded_on_create_and_edit(self):
        # 创建 1 个版本，编辑两次后共 3 个
        self.assertEqual(len(Version.objects.get_for_object(self.note)), 1)
        self.client.post(reverse('note_edit', args=[self.note.pk]), {'title': '版本笔记', 'content': '第二版'})
        self.client.post(reverse('note_edit', args=[self.note.pk]), {'title': '版本笔记', 'content': '第三版'})
        self.assertEqual(len(Version.objects.get_for_object(self.note)), 3)

    def test_save_without_revision_records_nothing(self):
        # 不经 create_revision 块直接保存 → 不留档
        self.note.content = '偷偷改'
        self.note.save()
        self.assertEqual(len(Version.objects.get_for_object(self.note)), 1)

    def test_history_page_owner_200(self):
        response = self.client.get(reverse('note_history', args=[self.note.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '历史版本')

    def test_history_page_other_404(self):
        self.client.force_login(self.other)
        response = self.client.get(reverse('note_history', args=[self.note.pk]))
        self.assertEqual(response.status_code, 404)

    def test_history_page_anonymous_redirects(self):
        self.client.logout()
        response = self.client.get(reverse('note_history', args=[self.note.pk]))
        self.assertEqual(response.status_code, 302)

    def test_compare_page_shows_diff(self):
        self.client.post(reverse('note_edit', args=[self.note.pk]), {'title': '版本笔记', 'content': '第二版'})
        response = self.client.get(reverse('note_history_compare', args=[self.note.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'diff-del')  # 删除的旧行（红）
        self.assertContains(response, 'diff-ins')  # 新增的新行（绿）

    def test_compare_with_single_version_falls_back(self):
        # 只有 1 个版本无法对比 → 回落到历史页并提示
        response = self.client.get(reverse('note_history_compare', args=[self.note.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['error'], '至少需要两个版本才能对比')

    def test_detail_view_does_not_create_version(self):
        # 浏览详情页 3 次：版本数不变（浏览量另算）
        version_count = len(Version.objects.get_for_object(self.note))
        for _ in range(3):
            self.client.get(reverse('note_detail', args=[self.note.pk]))
        self.assertEqual(len(Version.objects.get_for_object(self.note)), version_count)

    def test_view_count_deduped_within_session(self):
        # 同一个人反复刷新只计 1 次（原来刷新一次 +1，数字可以刷）
        for _ in range(3):
            self.client.get(reverse('note_detail', args=[self.note.pk]))
        self.note.refresh_from_db()
        self.assertEqual(self.note.view_count, 1)

    def test_view_count_increments_for_new_session(self):
        # 换一个会话（相当于换设备/换浏览器）访问 → 再 +1
        self.client.get(reverse('note_detail', args=[self.note.pk]))
        self.client.session.flush()          # 会话清空，去重记录随之消失
        self.client.force_login(self.user)   # 这篇是私有笔记，重新登录才能看
        self.client.get(reverse('note_detail', args=[self.note.pk]))
        self.note.refresh_from_db()
        self.assertEqual(self.note.view_count, 2)


class PdfTests(TestCase):
    """PDF 导出：作者/公开 200，私有非作者 403

    注意：前两条测试真实渲染 PDF（加载中文字体），单条约 2–5 秒。
    """

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@o.com', 'test12345')
        self.private_note = Note.objects.create(title='私有', content='中文内容', author=self.owner)
        self.public_note = Note.objects.create(title='公开', content='公开内容', author=self.owner, is_public=True)

    def test_pdf_owner_200(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('note_pdf', args=[self.private_note.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_pdf_public_anonymous_200(self):
        response = self.client.get(reverse('note_pdf', args=[self.public_note.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'%PDF'))

    def test_pdf_private_anonymous_403(self):
        response = self.client.get(reverse('note_pdf', args=[self.private_note.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertContains(response, '无权导出这篇笔记', status_code=403)


class AiViewsTests(TestCase):
    """AI 辅助写作：总结（权限与详情页一致）、扩写、修正格式

    全部 mock 掉 ai.llm_chat，测试不调用真实大模型 API、不花钱。
    """

    def setUp(self):
        self.owner = User.objects.create_user('ai_owner', 'a@a.com', 'test12345')
        self.other = User.objects.create_user('ai_other', 'b@b.com', 'test12345')
        self.pub = Note.objects.create(title='公开笔记', content='公开内容', author=self.owner, is_public=True)
        self.priv = Note.objects.create(title='私有笔记', content='私有内容', author=self.owner)

    # ---- AI 总结 ----

    @patch('notes.ai.llm_chat', return_value='这是 AI 生成的总结')
    def test_summarize_owner_200(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_summarize', args=[self.priv.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], '这是 AI 生成的总结')

    @patch('notes.ai.llm_chat', return_value='总结')
    def test_summarize_public_anonymous_200(self, mock_chat):
        # 公开笔记匿名可用（与详情页权限一致）
        response = self.client.post(reverse('note_ai_summarize', args=[self.pub.pk]))
        self.assertEqual(response.status_code, 200)

    @patch('notes.ai.llm_chat', return_value='总结')
    def test_summarize_private_anonymous_403(self, mock_chat):
        response = self.client.post(reverse('note_ai_summarize', args=[self.priv.pk]))
        self.assertEqual(response.status_code, 403)

    @patch('notes.ai.llm_chat', return_value='总结')
    def test_summarize_private_other_user_403(self, mock_chat):
        self.client.force_login(self.other)
        response = self.client.post(reverse('note_ai_summarize', args=[self.priv.pk]))
        self.assertEqual(response.status_code, 403)

    @patch('notes.ai.llm_chat', return_value='总结')
    def test_summarize_empty_content_400(self, mock_chat):
        note = Note.objects.create(title='空笔记', content='', author=self.owner)
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_summarize', args=[note.pk]))
        self.assertEqual(response.status_code, 400)

    @patch('notes.ai.llm_chat', side_effect=ai.LlmError('大模型调用失败，请稍后重试'))
    def test_summarize_llm_error_returns_message(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_summarize', args=[self.priv.pk]))
        self.assertEqual(response.status_code, 400)
        self.assertIn('大模型调用失败', response.json()['error'])

    @patch('notes.ai.llm_chat', return_value='总结')
    def test_summarize_get_405(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('note_ai_summarize', args=[self.priv.pk]))
        self.assertEqual(response.status_code, 405)

    # ---- AI 扩写 / 修正格式 ----

    @patch('notes.ai.llm_chat', return_value='扩写后的完整正文')
    def test_expand_ok(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_expand'), {'content': '大纲要点'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], '扩写后的完整正文')

    @patch('notes.ai.llm_chat', return_value='修正后的 Markdown')
    def test_fix_ok(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_fix'), {'content': '格式混乱的内容'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['result'], '修正后的 Markdown')

    def test_expand_anonymous_redirects(self):
        response = self.client.post(reverse('note_ai_expand'), {'content': 'x'})
        self.assertEqual(response.status_code, 302)

    def test_fix_anonymous_redirects(self):
        response = self.client.post(reverse('note_ai_fix'), {'content': 'x'})
        self.assertEqual(response.status_code, 302)

    @patch('notes.ai.llm_chat', return_value='x')
    def test_expand_empty_content_400(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_expand'), {'content': '  '})
        self.assertEqual(response.status_code, 400)

    def test_llm_chat_without_key_raises(self):
        # 没配置密钥时直接抛 LlmError，中文提示用户
        with override_settings(LLM_API_KEY=''):
            with self.assertRaises(ai.LlmError):
                ai.llm_chat([{'role': 'user', 'content': '你好'}])

    # ---- AI 智能标签推荐 ----

    @patch('notes.ai.llm_chat', return_value='["Django", "数据库"]')
    def test_ai_tags_ok(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_tags'), {'content': 'Django 笔记'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['tags'], ['Django', '数据库'])

    @patch('notes.ai.llm_chat', return_value='不是JSON')
    def test_ai_tags_bad_json_400(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_tags'), {'content': 'x'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('解析失败', response.json()['error'])

    @patch('notes.ai.llm_chat', return_value='"不是数组"')
    def test_ai_tags_not_list_400(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_tags'), {'content': 'x'})
        self.assertEqual(response.status_code, 400)

    @patch('notes.ai.llm_chat', return_value='["A", "B", "C", "D", "E", "F", "  ", "G"]')
    def test_ai_tags_cleans_and_caps_5(self, mock_chat):
        # 空串被过滤、最多保留 5 个
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_tags'), {'content': 'x'})
        self.assertEqual(response.json()['tags'], ['A', 'B', 'C', 'D', 'E'])

    def test_ai_tags_anonymous_redirects(self):
        response = self.client.post(reverse('note_ai_tags'), {'content': 'x'})
        self.assertEqual(response.status_code, 302)

    @patch('notes.ai.llm_chat', return_value='["A"]')
    def test_ai_tags_empty_content_400(self, mock_chat):
        self.client.force_login(self.owner)
        response = self.client.post(reverse('note_ai_tags'), {'content': '  '})
        self.assertEqual(response.status_code, 400)


class SemanticSearchTests(TestCase):
    """语义搜索：向量检索 + 权限过滤 + 关键词兜底（embedding 全部 mock）"""

    def setUp(self):
        self.owner = User.objects.create_user('sem_owner', 'a@a.com', 'test12345')
        self.other = User.objects.create_user('sem_other', 'b@b.com', 'test12345')
        self.pub = Note.objects.create(title='数据库优化', content='索引与慢查询', author=self.owner, is_public=True)
        self.priv = Note.objects.create(title='我的秘密', content='只有我能看', author=self.owner)
        self.other_priv = Note.objects.create(title='别人的秘密', content='不该出现', author=self.other)

    def _mock_search(self, ids):
        # mock 掉向量化和向量检索两个环节
        patch1 = patch('notes.embeddings.embed_text', return_value=[0.1, 0.2])
        patch2 = patch('notes.embeddings.search_similar', return_value=ids)
        return patch1, patch2

    def test_anonymous_only_public(self):
        with patch('notes.embeddings.embed_text', return_value=[0.1]), \
             patch('notes.embeddings.search_similar', return_value=[self.priv.pk, self.pub.pk, self.other_priv.pk]):
            response = self.client.get(reverse('note_semantic_search'), {'q': '数据库'})
        titles = [n.title for n in response.context['results']]
        self.assertIn('数据库优化', titles)
        self.assertNotIn('我的秘密', titles)
        self.assertNotIn('别人的秘密', titles)

    def test_logged_in_sees_own_private(self):
        self.client.force_login(self.owner)
        with patch('notes.embeddings.embed_text', return_value=[0.1]), \
             patch('notes.embeddings.search_similar', return_value=[self.priv.pk, self.pub.pk]):
            response = self.client.get(reverse('note_semantic_search'), {'q': '秘密'})
        titles = [n.title for n in response.context['results']]
        self.assertIn('我的秘密', titles)
        self.assertIn('数据库优化', titles)

    def test_fallback_keyword_when_embed_fails(self):
        # 向量服务挂了 → 回退关键词搜索，页面正常返回
        with patch('notes.embeddings.embed_text', side_effect=Exception('服务不可用')):
            response = self.client.get(reverse('note_semantic_search'), {'q': '数据库'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['fallback'])
        titles = [n.title for n in response.context['results']]
        self.assertIn('数据库优化', titles)

    def test_empty_query_shows_no_results(self):
        response = self.client.get(reverse('note_semantic_search'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['results']), [])

    def test_create_note_writes_index(self):
        # 创建笔记后同步写入向量库
        self.client.force_login(self.owner)
        with patch('notes.embeddings.index_note') as mock_index:
            self.client.post(reverse('note_create'), {'title': '新笔记', 'content': '内容'})
        self.assertEqual(mock_index.call_count, 1)

    def test_edit_note_updates_index(self):
        self.client.force_login(self.owner)
        with patch('notes.embeddings.index_note') as mock_index:
            self.client.post(reverse('note_edit', args=[self.priv.pk]), {'title': '新', 'content': '改'})
        self.assertEqual(mock_index.call_count, 1)

    def test_delete_note_removes_index(self):
        self.client.force_login(self.owner)
        with patch('notes.embeddings.remove_note') as mock_remove:
            self.client.post(reverse('note_delete', args=[self.priv.pk]))
        mock_remove.assert_called_once_with(self.priv.pk)

    def test_rebuild_index_command(self):
        # 管理命令遍历全部笔记重建索引
        from django.core.management import call_command
        fake_col = MagicMock()
        fake_col.get.return_value = {'ids': ['99', '100']}
        fake_col.count.return_value = 3
        with patch('notes.embeddings.get_collection', return_value=fake_col), \
             patch('notes.embeddings.index_note') as mock_index:
            call_command('rebuild_index')
        self.assertEqual(mock_index.call_count, 3)
        # 重建前必须先清空：已删除笔记的残留向量会让搜索命中不存在的 id
        fake_col.delete.assert_called_once_with(ids=['99', '100'])

    def test_rebuild_index_fails_loudly_when_notes_not_indexed(self):
        # 向量库写不进去时必须报错退出，不能像以前那样照样打印「重建完成」
        from django.core.management import call_command
        from django.core.management.base import CommandError
        fake_col = MagicMock()
        fake_col.get.return_value = {'ids': []}
        fake_col.count.return_value = 0
        with patch('notes.embeddings.get_collection', return_value=fake_col), \
             patch('notes.embeddings.index_note', return_value=False):
            with self.assertRaises(CommandError):
                call_command('rebuild_index')

    def test_rebuild_index_fails_when_vector_store_unavailable(self):
        # 向量库整个打不开（如缺 VC++ 运行库）→ 明确报错，而不是静默成功
        from django.core.management import call_command
        from django.core.management.base import CommandError
        with patch('notes.embeddings.get_collection', side_effect=ImportError('DLL load failed')):
            with self.assertRaises(CommandError):
                call_command('rebuild_index')


class VectorStoreIsolationTests(SimpleTestCase):
    """跑测试时向量库必须落在临时目录

    否则测试里创建的几十篇笔记会把真实笔记的向量和 is_public 元数据覆盖掉，
    表现为「匿名用户明明有公开笔记却搜不到」。
    """

    def test_chroma_dir_is_not_production_path(self):
        self.assertNotEqual(
            Path(settings.CHROMA_DIR).resolve(),
            (Path(settings.BASE_DIR) / 'chroma_db').resolve(),
        )


class StatsAskTests(TestCase):
    """自然语言查询统计：LLM 输出参数 → 白名单校验 → 写死的查询模板（LLM 全部 mock）"""

    def setUp(self):
        self.user = User.objects.create_user('stats_user', 'a@a.com', 'test12345')
        self.client.force_login(self.user)
        self.nb = Notebook.objects.create(name='后端本', user=self.user)
        self.tag = Tag.objects.create(name='后端', slug='后端')
        self.n1 = Note.objects.create(title='一', content='', author=self.user, view_count=10)
        self.n1.tags.add(self.tag)
        self.n2 = Note.objects.create(title='二', content='', author=self.user, view_count=5)
        self.n2.notebook = self.nb
        self.n2.save()

    def _ask(self, llm_return):
        with patch('notes.ai.llm_chat', return_value=llm_return):
            return self.client.post(reverse('note_stats_ask'), {'question': '测试问题'})

    def test_count_all(self):
        response = self._ask('{"time_range": "全部", "dimension": "全部", "metric": "篇数"}')
        self.assertEqual(response.status_code, 200)
        self.assertIn('2 篇', response.json()['answer'])

    def test_views_metric(self):
        response = self._ask('{"time_range": "全部", "dimension": "全部", "metric": "浏览量"}')
        self.assertIn('15 次', response.json()['answer'])

    def test_tag_dimension(self):
        response = self._ask('{"time_range": "全部", "dimension": "标签", "metric": "篇数", "tag": "后端"}')
        self.assertIn('1 篇', response.json()['answer'])
        self.assertIn('后端', response.json()['answer'])

    def test_notebook_dimension(self):
        response = self._ask('{"time_range": "全部", "dimension": "笔记本", "metric": "篇数", "tag": "后端本"}')
        self.assertIn('1 篇', response.json()['answer'])

    def test_invalid_params_fallback(self):
        # 非法参数回退默认：全部 + 篇数
        response = self._ask('{"time_range": "去年", "dimension": "颜色", "metric": "字数"}')
        self.assertIn('2 篇', response.json()['answer'])

    def test_bad_json_error(self):
        response = self._ask('乱七八糟不是JSON')
        self.assertEqual(response.status_code, 400)

    def test_anonymous_redirects(self):
        self.client.logout()
        response = self.client.post(reverse('note_stats_ask'), {'question': 'x'})
        self.assertEqual(response.status_code, 302)

    def test_empty_question_400(self):
        with patch('notes.ai.llm_chat', return_value='{}'):
            response = self.client.post(reverse('note_stats_ask'), {'question': '  '})
        self.assertEqual(response.status_code, 400)


class ProfileTests(TestCase):
    """个人中心：未登录跳转、登录可见"""
    """个人中心：未登录跳转、登录可见"""

    def setUp(self):
        self.user = User.objects.create_user('tester', 't@t.com', 'test12345')

    def test_profile_anonymous_redirects(self):
        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('/profile/', response.url)

    def test_profile_logged_in_200(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('profile'))
        self.assertEqual(response.status_code, 200)


class NoteApiTests(APITestCase):
    """REST API：列表过滤、创建作者固定、他人不可改、文档页"""

    def setUp(self):
        self.u1 = User.objects.create_user('api_owner', 'a@a.com', 'test12345')
        self.u2 = User.objects.create_user('api_other', 'b@b.com', 'test12345')
        self.nb1 = Notebook.objects.create(name='本一', user=self.u1)
        self.nb2 = Notebook.objects.create(name='本二', user=self.u2)
        self.pub = Note.objects.create(title='接口公开', content='公开', author=self.u1, is_public=True)
        self.priv = Note.objects.create(title='接口私有', content='私有', author=self.u1)

    def test_anonymous_list_only_public(self):
        response = self.client.get('/api/notes/')
        self.assertEqual(response.status_code, 200)
        titles = [n['title'] for n in response.json()['results']]
        self.assertIn('接口公开', titles)
        self.assertNotIn('接口私有', titles)

    def test_logged_in_sees_own_private(self):
        self.client.force_authenticate(user=self.u1)
        titles = [n['title'] for n in self.client.get('/api/notes/').json()['results']]
        self.assertIn('接口私有', titles)

    def test_list_is_paginated(self):
        # 列表接口按 20 条分页，避免数据一多就返回整库
        for i in range(25):
            Note.objects.create(title=f'批量{i}', content='', author=self.u1, is_public=True)
        data = self.client.get('/api/notes/').json()
        self.assertEqual(data['count'], 26)          # 25 条新建 + setUp 里那 1 条公开
        self.assertEqual(len(data['results']), 20)
        self.assertIsNotNone(data['next'])

    def test_private_detail_anonymous_404(self):
        # queryset 过滤设计：不泄露私有笔记的存在性
        response = self.client.get('/api/notes/{}/'.format(self.priv.pk))
        self.assertEqual(response.status_code, 404)

    def test_create_sets_author_to_current_user(self):
        self.client.force_authenticate(user=self.u1)
        response = self.client.post('/api/notes/', {'title': '接口新建', 'content': 'x'}, format='json')
        self.assertEqual(response.status_code, 201)
        note = Note.objects.get(title='接口新建')
        self.assertEqual(note.author, self.u1)
        self.assertEqual(response.json()['author'], 'api_owner')

    def test_anonymous_create_403(self):
        response = self.client.post('/api/notes/', {'title': 'x', 'content': 'y'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_other_cannot_modify_public_note(self):
        # 他人改/删公开笔记：对象级权限拒绝 → 403
        self.client.force_authenticate(user=self.u2)
        response = self.client.patch('/api/notes/{}/'.format(self.pub.pk), {'content': '篡改'}, format='json')
        self.assertEqual(response.status_code, 403)
        response = self.client.delete('/api/notes/{}/'.format(self.pub.pk))
        self.assertEqual(response.status_code, 403)

    def test_other_notebook_rejected_400(self):
        self.client.force_authenticate(user=self.u1)
        response = self.client.post('/api/notes/', {'title': 'x', 'notebook': self.nb2.pk}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_schema_and_docs_200(self):
        response = self.client.get('/api/schema/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('openapi', response.content.decode('utf-8'))
        response = self.client.get('/api/docs/')
        self.assertEqual(response.status_code, 200)


class MarkdownSanitizeTests(TestCase):
    """渲染层：Markdown 里的原始 HTML 必须按白名单清洗（防存储型 XSS）"""

    def test_script_tag_removed(self):
        html, _ = rendering.render('<script>alert(1)</script>\n\n正文')
        self.assertNotIn('<script', html)
        self.assertIn('正文', html)

    def test_event_handler_attribute_removed(self):
        html, _ = rendering.render('<img src=x onerror=alert(1)>')
        self.assertNotIn('onerror', html)
        self.assertNotIn('alert(1)', html.split('>')[0])

    def test_javascript_protocol_removed(self):
        html, _ = rendering.render('[点我](javascript:alert(1))')
        self.assertNotIn('javascript:', html)

    def test_iframe_removed(self):
        html, _ = rendering.render('<iframe src="http://evil.example"></iframe>')
        self.assertNotIn('<iframe', html)

    def test_normal_markdown_still_works(self):
        html, toc = rendering.render('# 一级\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode\n```', with_toc=True)
        self.assertIn('<h1', html)
        self.assertIn('<table>', html)
        self.assertIn('<pre>', html)
        self.assertIn('href="#', toc)

    def test_local_image_kept(self):
        html, _ = rendering.render('![图](/media/uploads/x.png)')
        self.assertIn('src="/media/uploads/x.png"', html)


class NoteFormValidationTests(TestCase):
    """表单校验：标题必填、标签整理、笔记本归属（网页端与 API 规则一致）"""

    def setUp(self):
        self.user = User.objects.create_user('form_user', 'f@f.com', 'test12345')
        self.other = User.objects.create_user('form_other', 'g@g.com', 'test12345')
        self.client.force_login(self.user)

    def test_edit_empty_title_rejected(self):
        note = Note.objects.create(title='原标题', content='原内容', author=self.user)
        response = self.client.post(reverse('note_edit', args=[note.pk]),
                                    {'title': '   ', 'content': '改内容'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['error'], '请输入标题')
        note.refresh_from_db()
        self.assertEqual(note.title, '原标题')     # 标题没被覆盖
        self.assertEqual(note.content, '原内容')    # 整张表单都没落库

    def test_create_in_other_users_notebook_rejected(self):
        theirs = Notebook.objects.create(name='别人的本', user=self.other)
        response = self.client.post(reverse('note_create'),
                                    {'title': '越权建本', 'notebook': theirs.pk})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Note.objects.filter(title='越权建本').exists())

    def test_edit_to_other_users_notebook_rejected(self):
        note = Note.objects.create(title='我的笔记', content='', author=self.user)
        theirs = Notebook.objects.create(name='别人的本', user=self.other)
        response = self.client.post(reverse('note_edit', args=[note.pk]),
                                    {'title': '我的笔记', 'notebook': theirs.pk})
        self.assertEqual(response.status_code, 200)
        note.refresh_from_db()
        self.assertIsNone(note.notebook)

    def test_own_notebook_still_selectable(self):
        mine = Notebook.objects.create(name='我的本', user=self.user)
        self.client.post(reverse('note_create'), {'title': '本里的笔记', 'notebook': mine.pk})
        self.assertEqual(Note.objects.get(title='本里的笔记').notebook, mine)

    def test_tags_trimmed_and_deduped(self):
        self.client.post(reverse('note_create'), {'title': '标签笔记', 'tags': ' 甲 , 甲 ,, 乙 '})
        note = Note.objects.get(title='标签笔记')
        self.assertEqual([t.name for t in note.tags.all()], ['甲', '乙'])


class SemanticSearchDedupTests(TestCase):
    """语义搜索：自己的公开笔记不该命中两次"""

    def setUp(self):
        self.user = User.objects.create_user('dedup_user', 'd@d.com', 'test12345')
        self.pub = Note.objects.create(title='我的公开', content='内容', author=self.user, is_public=True)

    def test_own_public_note_appears_once(self):
        self.client.force_login(self.user)
        with patch('notes.embeddings.embed_text', return_value=[0.1]), \
             patch('notes.embeddings.search_similar', return_value=[self.pub.pk]):
            response = self.client.get(reverse('note_semantic_search'), {'q': '内容'})
        pks = [n.pk for n in response.context['results']]
        self.assertEqual(pks.count(self.pub.pk), 1)

    def test_fallback_keyword_also_deduped(self):
        self.client.force_login(self.user)
        with patch('notes.embeddings.embed_text', side_effect=Exception('向量服务挂了')):
            response = self.client.get(reverse('note_semantic_search'), {'q': '内容'})
        self.assertTrue(response.context['fallback'])
        pks = [n.pk for n in response.context['results']]
        self.assertEqual(pks.count(self.pub.pk), 1)


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class SimilarityThresholdTests(TestCase):
    """相似度门槛：距离过大的结果要丢弃，避免无关查询也返回笔记"""

    def test_far_results_dropped(self):
        with patch('notes.embeddings.get_collection') as gc:
            gc.return_value.query.return_value = {'ids': [['1', '2']], 'distances': [[0.8, 1.35]]}
            self.assertEqual(embeddings.search_similar([0.1]), [1])

    def test_close_results_kept(self):
        with patch('notes.embeddings.get_collection') as gc:
            gc.return_value.query.return_value = {'ids': [['1', '2']], 'distances': [[0.71, 1.05]]}
            self.assertEqual(embeddings.search_similar([0.1]), [1, 2])

    def test_missing_distances_keeps_ids(self):
        # 兼容：万一 Chroma 没返回距离字段，不能把结果全丢掉
        with patch('notes.embeddings.get_collection') as gc:
            gc.return_value.query.return_value = {'ids': [['1']]}
            self.assertEqual(embeddings.search_similar([0.1]), [1])


@override_settings(MEDIA_ROOT=tempfile.mkdtemp())
class ImageCleanupTests(TestCase):
    """图片生命周期：删记录要删文件，删笔记要清掉没人再用的图"""

    def setUp(self):
        self.user = User.objects.create_user('img_user', 'i@i.com', 'test12345')
        self.client.force_login(self.user)

    def _make_image(self):
        buffer = io.BytesIO()
        Image.new('RGB', (10, 10)).save(buffer, 'PNG')
        upload = SimpleUploadedFile('x.png', buffer.getvalue(), content_type='image/png')
        return UploadedImage.objects.create(user=self.user, image=upload)

    def test_deleting_record_removes_file(self):
        img = self._make_image()
        path = img.image.path
        self.assertTrue(os.path.exists(path))
        img.delete()
        self.assertFalse(os.path.exists(path))

    def test_deleting_note_cleans_unreferenced_image(self):
        img = self._make_image()
        note = Note.objects.create(title='带图', content=f'![图]({img.image.url})', author=self.user)
        path = img.image.path
        self.client.post(reverse('note_delete', args=[note.pk]))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(UploadedImage.objects.filter(pk=img.pk).exists())

    def test_deleting_note_keeps_image_used_elsewhere(self):
        img = self._make_image()
        first = Note.objects.create(title='一', content=f'![图]({img.image.url})', author=self.user)
        Note.objects.create(title='二', content=f'![图]({img.image.url})', author=self.user)
        self.client.post(reverse('note_delete', args=[first.pk]))
        self.assertTrue(os.path.exists(img.image.path))
        self.assertTrue(UploadedImage.objects.filter(pk=img.pk).exists())


class ThumbnailAndCompressTests(TestCase):
    """图片压缩与列表缩略图

    背景：终端日志里首页两张配图 1.47MB / 2.35MB，而卡片只显示 130×90 像素。
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._tmp_dir = tempfile.TemporaryDirectory()
        cls._override = override_settings(MEDIA_ROOT=cls._tmp_dir.name)
        cls._override.enable()

    @classmethod
    def tearDownClass(cls):
        cls._override.disable()
        cls._tmp_dir.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.user = User.objects.create_user('img_user', 'i@i.com', 'test12345')
        self.client.force_login(self.user)

    def _upload(self, img, name='photo.png'):
        buffer = io.BytesIO()
        img.save(buffer, format='PNG')
        return self.client.post(reverse('upload_image'), {
            'image': SimpleUploadedFile(name, buffer.getvalue(), content_type='image/png'),
        })

    def test_oversize_upload_is_scaled_down(self):
        # 2400 宽超过上限：保存后应被压到 1600
        response = self._upload(Image.new('RGB', (2400, 1600), 'blue'))
        self.assertEqual(response.status_code, 200)
        obj = UploadedImage.objects.get()
        with Image.open(obj.image.path) as saved:
            self.assertLessEqual(max(saved.size), images.MAX_UPLOAD_EDGE)
            self.assertEqual(saved.size, (1600, 1067))

    def test_upload_creates_thumbnail(self):
        self._upload(Image.new('RGB', (800, 600), 'green'))
        obj = UploadedImage.objects.get()
        thumb = images.thumb_abs_path(obj.image.name)
        self.assertTrue(os.path.exists(thumb), '上传后应该生成 _thumb.jpg')
        with Image.open(thumb) as t:
            self.assertLessEqual(max(t.size), images.THUMB_EDGE)

    def test_small_image_keeps_original_format(self):
        # 10×10 的小图没必要重编码，应该原样存 PNG
        self._upload(Image.new('RGB', (10, 10), 'red'))
        obj = UploadedImage.objects.get()
        self.assertTrue(obj.image.name.endswith('.png'))

    def test_card_uses_thumbnail_but_detail_uses_original(self):
        self._upload(Image.new('RGB', (800, 600), 'green'))
        obj = UploadedImage.objects.get()
        note = Note.objects.create(
            title='带图笔记', content=f'![x]({obj.image.url})', author=self.user, is_public=True,
        )
        self.assertEqual(note.first_image(), obj.image.url)      # 详情页还是原图
        self.assertIn('_thumb.jpg', note.thumb_image())          # 卡片用缩略图
        body = self.client.get(reverse('home')).content.decode('utf-8')
        self.assertIn('_thumb.jpg', body)
        self.assertNotIn(obj.image.url, body)

    def test_thumb_falls_back_when_missing(self):
        # 缩略图不存在（老图 / 外链图）要回退原图，而不是给个 404 地址
        note = Note.objects.create(
            title='图丢了', content='![x](/media/uploads/2020/01/none.png)',
            author=self.user, is_public=True,
        )
        self.assertEqual(note.thumb_image(), '/media/uploads/2020/01/none.png')
        external = Note.objects.create(
            title='外链', content='![x](https://example.com/a.jpg)', author=self.user,
        )
        self.assertEqual(external.thumb_image(), 'https://example.com/a.jpg')

    def test_delete_removes_original_and_thumbnail(self):
        self._upload(Image.new('RGB', (800, 600), 'green'))
        obj = UploadedImage.objects.get()
        original = obj.image.path
        thumb = images.thumb_abs_path(obj.image.name)
        self.assertTrue(os.path.exists(original) and os.path.exists(thumb))
        obj.delete()
        self.assertFalse(os.path.exists(original))
        self.assertFalse(os.path.exists(thumb), '删图片记录时缩略图也要一起删掉')

    def test_make_thumbnails_command_backfills(self):
        self._upload(Image.new('RGB', (800, 600), 'green'))
        obj = UploadedImage.objects.get()
        thumb = images.thumb_abs_path(obj.image.name)
        os.remove(thumb)                     # 模拟功能上线前的老图片
        call_command('make_thumbnails', verbosity=0)
        self.assertTrue(os.path.exists(thumb), '命令应该补齐缺失的缩略图')


class CompressImagesCommandTests(TestCase):
    """compress_images：压缩「功能上线前就传好的」老图

    背景：详情页一张 2844×1280 的 JPEG 1.5MB、一张 1202×1201 的 PNG 2.4MB，
    而那个 PNG 的 alpha 全是 255（完全不透明），用 PNG 存等于白烧体积。
    """

    def setUp(self):
        self.user = User.objects.create_user('old_img_user', 'o@o.com', 'test12345')
        # 每个用例一个干净的 media 目录：命令会遍历整个 uploads，串了会互相干扰
        self._tmp_dir = tempfile.TemporaryDirectory()
        self._override = override_settings(MEDIA_ROOT=self._tmp_dir.name)
        self._override.enable()

    def tearDown(self):
        self._override.disable()
        self._tmp_dir.cleanup()

    def _write_old_file(self, rel, img, fmt, **kw):
        """直接把「老图」写到 media 下，绕开上传时的压缩逻辑"""
        path = os.path.join(str(settings.MEDIA_ROOT), rel.replace('/', os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img.save(path, fmt, **kw)
        return path

    @staticmethod
    def _photo(size):
        """模拟一张「照片感」的老图：小尺寸随机色块放大成大图。

        这样形态才接近真实照片 —— 大片平滑色块 + 缓慢过渡，
        PNG 存要 1 MB 以上，JPEG 只要十分之一；
        纯随机噪声反而不行（两种格式都压不动，测不出转换收益）。
        """
        base = Image.frombytes('RGB', (60, 60), os.urandom(60 * 60 * 3))
        return base.resize(size, Image.BICUBIC)

    def test_oversize_jpeg_is_scaled_and_shrunk(self):
        path = self._write_old_file(
            'uploads/2020/01/old.jpg', self._photo((2000, 1000)), 'JPEG', quality=100,
        )
        before = os.path.getsize(path)

        call_command('compress_images', verbosity=0)

        self.assertLess(os.path.getsize(path), before, '老图应该被压小')
        with Image.open(path) as saved:
            self.assertLessEqual(max(saved.size), images.MAX_UPLOAD_EDGE)

    def test_opaque_png_becomes_jpeg_and_references_are_rewritten(self):
        path = self._write_old_file('uploads/2020/01/furina.png', self._photo((900, 900)), 'PNG')
        old_url = settings.MEDIA_URL + 'uploads/2020/01/furina.png'
        record = UploadedImage.objects.create(
            image='uploads/2020/01/furina.png', user=self.user,
        )
        note = Note.objects.create(
            title='芙宁娜', content=f'看图\n\n![图]({old_url})', author=self.user, is_public=True,
        )

        call_command('compress_images', verbosity=0)

        new_path = os.path.join(str(settings.MEDIA_ROOT), 'uploads', '2020', '01', 'furina.jpg')
        self.assertTrue(os.path.exists(new_path), '不透明 PNG 应该转成 JPEG')
        self.assertFalse(os.path.exists(path), '转格式成功后旧 png 应当删掉')

        record.refresh_from_db()
        self.assertTrue(record.image.name.endswith('.jpg'), '数据库记录要指向新文件名')
        note.refresh_from_db()
        self.assertIn('/media/uploads/2020/01/furina.jpg', note.content)
        self.assertNotIn('.png', note.content)
        self.assertTrue(
            os.path.exists(images.thumb_abs_path('uploads/2020/01/furina.jpg')),
            '转格式后要重新生成缩略图',
        )

    def test_history_snapshot_also_rewritten(self):
        """旧版本快照里的地址也要改，否则「恢复这个版本」图片会 404"""
        self._write_old_file('uploads/2020/01/his.png', self._photo((900, 900)), 'PNG')
        old_url = settings.MEDIA_URL + 'uploads/2020/01/his.png'
        with reversion.create_revision():
            note = Note.objects.create(
                title='历史', content=f'![图]({old_url})', author=self.user, is_public=True,
            )
        version = Version.objects.get_for_object(note).first()
        self.assertIn('.png', version.field_dict['content'])

        call_command('compress_images', verbosity=0)

        # field_dict 是 cached_property，得重新取一次对象才能看到新数据
        version = Version.objects.get(pk=version.pk)
        self.assertIn('.jpg', version.serialized_data)
        self.assertNotIn('.png', version.field_dict['content'])

    def test_transparent_png_stays_png(self):
        # 有真实透明像素就不能转 JPEG，否则透明区会变成白块
        img = Image.new('RGBA', (900, 900), (0, 0, 0, 0))
        img.paste(self._photo((450, 450)).convert('RGBA'), (100, 100))
        path = self._write_old_file('uploads/2020/01/alpha.png', img, 'PNG')

        call_command('compress_images', verbosity=0)

        self.assertTrue(os.path.exists(path), '带透明通道的 PNG 应保留原格式')
        self.assertFalse(
            os.path.exists(os.path.join(str(settings.MEDIA_ROOT), 'uploads/2020/01/alpha.jpg'))
        )

    def test_keep_png_flag(self):
        self._write_old_file('uploads/2020/01/opaque.png', self._photo((900, 900)), 'PNG')

        call_command('compress_images', '--keep-png', verbosity=0)

        self.assertTrue(
            os.path.exists(os.path.join(str(settings.MEDIA_ROOT), 'uploads/2020/01/opaque.png'))
        )

    def test_dry_run_touches_nothing(self):
        path = self._write_old_file(
            'uploads/2020/01/old.jpg', self._photo((2000, 1000)), 'JPEG', quality=100,
        )
        before = os.path.getsize(path)

        call_command('compress_images', '--dry-run', verbosity=0)

        self.assertEqual(os.path.getsize(path), before, '--dry-run 不该改动文件')

    def test_small_image_is_left_alone(self):
        path = self._write_old_file(
            'uploads/2020/01/tiny.jpg', Image.new('RGB', (400, 300), 'red'), 'JPEG',
        )
        before = os.path.getsize(path)

        call_command('compress_images', verbosity=0)

        self.assertEqual(os.path.getsize(path), before, '又小又不超尺寸的图不该重编码')


class FaviconTests(SimpleTestCase):
    """网站图标：文件存在，根路径 /favicon.ico 不再 404"""

    def test_icon_files_exist(self):
        for name in ('img/favicon.ico', 'img/favicon-32.png', 'img/apple-touch-icon.png'):
            self.assertIsNotNone(finders.find(name), f'缺少静态文件 {name}')

    def test_root_favicon_redirects_to_static(self):
        response = self.client.get('/favicon.ico')
        self.assertEqual(response.status_code, 302)
        self.assertIn('favicon.ico', response.url)
