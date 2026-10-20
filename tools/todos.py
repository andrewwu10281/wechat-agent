"""待办 / 提醒工具：按用户读写 todos.json。

特点：
  - 按用户隔离：每个人只看得到自己的待办；
  - due 可选，存 ISO 时间（如 2026-10-24T15:00）；
  - 到点且没提醒过的，由 bot.py 主循环主动发微信提醒；
  - 纯 JSON 文件，方便查看和备份。
"""
import json
import os
from datetime import datetime

from tools.context import get_current_user

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TODO_FILE = os.path.join(BASE, "todos.json")


def _load() -> dict:
    """读 todos.json；文件不存在或损坏就返回一个空结构。"""
    if not os.path.exists(TODO_FILE):
        return {"todos": [], "next_id": 1}
    try:
        with open(TODO_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("格式不对")
        data.setdefault("todos", [])
        data.setdefault("next_id", 1)
        return data
    except Exception:
        return {"todos": [], "next_id": 1}


def _save(data: dict) -> None:
    with open(TODO_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _mine(data: dict) -> list:
    """只取当前用户的待办。"""
    uid = get_current_user()
    return [t for t in data["todos"] if t.get("user_id") == uid]


def _parse_due(due: str):
    """解析 ISO 时间；带时区的去掉时区，方便和本地时间比较。"""
    dt = datetime.fromisoformat(due)
    return dt.replace(tzinfo=None)


def add_todo(text: str, due: str = "") -> str:
    """添加一条待办/提醒。due 用 ISO 格式（如 2026-10-24T15:00），可留空。"""
    text = (text or "").strip()
    if not text:
        return "错误：待办内容为空。"
    due = (due or "").strip()
    if due:
        try:
            _parse_due(due)
        except ValueError:
            return "错误：due 时间格式不对，请用 2026-10-24T15:00 这样的 ISO 格式。"
    data = _load()
    tid = data["next_id"]
    data["next_id"] = tid + 1
    data["todos"].append({
        "id": tid,
        "user_id": get_current_user(),
        "text": text,
        "due": due,
        "done": False,
        "reminded": False,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save(data)
    tail = f"，提醒时间 {due}" if due else ""
    return f"已记下待办 #{tid}：{text}{tail}"


def list_todos() -> str:
    """列出当前用户未完成的待办（编号从 1 开始）。"""
    data = _load()
    items = [t for t in _mine(data) if not t.get("done")]
    if not items:
        return "当前没有未完成的待办。"
    lines = []
    for i, t in enumerate(items, 1):
        due = f"（{t['due']}）" if t.get("due") else ""
        lines.append(f"{i}. {t['text']}{due}")
    return "\n".join(lines)


def complete_todo(index: int) -> str:
    """把第 N 个未完成待办标记为完成。"""
    data = _load()
    items = [t for t in _mine(data) if not t.get("done")]
    try:
        target = items[int(index) - 1]
    except (ValueError, IndexError):
        return "错误：没有这个编号的待办。"
    target["done"] = True
    _save(data)
    return f"已完成：{target['text']}"


def delete_todo(index: int) -> str:
    """删除第 N 个未完成待办。"""
    data = _load()
    items = [t for t in _mine(data) if not t.get("done")]
    try:
        target = items[int(index) - 1]
    except (ValueError, IndexError):
        return "错误：没有这个编号的待办。"
    data["todos"] = [t for t in data["todos"] if t.get("id") != target.get("id")]
    _save(data)
    return f"已删除：{target['text']}"


def due_reminders(now=None) -> list:
    """返回"到点且没提醒过"的待办（供 bot 主循环调用）。"""
    now = now or datetime.now()
    data = _load()
    out = []
    for t in data["todos"]:
        if t.get("done") or t.get("reminded") or not t.get("due"):
            continue
        try:
            due = _parse_due(t["due"])
        except ValueError:
            continue
        if due <= now:
            out.append(t)
    return out


def mark_reminded(todo_id: int) -> None:
    """标记某条待办已经提醒过，避免重复发。"""
    data = _load()
    for t in data["todos"]:
        if t.get("id") == todo_id:
            t["reminded"] = True
    _save(data)
