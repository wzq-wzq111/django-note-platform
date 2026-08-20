"""
账号应用 — 单元测试

覆盖：注册（成功自动登录、密码不一致）、登录（成功/失败）、登出。

运行：python manage.py test accounts
"""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class RegisterTests(TestCase):
    """注册"""

    def test_register_page_200(self):
        response = self.client.get(reverse('register'))
        self.assertEqual(response.status_code, 200)

    def test_register_success_logs_in_and_redirects_home(self):
        response = self.client.post(reverse('register'), {
            'username': '新用户', 'password1': 'abc123456', 'password2': 'abc123456',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('home'))
        self.assertTrue(User.objects.filter(username='新用户').exists())
        # 注册成功后自动登录
        self.assertTrue(response.wsgi_request.user.is_authenticated)
        self.assertEqual(response.wsgi_request.user.username, '新用户')

    def test_register_password_mismatch_shows_form_error(self):
        response = self.client.post(reverse('register'), {
            'username': '新用户', 'password1': 'abc123456', 'password2': '不一样',
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('password2', response.context['form'].errors)
        self.assertFalse(User.objects.filter(username='新用户').exists())


class LoginTests(TestCase):
    """登录 / 登出"""

    def setUp(self):
        self.user = User.objects.create_user('tester', 't@t.com', 'test12345')

    def test_login_page_200(self):
        response = self.client.get(reverse('login'))
        self.assertEqual(response.status_code, 200)

    def test_login_success_redirects_my_notes(self):
        response = self.client.post(reverse('login'), {
            'username': 'tester', 'password': 'test12345',
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('my_notes'))

    def test_login_failure_shows_message(self):
        response = self.client.post(reverse('login'), {
            'username': 'tester', 'password': '错误密码',
        })
        self.assertEqual(response.status_code, 200)
        messages = [str(m) for m in response.context['messages']]
        self.assertIn('用户名或密码错误', messages)

    def test_logout_redirects_home_and_clears_session(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse('logout'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('home'))
        # 会话已清空
        self.assertNotIn('_auth_user_id', self.client.session)
