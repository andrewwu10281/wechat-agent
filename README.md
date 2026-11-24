# 微信 AI Agent

一个接入**个人微信**的 AI Agent：用 **DeepSeek**（Claude 兼容接口）当大脑，微信当入口，
通过**工具调用（tool calling）**完成查天气、算数、查学者学术信息等任务。

## 功能（对应验收点）

> 本版本**不注册专用工具**：三个验收任务都由模型**自己组合通用工具**完成。

| 验收点 | 怎么完成 |
|---|---|
| 查天气 | 模型用 `web_fetch` 调免费 Open-Meteo 接口（先地理编码拿经纬度，再查天气） |
| 算数 | 模型用 `run_command` 跑一段脚本，如 `python -c "print(12345*678+91)"` |
| 查学者信息 | 模型用 `web_fetch` 调 Semantic Scholar 接口，返回引用数/h指数/论文 |

## 架构

```
微信消息 ──▶ 官方微信后端（扫码登录 + 长轮询）──▶ agent.py（DeepSeek + 工具循环）──▶ 工具函数
     ▲                                              │
     └──────────────── 回复文本 ◀────────────────────┘
```

- `agent.py`：核心 Agent 循环，调用 DeepSeek 的 Anthropic 兼容接口，自动判断该调哪个工具。
- `tools/general.py`：**通用工具**，本版本唯一注册给模型的一组工具（见下）。
- `tools/weather.py`、`calculator.py`、`scholar.py`：早期三个**专用工具**，**已退役**（不再注册
  给模型），仅作历史保留。想回到带专用工具的旧版本：`git switch -c v1 v1-specialized-tools`。
- `bot.py`：微信接入层，走腾讯官方微信后端协议，扫码登录收发消息。
- `.env`：配置（API key、Scholar ID、代理等），不提交到 git。

## 通用工具

本项目给模型一套**通用能力**，由它自己组合完成任务——这和 Codex / Claude Code 等做法一致。
不为每个需求写专用工具，而是给基础工具、让模型编排。

| 通用工具 | 作用 |
|---|---|
| `run_command` | 跑一条命令并等待结束，返回**结构化结果**（状态 / 退出码 / stdout / stderr 分开）；`timeout` 默认 60s、最长 600s；`cwd` 可指定工作目录 |
| `run_background` | **后台**启动长命令（装依赖、跑测试、构建），立刻返回任务编号，不阻塞 |
| `wait_job` / `check_job` | 等任务结束（推荐，一次拿结果）/ 中途瞄一眼进度和最新输出 |
| `kill_job` / `list_jobs` | 终止任务（连同子进程）/ 列出所有任务 |
| `read_file` | 读文件，**返回带行号的内容**；大文件用 `offset` / `limit` 分段读（默认前 400 行） |
| `write_file` | 整体写入/覆盖文件；**已存在的文件必须先 `read_file` 读过**才允许覆盖 |
| `edit_file` | 改一处：精确匹配优先，不中则**忽略每行首尾空白**再匹配；命中多处会报错并列出各行号 |
| `multi_edit` | 一次改同一个文件的多处，**原子**执行：任一处不匹配就整批不写入 |
| `list_files` / `search_text` | 按通配符列文件 / 按正则全文检索（类 glob / grep），`search_text` 可用 `include="*.py"` 按文件名筛选 |
| `web_search` | **联网搜索**关键词，返回标题 / 网址 / 摘要（Bing 优先、DuckDuckGo 兜底）；拿到网址再用 `web_fetch` 读正文 |
| `web_fetch` | 联网抓取网页文本（可用于检索、查资料等） |
| `send_file` | 把项目里的文件**通过微信发给用户**：凭证类文件（`.env` / `*.key` / `id_rsa`）永久拒绝；其余文件需人工确认；协议不通时自动降级为「文件卡片」（路径 + 大小 + sha256） |
| `browser_search` | 用**浏览器**打开需要登录/JS 渲染的站点搜索（目前支持小红书）：持久化登录态、限频、缓存、风控检测，失败自动降级为 `web_search` |

