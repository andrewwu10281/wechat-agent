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
| `run_command` | 在项目目录下运行一条命令行命令（可跑脚本、做计算等） |
| `read_file` / `write_file` / `edit_file` | 读、写、改文件 |
| `list_files` / `search_text` | 按通配符列文件 / 按正则全文检索（类 glob / grep） |
| `web_fetch` | 联网抓取网页文本（可用于检索、查资料等） |

有了这套底线能力，像"算数""查天气/查资料"这类可组合的问题，模型可以**自己用
`run_command` 跑脚本、用 `web_fetch` 联网**完成，不必预先写死成专用工具。

> ⚠️ 安全提示：`run_command` 能在项目目录下执行命令，`read_file` 等文件操作被限制在项目目录内，
> 但这**不是真正的沙箱**。请只在可信、本地的环境使用，不要把带 `run_command` 的机器人暴露给不信任的人。

## 待办与提醒

- 说「待办：明天 9 点交报告」→ 记进 `todos.json`；
- 说「我的待办」→ 列出未完成项；
- 说「完成第 N 个 / 删除第 N 个」→ 更新清单；
- bot 主循环每轮检查「到点且未提醒」的待办，主动发微信提醒；
- 待办按用户隔离；`todos.json` 已加入 `.gitignore`（含个人数据，不提交）。

## 环境要求

- Python 3.8+（本机 3.13 已实测）
- Windows / Linux / macOS 均可
## 安装步骤

### 1. 安装 Python 依赖

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

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


## 微信接入说明

- 登录凭证保存在 `.wcbot_session.json`（已加入 `.gitignore`）。
- 支持多轮对话记忆：每个用户保留最近 20 条消息（约 10 轮），能理解「刚才那个」这类指代。
- 同一条消息重复投递会被去重，只回复一次；并忽略自己发出的消息，避免自回复。
- 调试群消息：设置环境变量 `DEBUG_MSG=1` 后启动 bot，会把收到的每条原始消息打印到日志，
  方便观察群聊字段结构（仅本地调试用；日志含消息内容，勿外传）。

## 风险提示

- 查学者信息走 Semantic Scholar 免费 API，免代理、国内可直连；免费额度偶尔会限流（429），模型遇到失败会自行重试。
- 通用工具（尤其 `run_command`）权限较大，请只在可信环境使用。
- 建议用小号测试，控制发送频率。
