"""MongoDB 连接复用：db_engine.params / db_engine.screener 共用。

pymongo 客户端线程安全、自带连接池与后台监控线程，同一 URI 全进程只建一个；
每次都新建 client 会泄漏连接池。
"""

from __future__ import annotations

import threading
from typing import Dict

from pymongo import MongoClient
from pymongo.database import Database

MONGO_SCHEMES = ("mongodb://", "mongodb+srv://")

MONGO_OPTIONS = {
    'serverSelectionTimeoutMS': 30000,  # 服务器选择超时
    'connectTimeoutMS': 20000,          # 连接超时
    'socketTimeoutMS': 30000,           # Socket超时
    'retryWrites': True,                # 启用重试写入
    'retryReads': True,                 # 启用重试读取
    # 不设 readPreference，读主节点（驱动默认）：写后即读一致，且短命进程不会因随机
    # 选中从节点而额外建立连接
}

_clients: Dict[str, MongoClient] = {}
_lock = threading.Lock()


def is_mongo_uri(uri: str) -> bool:
    return uri.startswith(MONGO_SCHEMES)


def mongo_client(uri: str) -> MongoClient:
    with _lock:
        client = _clients.get(uri)
        if client is None:
            client = MongoClient(uri, **MONGO_OPTIONS)
            _clients[uri] = client
        return client


def mongo_database(uri: str) -> Database:
    """库名取 URI 路径段（mongodb+srv://u:p@host/<db>?...）；未写库名抛 ConfigurationError。"""
    return mongo_client(uri).get_default_database()
