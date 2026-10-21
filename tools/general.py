"""通用工具集（harness 底座）

设计思路：不给每个需求写一个专用工具，而是给模型一套「基础能力」——
运行命令、读写文件、检索、联网——由模型自己组合来完成各种任务。
这和 Codex / Claude Code 这类真实 harness 的做法一致。

文件工具的关键设计（对标 Codex，让"改代码"真正可用）：
  1. read_file 返回**带行号**的内容，并支持 offset/limit 分段读大文件；
     否则大文件永远读不全（旧版超 8000 字符就被砍掉尾巴）。
  2. edit_file 先精确匹配，失败再退化为「忽略每行首尾空白」的匹配；
     多处命中时拒绝执行并列出各命中行号，避免改错位置；
     完全没命中时给出「最接近的位置 + 行号」，方便模型自我修正。
  3. multi_edit 支持一次改多处，且是**原子**的：任一处失败则整批不写入。
  4. write_file 有「先读后写」保护：已存在的文件必须先 read_file 读过
     （且读过之后没被别人改过）才允许覆盖，防止盲目覆盖毁掉文件。

安全说明：
  - 这套工具能读写文件、能执行命令，威力大、风险也大。
  - 文件操作被限制在项目目录（WORKSPACE）内。
  - 命令执行只做了最粗的拦截，不是真正的沙箱；请只在可信、本地的环境使用，
    不要把带 run_command 的机器人暴露给不信任的人。
"""
import difflib           #相似度比较，用于"没找到原文"时给出最接近的位置
import fnmatch           #按 *.py 这类通配符筛选文件名
import glob as _globmod  #按通配符找文件
import os                #路径 / 目录 / 文件判断
import re                #正则（检索、清洗网页）
import subprocess        #运行命令行命令

import requests          #发 HTTP 请求，抓网页

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  #项目根目录（tools/ 的上一级）
MAX_OUTPUT = 8000        # 单次返回给模型的字符上限，防止撑爆上下文
MAX_READ_LINES = 400     # read_file 默认最多返回多少行（大文件请用 offset/limit 分段）

# 最粗的危险命令拦截（不是沙箱，只是防止明显的灾难性误操作）
_BLOCKED = ("rm -rf /", "mkfs", "format c:", "del /f /s /q c:\\", ":(){", "dd if=")

# 记录「本进程里读过哪些文件、读的时候它的修改时间是多少」
# 用途：write_file 的「先读后写」保护——没读过、或读过之后文件又变了，就不许覆盖。
_observed: dict[str, float] = {}


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


def _line_no(text: str, pos: int) -> int:
    """把字符位置换算成行号（从 1 开始）。"""
    return text.count("\n", 0, max(0, pos)) + 1


def _remember(p: str) -> None:
    """记住「这个文件被读过了、读的时候是什么修改时间」。"""
    try:
        _observed[p] = os.path.getmtime(p)
    except OSError:
        _observed.pop(p, None)


def reset_observed() -> None:
    """清空「已读文件」记录（主要给测试用）。"""
    _observed.clear()


# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------
def run_command(command: str, timeout: int = 60) -> str:
    """在项目目录下运行一条命令行命令，返回输出（含退出码与 stderr）。"""
    #去掉首尾空格；空命令直接返回错误
    command = (command or "").strip()
    if not command:
        return "错误：命令为空。"
    #超时上限做个夹逼，防止模型传个离谱的值把程序挂死
    try:
        timeout = int(timeout)
    except Exception:
        timeout = 60
    timeout = max(1, min(timeout, 600))
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
        return f"命令超时（>{timeout}s）：{command}\n建议：拆成更小的步骤，或调大 timeout。"
    except Exception as e:
        return f"命令执行失败：{e}"
    #把 stdout 和 stderr 合起来；非 0 退出码也带上，方便模型判断失败
    out = r.stdout or ""
    if r.stderr:
        out += ("\n[stderr]\n" + r.stderr)
    head = "" if r.returncode == 0 else f"[退出码 {r.returncode}]\n"
    return _truncate(head + (out or "(无输出)"))


