# TriBrief

TriBrief 是一个轻量三赛道投研简报系统。它每天从 RSS 源抓取芯片/算力、具身智能/机器人、数据三个方向的新闻，用 OpenAI 兼容 LLM 做赛道判定、重要性打分、中文摘要和投资视角点评，然后渲染为 HTML 邮件并推送。

设计目标是：依赖少、流程清楚、可 Docker 复现、可 GitHub Actions 定时运行。

## 特性

- 三赛道聚焦：芯片/算力、具身智能/机器人、数据。
- 轻量依赖：只使用 `feedparser`、`requests`、`jinja2`、`pyyaml`、`python-dateutil`。
- LLM 接口开放：支持 DeepSeek、OpenAI、豆包、Ollama 等 OpenAI 兼容 `/chat/completions` 接口。
- 邮件友好 HTML：table 布局、inline 样式、移动端适配。
- 安全配置：API Key 和 SMTP 密码只从环境变量读取。
- 本地预览：`--dry-run` 不调用 LLM、不发邮件，只生成示例 HTML。

## 效果预览

运行 dry-run 后会生成：

```powershell
python brief.py --dry-run
```

输出文件位于：

```text
output/briefing_YYYY-MM-DD.html
```

当前仓库已有一份示例产物：`output/briefing_2026-06-26.html`。

## 工作流程

```text
RSS 源抓取
  -> 标准化标题、摘要、发布时间
  -> URL 与标题去重
  -> LLM 判定赛道、评分、摘要、投资视角
  -> 按赛道分组并限量
  -> LLM 生成今日要点
  -> Jinja2 渲染 HTML
  -> SMTP 发送邮件
```

## 快速开始

建议使用 Python 3.11+。

```powershell
python -m pip install -r requirements.txt
```

预览界面，不调用 LLM、不发邮件：

```powershell
python brief.py --dry-run
```

正常抓取并生成 HTML，但不发邮件：

```powershell
python brief.py --no-email
```

正式运行：

```powershell
python brief.py
```

可选参数：

```powershell
python brief.py --hours 48
python brief.py --config config.yaml
```

## 环境变量

复制 `.env.example` 为 `.env`，或在系统环境变量中配置：

```text
LLM_API_KEY=
SMTP_USER=
SMTP_PASSWORD=
```

说明：

- `LLM_API_KEY`：OpenAI 兼容服务的 API Key。
- `SMTP_USER`：发件邮箱账号。
- `SMTP_PASSWORD`：SMTP 授权码或密码。

## 配置说明

主配置文件是 `config.yaml`。建议先复制示例：

```powershell
Copy-Item config.example.yaml config.yaml
```

关键字段：

- `ai.base_url`：OpenAI 兼容接口根地址，例如 `https://api.deepseek.com/v1`。
- `ai.model`：模型名，例如 `deepseek-chat`。
- `ai.api_key_env`：读取 API Key 的环境变量名，默认 `LLM_API_KEY`。
- `filtering.time_window_hours`：抓取最近多少小时。
- `filtering.score_threshold`：低于该分数的条目丢弃。
- `filtering.max_per_track`：每个赛道最多保留几条。
- `sources`：按赛道配置 RSS 源。
- `email`：SMTP 和收件人配置。
- `output.dir`：HTML 输出目录。

## Docker 运行

预览界面：

```powershell
docker compose run --rm tribrief --dry-run
```

正式运行：

```powershell
docker compose run --rm tribrief
```

`docker-compose.yml` 会挂载：

- `./config.yaml:/app/config.yaml:ro`
- `./output:/app/output`

密钥通过 `.env` 注入。

## GitHub Actions 定时部署

工作流文件位于 `.github/workflows/daily.yml`。

触发方式：

- 每天 UTC 00:00，即北京时间 08:00。
- 支持 `workflow_dispatch` 手动触发。

