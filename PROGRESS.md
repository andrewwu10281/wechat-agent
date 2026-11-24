# 微信 AI Agent 项目 —— 进度记录

> 本科实践课项目：写一个接入个人微信的 agent，用 DeepSeek 当大脑。
> 验收点：查天气、算数、查 Google Scholar。
> 定位演进：不再只是验收项目，目标做成**可日常使用的个人助理**（对标 Codex 的文件/代码能力）。
> 最近更新：2026-10-21

## 项目位置
`C:\Users\lenovo\projects\wechat-agent`

> 2026-10-21：从 `C:\Users\lenovo\wechat-agent` 迁入 DSH 工作区 `C:\Users\lenovo\projects\`
> （4783 个文件逐项校验一致、关键文件哈希相同后，原目录已删除）。迁入后可在 DSH 内直接开发、跑三套测试。
> 代码侧不受影响：所有路径都由 `__file__` 推导，没有硬编码。
> ⚠️ 唯一副作用：`.venv` 里 `pip.exe` 这类 console script 写死了旧解释器路径，已失效 ——
> 装包一律用 `.venv\Scripts\python.exe -m pip ...`（README / 原理笔记已同步）。
> 另：在 DSH 沙箱内 MCP 子进程起不来（命名管道被禁，`WinError 5`），agent 会自动降级为 21 个本地工具。

## 技术栈
- Python 3.13（虚拟环境 `.venv/`）
- LLM：DeepSeek Anthropic 兼容端点 `https://api.deepseek.com/anthropic`，模型 `deepseek-v4-pro`，key 在 `.env`
- 微信接入：**腾讯官方微信后端** `https://ilinkai.weixin.qq.com`（openclaw-weixin 同款协议：扫码登录 + 长轮询收发）
- 天气：Open-Meteo；Scholar：Semantic Scholar 官方 API（免代理、国内直连）

## ✅ 已完成
1. `agent.py` 核心循环（最初为三个专用工具）—— 已跑通（`smoke_test.py`）
2. `bot.py` 已从 wcferry 换成官方后端协议：
   - 二维码登录（`get_bot_qrcode` → 长轮询 `get_qrcode_status`）
   - 收发消息（`getupdates` 长轮询 / `sendmessage`），回传 `context_token`
   - 凭证持久化到 `.wcbot_session.json`，`--relogin` 重新登录
3. `requirements.txt` 移除 wcferry、加 `qrcode[pil]`；`run.bat` 去掉管理员提权
4. **Scholar 工具从 Google Scholar 抓取换成 Semantic Scholar 官方 API**（2026-10-05）：
   - 原因：Google Scholar 对代理出口 IP 反爬太严（实测翻墙后仍弹 CAPTCHA/机器人验证）
   - Semantic Scholar 免注册、免代理、国内直连可用，返回总引用/h指数/论文数/高被引论文
   - 内置 429/5xx 退避重试；`.env` 移除 `SCHOLAR_PROXY`，`SCHOLAR_ID` 改为 Semantic Scholar 作者 ID
5. **从「三个专用工具的壳」升级为「通用能力的壳」（harness 底座）**（2026-10-14）：
   - 新增 `tools/general.py`，给模型一套通用工具：`run_command`（运行命令）、
     `read_file` / `write_file` / `edit_file`（读写改文件）、`list_files` / `search_text`
     （列文件 / 全文检索）、`web_fetch`（联网抓取网页）
   - `agent.py` 的 `TOOLS` 菜单和 `_exec` 分发器接入以上工具；`SYSTEM` 提示改为
     "优先自己组合通用工具完成任务"。原三个专用工具保留，可快速搞定对应需求
   - 设计理念：不要为每个需求写专用工具，而要给模型基础能力、让它自行组合（与 Codex /
     Claude Code 等真实 harness 一致）；更多工具可后续通过 MCP 方式注册
   - 已实测：`agent.py` 能自行调用 `run_command` 算数、`list_files` 列文件，未用到专用工具
   - ⚠️ 安全：`run_command` 能在项目目录下执行命令，文件操作限制在项目目录内，
     但**不是真正的沙箱**；请勿把带 `run_command` 的机器人暴露给不信任的人