# ---------------------------------------------------------------------------
# 读文件
# ---------------------------------------------------------------------------
def read_file(path: str, offset: int = 1, limit: int = MAX_READ_LINES) -> str:
    """读取项目内某个文本文件，返回**带行号**的内容；大文件用 offset/limit 分段读。"""
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
            text = f.read()
    except Exception as e:
        return f"读取失败：{e}"
    #标记「这个文件读过了」，后面的 write_file 才允许覆盖
    _remember(p)

    lines = text.splitlines()
    total = len(lines)
    #offset / limit 做健壮化处理（模型可能传字符串、0、负数）
    try:
        offset = max(1, int(offset))
    except Exception:
        offset = 1
    try:
        limit = int(limit)
    except Exception:
        limit = MAX_READ_LINES
    if limit <= 0:
        limit = MAX_READ_LINES
    if total == 0:
        return f"（{path} 是空文件，0 行）"
    if offset > total:
        return f"（{path} 共 {total} 行，offset={offset} 超出范围；请用 1~{total}）"

    end = min(total, offset - 1 + limit)   # 1-based，包含 end
    width = len(str(end))
    out = [f"{i + 1:>{width}}| {lines[i]}" for i in range(offset - 1, end)]
    header = f"（{path}：共 {total} 行，显示第 {offset}-{end} 行）"
    body = _truncate("\n".join(out), MAX_OUTPUT)
    tail = "" if end >= total else f"\n（还有 {total - end} 行，可用 offset={end + 1} 继续读）"
    return f"{header}\n{body}{tail}"


# ---------------------------------------------------------------------------
# 写文件（带「先读后写」保护）
# ---------------------------------------------------------------------------
def write_file(path: str, content: str) -> str:
    """把文本写入项目内某个文件（不存在则创建，存在则覆盖）。

    保护：已存在的文件必须先 read_file 读过，且读完之后没被改动过，才允许覆盖。
    """
    try:
        p = _within_workspace(path)
    except ValueError as e:
        return f"错误：{e}"
    content = content or ""

    #「先读后写」保护：防止盲目覆盖毁掉文件
    if os.path.isfile(p):
        seen = _observed.get(p)
        if seen is None:
            return (f"错误：{path} 已存在，但本次会话还没读过它，拒绝覆盖。\n"
                    f"请先 read_file 确认内容；如果只想改局部，用 edit_file / multi_edit 更安全。")
        try:
            if abs(os.path.getmtime(p) - seen) > 1e-6:
                return (f"错误：{path} 在你上次读取之后已被改动过，拒绝覆盖。\n"
                        f"请重新 read_file 确认最新内容后再写。")
        except OSError:
            pass

    try:
        #父目录不存在就自动建
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        #"w"：不存在就创建，存在就覆盖
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        _remember(p)   # 写完更新记录，后续操作才能继续
        return f"已写入 {path}（{len(content)} 字符）。"
    except Exception as e:
        return f"写入失败：{e}"


# ---------------------------------------------------------------------------
# 改文件：匹配逻辑 / 近似定位 / 单处与多处编辑
# ---------------------------------------------------------------------------
def _match_spans(text: str, old: str) -> tuple[list, str]:
    """在 text 里找 old 的位置。

    先做精确匹配；没有命中时，退化为「逐行比较、忽略每行首尾空白」的匹配，
    这样模型少打/多打几个空格也不至于直接失败。
    返回 (位置列表, "exact" / "fuzzy" / "none")。
    """
    spans = []
    start = 0
    while True:
        i = text.find(old, start)
        if i < 0:
            break
        spans.append((i, i + len(old)))
        start = i + 1          # 允许重叠，保证 3 处命中能全统计出来
    if spans:
        return spans, "exact"

    old_lines = old.splitlines()
    if not old_lines:
        return [], "none"
    lines = text.splitlines(keepends=True)
    offsets = []
    pos = 0
    for ln in lines:
        offsets.append(pos)
        pos += len(ln)
    n = len(old_lines)
    stripped_old = [o.strip() for o in old_lines]
    for i in range(len(lines) - n + 1):
        window = lines[i:i + n]
        if [w.strip() for w in window] == stripped_old:
            last = i + n - 1
            spans.append((offsets[i], offsets[last] + len(lines[last])))
    return spans, ("fuzzy" if spans else "none")


def _near_miss_report(text: str, old: str, radius: int = 3) -> str:
    """没找到原文时，指出最接近的位置（带行号），帮模型自己修正。"""
    lines = text.splitlines()
    if not lines:
        return "（文件是空的）"
    #挑 old 里最长的一行当"锚点"
    key = ""
    for cand in old.splitlines():
        c = cand.strip()
        if len(c) > len(key):
            key = c
    if not key:
        return "（要替换的原文全是空行，请给出具体内容）"
    best_ratio, best_idx = -1.0, -1
    for i, ln in enumerate(lines):
        ratio = difflib.SequenceMatcher(None, key, ln.strip()).ratio()
        if ratio > best_ratio:
            best_ratio, best_idx = ratio, i
    if best_idx < 0 or best_ratio < 0.4:
        return "（文件里没有明显相近的内容；请检查路径是否正确，或先用 search_text 定位）"
    lo = max(0, best_idx - radius)
    hi = min(len(lines), best_idx + radius + 1)
    width = len(str(hi))
    snippet = "\n".join(f"{i + 1:>{width}}| {lines[i]}" for i in range(lo, hi))
    return (f"最接近的位置在第 {best_idx + 1} 行（相似度 {best_ratio:.0%}）：\n{snippet}\n"
            f"请按文件里的实际内容重新给出原文（注意缩进与空格）。")


