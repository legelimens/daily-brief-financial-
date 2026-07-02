# Claude Code 构建任务:TriBrief —— 轻量三赛道投研简报系统

> 这是一份完整的项目构建规格。请你(Claude Code)据此从零创建整个项目,目标是**一次性产出一个可运行、可复现、界面精美的成品**。请严格遵守"轻量"原则:不引入任何非必要的依赖或文件。完成后请运行 dry-run 验证界面,并实测 RSS 源可用性。

---

## 0. 项目目标

构建一个每日自动运行的投研简报系统。它每天:

1. 从一批 RSS 源抓取**芯片/算力、具身智能、数据**三个赛道的**国内外**新闻;
2. 去重;
3. 用一个 LLM 对每条新闻**判定赛道、打重要性分、生成中文摘要 + 投资视角点评、标注一手/二手**,并过滤掉低分和不相关项;
4. 把结果渲染成一封**精美的 HTML 简报**;
5. 通过邮件推送给收件人(投资从业者)。

最终通过 GitHub Actions 定时在云端运行,收件人零配置只需收邮件;同时项目用 Docker 打包,保证可复现、可在任意机器本地运行。

**核心价值与设计取向**:轻量(代码量小、看得懂)、聚焦(只认三赛道)、准确(不编造、可溯源)、界面专业美观(这是重点,不要做成模板感的通用样式)。

---

## 1. 技术栈与硬性约束

- **语言**:Python 3.11+
- **依赖最小化**,只允许:`feedparser`、`requests`、`jinja2`、`pyyaml`、`python-dateutil`。**禁止**引入 LangChain、Flask、Celery、openai SDK、数据库等任何重型依赖。LLM 调用用 `requests` 直接打 HTTP。
- **LLM 接口**:使用 OpenAI 兼容的 `/v1/chat/completions` 接口(DeepSeek、豆包、OpenAI、Ollama 等都兼容),base_url、model、api_key 全部可配。
- **密钥**:所有密钥(LLM api key、SMTP 密码)只从**环境变量**读取,严禁硬编码或写进 config。
- **代码规范**:全部函数加类型注解;按职责清晰分函数;关键逻辑加中文注释;可读性优先于花哨。
- **健壮性**:任何单个 RSS 源失败、LLM 返回非法 JSON、邮件发送失败,都不能让整个程序崩溃,要捕获并记录日志后继续/优雅退出。

---

## 2. 项目结构(严格按此,不要多加文件)

```
tribrief/
├── config.yaml              # 源清单 + AI 参数 + 阈值 + 收件人(不含密钥)
├── config.example.yaml      # 同上的示例模板
├── brief.py                 # 主程序:抓取→去重→AI→渲染→发信
├── templates/
│   └── briefing.html        # Jinja2 简报模板 ← 界面核心
├── .env.example             # 列出所需环境变量
├── requirements.txt         # 锁定版本的依赖
├── Dockerfile               # 打包复现
├── docker-compose.yml       # 一键本地运行
├── .github/
│   └── workflows/
│       └── daily.yml         # 每日定时
└── README.md                # 部署与使用说明
```

---

## 3. 主程序 brief.py 详细规格

实现以下函数,`main()` 串起整个流程。请支持命令行参数:`--dry-run`(用内置示例数据生成 HTML,**不调用 LLM、不发邮件**,仅用于预览界面)、`--no-email`(正常跑但只生成 HTML 不发信)、`--hours N`(覆盖时间窗口)。

### `load_config(path) -> dict`
读取 yaml。对其中形如 `${VAR}` 的字符串做环境变量替换。

### `fetch_all(sources, hours) -> list[Item]`
遍历所有源,用 `feedparser` 抓取。每条标准化成一个 dict(建议定义为 dataclass `Item`),字段:
`title, url, summary_raw(原始摘要,去 HTML 标签), published(datetime), source_name, region('cn'|'global'), track_hint(来自源配置的赛道提示)`。
- 只保留发布时间在 `hours`(默认 72)内的条目;无发布时间的条目保守保留但标记。
- 单个源抓取异常要 try/except 跳过并打 warning 日志,记录失败源名。

