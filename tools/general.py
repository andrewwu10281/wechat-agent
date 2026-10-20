"""通用工具集（harness 底座）

设计思路：不给每个需求写一个专用工具，而是给模型一套「基础能力」——
运行命令、读写文件、检索、联网——由模型自己组合来完成各种任务。
这和 Codex / Claude Code 这类真实 harness 的做法一致。

安全说明：
  - 这套工具能读写文件、能执行命令，威力大、风险也大。
  - 文件操作被限制在项目目录（WORKSPACE）内。
  - 命令执行只做了最粗的拦截，不是真正的沙箱；请只在可信、本地的环境使用，
    不要把带 run_command 的机器人暴露给不信任的人。
"""
import glob as _globmod   #按通配符找文件
import os                 #路径 / 目录 / 文件判断
import re                 #正则（检索、清洗网页）
import subprocess         #运行命令行命令

import requests           #发 HTTP 请求，抓网页

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  #项目根目录（tools/ 的上一级）
MAX_OUTPUT = 8000  # 单次返回给模型的字符上限，防止撑爆上下文

# 最粗的危险命令拦截（不是沙箱，只是防止明显的灾难性误操作）
_BLOCKED = ("rm -rf /", "mkfs", "format c:", "del /f /s /q c:\\", ":(){", "dd if=")


def _within_workspace(path: str) -> str:
    """把路径解析成绝对路径，并确保它落在工作区内。"""
    #把相对路径拼到项目根目录下，再转成绝对路径
    p = os.path.abspath(os.path.join(WORKSPACE, path or "."))
    #如果解析后的路径不在项目目录里，就拒绝（防止读写到项目外）
    if os.path.commonpath([WORKSPACE, p]) != WORKSPACE:
        raise ValueError(f"路径超出工作区：{path}")
    return p


def _truncate(s: str, limit: int = MAX_OUTPUT) -> str:
    #去掉首尾空白；超过上限就截断，并注明原长度
    s = (s or "").strip()
    if len(s) > limit:
        return s[:limit] + f"\n…（输出被截断，原长 {len(s)} 字符）"
    return s


def run_command(command: str, timeout: int = 30) -> str:
    """在项目目录下运行一条命令行命令，返回输出。"""
    #去掉首尾空格；空命令直接返回错误
    command = (command or "").strip()
    if not command:
        return "错误：命令为空。"
    #最粗的危险命令拦截（不是沙箱，只是防明显误操作）
    low = command.lower()
    if any(b in low for b in _BLOCKED):
        return "错误：该命令被安全策略拦截。"
    try:
        #shell=True：交给系统 shell 执行；cwd=项目目录；capture_output：收集输出
        r = subprocess.run(
            command, shell=True, cwd=WORKSPACE,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"命令超时（>{timeout}s）：{command}"
    except Exception as e:
        return f"命令执行失败：{e}"
    #把 stdout 和 stderr 合起来；非 0 退出码也带上，方便模型判断失败
    out = r.stdout or ""
    if r.stderr:
        out += ("\n[stderr]\n" + r.stderr)
    head = "" if r.returncode == 0 else f"[退出码 {r.returncode}]\n"
    return _truncate(head + (out or "(无输出)"))


def read_file(path: str, max_chars: int = MAX_OUTPUT) -> str:
    """读取项目内某个文本文件的内容。"""
    #先把路径限制在项目目录内
    try:
        p = _within_workspace(path)
    except ValueError as e:
        return f"错误：{e}"
    #文件不存在就直接返回人话
    if not os.path.isfile(p):
        return f"错误：文件不存在：{path}"
    try:
        #按 UTF-8 读；读不了就替换，不崩
        with open(p, encoding="utf-8", errors="replace") as f:
            return _truncate(f.read(), max_chars)
    except Exception as e:
        return f"读取失败：{e}"


def write_file(path: str, content: str) -> str:
    """把文本写入项目内某个文件（不存在则创建，存在则覆盖）。"""
    try:
        p = _within_workspace(path)
    except ValueError as e:
        return f"错误：{e}"
    try:
        #父目录不存在就自动建
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        #"w"：不存在就创建，存在就覆盖
        with open(p, "w", encoding="utf-8") as f:
            f.write(content or "")
        return f"已写入 {path}（{len(content or '')} 字符）。"
    except Exception as e:
        return f"写入失败：{e}"


def edit_file(path: str, old: str, new: str) -> str:
    """把文件里的一段原文替换成新文字（用于修改文件）。"""
    try:
        p = _within_workspace(path)
    except ValueError as e:
        return f"错误：{e}"
    if not os.path.isfile(p):
        return f"错误：文件不存在：{path}"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return f"读取失败：{e}"
    #原文必须完全匹配才能替换，否则报错，避免改错位置
    if not old or old not in text:
        return "错误：没找到要替换的原文（需完全匹配）。"
    count = text.count(old)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new))
        return f"已替换 {path} 中 {count} 处。"
    except Exception as e:
        return f"写入失败：{e}"


