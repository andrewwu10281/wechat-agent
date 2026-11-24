"""浏览器搜索工具：让 agent 能自己打开网页、抓取需要登录才能看的内容。

为什么需要它：
  `web_fetch` 只会发 HTTP 请求，遇到"要登录 / 要 JS 渲染"的站点（小红书、知乎、微博）
  只能拿到登录页。浏览器自动化是这两类站点的唯一现实解法。

设计要点：
  1. **软依赖**：playwright 没装时，工具返回安装指引并降级到 web_search，
     不能让"没装库"把整轮对话搞崩（和 MCP 一样的思路）。
  2. **持久化登录态**：用 launch_persistent_context 把 cookie 存在本地目录，
     第一次人工扫码登录，之后免登录；过期了再人工过一次。
  3. **限频 + 缓存**：同一关键词 XHS_CACHE_TTL 内走缓存；两次请求至少间隔
     XHS_MIN_INTERVAL 秒——这类站点高频访问会触发风控。
  4. **失败降级**：被风控 / 选择器失效 / 超时 → 明确告知，并退回 web_search。
  5. **解析与抓取分离**：`_format_notes()` 是纯函数，可用固定数据离线测试，
     不依赖浏览器（和 web_search 的 `_parse_bing_html` 一个思路）。

环境变量：
  BROWSER_HEADLESS   1=无头（快，但更易被识别）0=有头（默认，首次登录必须）
  XHS_MIN_INTERVAL   两次搜索最小间隔秒数，默认 30
  XHS_CACHE_TTL      搜索结果缓存秒数，默认 600
  XHS_PROFILE_DIR    登录态目录，默认 .browser_profile（已 gitignore）
  XHS_TIMEOUT_MS     单次等待超时，默认 20000
  PLAYWRIGHT_BROWSERS_PATH  Chromium 内核目录，默认 <项目>/.ms-playwright
                      （本机 %LOCALAPPDATA% 建目录会被拒绝，所以装在项目内）
"""
import os
import threading
import time

from tools import general

_DEFAULT_INTERVAL = 30          # 秒
_DEFAULT_CACHE_TTL = 600        # 秒
_DEFAULT_TIMEOUT = 20000        # 毫秒
_MAX_LIMIT = 10

SEARCH_URL = "https://www.xiaohongshu.com/search_result?keyword={kw}"

# 这台机器上 %LOCALAPPDATA% 无法创建目录（EPERM），Chromium 默认装到项目内；
# 部署到别的机器时可用 PLAYWRIGHT_BROWSERS_PATH 覆盖。
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",
                      os.path.join(general.WORKSPACE, ".ms-playwright"))

_lock = threading.Lock()
_last_call = 0.0                # 上一次真正发起搜索的时间
_cache: dict = {}               # keyword -> (时间戳, 结果列表)


# ---------------------------------------------------------------------------
# 依赖与配置
# ---------------------------------------------------------------------------
def _playwright():
    """软依赖：没装就返回 None，由调用方给出安装指引。"""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        return None


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _browsers_path() -> str:
    """Chromium 内核目录（项目内，可用环境变量覆盖）。"""
    return os.environ["PLAYWRIGHT_BROWSERS_PATH"]


def _looks_like_url(value: str) -> bool:
    """`https://...` 这类值不是合法目录名（冒号在 Windows 路径里非法）。"""
    return "://" in (value or "")


def _chromium_ready() -> bool:
    """内核目录里是否已经有可执行文件；只看目录结构，不启动浏览器。"""
    import glob as _glob
    # 新版 Chrome for Testing 是 chrome-win64/，旧版是 chrome-win/
    pattern = os.path.join(_browsers_path(), "chromium-*", "chrome-*", "chrome.exe")
    return bool(_glob.glob(pattern))