### `dedupe(items) -> list[Item]`
去重逻辑:先按规范化 URL(去掉 query/fragment、统一大小写)去重;再对标题做轻量相似判断(如标题归一化后完全相同,或编辑距离/包含关系判断),合并指向同一事件的条目。保留信息更全的一条。

### `score_and_enrich(items, config) -> list[ScoredItem]`
核心步骤。把条目**分批**(每批不超过约 15 条,控制 token)发给 LLM,使用第 4 节的提示词。要求 LLM 对每条返回结构化字段。
- 严格 JSON 解析,带容错:若返回包含多余文本或代码块包裹,要剥离后再解析;解析失败的该批跳过并记日志,不崩溃。
- LLM 输出字段并入 Item,得到 `ScoredItem`,新增字段:`track('chip'|'embodied'|'data'|'drop')、score(0-10 float)、summary_cn(中文摘要)、angle_cn(投资视角点评)、tier('一手'|'二手')、conflict_note(可空,数字冲突说明)`。
- `track == 'drop'` 或 `score < config.filtering.score_threshold` 的条目丢弃。

### `build_briefing(scored, config) -> Briefing`
- 按 track 分三组。
- 每组按 `score` 降序、再按 `published` 降序排列。
- 每组取前 `config.filtering.max_per_track` 条(如 4)。
- 计算每组条数 `counts`。
- 再调用一次 LLM(用第 4.2 节的"今日要点"提示词)生成:`highlights`(3 行以内速览文本)和 `focus_track`(当天重点赛道)。dry-run 模式下用占位文本。
- 返回 Briefing 对象:`date, highlights, focus_track, groups{chip:[...], embodied:[...], data:[...]}, counts`。

### `render_html(briefing, template_path) -> str`
用 Jinja2 渲染 `templates/briefing.html`,返回完整 HTML 字符串,并把 HTML 同时写到 `output/briefing_YYYY-MM-DD.html` 便于存档/预览。

### `send_email(html, config) -> None`
用 `smtplib` + `email.mime` 发送 HTML 邮件。SMTP host/port/user 从 config(user 可用 `${SMTP_USER}`),密码从环境变量 `SMTP_PASSWORD`。主题形如 `投研简报 · YYYY-MM-DD`。失败要捕获并记录,不抛未处理异常。

### `main()`
解析参数 → load_config → (dry-run 走示例数据) → fetch_all → dedupe → score_and_enrich → build_briefing → render_html → (除非 --dry-run/--no-email)send_email。全程用 `logging` 打印每一步的进度与计数(抓到多少条、去重后多少、过滤后每赛道多少、是否发信成功)。

---

## 4. 嵌入代码的 LLM 提示词(必须原样实现)

### 4.1 评分与点评提示词(用于 `score_and_enrich`)

把这段作为 system prompt;user message 里附上当批新闻条目(编号 + 标题 + 来源 + 来源地区 + 发布日期 + 原始摘要)。要求模型只返回 JSON 数组。