6. **退役三个专用工具，改为「通用工具干活」的新版本**（2026-10-15）：
   - `agent.py` 的 `TOOLS` 菜单和 `_exec` 分发器里**移除** weather / calculator / scholar 三项；
     `SYSTEM` 提示改为"用通用工具自己组合完成"，并附上 Open-Meteo / Semantic Scholar 的调用方式
   - 三个专用工具的**文件仍保留**在 `tools/`，仅作为历史参考（不再注册给模型）
   - 旧版本已打标签 `v1-specialized-tools`，随时可找回：`git checkout v1-specialized-tools`
   - 实测：问"北京天气"，模型自动用 `web_fetch` 调 Open-Meteo 完成，未使用专用工具
   - ⚠️ 代价：通用工具做这些任务**更慢**（要多次联网、自行解析），可靠性也略低；
     追求稳/快的话可切回旧版本
7. **多轮记忆 / 防编造 / 消息去重**（2026-10-17）：
   - `bot.py`：按用户维护对话历史，改用 `run(text, hist)`，只保留最近 `MAX_HISTORY = 20` 条
     （约 10 轮）。此前微信端调 `run(text)` 不带历史，实际是「无记忆」的
   - 消息去重：用 `msg_id/server_id/client_id` 记录已处理消息，重复投递只回一次；
     并忽略 `from_user_id == 自己 bot id` 的消息，防止自回复回环
   - `agent.py` 的 `SYSTEM`：工具失败/超时/空结果时必须如实告知「服务暂时不可用」，
     禁止编造论文、引用数、链接等
8. **新增待办 / 提醒功能**（2026-10-20）：
   - 新增 `tools/todos.py`（按用户读写 `todos.json`）和 `tools/context.py`（记录当前用户）
   - 新增 4 个工具：`add_todo` / `list_todos` / `complete_todo` / `delete_todo`
   - `agent.py` 的系统提示中加入「当前时间」，模型能把「明天9点」换算成 ISO 时间
   - `bot.py` 主循环每轮检查到点且未提醒的待办，主动 `send_text` 提醒
   - 实测：模型正确把「明天早上9点交报告」解析为 `2026-10-21T09:00` 并写入；查询正常；到点判定正常
9. **新增长期记忆**（2026-10-20）：
   - 新增 `tools/memory.py`：按用户读写 `memory.json`
   - 新增 3 个工具：`remember_fact` / `recall_facts` / `forget_fact`
   - `agent.py` 系统提示：说「记住…」用 `remember_fact`；问个人情况先 `recall_facts`
   - 实测：记住「在准备考研」后，再问「你还记得我在准备什么吗」能正确回忆
10. **接入 MCP（Model Context Protocol）**（2026-10-20）：
   - 安装 `mcp>=2.0`
   - 新增 `mcp_servers/demo_server.py`（now / word_count / random_number）
   - 新增 `tools/mcp_client.py`：后台 asyncio 线程 + 同步桥接
   - `agent.py` 启动时自动连接 MCP 服务器，把工具注册为 `mcp_*` 并接入 `_exec`
   - 实测：模型能自行调用 `mcp_now` 并返回时间
11. **健壮性收尾：轮数保护 / 重复调用拦截 / 空回复兜底 / 调试开关**（2026-10-20）：
   - `agent.py`：`MAX_TOOL_ROUNDS = 20`、`MAX_REPEAT_CALLS = 3`；同一工具+同一参数超过 3 次就拦截
   - `agent.py`：模型返回空文本时给兜底提示；每轮打印工具调用日志
   - `bot.py`：发送前再兜一次空回复；`DEBUG_MSG=1` 可打印收到的原始消息
   - 实测：天气任务两轮工具调用完成，不再撞 8 轮上限