def _ensure_temp_dir() -> str:
    """Playwright 启动时要建临时目录；本机系统 Temp 不可写，统一放项目内 .tmp_pw/。"""
    import tempfile

    path = os.getenv("XHS_TMP_DIR") or os.path.join(general.WORKSPACE, ".tmp_pw")
    if not os.path.isabs(path):
        path = os.path.join(general.WORKSPACE, path)
    os.makedirs(path, exist_ok=True)
    tempfile.tempdir = path      # Python 侧：mkdtemp/gettempdir
    os.environ["TMP"] = path     # Node 子进程侧
    os.environ["TEMP"] = path
    return path


def browser_status() -> str:
    """诊断用：告诉用户浏览器能力现在是什么状态。"""
    pw = _playwright()
    has = "已安装" if pw else "未安装"
    chrome = "已安装" if _chromium_ready() else "未安装"
    path = _browsers_path()
    path_note = "  ⚠ 这像网址，请改成目录" if _looks_like_url(path) else ""
    return (f"playwright: {has}\n"
            f"Chromium 内核: {chrome}（{path}）{path_note}\n"
            f"登录态目录: {os.getenv('XHS_PROFILE_DIR', '.browser_profile')}\n"
            f"限频: {_env_int('XHS_MIN_INTERVAL', _DEFAULT_INTERVAL)} 秒/次\n"
            f"缓存: {_env_int('XHS_CACHE_TTL', _DEFAULT_CACHE_TTL)} 秒\n"
            f"有头模式: {'否（headless）' if os.getenv('BROWSER_HEADLESS') == '1' else '是'}")


# ---------------------------------------------------------------------------
# 纯函数：格式化（可离线测试）
# ---------------------------------------------------------------------------
def _format_notes(keyword: str, notes: list) -> str:
    """把抓到的笔记列表变成模型能读的文本。空字段也要能扛住。"""
    if not notes:
        return (f"搜索「{keyword}」没有拿到结果：可能被风控（出现了验证码/登录墙），"
                f"或页面结构变了。建议稍后再试，或改用 web_search。")
    lines = [f"小红书搜索「{keyword}」，共 {len(notes)} 条："]
    for i, n in enumerate(notes, 1):
        title = (n.get("title") or "(无标题)").strip()
        author = (n.get("author") or "").strip()
        likes = (n.get("likes") or "").strip()
        url = (n.get("url") or "").strip()
        lines.append(f"{i}. {title}")
        if author or likes:
            lines.append(f"   作者：{author or '未知'}" + (f"｜点赞：{likes}" if likes else ""))
        if url:
            lines.append(f"   链接：{url}")
    lines.append("（笔记内容质量参差，摘要只能当线索；要点进去看原文再下结论。）")
    return "\n".join(lines)


def _normalize_notes(raw: list, limit: int) -> list:
    """裁剪条数 + 丢掉完全没有标题和链接的脏数据。"""
    out = []
    for n in raw or []:
        if not isinstance(n, dict):
            continue
        title = (n.get("title") or "").strip()
        url = (n.get("url") or "").strip()
        # 小红书卡片里常混入"登录/广告"节点，没有标题也没有链接的直接丢
        if not title and not url:
            continue
        if "登录" in title or "扫码" in title:
            continue
        out.append({"title": title, "author": (n.get("author") or "").strip(),
                    "likes": (n.get("likes") or "").strip(), "url": url})
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 真正的浏览器抓取（只有装了 playwright 才会走到这里）
# ---------------------------------------------------------------------------
_JS_COLLECT = """
() => {
  const out = [];
  const nodes = document.querySelectorAll('section.note-item, div.note-item, .feeds-page .note-item');
  for (const n of nodes) {
    const t = n.querySelector('.title span, .footer .title, a.title');
    const au = n.querySelector('.author .name, .author-wrapper .name');
    const lk = n.querySelector('.like-wrapper .count');
    let url = '';
    try {
      const a = n.querySelector('a[href*="/explore/"], a[href*="/discovery/item/"]');
      if (a) url = a.href || new URL(a.getAttribute('href'), location.origin).href;
      if (!url) {
        // 少数版式把链接放在卡片外层，用 note-id 兜底拼原文地址
        const id = n.getAttribute('data-note-id') || (n.dataset && n.dataset.noteId) || '';
        if (id) url = 'https://www.xiaohongshu.com/explore/' + id;
      }
    } catch (e) { url = ''; }
    out.push({
      title: (t && t.textContent || '').trim(),
      author: (au && au.textContent || '').trim(),
      likes: (lk && lk.textContent || '').trim(),
      url: url
    });
  }
  return out;
}
"""


