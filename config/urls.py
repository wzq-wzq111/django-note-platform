from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
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
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
