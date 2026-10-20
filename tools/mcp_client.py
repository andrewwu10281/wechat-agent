"""最小 MCP 客户端：在后台线程跑 asyncio，把 MCP 工具桥接成同步调用。

为什么要后台线程：
  - MCP 官方 SDK 是异步的；
  - 我们的 agent 是同步的；
  - 所以在后台线程里跑一个 asyncio 事件循环，主线程用
    asyncio.run_coroutine_threadsafe(...) 把调用丢进去。

用法：
    mgr = MCPManager()
    mgr.start([sys.executable, "mcp_servers/demo_server.py"])
    mgr.list_tools()          # 返回 MCP Tool 列表
    mgr.call_tool("now", {})  # 同步调用，返回文本
"""
import asyncio
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPManager:
    def __init__(self):
        self._loop = None
        self._thread = None
        self._session = None
        self._tools = []
        self._ready = threading.Event()
        self._error = None

    def start(self, command, cwd=None, timeout=30):
        """启动 MCP 服务器并完成初始化；失败抛异常。"""
        self._thread = threading.Thread(
            target=self._run_loop, args=(command, cwd), daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError("MCP 服务器启动超时")
        if self._error:
            raise RuntimeError(self._error)

    def _run_loop(self, command, cwd):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._main(command, cwd))
        except Exception as e:
            self._error = str(e)
            self._ready.set()

    async def _main(self, command, cwd):
        try:
            params = StdioServerParameters(
                command=command[0], args=command[1:], cwd=cwd
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.list_tools()
                    self._tools = result.tools
                    self._session = session
                    self._ready.set()
                    # 保持会话存活，直到进程退出
                    await asyncio.Event().wait()
        except Exception as e:
            self._error = str(e)
            self._ready.set()

    def list_tools(self):
        """返回 MCP 服务器暴露的工具列表。"""
        return self._tools

    def call_tool(self, name, arguments=None, timeout=30):
        """同步调用一个 MCP 工具，返回文本结果。"""
        if not self._session or not self._loop:
            raise RuntimeError("MCP 尚未连接")
        fut = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments or {}), self._loop
        )
        result = fut.result(timeout=timeout)
        parts = []
        for item in result.content:
            if getattr(item, "type", "") == "text":
                parts.append(item.text)
            else:
                parts.append(str(item))
        return "\n".join(parts).strip() or "（MCP 工具无输出）"


def to_anthropic_tools(tools, prefix="mcp_"):
    """把 MCP 工具列表转成我们 agent 用的 TOOLS 菜单。"""
    out = []
    for t in tools:
        out.append({
            "name": f"{prefix}{t.name}",
            "description": t.description or f"MCP 工具 {t.name}",
            "input_schema": t.input_schema or {"type": "object", "properties": {}},
        })
    return out
