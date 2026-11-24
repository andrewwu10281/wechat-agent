"""文件发送工具：把项目里的文件通过微信发给用户。

为什么单独一个模块（而不是塞进 general.py）：
  1) 发文件是**数据外泄面**——agent 能读 .env，一旦能发文件，就能把密钥发出去。
     所以策略（能不能发）必须和传输（怎么发）分开，策略要能独立测试。
  2) general.py 已经 1100 行，再加文件和浏览器会变成上帝文件。

分层：
  - 策略层（本文件）：黑名单 → 路径围栏 → 大小限制 → 人工审批 → 审计 → 传输
  - 传输层（由外壳注册）：bot.py 调 set_transport() 注册真正的微信发送函数。
    本文件**不 import bot**，避免循环依赖；测试时也不装传输，走降级卡片。

协议现状（诚实说明）：
  微信 ilink 的"文件消息"字段没有公开文档。bot.send_file_message() 里按假设实现，
  字段名/item 类型都能用环境变量改；协议不通时，本模块自动降级为
  「文件卡片文本」（路径 + 大小 + sha256），功能仍然可用，不会把整轮对话搞崩。

审批模型和危险命令一致：
  批准权在外壳（bot.py），模型只能"提出请求"；批准绑定文件指纹、一次性、300 秒过期。
"""
import hashlib
import os
import re
import time

from tools import general  # 复用 WORKSPACE / _within_workspace / _audit

MAX_FILE_MB = 20                 # 单个文件大小上限
APPROVAL_TTL = 300               # 待确认 / 已批准 的有效期（秒）

# 永久拒绝：凭证/密钥类文件，**连审批都不给**（和 rm -rf / 同一个级别）
_BLOCKED_EXACT = {".env", ".wcbot_session.json", "audit.log"}
_BLOCKED_PATTERNS = (
    r"\.env(\.[^/]*)?$",         # .env.local / .env.prod
    r"id_rsa", r"id_ed25519",    # SSH 私钥
    r"\.pem$", r"\.key$", r"\.pfx$", r"\.p12$",
    r"credential", r"secret", r"\.git/", r"\.ssh/",
)

# 待确认 / 已批准（一次性、绑定文件路径指纹）
_pending: dict = {}
_armed: dict = {}

# 传输函数：由外壳注册；没注册时走降级卡片
_transport = None


# ---------------------------------------------------------------------------
# 策略层
# ---------------------------------------------------------------------------
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _sig(path: str) -> str:
    """文件指纹：批准"发 A"不等于批准"发 B"。"""
    return hashlib.sha256(("file:" + path).encode("utf-8")).hexdigest()[:16]


def _rel(path: str) -> str:
    return os.path.relpath(path, general.WORKSPACE).replace("\\", "/")


def check_file(path: str):
    """检查一个文件能不能发。返回 (绝对路径, 错误信息)；错误信息为空表示通过。"""
    try:
        p = general._within_workspace(path)          # 路径围栏：只能项目内
    except ValueError as e:
        return None, f"错误：{e}"
    if not os.path.isfile(p):
        return None, f"错误：文件不存在：{path}"

    rel = _rel(p)
    base = os.path.basename(p).lower()
    if base in _BLOCKED_EXACT or any(re.search(pat, rel, re.I) for pat in _BLOCKED_PATTERNS):
        return None, ("错误：这个文件属于凭证/敏感数据，**永久禁止发送**（审批也不行）。\n"
                      "如果只是想让用户看内容，请用 read_file 读出片段再转述。")
    size = os.path.getsize(p)
    if size > MAX_FILE_MB * 1024 * 1024:
        return None, (f"错误：文件太大（{size / 1048576:.1f}MB），上限 {MAX_FILE_MB}MB。\n"
                      "建议先压缩、或只发其中一部分。")
    return p, ""


def peek_send_pending():
    """供外壳查询：有没有一条正在等用户确认的文件发送。"""
    if not _pending:
        return None
    if time.time() - _pending.get("ts", 0) > APPROVAL_TTL:
        _pending.clear()
        return None
    return dict(_pending)


def arm_send_file():
    """外壳在"用户已明确同意"之后调用：放行那一次发送（一次性、指纹绑定）。

    ⚠️ 和 arm_pending_approval() 一样，这个函数**没有对应工具**，模型调不到。
    """
    p = peek_send_pending()
    if not p:
        return None
    _armed.clear()
    _armed.update({"sig": p["sig"], "expires": time.time() + APPROVAL_TTL})
    _pending.clear()
    general._audit("send_file-approved", path=p.get("rel"), size=p.get("size"))
    return p.get("rel")


def deny_send_file():
    """外壳在"用户拒绝/答非所问"之后调用：清掉待确认，不留后门。"""
    p = peek_send_pending()
    _pending.clear()
    _armed.clear()
    if p:
        general._audit("send_file-denied", path=p.get("rel"), size=p.get("size"))
    return bool(p)


def set_transport(fn) -> None:
    """由外壳（bot.py）注册真正的发送函数：fn(绝对路径, 显示名) -> 结果文本。"""
    global _transport
    _transport = fn


def _fallback_card(path: str, extra: str = "") -> str:
    """协议不通时的降级：给一张"文件卡片"，用户能知道文件是什么、在哪、校验和。"""
    size = os.path.getsize(path)
    return (f"[文件] {os.path.basename(path)}"
            f"（{size / 1024:.1f} KB，sha256 {_sha256(path)[:12]}）\n"
            f"本地路径：{_rel(path)}{extra}")


# ---------------------------------------------------------------------------
# 对外主函数（注册成工具）
# ---------------------------------------------------------------------------
def send_file(path: str, note: str = "") -> str:
    """把项目里的一个文件通过微信发给用户（需要用户确认）。

    流程：策略检查 → 没批准就登记待确认 → 批准后调用传输层 → 失败降级卡片。
    """
    path = (path or "").strip()
    if not path:
        return "错误：文件路径为空。"

    p, err = check_file(path)
    if err:
        general._audit("send_file-blocked", path=path, reason=err[:80])
        return err

    sig = _sig(p)
    approved = (_armed.get("sig") == sig and time.time() < _armed.get("expires", 0))
    if not approved:
        size = os.path.getsize(p)
        _pending.clear()
        _pending.update({"sig": sig, "path": p, "rel": _rel(p), "size": size,
                         "note": note, "ts": time.time()})
        general._audit("send_file-needs-approval", path=_rel(p), size=size)
        return ("这条操作会把电脑上的文件发出去，属于**数据外泄风险**，已上报给用户确认。\n"
                "请在回复里告诉用户：要发哪个文件、多大、发到哪里，请用户确认。\n"
                "**不要重复调用，也不要换文件绕过审批。**")

    _armed.clear()                                   # 一次性，用过即失效
    name = os.path.basename(p)
    try:
        result = _transport(p, name) if _transport else _fallback_card(p)
    except Exception as e:                            # 传输失败不能拖垮整轮对话
        general._audit("send_file-fallback", path=_rel(p), error=str(e)[:150])
        result = _fallback_card(p, f"\n（微信文件通道发送失败：{e}）")

    general._audit("send_file", path=_rel(p), size=os.path.getsize(p),
                   sha=_sha256(p)[:12], note=note[:80])
    if note:
        result += f"\n说明：{note}"
    return result
