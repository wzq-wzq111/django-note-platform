"""笔记应用的 REST API（DRF + drf-spectacular）"""
from django.db.models import Q
from rest_framework import permissions, serializers, viewsets

from .models import Note, Tag


class IsAuthorOrReadOnly(permissions.BasePermission):
    """读：所有人可见（ViewSet 的 queryset 已按公开/私有过滤）；
    写：仅作者本人。"""

    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return obj.author == request.user


class TagNameField(serializers.SlugRelatedField):
    """标签字段：客户端传标签名数组；写不存在的标签时自动创建（与网页端行为一致）"""

    def __init__(self, **kwargs):
        super().__init__(slug_field='name', queryset=Tag.objects.all(), **kwargs)

    def to_internal_value(self, data):
        tag, _ = Tag.objects.get_or_create(name=str(data), defaults={'slug': str(data)})
        return tag


class NoteSerializer(serializers.ModelSerializer):
    """笔记序列化：author 只读（防伪造作者）、标签用名称数组、笔记本只能选自己的"""
    author = serializers.CharField(source='author.username', read_only=True)
    tags = TagNameField(many=True, required=False)

    class Meta:
        model = Note
        fields = ['id', 'title', 'content', 'notebook', 'author', 'tags',
                  'is_public', 'is_pinned', 'view_count', 'created_at', 'updated_at']
        read_only_fields = ['view_count', 'created_at', 'updated_at']

    def validate_notebook(self, notebook):
        """笔记本只能选自己的"""
        user = self.context['request'].user
        if notebook is not None and notebook.user != user:
            raise serializers.ValidationError('只能选择自己的笔记本')
        return notebook


class NoteViewSet(viewsets.ModelViewSet):
    """笔记接口：GET /api/notes/、POST /api/notes/、GET/PUT/DELETE /api/notes/{id}/"""
    serializer_class = NoteSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly, IsAuthorOrReadOnly]

    def get_queryset(self):
        # 登录用户：公开笔记 + 自己的私有笔记；匿名：只有公开笔记
        user = self.request.user
        if user.is_authenticated:
            return Note.objects.filter(Q(is_public=True) | Q(author=user)).order_by('-updated_at')
        return Note.objects.filter(is_public=True).order_by('-updated_at')

    def perform_create(self, serializer):
        # 作者固定为当前登录用户，客户端无法伪造
        # 注意：经 serializer.save() 会触发 reversion 自动留档（合理行为）
        serializer.save(author=self.request.user)