def _search_impl(keyword: str, limit: int) -> list:
    """开浏览器搜一次，返回原始笔记列表。失败抛异常，由上层决定降级。"""
    _ensure_temp_dir()
    sync_playwright = _playwright()
    if sync_playwright is None:
        raise RuntimeError("未安装 playwright（pip install playwright && playwright install chromium）")
    if not _chromium_ready():
        raise RuntimeError("Chromium 内核未安装：请运行 .venv\\Scripts\\python -m playwright install chromium"
                           "（或 .venv\\Scripts\\python -m tools.browser install）")

    profile = _profile_dir()
    headless = os.getenv("BROWSER_HEADLESS", "0") == "1"
    timeout = _env_int("XHS_TIMEOUT_MS", _DEFAULT_TIMEOUT)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(SEARCH_URL.format(kw=keyword), wait_until="domcontentloaded", timeout=timeout)
            # 登录墙 / 风控：宁可如实报错，也不要返回一堆"登录"占位
            body = page.inner_text("body")[:2000]
            if "扫码登录" in body or "请登录" in body or "/login" in page.url:
                raise RuntimeError("需要登录（登录态过期或首次使用）：请以有头模式打开一次并人工扫码")
            if "验证" in body and "拖动" in body:
                raise RuntimeError("触发了滑块验证：请人工过一次，或降低频率")
            page.wait_for_selector("section.note-item", timeout=timeout)
            # 滚两屏，让懒加载的卡片出来
            for _ in range(2):
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(800)
            return page.evaluate(_JS_COLLECT) or []
        finally:
            ctx.close()


# ---------------------------------------------------------------------------
# 对外主函数（注册成工具）
# ---------------------------------------------------------------------------
def xiaohongshu_search(keyword: str, limit: int = 5) -> str:
    """在小红书搜索关键词，返回标题/作者/点赞/链接（限频 + 缓存 + 降级）。"""
    global _last_call          # 下面会赋值，必须声明，否则会被当成局部变量（踩过一次）
    keyword = (keyword or "").strip()
    if not keyword:
        return "错误：搜索关键词为空。"
    try:
        limit = int(limit)
    except Exception:
        limit = 5
    limit = max(1, min(limit, _MAX_LIMIT))

    interval = _env_int("XHS_MIN_INTERVAL", _DEFAULT_INTERVAL)
    ttl = _env_int("XHS_CACHE_TTL", _DEFAULT_CACHE_TTL)

    with _lock:
        now = time.time()
        cached = _cache.get(keyword)
        if cached and now - cached[0] < ttl:
            return _format_notes(keyword, cached[1][:limit]) + "\n（来自缓存）"
        if now - _last_call < interval:
            wait = int(interval - (now - _last_call))
            return (f"错误：上一次搜索才过了 {int(now - _last_call)} 秒，"
                    f"为防止触发风控，请 {wait} 秒后再试。")

        try:
            notes = _normalize_notes(_search_impl(keyword, limit), limit)
        except Exception as e:
            # 降级：用通用搜索兜底，并如实说明失败原因
            general._audit("browser_search", query=keyword, ok=False, error=str(e)[:150])
            fallback = general.web_search(f"{keyword} 小红书", 5)
            return (f"小红书搜索失败：{e}\n"
                    f"已降级为通用搜索（结果质量可能差一些）：\n{fallback}")
        _last_call = time.time()
        _cache[keyword] = (_last_call, notes)

    general._audit("browser_search", query=keyword, ok=True, results=len(notes),
                   links=sum(1 for n in notes if n.get("url")))
    return _format_notes(keyword, notes)


