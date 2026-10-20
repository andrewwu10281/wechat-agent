"""当前用户上下文：让工具知道该读写谁的数据。

bot.py 在处理每条消息前调用 set_current_user(from_user)；
命令行模式下默认是 "cli"。
本项目是单线程顺序处理消息的，所以用一个模块级变量就够了。
"""
_current_user = "cli"


def set_current_user(user_id: str) -> None:
    global _current_user
    _current_user = (user_id or "").strip() or "cli"


def get_current_user() -> str:
    return _current_user