```
你是一名专注于硬科技一级市场的投研分析助理,服务对象是一位做投资的资深从业者。你会收到一批新闻条目,请逐条分析并以严格 JSON 数组返回结果,不要输出任何解释性文字或 Markdown。

【三个赛道定义】
- chip(芯片/算力):AI 芯片(英伟达、AMD、华为昇腾、寒武纪、地平线等)、HBM、先进制程、Chiplet、EDA、半导体设备。
- embodied(具身智能/机器人):人形机器人本体、灵巧手、运动控制、VLA 模型、机器人基础模型。
- data(数据):机器人/具身学习所需的数据采集、遥操作、仿真、真机/合成数据,以及相关公司与一级市场动态。

【任务】对每一条新闻,输出以下字段:
- "id": 对应输入编号。
- "track": 该新闻最匹配的赛道,取 "chip" / "embodied" / "data";若与三个赛道都无关,或属于纯营销稿、旧闻综述、与投资判断无关的科普,则取 "drop"。
- "score": 0-10 的重要性评分(可带一位小数)。评分参考(从高到低):重大融资/并购/估值变化 ≈ 9-10;重要技术突破/流片/标志性新品 ≈ 7-9;政策/出口管制 ≈ 6-8;关键人事/战略 ≈ 5-7;一般动态 ≈ 3-5;边缘信息 < 3。
- "summary_cn": 一句话中文摘要,客观陈述事件本身,不超过 60 字。
- "angle_cn": 一句话"投资视角"点评,回答"so what"——这件事对投资意味着什么(如:估值水位、技术拐点对商业化的影响、对可对标标的或竞争对手的含义)。要具体,不空泛。
- "tier": 信息源等级。若来源是公司官方公告/官方新闻稿,或路透社、彭博等一手媒体,取 "一手";若是自媒体、二手转述、聚合稿,取 "二手"。
- "conflict_note": 仅当该条涉及的关键数字(融资额、估值等)在摘要中出现明显不确定或多种口径时,用一句话说明;否则留空字符串 ""。

【准确性铁律】
- 不要编造任何信息。摘要和点评只能基于输入内容,信息不足时如实简略,绝不脑补"据传""可能"。
- 不要拔高评分;拿不准赛道归属时,优先判 "drop"。
- 输出必须是合法 JSON 数组,每个元素含上述全部字段,顺序与输入一致。
```

### 4.2 今日要点提示词(用于 `build_briefing`)

把已入选的各赛道条目(标题 + 赛道 + 评分)作为输入,system prompt 如下,要求返回严格 JSON 对象 `{"highlights": "...", "focus_track": "chip|embodied|data"}`:

```
你是投研简报的主编。下面是今天已入选的新闻条目。请输出一个 JSON 对象,包含:
- "highlights": 3 行以内的中文速览,点出今天最关键的 1-3 件事,简洁有力,不超过 120 字。
- "focus_track": 今天最值得关注的重点赛道,取 "chip" / "embodied" / "data"。
只输出 JSON,不要其他文字。
```

---

## 5. 界面规格:templates/briefing.html(重点,务必做到位)

**设计目标**:专业投研简报的沉稳质感 + 杂志式排版。干净、克制、信息层级清晰,在**手机邮件客户端**里打开就清爽好读。**不要**做成 Bootstrap/通用模板那种千篇一律的样子。

### 5.1 兼容性要求(HTML 邮件)
- 主体结构用 `<table>` 布局(邮件最稳),但视觉用现代排版。
- 关键样式尽量 **inline**;同时在 `<head>` 放一份 `<style>`(Apple Mail / Gmail 支持)。
- 只用邮件安全的 CSS:避免 flex/grid 作为唯一布局手段(可用但要有 table 兜底)、避免外部字体文件、避免 JS。
- 加 `<meta name="viewport" content="width=device-width, initial-scale=1">`,用 media query 做窄屏适配。
- 字体用系统字体栈:`-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", Roboto, Helvetica, Arial, sans-serif`。

### 5.2 配色 token(浅色主题)
- 页面背景 `#F5F5F7`(极浅灰),内容容器白色 `#FFFFFF`,最大宽度 600px 居中。
- 主文字 `#1D1D1F`,次要文字/元信息 `#6E6E73`,分隔线 `#E5E5EA`。
- 赛道主色:芯片 `#34C759`(绿)、具身 `#0A84FF`(蓝)、数据 `#FF9F0A`(橙)。
- 每个赛道对应一个**浅色背景版**用于"投资视角"高亮块:绿 `#E8F8EE`、蓝 `#E6F1FF`、橙 `#FFF4E5`。