需要在仓库 Settings -> Secrets and variables -> Actions 中配置：

```text
LLM_API_KEY
SMTP_USER
SMTP_PASSWORD
```

运行结束后会上传 `output/*.html` 作为 artifact，便于排查邮件发送问题。

## 中文源接入 RSSHub

国内媒体原生 RSS 不稳定，建议自建 RSSHub：

```powershell
docker run -d --name rsshub -p 1200:1200 diygod/rsshub
```

本地访问：

```text
http://localhost:1200
```

生产环境建议部署到自己的服务器，然后在 `config.yaml` 中填入：

```yaml
sources:
  embodied:
    - { name: "机器之心", url: "https://你的rsshub域名/jiqizhixin/...", region: "cn" }
```

具体路由以 RSSHub 官方文档为准：`https://docs.rsshub.app`。微信公众号、付费墙和部分创投数据库抓取稳定性较差，建议把 RSSHub 国内源作为补充层，而不是唯一信息源。

## 自定义

新增 RSS 源：

```yaml
sources:
  chip:
    - { name: "新来源", url: "https://example.com/feed.xml", region: "global" }
```

调高过滤强度：

```yaml
filtering:
  score_threshold: 6.5
  max_per_track: 4
```

关闭邮件，只生成 HTML：

```yaml
email:
  enabled: false
```

## 源状态

当前配置中的 A 层英文 RSS 源需要在目标运行环境中定期复测。RSS 源长期可能改版、限流或返回空条目。

最近一次本地实测：2026-06-26。检查方式为 `requests` 拉取后交给 `feedparser` 解析。

| 赛道 | 来源 | 状态 |
|---|---|---|
| 芯片/算力 | IEEE Spectrum 半导体 | 可用，30 条 |
| 芯片/算力 | SemiWiki | 可用，5 条 |
| 芯片/算力 | EE Times | 可用，10 条 |
| 芯片/算力 | EDN | 可用，10 条 |
| 芯片/算力 | Semiconductor Today | HTTP 200 但无条目，已在默认配置中注释 |
| 芯片/算力 | Tom's Hardware | 可用，50 条 |
| 芯片/算力 | TechXplore 半导体 | 可用，30 条 |
| 芯片/算力 | arXiv cs.AR 硬件架构 | 可用，17 条 |
| 具身智能/机器人 | The Robot Report | 可用，15 条 |
| 具身智能/机器人 | IEEE Spectrum 机器人 | 可用，30 条 |
| 具身智能/机器人 | Robohub | 可用，75 条 |
| 具身智能/机器人 | New Atlas 机器人 | 可用，60 条 |
| 具身智能/机器人 | TechCrunch 机器人 | 可用，20 条 |
| 具身智能/机器人 | TechXplore 机器人 | 可用，30 条 |
| 具身智能/机器人 | arXiv cs.RO 机器人 | 可用，84 条 |
| 数据 | TechCrunch AI | 可用，20 条 |
| 数据 | Ars Technica | 可用，20 条 |
| 数据 | The Verge | 可用，10 条 |
| 数据 | Wired | 可用，50 条 |
| 数据 | MarkTechPost | 403 Forbidden，已在默认配置中注释 |
| 数据 | Unite.AI | 403 Forbidden，已在默认配置中注释 |
| 数据 | Simon Willison | 可用，30 条 |
| 数据 | arXiv cs.AI | 可用，277 条 |

## 成本

成本主要来自 LLM 调用。以 DeepSeek 等低价 OpenAI 兼容模型为例，几十到一两百条 RSS 条目的每日摘要通常是很低的量级。若源数量明显增加，可通过以下方式控制成本：

- 调小 `time_window_hours`。
- 调高 `score_threshold`。
- 减少噪音大的综合科技源。
- 保持每批最多 15 条左右，降低单次上下文压力。

## 许可

本项目目前未附带独立许可证。如需开源发布，请先补充合适的 License。
