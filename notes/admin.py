from django.contrib import admin
from .models import Notebook, Tag, Note, UploadedImage

@admin.register(Notebook)
class NotebookAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'created_at']

@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ['name', 'slug']
    prepopulated_fields = {'slug': ('name',)}

@admin.register(Note)
class NoteAdmin(admin.ModelAdmin):
    list_display = ['title', 'author', 'is_public', 'is_pinned', 'view_count', 'updated_at']
    list_filter = ['is_public', 'is_pinned', 'tags']
    search_fields = ['title', 'content']

@admin.register(UploadedImage)
class UploadedImageAdmin(admin.ModelAdmin):
    list_display = ['image', 'user', 'uploaded_at']
    list_filter = ['user']
