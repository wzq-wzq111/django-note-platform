"""用户系统 — 注册、登录、登出"""

from django.contrib import messages
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.forms import UserCreationForm
from django.core.cache import cache
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST

# 同一个「IP + 用户名」连续失败 5 次，锁 5 分钟。
# 原来登录接口没有任何限制，脚本可以无限次尝试密码。
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCK_SECONDS = 300


def _login_fail_key(request, username):
    ip = request.META.get('REMOTE_ADDR', '')
    return f'login_fail:{ip}:{username.strip().lower()[:50]}'


def register(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)
            messages.success(request, f'欢迎，{user.username}！')
            return redirect('home')
    else:
        form = UserCreationForm()
    return render(request, 'accounts/register.html', {'form': form})


def user_login(request):
    if request.method == 'POST':
        username = request.POST.get('username', '')
        password = request.POST.get('password', '')
        key = _login_fail_key(request, username)

        if cache.get(key, 0) >= LOGIN_MAX_ATTEMPTS:
            messages.error(request, f'密码错误次数过多，请 {LOGIN_LOCK_SECONDS // 60} 分钟后再试')
            return render(request, 'accounts/login.html', status=429)

        user = authenticate(request, username=username, password=password)
        if user:
            cache.delete(key)      # 登录成功清零计数
            login(request, user)
            return redirect('my_notes')

        # add 负责建键并带上过期时间，incr 保证并发下也能正确累加
        cache.add(key, 0, LOGIN_LOCK_SECONDS)
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, LOGIN_LOCK_SECONDS)
        messages.error(request, '用户名或密码错误')
    return render(request, 'accounts/login.html')


@require_POST
def user_logout(request):
    """登出只接受 POST：用 GET 链接登出会被第三方页面诱导触发（CSRF 登出）。"""
    logout(request)
    return redirect('home')
