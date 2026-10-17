"""Agent 核心：把 DeepSeek（Claude 兼容接口）+ 通用工具串成对话循环。

用法：
    python agent.py "北京今天天气怎么样"
    python agent.py                # 进入交互模式
"""
import json
import sys

import anthropic

import config
from tools import general
# 三个专用工具（weather / calculator / scholar）已退役：
# 文件仍保留在 tools/ 作为历史参考，但不再注册给模型；
# 相关任务改由通用工具（web_fetch / run_command）自行组合完成。

# 用 auth_token（而不是 api_key）让 SDK 走 Authorization: Bearer，跟 Claude Code 一致
client = anthropic.Anthropic(
    auth_token=config.DEEPSEEK_API_KEY,
    base_url=config.DEEPSEEK_BASE_URL,
)

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
- 拿到结果后用简洁、自然的中文回答，不要把原始 JSON 或大段数据直接甩给用户。
- 如果暂时没有合适的工具完成任务，直接说明即可。"""

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
    return f"未知工具：{name}"


def run(user_text: str, history=None) -> str:
    """跑一轮 Agent 循环，返回最终文本回答。history 是可选的历史消息列表。"""
    messages = list(history) if history else []
    messages.append({"role": "user", "content": user_text})

    for _ in range(8):  # 最多 8 轮工具调用，防止死循环
        resp = client.messages.create(
            model=config.DEEPSEEK_MODEL,
            max_tokens=2048,
            system=SYSTEM,
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
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": _exec(tu.name, inp),
            })
        messages.append({"role": "user", "content": results})

    return "（工具调用次数过多，已停止。）"


if __name__ == "__main__":  #只有文件被直接运行时，才会执行下面的代码 被import时不会执行
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
