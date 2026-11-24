"""浏览器搜索工具的测试：全部离线，不需要装 playwright、不联网。

跑法：
    .venv\\Scripts\\python test_browser.py
"""
import json
import os
import sys
import time

from tools import browser, general

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


def reset_state():
    browser._cache.clear()
    browser._last_call = 0.0


def main() -> int:
    orig_audit = general.AUDIT_FILE
    orig_pw = browser._playwright
    orig_impl = browser._search_impl
    orig_web = general.web_search
    general.AUDIT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "_tmp_browser_audit.log")
    reset_state()

    try:
        print("\n== 1. 参数校验 ==")
        r = browser.xiaohongshu_search("")
        check("空关键词有提示", "关键词为空" in r, r[:60])
        r = browser.browser_search("agent", site="zhihu")
        check("不支持的站点有说明", "暂不支持" in r and "xiaohongshu" in r, r[:80])
        # 用假 _playwright 保证这条不依赖本机是否装过 playwright
        browser._playwright = lambda: None
        try:
            r = browser.browser_search("agent", site="xhs")
        finally:
            browser._playwright = orig_pw
        check("xhs 别名可用（未装 playwright 时给指引）", "playwright" in r or "未安装" in r, r[:80])

        print("\n== 2. 纯函数：格式化 ==")
        r = browser._format_notes("agent", [])
        check("空结果给出原因", "没有拿到结果" in r, r[:60])
        notes = [{"title": "Agent 学习路线", "author": "小王", "likes": "1.2万",
                  "url": "https://www.xiaohongshu.com/explore/abc"}]
        r = browser._format_notes("agent", notes)
        check("正常结果含标题", "Agent 学习路线" in r, r[:80])
        check("正常结果含作者与点赞", "小王" in r and "1.2万" in r, r[:80])
        check("正常结果含链接", "explore/abc" in r, r[:80])
        r = browser._format_notes("agent", [{"title": ""}])
        check("缺字段不崩（标题兜底）", "(无标题)" in r, r[:80])

        print("\n== 3. 纯函数：清洗 ==")
        raw = [
            {"title": "正常笔记", "url": "https://x/1"},
            {"title": "", "url": ""},                       # 空卡片，丢弃
            {"title": "扫码登录", "url": "https://x/login"},  # 登录占位，丢弃
            {"title": "", "url": "https://x/2"},             # 有链接，保留
        ]
        out = browser._normalize_notes(raw, 10)
        check("丢掉空卡片", all(n["url"] != "" or n["title"] for n in out), out)
        check("丢掉登录占位", all("登录" not in n["title"] for n in out), out)
        check("保留有链接的卡片", any(n["url"] == "https://x/2" for n in out), out)
        out2 = browser._normalize_notes(raw, 1)
        check("limit 生效", len(out2) == 1, out2)

        print("\n== 4. 未安装 playwright：给指引并降级 ==")
        browser._playwright = lambda: None
        browser._search_impl = orig_impl       # 用真实现，触发"未安装"分支
        general.web_search = lambda q, n=5: "FALLBACK_RESULT"

        def fake_web(q, n=5):
            return "FALLBACK_RESULT"

        general.web_search = fake_web
        r = browser.xiaohongshu_search("agent 开发")
        check("提示未安装 playwright", "未安装" in r, r[:100])
        check("给出安装命令", "playwright install" in r, r[:160])
        check("降级为通用搜索", "FALLBACK_RESULT" in r, r[:160])

        print("\n== 5. 正常路径（替换掉浏览器实现）==")
        browser._playwright = orig_pw
        calls = []

        def fake_impl(kw, limit):
            calls.append((kw, limit))
            return [{"title": f"{kw} 笔记 {i}", "author": "作者", "likes": str(i),
                     "url": f"https://www.xiaohongshu.com/explore/{i}"} for i in range(8)]

        browser._search_impl = fake_impl
        reset_state()
        r = browser.xiaohongshu_search("agent", 3)
        check("结果条数受 limit 限制", r.count("https://www.xiaohongshu.com/explore/") == 3, r[:200])
        check("impl 收到正确参数", calls[-1] == ("agent", 3), calls)
        check("返回里带提示（摘要只是线索）", "线索" in r, r[-80:])

        print("\n== 6. 缓存 ==")
        before = len(calls)
        r2 = browser.xiaohongshu_search("agent", 3)
        check("第二次走缓存", "来自缓存" in r2, r2[-60:])
        check("缓存没有再调 impl", len(calls) == before, calls)

        print("\n== 7. 限频 ==")
        reset_state()
        browser._last_call = time.time()
        r = browser.xiaohongshu_search("另一个词", 3)
        check("太快时被拦住", "风控" in r and "秒后再试" in r, r[:100])
        reset_state()

        print("\n== 8. 抓取失败：如实报错 + 降级 ==")

        def boom(kw, limit):
            raise RuntimeError("触发了滑块验证")

        browser._search_impl = boom
        reset_state()
        r = browser.xiaohongshu_search("agent 现状")
        check("说明失败原因", "滑块验证" in r, r[:120])
        check("降级结果仍在", "FALLBACK_RESULT" in r, r[:200])

        print("\n== 9. 诊断与审计 ==")
        r = browser.browser_status()
        check("browser_status 不崩", "playwright" in r and "限频" in r, r[:80])
        check("browser_status 报告内核", "Chromium 内核" in r, r[:120])
        events = []
        if os.path.exists(general.AUDIT_FILE):
            with open(general.AUDIT_FILE, encoding="utf-8") as f:
                events = [json.loads(x)["event"] for x in f.read().splitlines() if x.strip()]
        check("失败也写审计", "browser_search" in events, events[:10])
    finally:
        browser._playwright = orig_pw
        browser._search_impl = orig_impl
        general.web_search = orig_web
        general.AUDIT_FILE = orig_audit
        reset_state()
        try:
            os.remove(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "_tmp_browser_audit.log"))
        except OSError:
            pass

    print("\n" + "=" * 46)
    print(f"通过 {_passed} 项，失败 {_failed} 项")
    print("=" * 46)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