### 5.3 版面结构(从上到下)
1. **页眉**:左侧简报名"投研速览 / TriBrief"(粗体,约 20px),右侧日期(次要文字)。下方一条细分隔线。
2. **今日要点卡片**:浅色圆角卡片(背景 `#FFFFFF`,带极淡边框或轻阴影,圆角 12px)。卡片内顶部一个小标题"今日要点",下面是 `highlights` 文本;再下面一行"重点赛道"+ 一个用 `focus_track` 对应赛道色填充的小徽章(pill 形状,白字)。
3. **三个赛道分区**,依次 chip → embodied → data。每个分区:
   - **分区头**:一个左侧带 4px 赛道色竖条的标题行,文字为赛道中文名(如"芯片 / 算力")+ 灰色小字标注本区条数(如"3 条")。
   - **新闻卡片列表**:每条新闻一张卡片。
4. **页脚**:一行次要文字,含生成时间和一句署名(如"由 TriBrief 自动生成"),以及一句免责声明"信息仅供参考,请以原始来源为准"。

### 5.4 新闻卡片(每条)的精确样式
- 卡片:白底,圆角 10px,内边距 16px,卡片之间留 12px 间距;可加极淡的底部边框或 1px 浅边框,**不要**重阴影。
- **第一行(标题)**:新闻标题,作为超链接指向 `url`,字号约 16px、粗体、主文字色、行高 1.4;点击在新标签打开。
- **第二行(元信息)**:小字(约 12px,次要文字色),依次为:发布日期(MM/DD)· 来源名 · 一个 `tier` 标签。`tier` 标签做成小 pill:"一手"用淡绿底深绿字,"二手"用淡灰底灰字。同行右侧可放一个**评分指示**:用赛道色画 1-3 个实心小圆点表示重要性(score≥8.5 三点、≥6.5 两点、否则一点),或直接显示分数,任选其一但要精致。
- **第三行(摘要)**:`summary_cn`,正文字号约 14px,主文字色稍浅,行高 1.6。
- **第四行(投资视角高亮块)**:这是重点。用该赛道的**浅色背景版**做一个圆角小块(圆角 8px、内边距 10-12px),左侧带一个该赛道主色的小竖条或一个小图标/标签"投资视角",块内文字为 `angle_cn`,字号约 13px。让 mentor 一眼就能锁定 so-what。
- **(可选)conflict_note**:若非空,在投资视角块下方用一行小字(橙色系)提示"⚠️ 数字待核实:{conflict_note}"。

### 5.5 响应式
- 容器在窄屏(<600px)宽度 100%、左右留 12-16px padding。
- 字号、间距在窄屏适当收紧但保持可读。
- 确保在 iPhone 邮件客户端竖屏下不出现横向滚动。

### 5.6 dry-run 示例数据
为 dry-run 准备一组内置示例条目(直接写在 brief.py 里),覆盖三个赛道、含一条带 conflict_note 的,用于完整展示界面所有元素。建议直接采用以下真实样例(可微调):
- 具身/一手:Bear Robotics 收购英国 Kinisi Robotics,获得 KR1 人形机器人与操作训练数据(score 8.5)。
- 具身/一手/带冲突:Neura Robotics 完成 C 轮,亚马逊、英伟达参投(标注估值口径存在 70 亿美元 / 40 亿欧元两种说法,填入 conflict_note,score 8)。
- 芯片/二手:沐曦股份与优必选合资成立曦选创智,布局国产具身智能芯片(score 7)。
- 芯片/二手:美光 HBM4 或在英伟达 Vera Rubin 平台拿到更大供货份额(score 6.5)。
- 数据/二手:国内具身 5 月融资环比降近六成,资本转向数据基础设施(score 6.5)。

---

## 6. 配置文件 config.example.yaml

提供一份完整、带注释的示例。结构如下(请填入第 9 节的真实源清单):

