"""长期记忆：按用户保存「关于你的事实」，跨对话保留。

和对话历史的区别：
  - 对话历史只保留最近 20 条，聊久了会忘；
  - 长期记忆是你明确让它「记住」的事，会一直留着，直到你让它忘掉。

示例：
  记住：我在准备考研，目标院校是浙大
  你还记得我在准备什么吗？
  忘掉考研
"""
import json
import os
from datetime import datetime

from tools.context import get_current_user

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_FILE = os.path.join(BASE, "memory.json")


def _load() -> dict:
    """读 memory.json；不存在或损坏就返回空结构。"""
    if not os.path.exists(MEMORY_FILE):
        return {"memories": []}
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("格式不对")
        data.setdefault("memories", [])
        return data
    except Exception:
        return {"memories": []}


def _save(data: dict) -> None:
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _mine(data: dict) -> list:
    """只取当前用户的记忆。"""
    uid = get_current_user()
    return [m for m in data["memories"] if m.get("user_id") == uid]


def remember_fact(text: str) -> str:
    """记住一条关于用户的事实。"""
    text = (text or "").strip()
    if not text:
        return "错误：要记住的内容为空。"
    data = _load()
    #简单去重：同样的话不重复存
    for m in _mine(data):
        if m.get("text") == text:
            return f"这条我已经记住了：{text}"
    data["memories"].append({
        "user_id": get_current_user(),
        "text": text,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save(data)
    return f"记住了：{text}"


def recall_facts(query: str = "") -> str:
    """回忆长期记忆；query 为空就返回全部，否则按关键词过滤。"""
    data = _load()
    items = _mine(data)
    q = (query or "").strip()
    if q:
        items = [m for m in items if q in m.get("text", "")]
    if not items:
        return "我这边还没有相关记忆。" if q else "我还没有记住关于你的事情。"
    return "\n".join(f"- {m['text']}" for m in items)


def forget_fact(query: str) -> str:
    """忘掉包含关键词的记忆。"""
    q = (query or "").strip()
    if not q:
        return "错误：请告诉我要忘掉什么。"
    data = _load()
    uid = get_current_user()
    before = len(data["memories"])
    data["memories"] = [
        m for m in data["memories"]
        if not (m.get("user_id") == uid and q in m.get("text", ""))
    ]
    removed = before - len(data["memories"])
    if removed == 0:
        return f"没找到包含「{q}」的记忆。"
    _save(data)
    return f"已忘掉 {removed} 条相关记忆。"