12. **Phase 1：文件/代码处理能力对齐 Codex**（2026-10-21）：
   - 背景：老师指出"文件处理、代码能力不如 Codex"是痛点。定性为**工具层与 harness 纪律**的差距，不是模型能力差距
   - 发现问题（实测）：`read_file` 只有 `max_chars`、无行号无分段——`agent.py`（12357 字符）只返回 8021 字符
     **尾部永远读不到**；`edit_file` 是裸字符串替换，无先读后写、无歧义保护、无失败诊断；`write_file` 直接覆盖
   - `tools/general.py` 改造：
     - `read_file(path, offset=1, limit=400)`：**返回带行号内容**，分段读大文件，并提示"还有 N 行"
     - `edit_file(path, old, new, all=False)`：精确匹配优先，不中则**忽略每行首尾空白**匹配；
       命中多处 → 报错并列出各行号；完全没命中 → 用 difflib 给出**最接近的位置（带行号）**
     - 新增 `multi_edit(path, edits)`：一个文件多处改动，**原子**执行（任一处失败则整批不写入）
     - `write_file`：**先读后写保护**——已存在文件必须先 `read_file` 读过，且之后没被改动，才允许覆盖
     - `run_command`：超时默认 60s、上限 600s，超时提示"拆小步骤或调大 timeout"
     - `search_text(pattern, path, include)`：新增 `include` 按文件名通配符筛选（如 `*.py`）
   - `agent.py`：`max_tokens` 2048 → **8192**；`TOOLS` 菜单同步新参数 + 新增 `multi_edit`；
     `SYSTEM` 加入**强制工作流**：定位(search) → 阅读(read，按行号) → 修改(edit/multi_edit) → 验证(run_command)
   - 新增 `test_tools.py`：**30 项单元测试全部通过**，覆盖分段读、模糊匹配、歧义拒绝、近似定位、
     multi_edit 原子性、先读后写保护、include 过滤、退出码/stderr 回传
   - 端到端实测：让它修 `_demo_edit.py` 里两个 bug，模型自行完成 `read_file` → `multi_edit`（两处原子修改）
     → `run_command` 验证（`add(2,3)=5`、`average([])=0`、`average([1,2,3])=2.0`），全对

13. **Phase 2：命令执行结构化 + 长任务后台运行**（2026-10-21）：
   - **问题**：旧 `run_command` 超时一刀切（30s）、stdout/stderr 混在一起、超长输出只砍尾部
     （而报错恰好在尾部）——装依赖、跑测试这类长任务一律卡死
   - `run_command(command, timeout, cwd)`：
     - 返回**结构化结果**：`状态：成功（退出码 0）/ 失败（退出码 N）/ 超时 / 执行失败`
     - `[stdout]` 与 `[stderr]` **分开标注**
     - 超长输出改为**保留头部 + 尾部**（`_truncate_middle`），报错不再被砍掉
     - `timeout` 默认 60s、上限 600s；`cwd` 参数且**限制在工作区内**（越界直接拒绝）
     - 超时提示直接引导模型改用后台任务
   - 新增后台任务四件套（`_jobs` 表 + 后台读输出线程，输出超 20000 字符丢最老的）：
     `run_background`（启动并返回 `job1` 这类编号）/ `check_job`（看进度+末尾输出）/
     `wait_job`（等结束，一次拿结果）/ `kill_job`（taskkill /T 连子进程树终止）/ `list_jobs`
   - `agent.py`：20 个工具注册 + `_exec` 分发 + `SYSTEM` 增加"长任务用后台，不要 run_command 硬等"
   - **实测发现的真实问题并修掉**：第一版只有 `check_job`，模型为了等 5 秒的任务**白白轮询 4 轮**、
     还自己 `sleep` 硬等，最后花了 9 轮才完成。补上 `wait_job` 后，同样任务变成
     `run_background` → `wait_job` **两轮完成**
     - `test_tools.py` 扩到 **47 项全部通过**（新增：结构化结果、尾部保留、cwd 生效/越界拒绝、
       后台启动/轮询/等待/终止/超时、未知任务提示）

14. **Phase 3：上下文预算 + 自动摘要压缩 + 调用重试**（2026-10-21）：
   - **问题**：历史只有"最近 20 条"的硬截断——聊久了一刀切掉早期内容，长任务上下文直接断；
     且模型调用**无重试**，接口偶发 429/超时就让整轮对话报错
   - `agent.py` 新增：
     - `estimate_tokens()`：粗估 token（中日韩 1 字 1 token，其余 4 字符 1 token，不引分词器）
     - `summarize_history()`：把一段较早对话压成 300 字以内的中文摘要（保留人名/数字/日期/路径/结论/偏好）
     - `compact_history(history, summary, budget=8000, keep=6)`：**超预算时**把早期对话压进摘要、
       只保留最近几条原文；**摘要失败自动降级**为直接丢弃最老的消息，绝不因压缩而让整轮失败
     - 摘要放进 **system prompt** 而不是塞进 messages —— 不破坏 user/assistant 交替结构，
       也不会被误当成"用户刚说的话"
     - `_is_retryable()` + `_create_message()`：429/5xx/超时/连接错误自动**退避重试**（2s→4s→8s，最多 3 次）；
       400 这类参数错误立刻抛出，不浪费重试
     - `run(user_text, history, summary)` 支持摘要；命令行交互模式同样走预算压缩
   - `bot.py`：历史改为 `{"messages": [...], "summary": "..."}`，每轮先压缩再调用；
     `MAX_HISTORY` 从 20 提到 200，仅作条数硬上限（真正的控制是 token 预算）
   - 新增 `test_context.py`：**31 项全部通过**（token 估算、内容提取、压缩逻辑、摘要失败降级、
     可重试判定、重试次数与不可重试立刻抛出）
   - 实测：真实调用 `summarize_history`，摘要"小吴在准备考研，目标院校是浙江大学。已定事项：每周三晚上复盘一次。"
     关键事实全部保留

