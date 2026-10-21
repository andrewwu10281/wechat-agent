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

安全机制（Phase 4，"可控"比"能干"更优先）：
  A. **路径围栏**：命令里出现的路径若解析到项目目录之外（`..`、绝对路径、`~`、`%TEMP%`），
     一律判为越界。文件工具本来就只能在项目内，命令工具现在也受同样约束。
  B. **危险命令确认（人机审批）**：删除/移动/git 强制重置/卸载包/taskkill 等操作默认拒绝。
     **批准权在外壳（bot.py），模型无法自行批准**——模型只能在回复里向用户说明并请示，
     用户同意后由 bot.py 调 `arm_pending_approval()` 放行那一条命令（一次性、指纹绑定）。
     ——这是"给聊天机器人用的审批流"：审批发生在微信对话里，不是弹窗，也不是模型自己说了算。
  C. **审计日志**：每次命令、每次写文件、每次拦截都追加一行 JSON 到 `audit.log`
     （已 gitignore），事后能查"谁在什么时候让它干了什么"。
  D. 三档强度：环境变量 `SECURITY_MODE = strict（默认）/ warn / off`。

⚠️ 诚实说明：**这不是操作系统级沙箱**。路径围栏是字符串层面的检查，`shell=True` 仍能
通过编码技巧绕过（例如把路径拼在脚本里）。真要对外提供服务，应该把命令执行放进容器
或受限用户，而不是只靠这些检查。
"""
import difflib           #相似度比较，用于"没找到原文"时给出最接近的位置
import fnmatch           #按 *.py 这类通配符筛选文件名
import glob as _globmod  #按通配符找文件
import hashlib           #命令指纹：审批只对"同一条命令"有效
import itertools         #生成后台任务的自增编号
import json              #写审计日志（JSON Lines）
import os                #路径 / 目录 / 文件判断
import re                #正则（检索、清洗网页）
import subprocess        #运行命令行命令
import threading         #后台任务的输出读取线程
import time              #记录任务耗时

import requests          #发 HTTP 请求，抓网页

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  #项目根目录（tools/ 的上一级）
MAX_OUTPUT = 8000        # 单次返回给模型的字符上限，防止撑爆上下文
MAX_READ_LINES = 400     # read_file 默认最多返回多少行（大文件请用 offset/limit 分段）
MAX_COMMAND_TAIL = 3000  # 命令输出超长时，尾部保留多少字符（报错信息通常在尾部）
MAX_JOB_OUTPUT = 20000   # 每个后台任务最多在内存里留多少字符输出（超出丢最老的）

# 最粗的危险命令拦截（不是沙箱，只是防止明显的灾难性误操作）
_BLOCKED = ("rm -rf /", "mkfs", "format c:", "del /f /s /q c:\\", ":(){", "dd if=")

# 后台任务表：job_id -> 任务信息（进程、输出缓冲、状态）
_jobs: dict[str, dict] = {}
_job_seq = itertools.count(1)   # 自增编号，生成 job1 / job2 ...

# 记录「本进程里读过哪些文件、读的时候它的修改时间是多少」
# 用途：write_file 的「先读后写」保护——没读过、或读过之后文件又变了，就不许覆盖。
_observed: dict[str, float] = {}

# ---------------------------------------------------------------------------
# 安全策略：模式 / 审计 / 危险命令判定 / 路径围栏
# ---------------------------------------------------------------------------
# strict（默认）：越界拒绝 + 危险命令必须审批
# warn          ：越界与危险命令只记审计、仍放行（自己机器上折腾时用）
# off           ：完全不检查（不建议）
SECURITY_MODE = (os.getenv("SECURITY_MODE", "strict") or "strict").strip().lower()
if SECURITY_MODE not in ("strict", "warn", "off"):
    SECURITY_MODE = "strict"

AUDIT_FILE = os.path.join(WORKSPACE, "audit.log")

# 人机审批状态：**批准权在外壳（bot.py），不在模型**。
# 模型无法调用 arm_pending_approval()——工具菜单里没有这个工具，它只能"提出请求"。
APPROVAL_TTL = 300       # 待确认 / 已批准 的有效期（秒）
_pending: dict = {}      # 正在等用户确认的那条危险命令
_armed: dict = {}        # 用户已批准的那条命令（一次性，绑定命令指纹）

# 一律拒绝：这些操作破坏性太大，不是"要不要审批"的问题
_DENY_RX = re.compile(
    r"rm\s+-[rf]{1,2}\s+/(\s|$|\*)"          # rm -rf /
    r"|mkfs"                                  # 格式化
    r"|format\s+[a-z]:"                       # format c:
    r"|del\s+/[fsq].*\b[a-z]:\\"              # del /f /s /q c:\
    r"|rd\s+/s\s+/q\s+[a-z]:\\"               # rd /s /q c:\
    r"|dd\s+if="                              # 裸写磁盘
    r"|>\s*/dev/sd"                           # 覆盖磁盘设备
    r"|:\(\)\s*\{\s*:\|:&\s*\};:"              # fork 炸弹
    r"|shutdown|reboot|halt\b"                # 关机/重启
    r"|reg\s+delete\s+hklm"                   # 删注册表根
    r"|Remove-Item\s+-Recurse\s+-Force\s+[a-z]:\\"
    r"|chmod\s+-R\s+777\s+/(\s|$)",           # 全盘放宽权限
    re.IGNORECASE,
)

# 危险操作：需要外壳（bot.py）在用户确认后放行，模型无权自行批准
_RISKY_RX = re.compile(
    r"\brm\b|\bdel\b|\brd\b|\brmdir\b|\bRemove-Item\b|\brmtree\b"   # 删除
    r"|\bmove\b|\bmv\b|\bMove-Item\b|\bren\b|\bRename-Item\b"        # 移动/改名
    r"|\bgit\s+(reset\s+--hard|clean\b|push\b.*--force|checkout\s+--\s)"  # 丢改动
    r"|\bpip\s+uninstall\b|\bnpm\s+uninstall\b"                      # 卸载
    r"|\btaskkill\b|\bStop-Process\b"                                # 杀进程
    r"|\bsudo\b|\brunas\b|\bchmod\b|\bchown\b|\breg\s+add\b"          # 提权/改权限
    r"|\bsetx\b|\bnetsh\b"                                            # 改系统配置
    r"|\b(curl|wget)\b[^|]*\|\s*(bash|sh|python)"                    # 管道执行远程脚本
    r"|\bStart-Process\b.*-Verb\s+RunAs",                            # 提权启动
    re.IGNORECASE,
)


def _audit(event: str, **fields) -> None:
    """追加一行审计记录到 audit.log（JSON Lines）。审计失败绝不影响主流程。"""
    try:
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event}
        try:
            from tools.context import get_current_user   # 记录"是谁让它干的"
            rec["user"] = get_current_user()
        except Exception:
            pass
        rec.update(fields)
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _looks_like_path(tok: str) -> bool:
    """粗略判断一个命令行参数像不像路径。

    注意：`/q`、`-rf`、`/s` 这类是**命令行开关**，不是 Unix 绝对路径——
    误判它们会让所有带参数的 Windows 命令（del /q、dir /s…）都被当成"越界"。
    """
    if not tok or len(tok) < 2:
        return False
    if re.match(r"^[-/][A-Za-z]{1,4}$", tok):   # 开关，如 /q、-rf、--force
        return False
    if tok.startswith(("~", "..", "%", "$")):
        return True
    if re.match(r"^[A-Za-z]:[\\/]", tok):       # C:\...
        return True
    if tok.startswith(("/", "\\")):
        return True
    return ("/" in tok or "\\" in tok)


def _find_escapes(command: str) -> list:
    """找出命令里解析到工作区之外的路径参数（字符串层面检查，不是真沙箱）。"""
    hits = []
    for raw in re.split(r"[\s\"'=;,()|&<>]+", command or ""):
        tok = raw.strip()
        if not _looks_like_path(tok):
            continue
        if tok.startswith(("http://", "https://")):
            continue
        # %TEMP% / $HOME / ~ 这类无法静态解析，一律按"可能越界"处理
        if "%" in tok or tok.startswith(("$", "~")):
            hits.append(tok)
            continue
        try:
            p = os.path.abspath(tok if os.path.isabs(tok) else os.path.join(WORKSPACE, tok))
            if os.path.commonpath([WORKSPACE, p]) != WORKSPACE:
                hits.append(tok)
        except Exception:
            hits.append(tok)      # 解析不了也算可疑
    return hits


def _sig(command: str) -> str:
    """命令指纹：审批只对"同一条命令"有效，换了命令立刻失效。"""
    return hashlib.sha256((command or "").strip().encode("utf-8")).hexdigest()[:16]


def peek_pending_approval():
    """供外壳（bot.py）查询：有没有一条正在等用户确认的危险命令。"""
    if not _pending:
        return None
    if time.time() - _pending.get("ts", 0) > APPROVAL_TTL:
        _pending.clear()
        return None
    return dict(_pending)


def arm_pending_approval():
    """外壳在"用户已明确同意"之后调用：放行那一条命令（一次性、指纹绑定）。

    ⚠️ 这个函数**没有对应的工具**，模型无法调用它 —— 这是"审批权在人类一侧"的技术保证。
    """
    p = peek_pending_approval()
    if not p:
        return None
    _armed.clear()
    _armed.update({"sig": p["sig"], "expires": time.time() + APPROVAL_TTL})
    _pending.clear()
    _audit("approval-granted", command=p.get("command"), tool=p.get("tool"))
    return p.get("command")


def deny_pending_approval():
    """外壳在"用户拒绝"之后调用：清掉待确认命令，不留后门。"""
    p = peek_pending_approval()
    _pending.clear()
    _armed.clear()
    if p:
        _audit("approval-denied", command=p.get("command"), tool=p.get("tool"))
    return bool(p)


def _command_policy(command: str, cwd: str, tool: str) -> tuple:
    """判定一条命令能不能跑。返回 (decision, reply)。

    decision: "allow"（直接跑）/ "deny"（拒绝，reply 是给模型的说明）

    危险操作的批准权**不在模型手里**：只有外壳调过 arm_pending_approval()，
    且命令指纹一致，才放行一次。
    """
    if SECURITY_MODE == "off":
        return "allow", ""
    cmd = command or ""
    # ① 一律拒绝的破坏性命令
    if _DENY_RX.search(cmd):
        _audit("blocked", tool=tool, command=cmd, reason="deny-list")
        return "deny", ("错误：这条命令属于破坏性操作（删除/格式化/关机/改系统配置等），"
                        "已被安全策略永久拒绝，不能通过授权绕过。请换一种安全的做法。")
    # ② 越界路径：strict 直接拒绝
    escapes = _find_escapes(cmd)
    if escapes:
        if SECURITY_MODE == "strict":
            _audit("blocked", tool=tool, command=cmd, reason="escape", paths=escapes)
            return "deny", (f"错误：命令里这些路径超出了项目目录，已被拒绝：{', '.join(escapes[:5])}\n"
                            f"项目工作目录是：{WORKSPACE}。请只操作项目内的文件。")
        _audit("warn-escape", tool=tool, command=cmd, paths=escapes)
    # ③ 危险操作
    if _RISKY_RX.search(cmd):
        if SECURITY_MODE == "warn":
            _audit("risky-allowed", tool=tool, command=cmd, reason="warn-mode")
            return "allow", ""
        if _armed and _armed.get("sig") == _sig(cmd) and time.time() < _armed.get("expires", 0):
            _armed.clear()                      # 一次性，用过立刻失效
            _audit("command-approved", tool=tool, command=cmd)
            return "allow", ""
        _pending.clear()
        _pending.update({"sig": _sig(cmd), "command": cmd, "tool": tool, "ts": time.time()})
        _audit("needs-approval", tool=tool, command=cmd)
        return "deny", ("这条命令属于**有风险的操作**（删除/移动/强推/卸载/杀进程等），已被拦下，"
                        "并且已经上报给用户确认。\n"
                        "请在回复里告诉用户：要做什么、会有什么后果，请用户确认。\n"
                        "**不要重复调用这条命令，也不要换用别的命令绕过审批。**")
    return "allow", ""


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


def _truncate_middle(s: str, limit: int = MAX_OUTPUT, keep_tail: int = MAX_COMMAND_TAIL) -> str:
    """超长输出保留「头部 + 尾部」，中间省略。

    为什么不用只砍尾部：命令的**报错信息通常出现在末尾**，只留头部会把最有用的部分丢掉。
    """
    s = (s or "").strip()
    if len(s) <= limit:
        return s
    keep_tail = min(keep_tail, limit // 2)
    head_len = max(0, limit - keep_tail - 40)
    omitted = len(s) - head_len - keep_tail
    return (s[:head_len] + f"\n…（中间省略 {omitted} 字符）…\n" + s[-keep_tail:])


def _resolve_cwd(cwd: str) -> str:
    """把命令的工作目录限制在工作区内；不传就用项目根目录。"""
    cwd = (cwd or "").strip()
    if not cwd:
        return WORKSPACE
    p = _within_workspace(cwd)
    if not os.path.isdir(p):
        raise ValueError(f"目录不存在：{cwd}")
    return p


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
def run_command(command: str, timeout: int = 60, cwd: str = "") -> str:
    """在项目内运行一条命令，返回结构化结果（执行状态 / 超时 / 退出码 / stdout+stderr）。

    输出超长时保留「头部 + 尾部」——报错通常在尾部，不能只留头部。
    危险操作的批准权在外壳（bot.py 调 arm_pending_approval），模型无权自行批准。
    """
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
    try:
        workdir = _resolve_cwd(cwd)
    except ValueError as e:
        return f"错误：{e}"
    #安全策略判定：破坏性命令拒绝、越界路径拒绝、危险操作要审批
    decision, reply = _command_policy(command, workdir, "run_command")
    if decision == "deny":
        return reply
    try:
        #shell=True：交给系统 shell 执行；capture_output：收集输出
        r = subprocess.run(
            command, shell=True, cwd=workdir,
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        _audit("command", tool="run_command", command=command, result="timeout")
        return (f"状态：超时（>{timeout}s），命令可能仍在运行或已被终止：{command}\n"
                f"建议：需要长时间运行的任务改用 run_background 起后台任务，再用 check_job 轮询。")
    except Exception as e:
        _audit("command", tool="run_command", command=command, result="error", error=str(e))
        return f"状态：执行失败：{e}"
    _audit("command", tool="run_command", command=command,
           result="ok" if r.returncode == 0 else "failed", rc=r.returncode)
    #把 stdout 和 stderr 分开标注，再附退出码，结构清晰便于模型判断
    parts = [f"状态：{'成功（退出码 0）' if r.returncode == 0 else f'失败（退出码 {r.returncode}）'}"]
    if r.stdout:
        parts.append("[stdout]\n" + r.stdout)
    if r.stderr:
        parts.append("[stderr]\n" + r.stderr)
    if not r.stdout and not r.stderr:
        parts.append("(无输出)")
    return _truncate_middle("\n".join(parts))


# ---------------------------------------------------------------------------
# 后台任务：长命令（装依赖、跑测试、构建）不该把 agent 卡死
# ---------------------------------------------------------------------------
def _job_brief(job: dict) -> str:
    """一行摘要：状态 + 耗时 + 命令。"""
    status = "已结束" if job["done"] else "运行中"
    dur = time.time() - job["started"]
    rc = f"，退出码 {job['rc']}" if job["done"] else ""
    return f"{status}{rc}，已运行 {dur:.0f}s，命令：{job['command']}"


def run_background(command: str, cwd: str = "") -> str:
    """在后台启动一条命令，立刻返回任务编号；用 check_job 看进度、kill_job 终止。"""
    command = (command or "").strip()
    if not command:
        return "错误：命令为空。"
    try:
        workdir = _resolve_cwd(cwd)
    except ValueError as e:
        return f"错误：{e}"
    decision, reply = _command_policy(command, workdir, "run_background")
    if decision == "deny":
        return reply
    try:
        #stderr 合并进 stdout，保证输出顺序可读
        proc = subprocess.Popen(
            command, shell=True, cwd=workdir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except Exception as e:
        return f"错误：启动失败：{e}"

    jid = f"job{next(_job_seq)}"
    job = {
        "id": jid, "command": command, "proc": proc, "buf": [], "size": 0,
        "dropped": 0, "started": time.time(), "done": False, "rc": None,
    }
    _jobs[jid] = job

    def _reader() -> None:
        #后台线程持续读输出，边读边丢最老的，防止内存无限涨
        try:
            for line in proc.stdout:
                job["buf"].append(line)
                job["size"] += len(line)
                while job["size"] > MAX_JOB_OUTPUT and len(job["buf"]) > 1:
                    job["size"] -= len(job["buf"].pop(0))
                    job["dropped"] += 1
        except Exception:
            pass
        finally:
            job["rc"] = proc.wait()
            job["done"] = True

    threading.Thread(target=_reader, daemon=True).start()
    _audit("background", tool="run_background", command=command, job=jid)
    return (f"状态：已在后台启动 {jid}\n{_job_brief(job)}\n"
            f"用 check_job(job_id=\"{jid}\") 查看进度和最新输出；用 kill_job(job_id=\"{jid}\") 终止。")


def check_job(job_id: str, tail_chars: int = 2000) -> str:
    """查看后台任务的进度和最新输出（默认只给末尾 2000 字符）。"""
    jid = (job_id or "").strip()
    job = _jobs.get(jid)
    if not job:
        ids = "、".join(_jobs) or "（当前没有后台任务）"
        return f"错误：没有这个后台任务：{job_id}。现有的：{ids}"
    try:
        tail_chars = int(tail_chars)
    except Exception:
        tail_chars = 2000
    tail_chars = max(200, min(tail_chars, 8000))
    out = "".join(job["buf"])
    note = ""
    if job["dropped"]:
        note = f"（早期输出已丢弃 {job['dropped']} 行）\n"
    if len(out) > tail_chars:
        out = "…（更早的输出已省略）\n" + out[-tail_chars:]
    return (f"任务 {jid}：{_job_brief(job)}\n{note}"
            f"---- 输出末尾 ----\n{out.rstrip() or '(还没有输出)'}")


def kill_job(job_id: str) -> str:
    """终止一个仍在运行的后台任务（连同它派生的子进程）。"""
    jid = (job_id or "").strip()
    job = _jobs.get(jid)
    if not job:
        return f"错误：没有这个后台任务：{job_id}。"
    if job["done"]:
        return f"任务 {jid} 已经结束了（退出码 {job['rc']}），无需终止。"
    proc = job["proc"]
    try:
        if os.name == "nt":
            #Windows 下用 taskkill 连子进程树一起结束
            subprocess.run(f"taskkill /F /T /PID {proc.pid}", shell=True,
                           capture_output=True, timeout=20)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    except Exception as e:
        return f"错误：终止失败：{e}"
    job["done"] = True
    if job["rc"] is None:
        job["rc"] = proc.poll()
    return f"状态：已终止 {jid}（{job['command']}）"


def wait_job(job_id: str, timeout: int = 60, tail_chars: int = 3000) -> str:
    """等后台任务结束（最多 timeout 秒），返回最终状态与输出末尾。

    为什么不只用 check_job 轮询：模型每轮轮询都要走一次 LLM 调用（慢且花钱）。
    能接受等待时用 wait_job 一次拿到结果，更省也更稳。
    """
    jid = (job_id or "").strip()
    job = _jobs.get(jid)
    if not job:
        return f"错误：没有这个后台任务：{job_id}。"
    try:
        timeout = int(timeout)
    except Exception:
        timeout = 60
    timeout = max(1, min(timeout, 600))
    deadline = time.time() + timeout
    while not job["done"] and time.time() < deadline:
        time.sleep(0.2)
    if job["done"]:
        return check_job(jid, tail_chars)
    return f"任务 {jid} 在 {timeout}s 内还没结束，仍在运行。\n" + check_job(jid, 1500)


def list_jobs() -> str:
    """列出本次进程里所有后台任务及其状态。"""
    if not _jobs:
        return "当前没有后台任务。"
    return "\n".join(f"- {jid}: {_job_brief(job)}" for jid, job in _jobs.items())


def read_audit_log(limit: int = 30, event: str = "") -> str:
    """读最近的审计记录（谁在哪一刻让它跑了什么命令 / 写了什么文件 / 被拦了什么）。"""
    if not os.path.exists(AUDIT_FILE):
        return "还没有审计记录（audit.log 不存在）。"
    try:
        limit = max(1, min(int(limit), 200))
    except Exception:
        limit = 30
    want = (event or "").strip()
    try:
        with open(AUDIT_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取审计日志失败：{e}"
    picked = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if want and rec.get("event") != want:
            continue
        picked.append(rec)
    if not picked:
        return f"没有匹配的审计记录（event={want or '全部'}）。"
    out = []
    for rec in picked[-limit:]:
        parts = [f"{rec.get('ts','')} [{rec.get('event','')}]"]
        for k in ("tool", "user", "command", "path", "job", "result", "rc", "reason", "approved"):
            if rec.get(k) not in (None, "", False):
                parts.append(f"{k}={rec[k]}")
        out.append(" | ".join(parts))
    head = f"（audit.log 共 {len(lines)} 行，显示最近 {len(out)} 条{('，event=' + want) if want else ''}）"
    return head + "\n" + "\n".join(out)


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
        _audit("write_file", path=os.path.relpath(p, WORKSPACE), chars=len(content))
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
        _audit("edit_file", path=os.path.relpath(p, WORKSPACE), detail=info)
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
        _audit("multi_edit", path=os.path.relpath(p, WORKSPACE), edits=len(edits))
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
