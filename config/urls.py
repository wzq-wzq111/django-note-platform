from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.templatetags.static import static as static_url
from django.views.generic import RedirectView
from rest_framework.routers import DefaultRouter
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from notes.api import NoteViewSet

# API 路由（放在 config/urls 而不是 notes/urls，避免与 '' 前缀纠缠）
router = DefaultRouter()
router.register('api/notes', NoteViewSet, basename='note')

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('', include('notes.urls')),
    path('', include(router.urls)),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),   # OpenAPI 描述文件
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='docs'),  # Swagger 界面
    # 浏览器、爬虫、书签会直接请求根路径的 /favicon.ico（不读 <link rel="icon">），
    # 不处理就是一条 404 刷在日志里。永久重定向也别用 permanent=True，
    # 否则换图标后用户浏览器会一直用缓存里的旧图。
    path('favicon.ico', RedirectView.as_view(url=static_url('img/favicon.ico'))),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
