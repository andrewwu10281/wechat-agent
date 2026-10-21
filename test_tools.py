"""工具层单元测试：验证 Phase 1 的文件/代码处理能力。

直接跑（不花 API 费用、不碰微信）：
    .venv\\Scripts\\python test_tools.py

覆盖点：
  1. read_file：带行号、offset/limit 分段、超长文件默认只给前 400 行
  2. edit_file：精确匹配 / 忽略空白匹配 / 多处命中报错 / 没命中给近似位置
  3. multi_edit：多处同时改 + 原子性（任一处失败则整批不写入）
  4. write_file：先读后写保护；新文件可直接写
  5. search_text：include 按文件名筛选
  6. run_command：退出码与 stderr 能传回来
"""
import os
import shutil
import sys

from tools import general

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, "_tmp_phase1_test")
REL_DIR = "_tmp_phase1_test"

_passed = 0
_failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}   {detail}")


def _read(rel: str) -> str:
    with open(os.path.join(BASE, rel.replace("/", os.sep)), encoding="utf-8") as f:
        return f.read()


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    general.reset_observed()

    rel = f"{REL_DIR}/sample.py"
    # 造一个 47 行的样例文件
    lines = [f"value_{i} = {i}  # 填充行 {i}" for i in range(1, 41)]
    lines += [
        "def target(a, b):",
        "    return a + b",
        "",
        "DUP = 1",
        "DUP = 1",
        "DUP = 1",
        "TAIL_MARK = 'end'",
    ]
    with open(os.path.join(BASE, REL_DIR, "sample.py"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # 再造一个大文件（450 行），用于验证默认 400 行上限
    big_rel = f"{REL_DIR}/big.py"
    with open(os.path.join(BASE, REL_DIR, "big.py"), "w", encoding="utf-8") as f:
        f.write("\n".join(f"LINE_{i} = {i}" for i in range(1, 451)) + "\n")

    # 再造一个带唯一标记的 py 文件，用于测 include 过滤
    other_rel = f"{REL_DIR}/other.py"
    with open(os.path.join(BASE, REL_DIR, "other.py"), "w", encoding="utf-8") as f:
        f.write("MARKER_UNIQUE_TEXT = 1\n")

    general.reset_observed()

    print("\n== 1. read_file ==")
    r = general.read_file(rel)
    check("返回带行号（含 '1| '）", "1| " in r)
    check("说明总行数", "共 47 行" in r, r.splitlines()[0])
    check("小文件一次读全（不该提示继续读）", "继续读" not in r, r[-80:])
    r = general.read_file(rel, offset=1, limit=10)
    check("limit 生效并提示剩余行数", "还有 37 行" in r, r[-80:])
    tail = general.read_file(rel, offset=41, limit=10)
    check("offset/limit 能读到尾部", "TAIL_MARK" in tail, tail[-120:])
    check("offset 超范围有提示", "超出范围" in general.read_file(rel, offset=999))
    r = general.read_file(big_rel)
    check("大文件默认只给前 400 行", "显示第 1-400 行" in r, r.splitlines()[0])
    check("大文件提示还能继续读", "还有 50 行" in r, r[-80:])

    print("\n== 2. edit_file ==")
    r = general.edit_file(rel, "def target(a, b):", "def target_renamed(a, b):")
    check("精确匹配替换成功", r.startswith("已替换") and "1 处" in r, r)
    r = general.edit_file(rel, "def target_renamed(a, b):\nreturn a + b",
                          "def target_renamed(a, b):\n    return a - b")
    check("缩进不一致也能匹配（fuzzy）", r.startswith("已替换") and "忽略行首尾空白" in r, r)
    r = general.edit_file(rel, "DUP = 1", "DUP = 2")
    check("多处命中被拒绝", r.startswith("错误") and "出现 3 次" in r, r)
    check("拒绝时列出各处行号", "44" in r and "45" in r, r)
    r = general.edit_file(rel, "DUP = 1", "DUP = 9", all=True)
    check("all=true 全部替换", r.startswith("已替换") and "3 处" in r, r)
    r = general.edit_file(rel, "TAIL_MARKK = 'end'", "X")
    check("没命中时给出最接近的位置", "最接近的位置在第" in r, r[:200])
    check("没命中时不会改动文件", "TAIL_MARK = 'end'" in _read(rel))

    print("\n== 3. multi_edit ==")
    r = general.multi_edit(rel, [
        {"old": "value_1 = 1  # 填充行 1", "new": "value_1 = 100  # 改过"},
        {"old": "value_2 = 2  # 填充行 2", "new": "value_2 = 200  # 改过"},
    ])
    after = _read(rel)
    check("两处一起改成功", r.startswith("已在") and "2 处" in r, r)
    check("第一处真的改了", "value_1 = 100" in after)
    check("第二处真的改了", "value_2 = 200" in after)

    before = after
    r = general.multi_edit(rel, [
        {"old": "value_3 = 3  # 填充行 3", "new": "value_3 = 300"},
        {"old": "这段根本不存在的内容", "new": "x"},
    ])
    check("原子性：第二处失败则整体报错", r.startswith("错误：第 2 项"), r[:150])
    check("原子性：文件完全没变", before == _read(rel))

    print("\n== 4. write_file 先读后写保护 ==")
    general.reset_observed()                     # 模拟"本次会话没读过这个文件"
    r = general.write_file(rel, "随便覆盖")
    check("没读过就覆盖被拒绝", r.startswith("错误") and "还没读过" in r, r)
    check("被拒绝后文件没变", "value_1 = 100" in _read(rel))
    general.read_file(rel)                       # 先读
    r = general.write_file(rel, "全新内容\n")
    check("读过之后允许覆盖", r.startswith("已写入"), r)
    r = general.write_file(f"{REL_DIR}/brand_new.txt", "hi")
    check("新文件可直接创建", r.startswith("已写入"), r)

    print("\n== 5. search_text include 筛选 ==")
    r = general.search_text("MARKER_UNIQUE_TEXT", REL_DIR, include="*.txt")
    check("include 过滤掉不匹配文件", ("无文件名匹配" in r or "无匹配" in r) and "other.py" not in r, r[:120])
    r = general.search_text("MARKER_UNIQUE_TEXT", REL_DIR, include="*.py")
    check("include=*.py 能找到目标", "other.py:1" in r, r[:160])
    r = general.search_text("MARKER_UNIQUE_TEXT", REL_DIR)
    check("不传 include 也能找到", "other.py:1" in r, r[:160])

    print("\n== 6. run_command ==")
    r = general.run_command('python -c "import sys; sys.exit(7)"')
    check("非零退出码会回传", "退出码 7" in r, r[:120])
    r = general.run_command('python -c "import sys; sys.stderr.write(\'boom\')"')
    check("stderr 会回传", "boom" in r, r[:120])
    r = general.run_command("")
    check("空命令有提示", "命令为空" in r)

    shutil.rmtree(TMP, ignore_errors=True)
    general.reset_observed()

    print("\n" + "=" * 46)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    print("=" * 46)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