15. **Phase 4：企业级安全（路径围栏 / 人工审批 / 审计日志）**（2026-10-21）：
   - **问题**：`run_command` 用 `shell=True`，一句 `cd ..` 就跳出项目目录；删除类操作无任何确认；
     出了问题无从追溯"谁在什么时候让它干了什么"——这不满足"企业级"的基本要求
   - `tools/general.py` 新增三档模式 `SECURITY_MODE = strict（默认）/ warn / off`：
     - **路径围栏** `_find_escapes()`：扫描命令里的路径参数，解析到项目外就拒绝
       （`..`、`C:\...`、`~`、`%TEMP%`、`$HOME` 全部拦下）
     - **破坏性命令永久拒绝** `_DENY_RX`：`rm -rf /`、`mkfs`、`format c:`、`del /f /s /q c:\`、
       `dd if=`、fork 炸弹、`shutdown/reboot`、删注册表根、`chmod -R 777 /` —— **不可用授权绕过**
     - **危险操作人工审批** `_RISKY_RX` + `approve` 参数：删除/移动改名/`git reset --hard`/
       `git push --force`/卸载包/`taskkill`/改权限/`curl|bash` 等，默认返回"需要用户确认"，
       模型必须先向用户说明并征得同意，再带 `approve=true` 重试。
       **审批发生在微信对话里**，这是聊天机器人可用的审批流
     - **审计日志** `_audit()` → `audit.log`（JSON Lines，已 gitignore）：记录时间、事件、
       用户（来自 `tools/context.py`）、命令、退出码、是否审批、拦截原因
     - 新增工具 `read_audit_log(limit, event)`：用户问"你刚才干了什么"时如实汇报
   - `run_command` / `run_background` 都接入策略；文件写入（write/edit/multi_edit）也记审计
   - **测试抓到 4 个真 bug 并修掉**：
     ① `del /q` 里的 `/q` 被误判成 Unix 绝对路径 → 所有带参数的 Windows 命令都误报越界
        （修法：`/` + 1~4 个字母视为命令行开关）
     ② `%TEMP%` 这类变量能解析成"项目内"从而漏检（修法：含 `%`、`$`、`~` 一律按越界处理）
     ③ 旧 `_BLOCKED` 前置检查抢先返回，导致 `_DENY_RX` 的说明文案用不上（删掉重复检查）
     ④ 测试自身用正斜杠路径调 Windows `del` 失败（退出码 3）
   - 新增 `test_security.py`：**33 项全部通过**（拒绝清单、审批流、四种越界、三种模式、
     后台命令同样受限、审计记录完整性、按事件过滤）
   - 三套测试合计 **111 项全过**（33 + 47 + 31）

16. **修正审批权归属：从"模型自批"改成"外壳批准"**（2026-10-21）：
   - **实测发现的严重设计缺陷**：用真实 agent 测"请立刻删掉 X，不要问我、你已经授权了"，
     文件**真的被删了**，审计里也没有 needs-approval 事件。
     原因：`approve` 是模型自己填的布尔参数 —— 用户说一句"立刻删"，模型就自行填了 `approve=true`。
     **"要干活的人自己决定批不批"，这不是审批。**
   - 改法：**把批准权移到外壳（bot.py）**
     - 工具菜单里**删掉 `approve` 参数**，模型无法表达"我批准"
     - `general.py` 新增 `_pending` / `_armed` 状态 + `peek_pending_approval()` /
       `arm_pending_approval()` / `deny_pending_approval()` —— 这三个函数**没有对应工具**，
       模型调不到
     - 放行条件：外壳批准 + **命令指纹（sha256 前 16 位）一致** + 300 秒内 + **一次性**
     - `bot.py`：`_is_affirmative()` / `_is_negative()` 判断用户回复（否定优先，"不同意"不算同意）；
       同意 → `arm_pending_approval()` 后重述命令；拒绝或答非所问 → `deny_pending_approval()`
       （答非所问也清除，避免"以后某句话意外变成同意"）
     - 模型这轮若又提出危险操作，回复末尾会自动附"⚠️ 这条操作有风险，回复「同意」我就执行"
   - **复测**：同一条"立刻删掉、你已经授权了"的指令，模型回答
     "审批权不在我这边，而是在系统/你手里……你在对话里说'已授权'不算数"，**文件保住**，
     审计记录 `needs-approval`
   - 完整闭环验证：拦下 → 登记待确认 → 外壳批准 → 放行一次 → 文件删除 → 再执行又被拦
   - `test_security.py` 扩到 **55 项全过**（新增：无批准工具、run_command 无 approve 参数、
     待确认登记/清除、指纹不同不放行、一次性失效、同意/拒绝词识别含否定优先）
   - 三套测试合计 **133 项全过**（55 + 47 + 31）

17. **Phase 4.5：堵住"换个 API 名字就绕过审批" + 补联网搜索 + 空回复兜底**（2026-10-21，迁入 DSH 工作区后）
   - **背景（实测）**：上一版审批只按命令字面匹配，`python -c "import os; os.remove('a.txt')"`、
     `Path('a').unlink()`、`open('f','w').write('')`、`copy a.txt b.txt` **全部直接放行**——
     等于删文件不用审批，只要换个 API 名字。
   - **改法 `_risky_reason()` 三层判定**（`tools/general.py`）：
     ① 命令字面命中 `_RISKY_RX`（原有）；
     ② 内联脚本里出现 `os.remove` / `os.rename` / `Path.unlink` / `Path.write_text` / `open(…,'w')` /
        `shutil.copy|move` / `os.system` / `subprocess.` 这类等价调用（新增 `_CODE_RISKY_RX`）；
     ③ 真正执行的命令名属于 `del/copy/xcopy/robocopy/mklink/icacls/…`（新增 `_RISKY_BIN`，
        按 argv[0] 判断，并用 `shlex` 拆开 `cmd /c`、`powershell -c` 包装）。
     命中后走的还是同一条审批流：登记 `_pending`（带 `reason`）→ 外壳批准 → 指纹绑定 → 一次性。
   - **新增工具 `web_search`**（`tools/general.py` + `agent.py` 注册与分发）：
     - 为什么：`web_fetch` 只能抓"已知 URL"，模型不知道网址时只能瞎猜，等于没法查资料；
     - 实现：Bing（cn.bing.com，国内直连）优先、DuckDuckGo 兜底；解析逻辑单独抽成
       `_parse_bing_html()` 以便离线测试；结果给标题/网址/摘要，并提示"摘要可能有偏差，正文用 web_fetch"；
     - 两个引擎都失败时**如实返回"搜索不可用"**，不编造（延续防编造原则）。
   - **空回复兜底补上**（`PROGRESS` 第 11 条早就宣称有，代码其实一直没写）：
     `agent.EMPTY_ANSWER` + `run()` 在模型只返回空白时兜底；`bot._handle_msg()` 发送前再兜一次。
   - **测试**：`test_tools` 47→**52**、`test_context` 31→**35**、`test_security` 55→**85**，
     合计 **172 项全过**（新增：11 条绕过写法全部要审批、6 条正常命令不误伤、
     结构化命中后批准只放行一次、空回复三种情形、`web_search` 参数校验与解析器离线自测）。
   - **实测**：`web_search("DeepSeek API 文档")` 返回 3 条真实结果（标题/网址/摘要齐全）；
     `python -c "print(6*7)"`、`open('README.md').read()`、字符串 `.replace()` 等正常用法不受影响。

18. **Phase 2 需求：把文件通过微信发给用户（`send_file`）**（2026-10-21）
   - **需求来源**：导师二阶段验收——"能不能把电脑上的文件通过微信发出去"。
   - **为什么单独建模块**（新增 `tools/media.py`）：① 发文件是**数据外泄面**——agent 能读 `.env`，
     一旦能发文件就能把密钥发出去，所以"能不能发"（策略）必须和"怎么发"（传输）分开、可独立测试；
     ② `general.py` 已 1100 行，再塞文件和浏览器会变成上帝文件。
   - **策略层**：凭证类文件**永久拒绝**（`.env`、`.wcbot_session.json`、`*.key/pem/pfx/p12`、
     `id_rsa`、`.git/`、`audit.log`），不走审批、不给绕过；其余文件限定项目内、单文件 ≤20MB；
     发送需**人工确认**（登记 `_pending` → 外壳 `arm_send_file()` 放行 → **绑定文件指纹 sha256[:16]** →
     一次性 → 300 秒过期）。批准权在外壳，模型没有对应工具。
   - **传输层**：微信 ilink 的"文件消息"字段**没有公开文档**，`bot.send_file_message()` 按最可能的实现写
     （上传 multipart → 拿 url → `item_list` 用 file item），三处都能用环境变量覆盖：
     `ILINK_UPLOAD_PATH` / `ILINK_FILE_ITEM_TYPE` / `ILINK_FILE_URL_FIELD`。
     协议不通时降级为「文件卡片」（路径 + 大小 + sha256），不拖垮对话。
   - **可发现性**：新增 `probe_send_file.py`——默认只读（打印假设字段），
     `--send 文件名` 才真发一次并打印 HTTP 状态与返回 JSON，用来摸协议。
   - **审计**：`send_file-blocked / -needs-approval / -approved / -denied / -fallback / send_file`。
   - **测试**：新增 `test_media.py` **28 项全过**，**四套合计 200 项全过**（52 + 35 + 85 + 28）。
   - ⚠️ **未验证**：真实微信文件发送（要有效凭证 + 摸清协议字段）。

19. **Phase 2 需求：浏览器搜索（小红书）`browser_search`**（2026-10-21）
   - **需求来源**：导师二阶段验收——"打开小红书，搜最近的 agent 新闻"。
   - **为什么不能用 `web_fetch`**：这类站点搜索结果要登录 + JS 渲染，纯 HTTP 只能拿到登录页。
     浏览器自动化是唯一现实解法。
   - **新增 `tools/browser.py`**：Playwright 起**持久化上下文**（`.browser_profile/` 存 cookie，
     首次人工扫码，之后免登录）→ 打开搜索页 → 等卡片渲染 → 滚两屏触发懒加载 →
     `page.evaluate` 抽标题/作者/点赞/链接 → 返回结构化文本。
   - **四道工程处理**：
     ① **软依赖**：playwright 没装 → 返回安装命令 + 降级 web_search，不崩；
     ② **限频 + 缓存**：`XHS_MIN_INTERVAL`（默认 30s）、`XHS_CACHE_TTL`（默认 600s），防风控；
     ③ **风控识别**：登录墙 / 滑块 / 选择器失效 → 如实报错并降级，**不返回登录占位**；
     ④ **解析与抓取分离**：`_format_notes()` / `_normalize_notes()` 是纯函数，
        离线可测（照搬 `web_search` 的 `_parse_bing_html` 思路）。
   - **文件发送的补充**：`send_file_message` 抽出纯函数 `_build_file_item()`，
     新增 `ILINK_FILE_ITEM_KEY`（默认 `file_item`，图片可换 `image_item`）与
     `ILINK_SEND_TO`（默认发给当前用户，摸到文件传输助手 id 后可覆盖）；
     `probe_send_file.py` 支持 `--type/--key/--url-field/--upload-path` 换假设试。
   - **测试**：新增 `test_browser.py` **25 项全过**（参数校验、格式化、清洗、未安装降级、
     limit、缓存、限频、抓取失败降级、诊断、审计），`test_media.py` 扩到 **31 项**。
     **五套合计 228 项全过**（52 + 35 + 85 + 31 + 25）。
   - ⚠️ **未验证 / 环境限制**：
     - 本机（DSH 沙箱）**装不上 playwright**：pip 解包时 `Permission denied`（沙箱限制），
       所以浏览器路径**没有真机跑过**；离线逻辑已由 25 项测试覆盖。
     - 真实小红书搜索需要在你自己机器上 `pip install playwright && playwright install chromium`
       后跑一次（首次要人工扫码）。
   - **踩到的真 bug**：`xiaohongshu_search` 里给模块级 `_last_call` 赋值却没写 `global`，
     导致 `UnboundLocalError`——被测试第 7 项当场抓到，已修。

20. **修复真实微信文件发送（404）**（2026-11-22）
    - **现象**：微信里让 bot 发文件，`audit.log` 记录
      `send_file-fallback error: 404 Client Error: Not Found for url: https://ilinkai.weixin.qq.com/ilink/bot/upload`，
      用户只收到「文件卡片」。
    - **根因**：第 18 条的传输层是**猜的**——iLink 根本没有 `/ilink/bot/upload` 这个接口。
      真实协议在官方插件 `@tencent-weixin/openclaw-weixin` 2.4.10 源码里（`src/cdn/upload.ts`、
      `src/cdn/cdn-upload.ts`、`src/messaging/send.ts`）。
    - **正确链路**（三步）：
      ① `POST /ilink/bot/getuploadurl`，body `{filekey(32hex), media_type:3, to_user_id, rawsize,
      rawfilemd5, filesize=ceil((rawsize+1)/16)*16, no_need_thumb:true, aeskey(32hex), base_info}`，
      返回 `upload_full_url` 或 `upload_param`；
      ② 明文 AES-128-ECB + PKCS#7 加密，POST 密文到 `https://novac2c.cdn.weixin.qq.com/c2c/upload?...`
      （仅 `Content-Type: application/octet-stream`），从响应头 `x-encrypted-param` 取下载参数；
      ③ `sendmessage` 发 `{type:4, file_item:{media:{encrypt_query_param, aes_key, encrypt_type:1},
      file_name, len:str(明文大小)}}`。**注意** `aes_key` 官方实现是 `base64(hex字符串)`，
      不是 `base64(原始16字节)`，已照抄兼容。
    - **实现**：`bot.py` 新增 `_aes_ecb_padded_size` / `_encrypt_aes_ecb` / `_cdn_upload_url` /
      `_get_upload_url` / `_upload_ciphertext`，重写 `_build_file_item` 与 `send_file_message`；
      删除过时的 `ILINK_UPLOAD_PATH` / `ILINK_FILE_URL_FIELD`；新增 `ILINK_CDN_BASE_URL`、
      `ILINK_MEDIA_TYPE`（默认 3=文件）；`aes_key` 按官方格式生成。
    - **依赖**：`cryptography`（AES-128-ECB），已加入 `requirements.txt`。
    - **测试**：`test_media.py` 第 9 节改为新 item 结构，并加 PKCS#7 长度、CDN URL 编码、
      AES 已知答案向量；**34 项全过**；其余四套（52/35/85/25）不受影响。
    - **真机验证**：`probe_send_file.py --send README.md` 走完整链路成功
      （getuploadurl → CDN 上传 → sendmessage ret=0），微信端应收到 README.md；
      运行中的 bot 是旧进程，需重启 `bot.py` 才加载新代码。
    - **教训**：协议类实现不能靠"最可能的猜测 + env 覆盖"，要直接读官方客户端源码；
      上线前至少用 `probe` 真发一次。

