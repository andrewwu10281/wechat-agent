"""跑通核心逻辑的冒烟测试：先验证大脑 + 通用工具，再接微信。"""
from agent import run

print("=" * 50)
print("测试 1：算数")
print("=" * 50)
print(run("帮我算一下 12345 * 678 + 91 等于多少"))

print("\n" + "=" * 50)
print("测试 2：天气")
print("=" * 50)
print(run("北京今天天气怎么样"))

print("\n" + "=" * 50)
print("测试 3：学术信息（走 web_fetch 调 Semantic Scholar）")
print("=" * 50)
print(run("查一下 Andrew Ng 的学术信息"))

print("\n" + "=" * 50)
print("测试 4：通用工具（列出 .py 文件）")
print("=" * 50)
print(run("列出项目里有哪些 .py 文件"))