**超长输出保留「头部 + 尾部」**：命令的报错通常在末尾，只砍尾部会把最有用的信息丢掉。

### 改代码的工作流（写在 system prompt 里，强制模型遵守）

1. **定位**：`search_text`（可加 `include` 筛选）
2. **阅读**：`read_file`，大文件用 `offset` / `limit` 分段，按行号看清楚再动手
3. **修改**：局部改动 `edit_file`；多处改动 `multi_edit`（原子）；整份重写才用 `write_file`
4. **验证**：改完用 `run_command` 跑 `python -m compileall` 或测试脚本，把结果如实回报
   ——耗时长的验证用 `run_background` + `wait_job`，别用 `run_command` 硬等

这套"读—改—验"的纪律，是让它在文件处理/代码任务上接近 Codex 的关键；
配套 `test_tools.py` 覆盖了上述每个行为（`read_file` 分段、`edit_file` 模糊匹配与歧义拒绝、
`multi_edit` 原子性、`write_file` 先读后写、`search_text` 过滤、`run_command` 结构化结果与 cwd、
后台任务的启动/等待/终止、`web_search` 参数与解析器），共 **52 项**。

有了这套底线能力，像"算数""查天气/查资料"这类可组合的问题，模型可以**自己用
`run_command` 跑脚本、用 `web_search` 找网址、用 `web_fetch` 读正文**完成，不必预先写死成专用工具。

## 安全机制（企业级可控）

三档强度，用环境变量控制：`SECURITY_MODE = strict（默认）/ warn / off`。

| 机制 | 行为 |
|---|---|
| **路径围栏** | 命令里出现项目外的路径（`..`、`C:\`、`~`、`%TEMP%`）→ 直接拒绝。文件工具本来就只能在项目内 |
| **破坏性命令永久拒绝** | `rm -rf /`、`mkfs`、`format c:`、`shutdown`、删注册表根等 → 不可通过授权绕过 |
| **危险操作需人工审批** | 删除/移动改名/`git reset --hard`/`git push --force`/卸载包/杀进程/改权限 → 先拦下并把这条命令登记为"待确认"；**批准权在 `bot.py` 这一层，模型没有对应工具、无法自行批准**。你在微信里回复"同意"，命令才被放行一次（且绑定命令指纹，换命令无效） |
| **换个写法也绕不过** | 判定不只看命令字面：真正执行的是不是 `del` / `copy` / `xcopy` / `robocopy`，以及 `python -c "…"` 里有没有 `os.remove` / `Path.unlink` / `open(…,'w')` / `shutil.copy` 这类等价调用，都会纳入审批 |
| **审计日志** | 每次命令、写文件、拦截、待确认、批准/拒绝都追加一行 JSON 到 `audit.log`（已 gitignore）；可问"你刚才干了什么"（`read_audit_log`） |
| **文件外发管控** | 发文件是数据外泄面：`.env`、`.wcbot_session.json`、`*.key`/`*.pem`/`*.p12`、`id_rsa`、`.git/` **永久拒绝（审批也不行）**；其余文件需人工确认，绑定文件指纹、一次性、300 秒过期；审计记录路径 / 大小 / sha256 |

**审批发生在微信对话里**（不是弹窗、也不是模型自己说了算）：模型会说"我要删 X，会有什么后果，你同意吗"，你回"可以"它才执行。

> ⚠️ 诚实边界：**这不是操作系统级沙箱**。路径围栏是字符串层面的检查，`shell=True` 理论上仍可通过
> 编码技巧绕过。真要对外提供服务，应把命令执行放进容器或受限用户，而不是只靠这些检查。

## 待办与提醒

- 说「待办：明天 9 点交报告」→ 记进 `todos.json`；
- 说「我的待办」→ 列出未完成项；
- 说「完成 #3 / 删除 #3」（或「完成交报告」）→ 更新清单；
- 添加待办后会自动把当前列表（含 `#编号`）回给你，不用再问一次；
- bot 主循环每轮检查「到点且未提醒」的待办，主动发微信提醒；
- 待办按用户隔离；`todos.json` 已加入 `.gitignore`（含个人数据，不提交）。