def list_files(pattern: str = "**/*") -> str:
    """按通配符列出项目内的文件，例如 '**/*.py'。"""
    pattern = (pattern or "**/*").strip()
    try:
        #glob 按通配符找文件；recursive=True 支持 **
        hits = _globmod.glob(os.path.join(WORKSPACE, pattern), recursive=True)
    except Exception as e:
        return f"查找失败：{e}"
    rels = []
    for h in hits:
        rel = os.path.relpath(h, WORKSPACE)  #转成相对项目目录的路径，输出更短
        #跳过虚拟环境、缓存、git 目录，避免刷屏
        if rel.split(os.sep)[0] in (".venv", "__pycache__", ".git"):
            continue
        rels.append(rel)
    return _truncate("\n".join(sorted(rels)) or "(无匹配)")


def search_text(pattern: str, path: str = ".") -> str:
    """在项目文件里按正则搜索文本，返回命中的行（类似 grep）。"""
    try:
        root = _within_workspace(path)
    except ValueError as e:
        return f"错误：{e}"
    try:
        rx = re.compile(pattern or "")  #把正则编译一次，后面重复用
    except re.error as e:
        return f"正则表达式错误：{e}"
    # path 既可以是文件，也可以是目录
    if os.path.isfile(root):
        files_to_scan = [root]
    elif os.path.isdir(root):
        files_to_scan = []
        #os.walk 递归遍历目录
        for cur, dirs, files in os.walk(root):
            #跳过虚拟环境、缓存、git 目录
            dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__", ".git")]
            for fn in files:
                files_to_scan.append(os.path.join(cur, fn))
    else:
        return f"错误：路径不存在：{path}"
    out = []
    for fp in files_to_scan:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                #逐行找，命中就记成 "相对路径:行号: 内容"
                for i, line in enumerate(f, 1):
                    if rx.search(line):
                        rel = os.path.relpath(fp, WORKSPACE)
                        out.append(f"{rel}:{i}: {line.strip()[:200]}")
                        #最多返回 100 条，防止刷屏
                        if len(out) >= 100:
                            return _truncate("\n".join(out) + "\n…（命中过多，已截断）")
        except Exception:
            continue  #单个文件读不了就跳过，不影响其他文件
    return _truncate("\n".join(out) or "(无匹配)")


def web_fetch(url: str, max_chars: int = 6000) -> str:
    """抓取一个网页并返回其中的纯文本（用于联网检索、查天气、查资料等）。"""
    url = (url or "").strip()
    #只允许 http/https，防止乱传别的协议
    if not url.startswith(("http://", "https://")):
        return "错误：请提供以 http(s):// 开头的完整网址。"
    #伪装成浏览器，很多网站会拒绝默认的 requests UA
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)  #发请求，最多等 20 秒
        r.raise_for_status()  #HTTP 错误码就抛异常
        text = r.text
    except Exception as e:
        return f"抓取失败（检查网址/网络）：{e}"
    #去掉脚本、样式、标签、&nbsp;，压成一行纯文本
    text = re.sub(r"<script.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return _truncate(text, max_chars)