```yaml
ai:
  base_url: "https://api.deepseek.com/v1"   # OpenAI 兼容接口
  model: "deepseek-chat"
  api_key_env: "LLM_API_KEY"                # 密钥从此环境变量读取
  temperature: 0.3

filtering:
  time_window_hours: 72      # 抓取最近多少小时
  score_threshold: 6.0       # 低于此分丢弃
  max_per_track: 4           # 每赛道最多保留几条

sources:
  chip:
    - { name: "IEEE Spectrum Semiconductors", url: "<RSS>", region: "global" }
    # ...
  embodied:
    - { name: "The Robot Report", url: "<RSS>", region: "global" }
    # ...
  data:
    - { name: "...", url: "<RSS>", region: "global" }
    # ...

email:
  enabled: true
  smtp_host: "smtp.gmail.com"
  smtp_port: 465
  smtp_user: "${SMTP_USER}"
  from_name: "TriBrief"
  to:
    - "mentor@example.com"

output:
  dir: "output"
```

`config.yaml` 本身可被 .gitignore 忽略(避免泄露收件人),仓库只提交 `config.example.yaml`。

---

## 7. .env.example

```
# LLM(OpenAI 兼容接口)的 API Key
LLM_API_KEY=

# 发件邮箱 SMTP 账号与授权码/密码
SMTP_USER=
SMTP_PASSWORD=
```

---

## 8. 部署相关文件

### requirements.txt
锁定明确版本(选择当前稳定版本),仅包含第 1 节允许的依赖。

### Dockerfile
基于 `python:3.11-slim`;`pip install -r requirements.txt`;`ENTRYPOINT ["python", "brief.py"]`。

### docker-compose.yml
一个 `tribrief` 服务,构建本地镜像,挂载 `config.yaml`、`./output`,通过 `env_file: .env` 注入密钥。让 `docker compose run --rm tribrief --dry-run` 可直接预览,`docker compose run --rm tribrief` 正式跑。

### .github/workflows/daily.yml
- 触发:`schedule` cron(默认每天北京时间 08:00,即 UTC 0:00,写明 cron 表达式并在注释里说明时区换算)+ `workflow_dispatch`(手动触发以便测试)。
- 步骤:checkout → setup-python 3.11 → `pip install -r requirements.txt` → 运行 `python brief.py`。
- 密钥通过 GitHub Actions Secrets 注入为环境变量(`LLM_API_KEY`、`SMTP_USER`、`SMTP_PASSWORD`)。
- 在 README 中写明需要在仓库 Settings → Secrets 配置哪些项。
- config.yaml 在 CI 中如何提供:可在仓库内提交一份不含密钥的 config.yaml(收件人若敏感则也用 secret 注入或单独说明),并在 workflow 注释中说明。

---

## 9. RSS 源清单(覆盖面优先,构建时逐个实测)

源组织为三层:**A 国外原生 RSS(开箱可用)**、**B 国内(经 RSSHub 接入)**、**C 一级市场/融资(单列,最难补)**。

**构建时要求**:用 feedparser **逐个实测** A 层每个 URL;能解析出条目的写入 config 并标注赛道;解析失败或为空的注释掉,并在 README 的「源状态」小节列出。**不要假设任一 URL 永久有效**。B、C 层按说明接入。A 层英文源能跑通即可让系统整体可运行;B、C 层供用户后续接入。

### A 层:国外原生 RSS(英文,开箱可用,region: global)

下列地址为常见 feed 形式,以实测为准。

**A1. 芯片 / 算力**
| 媒体 | Feed |
|---|---|
| IEEE Spectrum (Semiconductors) | `https://spectrum.ieee.org/feeds/topic/semiconductors.rss` |
| SemiWiki | `https://semiwiki.com/feed/` |
| EE Times | `https://www.eetimes.com/feed/` |
| EDN | `https://www.edn.com/feed/` |
| Semiconductor Today | `https://www.semiconductor-today.com/rss.shtml`(见站点 RSS 页) |
| Tom's Hardware | `https://www.tomshardware.com/feeds/all` |
| TechXplore (Semiconductors) | `https://techxplore.com/rss-feed/semiconductors-news/` |
| arXiv 硬件架构 cs.AR | `http://export.arxiv.org/rss/cs.AR` |