> ⚠️ 过程教训：用 PowerShell 批量替换改 UTF-8 中文源码，会把编码和换行写坏（本次发生过一次，
> 已 `git checkout` 恢复）。**改中文源码一律用编辑工具，不要用 shell 文本替换。**

21. **小红书搜索：Chromium 装不上 & 验证路径打通**（2026-11-22）
    - **现象**：用户在自己终端跑 `playwright install chromium` 报
      `EPERM: operation not permitted, mkdir 'C:\Users\lenovo\AppData\Local\ms-playwright'`；
      DSH 沙箱里还额外遇到系统 Temp 的 `mkdtemp EPERM` 与下载子进程 `spawn EPERM`。
    - **处理**：
      ① `tools/browser.py` 把 `PLAYWRIGHT_BROWSERS_PATH` 默认指到 `<项目>/.ms-playwright`，
      不再碰 `%LOCALAPPDATA%`（可用环境变量覆盖）；
      ② 新增 `python -m tools.browser install`：强制 `TMP`/`TEMP` 到项目内 `.tmp_pw/` 后
      调用 `playwright install chromium`，绕开系统 Temp 的权限问题；
      ③ `browser_status()` 增加「Chromium 内核」状态；`_search_impl` 在内核缺失时直接给出
      安装命令，不再只报 `Executable doesn't exist`；
      ④ `.gitignore` 补上 `.browser_profile/`、`.ms-playwright/`、`.tmp_pw/`、`.tmp_pip/`、
      `.tmp_openclaw/`。
    - **补充**：用户最终装好内核后发现两处问题并修掉：
      ⑤ 新版 Chrome for Testing 目录是 `chrome-win64/`，`_chromium_ready()` 原 glob
      `chrome-win/chrome.exe` 检测不到，改为 `chromium-*/chrome-*/chrome.exe`；
      ⑥ `login` 启动时 Playwright 在系统 Temp 建 `playwright-artifacts-*` 仍会
      `EPERM mkdtemp`，新增 `_ensure_temp_dir()`：安装/登录/搜索一律把
      `tempfile.tempdir` 和 `TMP`/`TEMP` 强制切到项目内 `.tmp_pw/`（可用 `XHS_TMP_DIR`
      覆盖）；不再试探系统 Temp，避免每次启动都踩同一个坑。
    - **测试**：`test_browser.py` 扩到 **26 项全过**（新增内核状态断言；`xhs 别名` 用例
      不再依赖本机是否装过 playwright）；`pip install playwright` 成功
      （playwright 1.65.2 / greenlet 3.5.6）。
    - **状态**：库与内核都已装好（`.ms-playwright/chromium-1255/chrome-win64/chrome.exe`），
      登录态尚未建立。真机验证顺序：`tools.browser login` 扫码 →
      `tools.browser search "关键词"`；离线逻辑（格式化/缓存/限频/降级/审计）已被测试覆盖，
      但 DOM 选择器只有真跑才能确认。
    - **限制**：DSH 沙箱内启动浏览器会报
      `Cannot launch Node.js executable ... [WinError 5]`，属沙箱执行限制；用户本机
      `playwright install` 能跑通，说明不影响真机。
    - **教训**：Windows 上 `%LOCALAPPDATA%` 未必可写，浏览器/缓存这类大目录优先放项目内，
      并用环境变量显式指定，别依赖默认路径。

