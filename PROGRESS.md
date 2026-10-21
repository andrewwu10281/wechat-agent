# 微信 AI Agent 项目 —— 进度记录

> 本科实践课项目：写一个接入个人微信的 agent，用 DeepSeek 当大脑。
> 验收点：查天气、算数、查 Google Scholar。
> 定位演进：不再只是验收项目，目标做成**可日常使用的个人助理**（对标 Codex 的文件/代码能力）。
> 最近更新：2026-10-21

## 项目位置
`C:\Users\lenovo\wechat-agent`

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

## 📋 后续路线（对标 Codex，逐阶段推进）
- **Phase 2｜命令执行升级**：结构化结果（退出码/stdout/stderr 分离）、后台运行 + 查状态/终止、`cwd` 参数
- **Phase 3｜上下文与可靠性**：历史 token 预算 + 自动摘要压缩；LLM 调用重试/退避
- **Phase 4｜企业级安全**：命令执行的工作区约束与危险操作审批、审计日志
- **Phase 5｜像助理**：agent 自身的计划跟踪、周期定时任务

## ⚠️ 待验证（下一步）
1. 【可选】实际跑 `python bot.py` 重新扫码（凭证是 10-02 存的，可能已失效），确认微信端 Scholar 也能回复。
2. `SCHOLAR_ID` 可选填：在 agent 里按姓名搜到自己后，把返回的数字 ID 填进 `.env`，即可「查一下我的 Scholar」。

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


