"""Agent 核心：把 DeepSeek（Claude 兼容接口）+ 通用工具串成对话循环。

用法：
    python agent.py "北京今天天气怎么样"
    python agent.py                # 进入交互模式
"""
import json
import logging
import sys
from datetime import datetime

import anthropic

import config
from tools import general, todos
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
- 你有一套通用工具：run_command（运行命令）、read_file / write_file / edit_file（读写改文件）、
  list_files / search_text（列文件 / 全文检索）、web_fetch（联网抓取网页）。
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
  说「我的待办」调用 list_todos；说「完成第 N 个」用 complete_todo，「删除第 N 个」用 delete_todo。
  把「明天 9 点」「周五下午 3 点」等换算成 ISO 时间（YYYY-MM-DDTHH:MM:SS），参考下面的当前时间。
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
        "description": "读取项目内某个文本文件的内容。",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对项目根目录的文件路径"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "把文本写入项目内某个文件（不存在就创建，存在则覆盖原内容）。",
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
        "description": "把文件里的一段原文替换成新文字（用于修改文件）。原文需完全匹配。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string", "description": "要被替换的原文"},
                "new": {"type": "string", "description": "替换成的新文字"},
            },
            "required": ["path", "old", "new"],
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
        "description": "把第 N 个未完成待办标记为完成。",
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "编号，从 1 开始"}},
            "required": ["index"],
        },
    },
    {
        "name": "delete_todo",
        "description": "删除第 N 个未完成待办。",
        "input_schema": {
            "type": "object",
            "properties": {"index": {"type": "integer", "description": "编号，从 1 开始"}},
            "required": ["index"],
        },
    },
]


def _exec(name: str, args: dict) -> str:
    if name == "run_command":
        return general.run_command(args.get("command", ""))
    if name == "read_file":
        return general.read_file(args.get("path", ""))
    if name == "write_file":
        return general.write_file(args.get("path", ""), args.get("content", ""))
    if name == "edit_file":
        return general.edit_file(args.get("path", ""), args.get("old", ""), args.get("new", ""))
    if name == "list_files":
        return general.list_files(args.get("pattern", "**/*"))
    if name == "search_text":
        return general.search_text(args.get("pattern", ""), args.get("path", "."))
    if name == "web_fetch":
        return general.web_fetch(args.get("url", ""))
    if name == "add_todo":
        return todos.add_todo(args.get("text", ""), args.get("due", ""))
    if name == "list_todos":
        return todos.list_todos()
    if name == "complete_todo":
        return todos.complete_todo(args.get("index", 0))
    if name == "delete_todo":
        return todos.delete_todo(args.get("index", 0))
    return f"未知工具：{name}"


def run(user_text: str, history=None) -> str:
    """跑一轮 Agent 循环，返回最终文本回答。history 是可选的历史消息列表。"""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": user_text})

    call_counts = {}   # (工具名, 参数) -> 本次 run 里调用了几次

    for round_no in range(1, MAX_TOOL_ROUNDS + 1):
        resp = client.messages.create(
            model=config.DEEPSEEK_MODEL,
            max_tokens=2048,
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