## 长期记忆

- 说「记住：我在准备考研」→ 记进 `memory.json`，跨对话保留；
- 问「你还记得…」或涉及个人情况/偏好时，会自动调用 `recall_facts`；
- 说「忘掉考研」→ 删除匹配的记忆；
- 按用户隔离；`memory.json` 已加入 `.gitignore`。

## MCP 扩展（可插拔工具）

项目内置了一个最小 MCP 示例：

- `mcp_servers/demo_server.py`：暴露 `now` / `word_count` / `random_number` 三个工具；
- `tools/mcp_client.py`：在后台线程跑 asyncio，把 MCP 工具桥接成同步调用；
- `agent.py` 启动时自动连接示例服务器，把工具注册成 `mcp_now` 等，模型可直接调用。

想接自己的 MCP 服务器，在 `.env` 里加一行 `MCP_SERVER_CMD=python 路径\你的服务器.py`；
不设置时默认加载内置示例服务器。

安装依赖：`.venv\Scripts\python.exe -m pip install mcp`（国内可加 `-i https://pypi.tuna.tsinghua.edu.cn/simple`）。

## 环境要求

- Python 3.8+（本机 3.13 已实测）
- Windows / Linux / macOS 均可
## 安装步骤

### 1. 安装 Python 依赖

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

> 用 `python.exe -m pip` 而不是 `pip.exe`：虚拟环境里的 `pip.exe` 这类启动器写死了创建时的解释器绝对路径，
> 项目目录搬过家之后它会失效（`python -m pip` 永远用当前解释器，不受影响）。

### 2. 配置 `.env`

打开 `.env`，确认/填写：

- `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL`：默认已配好（DeepSeek 官方接口）。
- `SCHOLAR_ID`：你自己的 Semantic Scholar 作者 ID（按姓名搜索后返回的那串数字，查"我的 Scholar"用，可选）。

### 3. 启动机器人

```bash
.venv\Scripts\python bot.py
```

首次运行会弹出二维码，用手机微信「扫一扫」扫码并确认授权；登录成功后即开始接收消息。
换号或凭证失效时用 `.venv\Scripts\python bot.py --relogin` 重新登录。

### 4. 自测（不碰微信、先验大脑和工具）

```bash
# 工具层单元测试：不花 API 费用，验证读写改文件的每个行为
.venv\Scripts\python test_tools.py
.venv\Scripts\python test_context.py
.venv\Scripts\python test_security.py
.venv\Scripts\python test_media.py
.venv\Scripts\python test_browser.py

# 浏览器搜索（小红书）需要额外装一次：
# .venv\Scripts\python -m pip install playwright
# 内核装到项目内（本机 %LOCALAPPDATA% 无写权限，直接 install 会 EPERM）：
.venv\Scripts\python -m tools.browser install

# 端到端冒烟测试：会真实调用 DeepSeek，验证算数/天气/学术/通用工具
.venv\Scripts\python smoke_test.py
```


## 微信接入说明

- 登录凭证保存在 `.wcbot_session.json`（已加入 `.gitignore`）。
- 支持多轮对话记忆：按**token 预算**（默认 8000）保留历史，超预算时把更早的对话**压成摘要**
  并放进 system prompt，因此聊很久也不会"失忆"；能理解「刚才那个」这类指代。
- 同一条消息重复投递会被去重，只回复一次；并忽略自己发出的消息，避免自回复。
- 工具调用最多 20 轮；同一工具 + 同一参数重复太多次会被拦截，防止死循环。
- **空回复兜底**：模型只返回空白（或干脆没有文本）时，会用一句人话兜底，绝不给你发空消息
  （微信端空白消息既没意义，也容易触发风控）。
