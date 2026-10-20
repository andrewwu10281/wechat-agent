"""最小 MCP 服务器示例：暴露 3 个工具。

通常由 agent.py 通过 stdio 自动拉起，也可以手动运行：
    .venv\\Scripts\\python mcp_servers\\demo_server.py
"""
import random
from datetime import datetime

from mcp.server.mcpserver import MCPServer

#创建一个 MCP 服务器，名字叫 demo
mcp = MCPServer("demo")


#@mcp.tool 装饰器：把这个普通函数注册成一个 MCP 工具
@mcp.tool(description="返回当前日期和时间")
def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@mcp.tool(description="统计一段文本的字数")
def word_count(text: str) -> int:
    return len(text or "")


@mcp.tool(description="生成一个随机整数")
def random_number(min_value: int = 1, max_value: int = 100) -> int:
    return random.randint(min_value, max_value)


if __name__ == "__main__":
    mcp.run()  # 默认 stdio：由客户端（agent.py）启动
