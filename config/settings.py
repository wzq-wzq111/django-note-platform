"""
Django settings — 笔记平台
"""
import os
import sys
import tempfile
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file():
    """从项目根目录的 .env 文件加载本地配置（如 API 密钥，不入 git 仓库）"""
    env_path = BASE_DIR / '.env'
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, _, value = line.partition('=')
            os.environ.setdefault(key.strip(), value.strip())


_load_env_file()

# 运行模式：本地默认 DEBUG，部署时设 DJANGO_DEBUG=0
DEBUG = os.environ.get('DJANGO_DEBUG', '1').strip().lower() not in {'0', 'false', 'no', 'off'}

# 密钥从环境变量读取，防止泄露到公开仓库；
# 开发环境给一个带明确标记的默认值，生产环境缺失就直接启动失败（fail fast，而不是悄悄用弱密钥上线）
SECRET_KEY = os.environ.get('SECRET_KEY', '').strip()
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = 'django-insecure-dev-key-本地开发专用'
    else:
        raise ImproperlyConfigured(
            '生产环境必须设置环境变量 SECRET_KEY（可用 python -c "import secrets;print(secrets.token_urlsafe(50))" 生成）'
        )

# 允许访问的域名：生产必须显式配置，未配置时为空列表（全部拒绝），不会默认放行任何人
_allowed = os.environ.get('DJANGO_ALLOWED_HOSTS', '').strip()
if _allowed:
    ALLOWED_HOSTS = [h.strip() for h in _allowed.split(',') if h.strip()]
elif DEBUG:
    ALLOWED_HOSTS = ['*']      # 本地开发：手机、局域网设备也能访问
else:
    ALLOWED_HOSTS = []

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'crispy_forms',
    'crispy_bootstrap5',
    'reversion',      # 历史版本：每次保存自动留档
    'rest_framework',       # REST API
    'drf_spectacular',      # Swagger 接口文档
    'notes',
    'accounts',
]

CRISPY_ALLOWED_TEMPLATE_PACKS = 'bootstrap5'
CRISPY_TEMPLATE_PACK = 'bootstrap5'

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
        ],
    },
}]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# 密码强度校验：原来为空列表 = 任何密码（包括 "123"）都能注册。
# 只在「设置/修改密码」时生效，不影响已有账号登录。
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'static']

MEDIA_URL = '/media/'  # 必须有前导斜杠，否则图片地址是相对路径，在 /note/5/ 等页面会 404
MEDIA_ROOT = BASE_DIR / 'media'

# 语义搜索的向量库目录（chroma_db/，不入 git）。
# 跑测试时必须换到独立临时目录：测试会批量创建/修改笔记并触发写索引，
# 如果指向生产目录，会把真实笔记的向量和 is_public 元数据覆盖成测试数据
# （曾导致匿名用户搜不到公开笔记），所以这里按运行方式分流。
_TESTING = 'test' in sys.argv or 'pytest' in sys.argv or os.environ.get('DJANGO_TESTING') == '1'
CHROMA_DIR = (
    Path(tempfile.mkdtemp(prefix='chroma_test_')) if _TESTING
    else Path(os.environ.get('CHROMA_DIR') or BASE_DIR / 'chroma_db')
)

# 测试环境不调用外部向量化接口：单元测试不该依赖网络和 API 配额
EMBEDDINGS_ENABLED = not _TESTING

LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# 本地缓存：PDF 导出结果、登录失败计数都用它（单进程够用；多进程部署可换 Redis）
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'note-platform',
        'TIMEOUT': 300,
    }
}

# ===== 生产环境安全项：只在 DEBUG=False 时开启 =====
# 本地是 http，开启 SECURE_SSL_REDIRECT 会把自己锁在重定向里，所以必须区分环境。
if not DEBUG:
    SECURE_SSL_REDIRECT = os.environ.get('DJANGO_SECURE_SSL_REDIRECT', '1') == '1'
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'same-origin'

# 日志：语义搜索/向量库这类「被吞掉的异常」要能在控制台看到，否则功能坏了没人知道
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'simple': {'format': '[{levelname}] {asctime} {name}: {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'notes': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
    },
}

# DRF 配置：Session + Basic 两种认证（浏览器和 curl 都能用）；未登录只读
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.SessionAuthentication',
        'rest_framework.authentication.BasicAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticatedOrReadOnly',
    ],
    'DEFAULT_SCHEMA_CLASS': 'drf_spectacular.openapi.AutoSchema',
    # 列表接口分页：原来一次返回全部笔记，数据一多响应体会越来越大
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

# ===== AI 大模型配置（密钥从环境变量读取，不进代码仓库）=====
# LLM_API_KEY：在系统环境变量里设置（或运行前 set LLM_API_KEY=xxx）
LLM_API_KEY = os.environ.get('LLM_API_KEY', '')
LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.deepseek.com')
LLM_MODEL = os.environ.get('LLM_MODEL', 'deepseek-chat')

# Swagger 文档标题信息
SPECTACULAR_SETTINGS = {
    'TITLE': '笔记平台 API',
    'DESCRIPTION': '个人知识库与笔记管理平台的 REST API',
    'VERSION': '1.0.0',
    'SERVE_INCLUDE_SCHEMA': False,
}