- **发文件**：说「把这个文件发我」时，模型会调用 `send_file`；**凭证类文件永久拒绝**，
  其余文件需要你回复「同意」才发。传输层按官方插件 `openclaw-weixin` 的真实协议实现：
  `getuploadurl` 申请参数 → **AES-128-ECB 加密后上传到微信 CDN** → `sendmessage` 发
  `file_item`（`media.encrypt_query_param` 引用 CDN 参数）。上传或发送失败时自动降级为
  「文件卡片」（路径 + 大小 + sha256）。
  可用 `ILINK_CDN_BASE_URL` 覆盖 CDN 域名；`ILINK_MEDIA_TYPE` / `ILINK_FILE_ITEM_TYPE` /
  `ILINK_FILE_ITEM_KEY` 仅用于排查图片/视频形态。验证链路：
  `python probe_send_file.py`（只读打印计划），确认后 `--send 文件名` 真发一次。
- 调试群消息：设置环境变量 `DEBUG_MSG=1` 后启动 bot，会把收到的每条原始消息打印到日志，
  方便观察群聊字段结构（仅本地调试用；日志含消息内容，勿外传）。

## 浏览器搜索（小红书）

`browser_search` 用 Playwright 开真浏览器，能拿到"必须登录才能看"的内容（小红书、知乎这类）。

```bash
# 1) 装库（只需一次）
.venv\Scripts\python -m pip install playwright
# 2) 装 Chromium 内核（装到项目内的 .ms-playwright/，避开 %LOCALAPPDATA% 的 EPERM）
.venv\Scripts\python -m tools.browser install
# 3) 看状态：playwright / 内核 / 登录态
.venv\Scripts\python -m tools.browser status
# 4) 首次扫码登录（会弹出浏览器，扫码后回终端按回车）
.venv\Scripts\python -m tools.browser login
# 5) 不经过微信，真搜一次
.venv\Scripts\python -m tools.browser search "agent 学习"
```

网络慢导致内核下载失败时，先设镜像再跑第 2 步（注意：这是 **PowerShell** 写法）：

```powershell
$env:PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"
```

如果你用的是 cmd.exe，对应写法是：

```cmd
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
```

- **首次要人工登录**：`BROWSER_HEADLESS=0`（默认）跑一次，弹出的浏览器里扫码登录，
  cookie 存在 `.browser_profile/`（已 gitignore），之后免登录；过期了再人工过一次。
- **临时目录**：Playwright 安装/启动时的临时目录统一放项目内 `.tmp_pw/`（已 gitignore），
  因为本机系统 `%TEMP%` 不可写；要换位置可设 `XHS_TMP_DIR`。万一仍报
  `EPERM ... playwright-artifacts`，先确认该目录可写再重试。
- **限频与缓存**：`XHS_MIN_INTERVAL`（默认 30 秒）、`XHS_CACHE_TTL`（默认 600 秒）。
  别调太低，会触发风控。
- **失败会降级**：登录墙 / 滑块 / 选择器失效 → 明确报错 + 退回 `web_search`，
  并在回复里说明"这是通用搜索的结果"。
- **合规提醒**：只做低频、个人用途的检索，不做批量抓取。

## 风险提示

- 查学者信息走 Semantic Scholar 免费 API，免代理、国内可直连；免费额度偶尔会限流（429），模型遇到失败会自行重试。
- `web_search` 走 Bing（国内可直连）与 DuckDuckGo（境外更稳），解析的是搜索结果页 HTML：
  页面改版可能导致解析不到结果，此时会**如实报"搜索不可用"**，不会编造内容。
- 通用工具（尤其 `run_command`）权限较大，请只在可信环境使用。
- 建议用小号测试，控制发送频率。