def browser_search(query: str, site: str = "xiaohongshu", limit: int = 5) -> str:
    """统一入口：目前支持 site=xiaohongshu。以后加知乎/微博在这里扩。"""
    site = (site or "xiaohongshu").strip().lower()
    if site in ("xiaohongshu", "xhs", "小红书"):
        return xiaohongshu_search(query, limit)
    return (f"错误：暂不支持 site={site}（目前只支持 xiaohongshu）。"
            f"需要抓网页可以用 web_fetch。")


# ---------------------------------------------------------------------------
# 命令行：验证/排障用（bot 里用不到，但"手动验一次"时非常有用）
#   python -m tools.browser status      # 看依赖、内核、登录态目录、限频
#   python -m tools.browser install     # 下载 Chromium 内核（装到项目内）
#   python -m tools.browser login       # 开有头浏览器，人工扫码，把登录态存下来
#   python -m tools.browser search 关键词
# ---------------------------------------------------------------------------
def _cli() -> int:
    import sys
    args = sys.argv[1:]
    cmd = (args[0] if args else "status").lower()

    if cmd == "status":
        print(browser_status())
        print(f"登录态目录是否存在: {os.path.isdir(_profile_dir())}")
        return 0

    if cmd == "install":
        browsers = _browsers_path()
        mirror = os.getenv("PLAYWRIGHT_DOWNLOAD_HOST", "")
        if _looks_like_url(browsers):
            print(f"PLAYWRIGHT_BROWSERS_PATH 被设成了网址：{browsers}")
            print("这个变量必须是目录（默认 <项目>\\.ms-playwright）；")
            print("下载镜像要设 PLAYWRIGHT_DOWNLOAD_HOST，不要设到 BROWSERS_PATH。")
            return 2
        if mirror and not mirror.startswith(("http://", "https://")):
            print(f"PLAYWRIGHT_DOWNLOAD_HOST 看起来不是网址：{mirror}")
            print("正确写法：https://npmmirror.com/mirrors/playwright")
            return 2
        # 下载/解压临时目录强制放项目内：这台机器的系统 Temp 同样会 EPERM
        _ensure_temp_dir()
        print(f"安装 Chromium 内核到 {browsers} ...")
        print(f"下载镜像: {mirror or '官方 CDN（未设置 PLAYWRIGHT_DOWNLOAD_HOST）'}")
        import subprocess
        try:
            rc = subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
        except OSError as e:
            print(f"启动安装程序失败：{e}")
            return 2
        if rc == 0:
            print("完成。可以用 python -m tools.browser status 复查。")
        else:
            print(f"安装失败（退出码 {rc}）。网络慢时可先设置镜像再重试：")
            print('  $env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"')
        return rc

    if cmd == "login":
        pw = _playwright()
        if pw is None:
            print("还没装浏览器依赖，先跑：")
            print("  .venv\\Scripts\\python.exe -m pip install playwright")
            print("  .venv\\Scripts\\python.exe -m playwright install chromium")
            return 1
        profile = _profile_dir()
        _ensure_temp_dir()
        with pw() as p:
            ctx = p.chromium.launch_persistent_context(profile, headless=False, locale="zh-CN")
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded", timeout=30000)
            print("浏览器已打开：请在里面扫码登录，登录成功后再回来按回车。")
            try:
                input()
            except EOFError:
                pass
            ctx.close()
        print(f"登录态已保存到：{profile}")
        return 0

    if cmd == "search":
        kw = args[1] if len(args) > 1 else "agent"
        print(xiaohongshu_search(kw, 5))
        return 0

    print("用法：python -m tools.browser [status|install|login|search 关键词]")
    return 1


def _profile_dir() -> str:
    profile = os.getenv("XHS_PROFILE_DIR", ".browser_profile")
    return profile if os.path.isabs(profile) else os.path.join(general.WORKSPACE, profile)


if __name__ == "__main__":
    raise SystemExit(_cli())
