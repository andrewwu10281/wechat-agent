"""安全机制测试：路径围栏 / 危险命令审批 / 审计日志。

直接跑（不花 API 费用）：
    .venv\\Scripts\\python test_security.py
"""
import json
import os
import shutil
import sys

from tools import general

BASE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(BASE, "_tmp_sec_test")

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


def audit_lines():
    if not os.path.exists(general.AUDIT_FILE):
        return []
    with open(general.AUDIT_FILE, encoding="utf-8") as f:
        return [json.loads(x) for x in f.read().splitlines() if x.strip()]


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    general.reset_observed()

    # 审计日志改到临时目录，别污染真实 audit.log
    orig_audit = general.AUDIT_FILE
    general.AUDIT_FILE = os.path.join(TMP, "audit.log")
    orig_mode = general.SECURITY_MODE
    general.SECURITY_MODE = "strict"

    try:
        print("\n== 1. 默认模式 ==")
        check("默认是 strict", orig_mode == "strict", orig_mode)

        print("\n== 2. 破坏性命令：永久拒绝 ==")
        for cmd, label in [("rm -rf /", "rm -rf /"),
                          ("mkfs.ext4 /dev/sda", "mkfs"),
                          ("format c:", "format c:"),
                          ("shutdown -h now", "shutdown")]:
            r = general.run_command(cmd)
            check(f"{label} 被拒绝", r.startswith("错误") and "永久拒绝" in r, r[:100])

        print("\n== 3. 危险操作：需要用户审批 ==")
        target = "_tmp_sec_test/doomed.txt"
        target_cmd = target.replace("/", "\\")      # Windows 的 del 只认反斜杠
        with open(os.path.join(BASE, target), "w", encoding="utf-8") as f:
            f.write("x")
        r = general.run_command(f'del /q "{target_cmd}"')
        check("未审批时被拦下", "需要用户确认" in r, r[:150])
        check("说明里要求问用户", "向用户说明" in r or "明确同意" in r, r[:200])
        check("文件还在（没被执行）", os.path.exists(os.path.join(BASE, target)))
        r = general.run_command(f'del /q "{target_cmd}"', approve=True)
        check("审批后不再拦", "需要用户确认" not in r, r[:150])
        check("审批后文件被删掉", not os.path.exists(os.path.join(BASE, target)), r[:150])

        print("\n== 4. 路径围栏：命令不能跳出项目目录 ==")
        for cmd, label in [("python ..\\outside.py", "..\\ 越界"),
                          ("type C:\\Windows\\win.ini", "绝对路径 C:\\"),
                          ("dir %TEMP%", "%TEMP% 变量"),
                          ("cd .. && dir", "cd ..")]:
            r = general.run_command(cmd)
            check(f"{label} 被拒绝", r.startswith("错误") and "超出了项目目录" in r, r[:120])

        print("\n== 5. 项目内的正常命令不受影响 ==")
        r = general.run_command('python -c "print(6*7)"')
        check("项目内命令正常执行", "成功（退出码 0）" in r and "42" in r, r[:150])
        r = general.run_command("python -m compileall tools/general.py")
        check("解析项目内相对路径没问题", "超出" not in r, r[:150])

        print("\n== 6. warn / off 模式 ==")
        general.SECURITY_MODE = "warn"
        r = general.run_command('python ..\\outside.py')
        check("warn 模式不拦越界（但仍记录）", "超出了项目目录" not in r, r[:100])
        general.SECURITY_MODE = "off"
        r = general.run_command('del /q no_such_file.txt')
        check("off 模式不拦危险操作", "需要用户确认" not in r, r[:100])
        general.SECURITY_MODE = "strict"
        r = general.run_command('del /q no_such_file.txt')
        check("切回 strict 又拦住了", "需要用户确认" in r, r[:100])

        print("\n== 7. 后台命令也受同样约束 ==")
        r = general.run_background("rm -rf /")
        check("后台破坏性命令被拒绝", "永久拒绝" in r, r[:100])
        r = general.run_background('del /q "x.txt"')
        check("后台危险操作要审批", "需要用户确认" in r, r[:120])
        r = general.run_background('python -c "import time; time.sleep(0.1)"')
        check("后台正常命令可启动", "已在后台启动" in r, r[:120])

        print("\n== 8. 审计日志 ==")
        # 造一次写文件 + 一次被拦
        general.write_file("_tmp_sec_test/w.txt", "hello")
        general.run_command("rm -rf /")
        recs = audit_lines()
        events = [r["event"] for r in recs]
        check("记录了命令执行", "command" in events, events[:10])
        check("记录了文件写入", "write_file" in events, events[:10])
        check("记录了拦截事件", "blocked" in events, events[:10])
        check("记录了待审批事件", "needs-approval" in events, events[:10])
        check("记录了后台任务", "background" in events, events[:10])
        blocked = [r for r in recs if r["event"] == "blocked"]
        check("拦截记录带原因", any(r.get("reason") for r in blocked), blocked[:1])
        check("记录带时间戳", all(r.get("ts") for r in recs))
        check("记录带用户", any(r.get("user") for r in recs), recs[:1])

        r = general.read_audit_log(5)
        check("read_audit_log 能读", "audit.log 共" in r, r[:120])
        r = general.read_audit_log(10, "blocked")
        check("read_audit_log 可按事件过滤", "blocked" in r, r[:160])
        check("过滤生效（不含 write_file）", "write_file" not in r, r[:160])
    finally:
        general.AUDIT_FILE = orig_audit
        general.SECURITY_MODE = orig_mode
        shutil.rmtree(TMP, ignore_errors=True)
        general.reset_observed()

    print("\n" + "=" * 46)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    print("=" * 46)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