**A2. 具身智能 / 机器人**
| 媒体 | Feed |
|---|---|
| The Robot Report | `https://www.therobotreport.com/feed/` |
| IEEE Spectrum (Robotics) | `https://spectrum.ieee.org/feeds/topic/robotics.rss` |
| Robohub | `https://robohub.org/feed/` |
| New Atlas (Robotics) | `https://newatlas.com/robotics/index.rss` |
| TechCrunch (Robotics) | `https://techcrunch.com/category/robotics/feed/` |
| TechXplore (Robotics) | `https://techxplore.com/rss-feed/robotics-news/` |
| arXiv 机器人 cs.RO | `http://export.arxiv.org/rss/cs.RO` |

**A3. 数据 / AI / 综合科技(含融资信号)**
| 媒体 | Feed |
|---|---|
| TechCrunch (AI) | `https://techcrunch.com/category/artificial-intelligence/feed/` |
| The AI Insider(AI 融资/并购,融资信号主力) | 原生 RSS,实测确认地址 |
| Ars Technica | `https://feeds.arstechnica.com/arstechnica/index` |
| The Verge | `https://www.theverge.com/rss/index.xml` |
| Wired | `https://www.wired.com/feed/rss` |
| MarkTechPost | `https://www.marktechpost.com/feed/` |
| Unite.AI | `https://www.unite.ai/feed/` |
| Simon Willison(个人,高质量 LLM) | `https://simonwillison.net/atom/everything/` |
| arXiv AI cs.AI | `http://export.arxiv.org/rss/cs.AI` |

> arXiv 偏学术、量大,打分阈值会自然过滤掉绝大多数,保留作"技术信号"层。综合科技源(Verge/Ars/Wired)量大噪音多,靠 AI 打分过滤或调高门槛。

### B 层:国内源(经 RSSHub 接入)

国内媒体原生 RSS 普遍缺失,需自建 **RSSHub**(开源:`https://github.com/DIYgod/RSSHub`)生成 feed。最简部署:Docker 一行起一个实例,再用 `https://<你的rsshub域名>/<路由>` 作为 feed 填进 config。

**要求**:在 README 写明 RSSHub 的 Docker 部署方式;在 config.example.yaml 的国内源处用占位注释标明路由方向,**不要写死可能失效的路由**,具体以 RSSHub 官方文档 `https://docs.rsshub.app` 为准。下表为目标媒体 + 路由方向:

| 媒体 | 赛道 | 路由方向(查文档确认) |
|---|---|---|
| 36氪 | 数据/综合 | `/36kr/...` |
| 机器之心 | 具身/数据 | `/jiqizhixin/...` |
| 量子位 | 具身/数据 | 公众号路由或 `/qbitai/...` |
| 雷峰网 leiphone | 具身/芯片 | `/leiphone/...` |
| 虎嗅 | 综合 | `/huxiu/...` |
| 钛媒体 | 综合/创投 | `/tmtpost/...` |
| 半导体行业观察(公众号) | 芯片 | 微信公众号路由(**不稳定**) |
| 芯东西 / 智东西(公众号) | 芯片/具身 | 微信公众号路由(**不稳定**) |
| 微博(指定公司/KOL) | 全赛道 | `/weibo/user/<uid>` |
| 知乎专栏(指定专栏) | 全赛道 | `/zhihu/zhuanlan/<id>` |

### C 层:一级市场 / 融资(投资最关键,也最难补)

