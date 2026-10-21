"""Agent 核心：把 DeepSeek（Claude 兼容接口）+ 通用工具串成对话循环。

用法：
    python agent.py "北京今天天气怎么样"
    python agent.py                # 进入交互模式
"""
import json
import logging
import os
import shlex
import sys
from datetime import datetime

import anthropic

import config
from tools import general, memory, todos
try:
    from tools.mcp_client import MCPManager, to_anthropic_tools
except Exception:
    MCPManager = None
    to_anthropic_tools = None
# 三个专用工具（weather / calculator / scholar）已退役：
# 文件仍保留在 tools/ 作为历史参考，但不再注册给模型；
# 相关任务改由通用工具（web_fetch / run_command）自行组合完成。

# 用 auth_token（而不是 api_key）让 SDK 走 Authorization: Bearer，跟 Claude Code 一致
client = anthropic.Anthropic(
    auth_token=config.DEEPSEEK_API_KEY,
    base_url=config.DEEPSEEK_BASE_URL,
)

MAX_TOOL_ROUNDS = 20   # 最多工具调用轮数（通用工具任务可能要多步，原 8 轮太紧）
MAX_REPEAT_CALLS = 3   # 同一工具 + 同一参数最多连续重复几次，超过就拦截
LOG = logging.getLogger("wechat-agent")

SYSTEM = """你是一个个人微信助手，可以通过工具自主完成任务。
- 你有一套通用工具：run_command（运行命令）、read_file / write_file / edit_file / multi_edit（读写改文件）、
  list_files / search_text（列文件 / 全文检索）、web_fetch（联网抓取网页）。
- 改代码/改文件时，必须遵守这个工作流，不要跳步：
  1) 定位：先用 search_text（可用 include 按 *.py 之类筛选）找到相关位置；
  2) 阅读：再用 read_file 读目标文件。默认只返回前 400 行且带行号，
     文件大的话用 offset/limit 分段读完你需要的部分，不要凭猜测改。
  3) 修改：只改局部用 edit_file（原文要能唯一定位；命中多处会报错，此时给出更长的原文）；
     同一个文件要改好几处，用 multi_edit 一次提交，它是原子的——任一处不匹配就整批不写入。
     只有当整个文件都要重写时才用 write_file；已存在的文件必须先 read_file 读过才允许覆盖。
  4) 验证：改完用 run_command 跑一遍验证（例如 python -m compileall <文件>、或对应的测试/脚本），
     并把验证结果如实告诉用户。验证失败就看报错、修正后重试。
- 请自己组合这些通用工具完成任务，常见做法：
  - 算数：用 run_command 跑一段脚本，例如 python -c "print(12345*678+91)"。
  - 查天气：用 web_fetch 调 Open-Meteo 免费接口——先地理编码
    https://geocoding-api.open-meteo.com/v1/search?name=城市&count=1&language=zh 拿经纬度，
    再查 https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..&current=temperature_2m,weather_code。
  - 查学者：用 web_fetch 调 Semantic Scholar 接口
    https://api.semanticscholar.org/graph/v1/author/search?query=名字&fields=name,citationCount,hIndex。
- 工具返回失败、超时或空结果时，必须如实告诉用户「服务暂时不可用」并建议稍后再试；
  禁止编造论文、引用数、作者、链接或任何数据。不确定的信息要说明不确定，不要猜。
- 待办/提醒：用户说「待办 / 提醒我 / 记住要做…」时调用 add_todo；
  说「我的待办」调用 list_todos；完成/删除时，先看列表里的 #编号，
  用 complete_todo / delete_todo 传这个 #编号（或内容关键词，如「交报告」）。
  把「明天 9 点」「周五下午 3 点」等换算成 ISO 时间（YYYY-MM-DDTHH:MM:SS），参考下面的当前时间。
- 长期记忆：用户说「记住…」时调用 remember_fact；问「你还记得…」或涉及个人情况/偏好时，
  先调用 recall_facts 查一下；说「忘掉…」时调用 forget_fact。
- 拿到结果后用简洁、自然的中文回答，不要把原始 JSON 或大段数据直接甩给用户。
- 如果暂时没有合适的工具完成任务，直接说明即可。"""


