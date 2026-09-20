"""
MemoryStore — 跨 session 的持久化记忆

按 topic 组织，支持 append / update / query。
底层是 JSON 文件，后续可换成向量数据库。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from .io import atomic_json, file_lock, safe_path
import hashlib


class MemoryStore:
    def __init__(self, memory_dir: str | Path):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 基础读写
    # ------------------------------------------------------------------

    def _topic_file(self, topic: str) -> Path:
        import re
        safe = topic if re.fullmatch(r"[A-Za-z0-9_-]+", topic) else hashlib.sha256(topic.encode()).hexdigest()
        try:
            return safe_path(self.memory_dir, f"{safe}.json")
        except ValueError:
            return safe_path(self.memory_dir, hashlib.sha256(topic.encode()).hexdigest() + ".json")

    def _load_topic(self, topic: str) -> dict:
        f = self._topic_file(topic)
        if not f.exists():
            return {"topic": topic, "entries": []}
        with open(f, "r", encoding="utf-8") as fp:
            return json.load(fp)

    def _save_topic(self, topic: str, data: dict) -> None:
        atomic_json(self._topic_file(topic), data)

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def append(self, topic: str, content: Any, tags: Optional[list[str]] = None) -> None:
        """向某个 topic 追加一条记忆。"""
        with file_lock(self._topic_file(topic).with_suffix(".lock"), timeout=5):
            data = self._load_topic(topic)
            data["entries"].append({
                "content": content,
                "tags": tags or [],
                "created_at": datetime.now().isoformat(),
            })
            self._save_topic(topic, data)

    def get_all(self, topic: str) -> list[dict]:
        """获取某个 topic 的所有记忆条目。"""
        return self._load_topic(topic)["entries"]

    def get_latest(self, topic: str, n: int = 5) -> list[dict]:
        """获取最近 n 条记忆。"""
        return self.get_all(topic)[-n:] if n > 0 else []

    def search_by_tag(self, topic: str, tag: str) -> list[dict]:
        return [e for e in self.get_all(topic) if tag in e.get("tags", [])]

    def set_kv(self, topic: str, key: str, value: Any) -> None:
        """在 topic 下存储键值对（覆盖写）。"""
        with file_lock(self._topic_file(topic).with_suffix(".lock"), timeout=5):
            data = self._load_topic(topic)
            if "kv" not in data:
                data["kv"] = {}
            data["kv"][key] = {"value": value, "updated_at": datetime.now().isoformat()}
            self._save_topic(topic, data)

    def get_kv(self, topic: str, key: str, default: Any = None) -> Any:
        data = self._load_topic(topic)
        entry = data.get("kv", {}).get(key)
        return entry["value"] if entry else default

    def list_topics(self) -> list[str]:
        return [f.stem for f in self.memory_dir.glob("*.json")]

    def summary(self) -> dict:
        result = {}
        for topic in self.list_topics():
            data = self._load_topic(topic)
            result[topic] = {
                "entries": len(data.get("entries", [])),
                "kv_keys": list(data.get("kv", {}).keys()),
            }
        return result