def _ambiguous_msg(text: str, spans: list, which: str) -> str:
    """原文命中多处时的提示：列出各处行号，要求模型给出唯一原文。"""
    where = "、".join(str(_line_no(text, s)) for s, _ in spans[:10])
    more = "" if len(spans) <= 10 else " 等"
    return (f"错误：{which}原文在文件中出现 {len(spans)} 次（第 {where}{more} 行），无法确定改哪一处。\n"
            f"请给出更长、能唯一定位的原文，或传 all=true 一次性全部替换。")


def _do_edits(text: str, old: str, new: str, all_matches: bool, which: str = ""):
    """在 text 上执行一次替换。返回 (新文本, 提示语) 或 (None, 错误信息)。"""
    spans, kind = _match_spans(text, old)
    if not spans:
        return None, f"错误：{which}没找到要替换的原文。" + chr(10) + _near_miss_report(text, old)
    if len(spans) > 1 and not all_matches:
        return None, _ambiguous_msg(text, spans, which)
    out = text
    #从后往前替换，避免前面的替换让后面的位置错位
    for s, e in sorted(spans, reverse=True):
        out = out[:s] + (new or "") + out[e:]
    note = "" if kind == "exact" else "（原文按「忽略行首尾空白」匹配成功）"
    return out, f"{len(spans)} 处{note}"


def edit_file(path: str, old: str, new: str, all: bool = False) -> str:
    """把文件里的一段原文替换成新文字；原文需唯一匹配（除非 all=True）。"""
    try:
        p = _within_workspace(path)
    except ValueError as e:
        return f"错误：{e}"
    if not os.path.isfile(p):
        return f"错误：文件不存在：{path}"
    if not old:
        return "错误：要替换的原文不能为空。"
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except Exception as e:
        return f"读取失败：{e}"
    _remember(p)

    new_text, info = _do_edits(text, old, new, all)
    if new_text is None:
        return info          # 没改动文件，原样返回错误提示
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(new_text)
        _remember(p)
        return f"已替换 {path} 中 {info}。"
    except Exception as e:
        return f"写入失败：{e}"


def multi_edit(path: str, edits) -> str:
    """对一个文件做多处替换。**原子**：任一处失败则整批不写入。"""
    try:
        p = _within_workspace(path)
    except ValueError as e:
        return f"错误：{e}"
    if not os.path.isfile(p):
        return f"错误：文件不存在：{path}"
    if not isinstance(edits, list) or not edits:
        return '错误：edits 必须是非空列表，每项形如 {"old": "...", "new": "..."}。'
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            work = f.read()
    except Exception as e:
        return f"读取失败：{e}"
    _remember(p)

    original = work
    for k, item in enumerate(edits, 1):
        if not isinstance(item, dict):
            return f'错误：第 {k} 项格式不对，应为 {{"old": "...", "new": "..."}}。（本次未写入任何内容）'
        old = item.get("old", "")
        new = item.get("new", "")
        if not old:
            return f"错误：第 {k} 项的 old 为空。（本次未写入任何内容）"
        new_work, info = _do_edits(work, old, new, False, which=f"第 {k} 项")
        if new_work is None:
            return info + "\n（本次未写入任何内容，文件保持原样）"
        work = new_work

    if work == original:
        return "（内容和原来一样，没有实际改动）"
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(work)
        _remember(p)
        return f"已在 {path} 中应用 {len(edits)} 处修改。"
    except Exception as e:
        return f"写入失败：{e}"


# ---------------------------------------------------------------------------
# 列文件 / 搜内容 / 抓网页
# ---------------------------------------------------------------------------
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


def search_text(pattern: str, path: str = ".", include: str = "") -> str:
    """在项目里按正则搜索文本，返回命中的行（类似 grep）。

    include 可以按文件名通配符筛选，例如 "*.py"，避免在无关文件里刷出噪声。
    """
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

    #按文件名通配符筛选（如 *.py）
    include = (include or "").strip()
    if include:
        files_to_scan = [f for f in files_to_scan if fnmatch.fnmatch(os.path.basename(f), include)]
        if not files_to_scan:
            return f"（没有文件名匹配 {include} 的文件）"

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