def _system_prompt() -> str:
    """在系统提示里带上当前时间，模型才能把「明天 9 点」换算成具体日期。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return SYSTEM + f"\n当前时间：{now}（Asia/Shanghai）。"

TOOLS = [
    {
        "name": "run_command",
        "description": "在项目目录下运行一条命令行命令并返回输出。可用来跑脚本、做计算、查看环境等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令，例如 python -c \"print(12345*678+91)\""}
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取项目内某个文本文件，返回带行号的内容。文件较大时用 offset/limit 分段读取。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的文件路径"},
                "offset": {"type": "integer", "description": "从第几行开始读（1 表示第一行），默认 1"},
                "limit": {"type": "integer", "description": "最多读多少行，默认 400"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "把文本写入项目内某个文件（不存在就创建，存在则整体覆盖）。已存在的文件必须先 read_file 读过才允许覆盖。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对项目根目录的文件路径"},
                "content": {"type": "string", "description": "要写入的文本"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": "把文件里的一段原文替换成新文字。原文需能唯一匹配；命中多处会报错并要求给出更长的原文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string", "description": "要被替换的原文（建议从 read_file 的行号内容里复制）"},
                "new": {"type": "string", "description": "替换成的新文字"},
                "all": {"type": "boolean", "description": "为 true 时把命中的多处全部替换，默认 false"},
            },
            "required": ["path", "old", "new"],
        },
    },
    {
        "name": "multi_edit",
        "description": "一次修改同一个文件的多处内容（原子操作：任一处失败则整批不写入）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "edits": {
                    "type": "array",
                    "description": "要执行的替换列表，按顺序应用",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old": {"type": "string", "description": "要替换的原文（需唯一匹配）"},
                            "new": {"type": "string", "description": "替换成的新文字"},
                        },
                        "required": ["old", "new"],
                    },
                },
            },
            "required": ["path", "edits"],
        },
    },
    {
        "name": "list_files",
        "description": "按通配符列出项目内的文件，例如 '**/*.py'。",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "通配符模式，默认 '**/*'"}},
            "required": [],
        },
    },
    {
        "name": "search_text",
        "description": "在项目里按正则表达式搜索文本，返回命中的行（类似 grep）。path 可为文件或目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索起始目录，默认项目根目录"},
                "include": {"type": "string", "description": "只搜文件名匹配该通配符的文件，例如 *.py"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "web_fetch",
        "description": "抓取一个网页并返回其中的纯文本（用于联网检索、查天气、查资料等）。",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "以 http(s):// 开头的完整网址"}},
            "required": ["url"],
        },
    },
    {
        "name": "add_todo",
        "description": "添加一条待办或提醒。用户说「待办 / 提醒我 / 记住要做…」时使用。due 用 ISO 时间，没有明确时间就留空。",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "待办内容"},
                "due": {"type": "string", "description": "提醒时间，ISO 格式如 2026-10-24T15:00；可留空"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_todos",
        "description": "列出当前用户未完成的待办。",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "complete_todo",
        "description": "把指定待办标记为完成。target 传列表里的 #编号（如 #3）或内容关键词（如 交报告）。",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "#编号或内容关键词"}},
            "required": ["target"],
        },
    },
    {
        "name": "delete_todo",
        "description": "删除指定待办。target 传列表里的 #编号（如 #3）或内容关键词（如 交报告）。",
        "input_schema": {
            "type": "object",
            "properties": {"target": {"type": "string", "description": "#编号或内容关键词"}},
            "required": ["target"],
        },
    },
    {
        "name": "remember_fact",
        "description": "记住一条关于用户的事实（长期保留）。用户说「记住…」时使用。",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要记住的事实"}},
            "required": ["text"],
        },
    },
    {
        "name": "recall_facts",
        "description": "回忆关于用户的长期记忆。query 为空返回全部，否则按关键词过滤。",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "关键词，可空"}},
            "required": [],
        },
    },
    {
        "name": "forget_fact",
        "description": "忘掉包含关键词的长期记忆。",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "要忘掉的关键词"}},
            "required": ["query"],
        },
    },
]

MCP_PREFIX = "mcp_"   #MCP 工具统一加这个前缀，避免重名
mcp_client = None     #MCP 客户端；没连上就是 None


def _init_mcp() -> None:
    """配置了 MCP 服务器（或存在示例服务器）时，把它的工具注册进 TOOLS。"""
    global mcp_client
    if MCPManager is None:
        return  #没装 mcp 库就跳过，不影响本地工具
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        #优先用 .env 里的 MCP_SERVER_CMD；没配就用内置示例服务器
        demo = os.path.join(base, "mcp_servers", "demo_server.py")
        cmd = os.getenv("MCP_SERVER_CMD", "").strip()
        if cmd:
            parts = shlex.split(cmd)
        elif os.path.exists(demo):
            parts = [sys.executable, demo]
        else:
            return
        #启动服务器 + 列出工具
        mcp_client = MCPManager()
        mcp_client.start(parts, cwd=base)
        #把 MCP 工具转成我们的菜单格式，接到 TOOLS 后面
        extra = to_anthropic_tools(mcp_client.list_tools(), MCP_PREFIX)
        TOOLS.extend(extra)
        LOG.info("MCP 已连接，注册 %d 个工具：%s", len(extra), [t["name"] for t in extra])
    except Exception as e:
        #MCP 失败不影响本地工具，只记日志
        LOG.warning("MCP 初始化失败：%s", e)
        mcp_client = None


_init_mcp()


def _exec(name: str, args: dict) -> str:
    if name == "run_command":
        return general.run_command(args.get("command", ""))
    if name == "read_file":
        return general.read_file(args.get("path", ""), args.get("offset", 1), args.get("limit", 400))
    if name == "write_file":
        return general.write_file(args.get("path", ""), args.get("content", ""))
    if name == "edit_file":
        return general.edit_file(args.get("path", ""), args.get("old", ""),
                                 args.get("new", ""), bool(args.get("all", False)))
    if name == "multi_edit":
        return general.multi_edit(args.get("path", ""), args.get("edits", []))
    if name == "list_files":
        return general.list_files(args.get("pattern", "**/*"))
    if name == "search_text":
        return general.search_text(args.get("pattern", ""), args.get("path", "."),
                                   args.get("include", ""))
    if name == "web_fetch":
        return general.web_fetch(args.get("url", ""))
    if name == "add_todo":
        return todos.add_todo(args.get("text", ""), args.get("due", ""))
    if name == "list_todos":
        return todos.list_todos()
    if name == "complete_todo":
        return todos.complete_todo(args.get("target", ""))
    if name == "delete_todo":
        return todos.delete_todo(args.get("target", ""))
    if name == "remember_fact":
        return memory.remember_fact(args.get("text", ""))
    if name == "recall_facts":
        return memory.recall_facts(args.get("query", ""))
    if name == "forget_fact":
        return memory.forget_fact(args.get("query", ""))
    if mcp_client and name.startswith(MCP_PREFIX):
        return mcp_client.call_tool(name[len(MCP_PREFIX):], args)
    return f"未知工具：{name}"


def run(user_text: str, history=None) -> str:
    """跑一轮 Agent 循环，返回最终文本回答。history 是可选的历史消息列表。"""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": user_text})

    call_counts = {}   # (工具名, 参数) -> 本次 run 里调用了几次

    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        resp = client.messages.create(
            model=config.DEEPSEEK_MODEL,
            max_tokens=8192,
            system=_system_prompt(),
            tools=TOOLS,
            messages=messages,
        ) #进行网络请求
        messages.append({"role": "assistant", "content": resp.content})
        #content 是一个列表，里面每个元素是一个字典，type 字段表示内容类型（text / tool_use / tool_result），text 字段是文本内容，tool_use_id 是工具调用的唯一 ID

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        results = [] #工具调用结果列表
        for tu in tool_uses:
            inp = tu.input #提取输入参数
            if isinstance(inp, str):  # 有的接口会把 input 返回成 JSON 字符串，兜底解析
                try:
                    inp = json.loads(inp)
                except Exception:
                    inp = {}

            # 重复调用保护：同一工具 + 同一参数调用太多次，就不再执行
            try:
                sig = (tu.name, json.dumps(inp, ensure_ascii=False, sort_keys=True))
            except Exception:
                sig = (tu.name, repr(inp))
            call_counts[sig] = call_counts.get(sig, 0) + 1

            if call_counts[sig] > MAX_REPEAT_CALLS:
                content = ("错误：你已经用完全相同的参数重复调用同一工具多次，结果不会改变。"
                           "请换一种方法，或如实告诉用户这一步失败了。")
                LOG.warning("拦截重复工具调用：%s", tu.name)
            else:
                LOG.info("第 %d 轮工具调用：%s %s", round_no, tu.name,
                         json.dumps(inp, ensure_ascii=False)[:200])
                content = _exec(tu.name, inp)
                LOG.info("工具结果 %s：%s", tu.name, (content or "")[:200])

            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": content,
            })
        messages.append({"role": "user", "content": results})

    return (f"（这个问题需要的步骤太多，我尝试了 {MAX_TOOL_ROUNDS} 轮工具调用仍未完成。"
            f"请把问题拆小一点再试，或换一种问法。）")


if __name__ == "__main__":  #只有文件被直接运行时才会执行下面的代码 被import时不会执行
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if len(sys.argv) > 1:
        q = " ".join(sys.argv[1:])
        print("你：", q)
        print("助手：", run(q))
    else:
        print("交互模式，输入消息回车，Ctrl+C 退出。")
        history = []
        while True:
            try:
                q = input("你：")
            except (EOFError, KeyboardInterrupt):
                break
            if not q.strip():
                continue
            ans = run(q, history)
            history.append({"role": "user", "content": q})
            history.append({"role": "assistant", "content": [{"type": "text", "text": ans}]})
            print("助手：", ans)
