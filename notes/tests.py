"""
笔记应用 — 单元测试

覆盖：模型、目录生成、权限、笔记增删改、图片上传、历史版本、
PDF 导出、个人中心、REST API。

运行：python manage.py test notes
"""
import io
import re
import tempfile
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image
from reversion.models import Version
from rest_framework.test import APITestCase

from .models import Note, Notebook, Tag, UploadedImage, note_image_path
from . import ai


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
        # 浏览详情页 3 次：版本数不变，浏览量 +3
        version_count = len(Version.objects.get_for_object(self.note))
        for _ in range(3):
            self.client.get(reverse('note_detail', args=[self.note.pk]))
        self.assertEqual(len(Version.objects.get_for_object(self.note)), version_count)
        self.note.refresh_from_db()
        self.assertEqual(self.note.view_count, 3)


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


class ProfileTests(TestCase):
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
        titles = [n['title'] for n in response.json()]
        self.assertIn('接口公开', titles)
        self.assertNotIn('接口私有', titles)

    def test_logged_in_sees_own_private(self):
        self.client.force_authenticate(user=self.u1)
        titles = [n['title'] for n in self.client.get('/api/notes/').json()]
        self.assertIn('接口私有', titles)

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