| 来源 | 接入方式 | 说明 |
|---|---|---|
| The AI Insider | 原生 RSS(已并入 A3) | 英文 AI 融资/并购,质量高 |
| IT桔子 | RSSHub 路由或网页定期抓取 | 国内融资数据库,部分付费 |
| 投中网 / 铅笔道 / 创业邦 | RSSHub 路由(查文档) | 国内创投媒体,覆盖融资动态 |
| Crunchbase | 付费 API,无好 RSS | 作人工补充,不强求自动化 |
| PitchBook / The Information | 付费墙 | 抓不到,人工/付费 |

### 注定的缺口(覆盖面天花板,需接受)

- **微信公众号**:国内一手信息主阵地,但封闭;RSSHub 能抓部分但**不稳定、易失效**,无法可靠全覆盖。
- **付费墙 / 专业数据库**:自动化抓不到。
- **未公开 deal、闭门会、线下**:任何系统都覆盖不了。

应对策略已写进系统设计:**A/B/C 层尽量多接(广覆盖)+ AI 打分阈值从严 + 每赛道限定条数(控总量)**——覆盖在抓取层做加法,质量在筛选层做减法。

### 接入节奏建议

1. 初期把 A 层全部接入,先让系统转起来(开箱可跑)。
2. 再搭 RSSHub 接 B 层国内源,每加一个观察其条目质量,差的去掉。
3. C 层按表能接的接,接受部分靠人工。
4. `score_threshold` 初设 6.0,源接多后噪音大可上调至 6.5–7;`max_per_track` 设 4–5,控制每天总量在十几条内。

---

## 10. 错误处理与日志

- 全程用标准 `logging`,INFO 级别打印每一步进度和计数。
- RSS 单源失败:warning + 跳过 + 计入"失败源"列表,最后汇总。
- LLM 批次解析失败:warning + 跳过该批 + 继续。
- 某赛道当天 0 条:正常,模板中该赛道显示"今日暂无相关动态",不报错。
- 邮件失败:error 日志 + 退出码非 0(便于 CI 报警),但 HTML 已落盘。

---

## 11. 测试与验收标准

完成后请**自行执行并确认**:

1. `python brief.py --dry-run` 能跑通,在 `output/` 生成示例 HTML;打开后界面符合第 5 节规格(三赛道分区、彩色徽章、投资视角高亮块、tier 标签、conflict_note 提示、响应式)。请把这个示例 HTML 作为交付的一部分展示。
2. 对第 9 节英文 RSS 源做一次真实抓取测试,报告哪些可用、哪些失效(失效的在 config 注释并在 README 记录)。
3. `python brief.py --no-email` 在配了 LLM key 的情况下能完整跑通抓取→打分→渲染(若无 key,说明如何配置后即可)。
4. 代码通过基本检查:无语法错误、关键函数有类型注解和注释、无硬编码密钥。
5. README 完整:项目简介、架构图或流程说明、本地运行(含 dry-run 预览)、Docker 运行、GitHub Actions 部署(含需配置的 Secrets)、如何增删 RSS 源、中文源接入 RSSHub 说明、成本说明(用 DeepSeek 等的大致量级)。

---

## 12. README.md 必含章节

简介与特性 → 效果预览(放 dry-run 生成的截图或 HTML 链接)→ 工作流程 → 快速开始(本地 / Docker,含 `--dry-run` 预览)→ 配置说明(AI、源、过滤、邮件)→ 中文源接入(RSSHub)→ GitHub Actions 定时部署(需配置的 Secrets 列表)→ 自定义(增删源、调阈值、改每赛道条数)→ 源状态(实测结果)→ 成本与许可。

---

**完成标准回顾**:一个文件数极少、依赖极轻、`--dry-run` 即可预览精美界面、英文源开箱可跑、中文源有清晰接入路径、可 Docker 复现、可 GitHub Actions 定时推送邮件的三赛道投研简报系统。请开始构建,并在结束时展示 dry-run 的界面成果和 RSS 实测报告。
