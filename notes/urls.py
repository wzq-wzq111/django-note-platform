from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('my/', views.my_notes, name='my_notes'),
    path('profile/', views.profile, name='profile'),
    path('upload/', views.upload_image, name='upload_image'),
    path('note/<int:pk>/', views.note_detail, name='note_detail'),
    path('note/new/', views.note_create, name='note_create'),
    path('note/<int:pk>/edit/', views.note_edit, name='note_edit'),
    path('note/<int:pk>/delete/', views.note_delete, name='note_delete'),
    path('note/<int:pk>/pdf/', views.note_pdf, name='note_pdf'),
    path('note/<int:pk>/ai-summarize/', views.note_ai_summarize, name='note_ai_summarize'),
    path('ai-expand/', views.note_ai_expand, name='note_ai_expand'),
    path('ai-fix/', views.note_ai_fix, name='note_ai_fix'),
    path('ai-tags/', views.note_ai_tags, name='note_ai_tags'),
    path('search-semantic/', views.note_semantic_search, name='note_semantic_search'),
    path('stats-ask/', views.note_stats_ask, name='note_stats_ask'),
    path('note/<int:pk>/history/', views.note_history, name='note_history'),
    path('note/<int:pk>/history/compare/', views.note_history_compare, name='note_history_compare'),
    path('notebook/new/', views.notebook_create, name='notebook_create'),
]