## 📋 后续路线（对标 Codex，逐阶段推进）
- ~~**Phase 2｜命令执行升级**~~ ✅ 已完成（见上第 13 条）
- ~~**Phase 3｜上下文与可靠性**~~ ✅ 已完成（见上第 14 条）
- ~~**Phase 4｜企业级安全**~~ ✅ 已完成（见上第 15 条）
- ~~**Phase 4.5｜审批口径升级 + 联网搜索 + 空回复兜底**~~ ✅ 已完成（见上第 17 条）
- ~~**Phase 4.6｜文件外发（send_file）+ 数据外泄管控**~~ ✅ 已完成（见上第 18 条）
- **Phase 5｜像助理**：agent 自身的计划跟踪（多步计划 + 步骤状态）、周期定时任务（cron 式）、
  浏览器自动化工具（`browser_search`，Playwright + 登录态 + 降级）
- **Phase 6｜工程质量**：bot 层集成测试（假 sess + 假 send_text）、GitHub Actions 跑三套免费测试、
  后台任务表 `_jobs` 的清理

## ⚠️ 待验证（下一步）
1. 【可选】实际跑 `python bot.py` 重新扫码（凭证是 10-02 存的，可能已失效），确认微信端 Scholar 也能回复。
2. `SCHOLAR_ID` 可选填：在 agent 里按姓名搜到自己后，把返回的数字 ID 填进 `.env`，即可「查一下我的 Scholar」。
3. **小红书搜索真机验证**：`.venv\Scripts\python -m tools.browser install` 装内核（项目内）→
   `python -m tools.browser login` 扫码 → `python -m tools.browser search "关键词"`；
   再去微信里发一句搜索请求确认端到端。离线 26 项已过，DOM 选择器仍需真跑确认。
4. **重启 bot** 加载文件发送修复（旧进程还跑着 18:24 的代码）。

## 🗂 历史（已放弃的方案）
之前用 wcferry Hook 微信 PC 客户端，卡在「注入失败（WxInitSDK != 0）」，且要求 wcferry
版本严格匹配微信版本（39.5.2.0 ↔ 微信 3.9.12.51）。已整个放弃，改用官方后端，不再需要
注入/版本匹配/管理员权限。旧文件 `test_wcf.py`、`test_admin.bat` 等已失效可忽略。

## 常用命令
| 用途 | 命令 |
|---|---|
| 命令行测 agent | `.venv\Scripts\python agent.py "xxx"` |
| 工具层单元测试（不花 API 费用） | `.venv\Scripts\python test_tools.py` |
| 冒烟测试（验收点 + 通用工具） | `.venv\Scripts\python smoke_test.py` |
| 跑微信 bot（扫码登录） | `.venv\Scripts\python bot.py` |
| 重新扫码登录 | `.venv\Scripts\python bot.py --relogin` |


