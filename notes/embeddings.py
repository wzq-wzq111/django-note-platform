"""
语义搜索 — 笔记向量化与相似检索

嵌入模型：智谱 GLM embedding-3（OpenAI 兼容接口，密钥 EMBED_API_KEY 从 .env 读）
向量库：Chroma（本地持久化，chroma_db/ 目录，不入 git 仓库）

约定：向量相关操作全部吞掉异常，失败时语义搜索回退关键词搜索，
绝不因为向量库的问题影响笔记保存等核心功能。
"""
import logging
import os

from django.conf import settings
from openai import OpenAI

logger = logging.getLogger(__name__)

EMBED_MODEL = 'embedding-3'
EMBED_BASE_URL = os.environ.get('EMBED_BASE_URL', 'https://open.bigmodel.cn/api/paas/v4/')
EMBED_API_KEY = os.environ.get('EMBED_API_KEY', '')

# 向量库目录不在这里定死：settings.CHROMA_DIR 会在跑测试时指向临时目录，
# 这里每次调用都重新读，避免模块级常量被 import 时的值锁住。
# 相似度门槛（Chroma 默认的平方 L2 距离，越小越像）
# 实测智谱 embedding-3：相关查询 0.71~1.07，无关查询 1.31~1.41，中间有断层。
# 不设门槛的话「今天天气怎么样」也会返回笔记——向量检索永远会给出 top_k 条。
# 注意：如果以后把集合改成 cosine 空间，这个阈值要跟着重新标定。
MAX_DISTANCE = 1.2

_client = None
_client_path = None


class EmbedError(Exception):
    """向量化失败（未配置密钥 / 网络错误等）"""


def get_collection():
    """获取（不存在则创建）Chroma 集合，同一路径复用客户端。

    客户端按目录缓存：settings.CHROMA_DIR 变了（测试切临时目录）就重建，
    否则会一直连着上一次打开的库，测试数据照样写进生产目录。
    """
    global _client, _client_path
    path = str(settings.CHROMA_DIR)
    if _client is None or _client_path != path:
        # 延迟导入：chromadb 的 Rust 绑定依赖 VC++ 运行库，
        # 缺库时导入即崩。放在函数里，向量功能坏掉也不影响整个站点启动。
        import chromadb
        _client = chromadb.PersistentClient(path=path)
        _client_path = path
    return _client.get_or_create_collection('notes')


def embed_text(text: str) -> list:
    """文本 → 向量。失败抛 EmbedError，调用方决定回退策略"""
    if not getattr(settings, 'EMBEDDINGS_ENABLED', True):
        # 跑测试时不打外部接口：单元测试不该依赖网络和 API 配额
        raise EmbedError('测试环境不调用外部向量化接口')
    if not EMBED_API_KEY:
        raise EmbedError('未配置智谱密钥 EMBED_API_KEY')
    client = OpenAI(api_key=EMBED_API_KEY, base_url=EMBED_BASE_URL)
    try:
        resp = client.embeddings.create(model=EMBED_MODEL, input=text[:2000])
    except Exception as exc:
        raise EmbedError('向量化失败') from exc
    return resp.data[0].embedding


def index_note(note) -> bool:
    """把一篇笔记写入向量库（保存后调用；失败只记日志，不影响保存）

    返回是否成功：管理命令要据此判断重建有没有真的写进去。
    保存笔记这条路径不看返回值——向量库坏了也不能让用户存不了笔记。
    """
    if not getattr(settings, 'EMBEDDINGS_ENABLED', True):
        # 测试环境跳过写入（目录是临时库，写了也没意义），且不刷日志
        return False
    try:
        vec = embed_text(f'{note.title}\n{note.content}')
        get_collection().upsert(
            ids=[str(note.pk)],
            embeddings=[vec],
            metadatas=[{'author_id': note.author_id, 'is_public': note.is_public}],
        )
        return True
    except Exception as exc:
        # 原来这里是 pass：向量库坏了在日志里一点痕迹都没有，看起来像功能正常。
        # 只记原因不记堆栈：环境问题（缺 VC++ 运行库等）每次保存都会触发，堆栈会刷屏。
        logger.warning('笔记 %s 写入向量库失败（不影响笔记保存）：%s', note.pk, exc)
        return False


def remove_note(note_id):
    """从向量库删除一篇笔记（笔记删除后调用；失败只记日志）"""
    try:
        get_collection().delete(ids=[str(note_id)])
    except Exception as exc:
        logger.warning('笔记 %s 从向量库删除失败：%s', note_id, exc)


def search_similar(query_vec: list, top_k: int = 10, max_distance: float = MAX_DISTANCE) -> list:
    """按向量检索最相似笔记的 id 列表（相似度从高到低）。

    超过 max_distance 的结果直接丢弃：向量检索不会"查无结果"，
    不设门槛时任何无关查询都会返回一堆笔记，看起来像搜索坏了。
    """
    raw = get_collection().query(query_embeddings=[query_vec], n_results=top_k)
    ids = raw.get('ids', [[]])[0]
    distances = raw.get('distances', [[]])[0]
    if not distances:
        # 理论上不会发生（Chroma 默认就返回距离），保底不因缺字段丢结果
        return [int(i) for i in ids]
    return [int(i) for i, d in zip(ids, distances) if d <= max_distance]
