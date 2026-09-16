# AI 与数据日报（TriBrief）

每天采集国内外 AI 和数据新闻，生成中文摘要、简短影响分析和 HTML 邮件。
目标每天15–20条：AI最多12条、数据最多8条；两栏优先选国内、国外各2条合格新闻，不足不凑数。
主新闻为最近24小时；栏目未满时可补24–48小时的重要漏报，每栏最多2条，明确标为“重要补充”。

## 内容范围

- AI：大模型、多模态、智能体、应用、开源工具、重要研究与政策。
- 数据：数据集、采集标注、合成数据、数据库、数据工程、湖仓、治理、安全隐私、数据要素及交易。
- 芯片和机器人不独立成栏，直接影响上述领域的重要事件仍可入选。
- 过滤营销、教程、回顾、无实质进展的会议，融资不自动优先。
- “早报｜…”等混合多事件的汇总页不作为单条新闻入选，避免重复拼盘挤占名额。
- 国内外按事件主体或所在地判断，与媒体地区分开；跨国和未知单列。

## 使用

建议Python 3.11+：`pip install -r requirements.txt`。
复制 `.env.example` 为 `.env`，设置 `LLM_API_KEY`、`SMTP_USER`、`SMTP_PASSWORD`。
`config.yaml` 保留原模型与邮件设置。不要把密钥写入配置。

当前模型为 DeepSeek `deepseek-chat`，接口根地址 `https://api.deepseek.com/v1`。
本地 `.env` 的 `LLM_API_KEY` 应填写 DeepSeek 密钥；远端运行前也需要更新 GitHub 的同名 Secret。
每批5条，批次间隔2秒，通过 `ai.batch_size`、`ai.request_interval_seconds` 调整。切换其他兼容模型时可选配 `ai.thinking`。
429退避重试耗尽后停止后续批次，不自动切换模型。智谱错误1305表示模型当前访问量过大，可稍后重试。
输出被截断时会提高输出上限重试，仍失败则拆小批次；完整分析批次缓存于 `output/analysis_cache`。模型、提示词或新闻材料变化会使用不同缓存，失败结果不写入缓存。

```powershell
# 虚构版式预览：无模型调用、无邮件
python brief.py --dry-run
# 订阅与网页来源实测：无模型调用、无邮件
python brief.py --test-sources
# 真实新闻预览：调用模型，不发邮件、不写发送历史
python brief.py --no-email
# 正式运行：发送成功后写历史
python brief.py
# 手动扩大窗口，邮件会标明实际窗口
python brief.py --hours 48 --no-email
```

输出为 `output/briefing_YYYY-MM-DD.html`、`output/sources.json`、`output/candidates.json` 和 `output/selected_YYYY-MM-DD.json`。
同一天的示例、预览和正式HTML使用同名文件，后一次会覆盖前一次。

## 来源验证

2026-09-15本机实测，保持证书校验，加载系统信任证书。以下为快照，不代表长期可用。

| 来源 | 接入 | 解析条数 | 最近24小时（限量前） |
|---|---|---:|---:|
| IT之家 | RSS | 60 | 60 |
| 爱范儿 | RSS | 20 | 4 |
| 雷峰网 | RSS | 20 | 12 |
| TechCrunch AI | RSS | 20 | 11 |
| Google DeepMind | 官方RSS | 100 | 0 |
| Hugging Face | 官方平台RSS | 862 | 0 |
| 国家数据局 | 列表与正文 | 25 | 3 |
| Databricks | 列表与正文 | 12 | 1 |
| PingCAP | 官方RSS | 10 | 0 |
| MongoDB | 官方RSS | 50 | 0 |

量子位返回403，机器之心候选RSS返回网页，36氪解析0条，均未启用。
RSSHub不是默认依赖。新增来源应先核对文章链接、发布日期、正文和更新频率。

2026-09-16新增6个已解析出条目的来源：钛媒体（17条）、InfoQ中文（20条）、开源中国（50条）、InfoQ国际（15条）、Elastic（40条）、KDnuggets（10条）。这些是订阅总条数，并非全部符合新闻时间窗口。
BigDataWire/Datanami返回403；本次测试的Snowflake、Confluent和PingCAP中文RSS地址返回404，未启用。

## 时效、质量与历史

- 只采用发布时间，不用更新时间将旧文章变成新闻；无日期与未来日期排除。
- 网页只有日期时标明“仅日期”，按配置时区当天零点保守过滤，不伪造时分。
- 短摘要补采正文，每条最多6000字符进入模型；事实与影响分析分开，缺少证据时不推断竞品、估值或商业化时间。
- URL/标题先去重，再由模型生成跨语言事件标识，合并同次和历史事件；实质新进展使用新标识。
- 模型语义去重可能漏判或误判，不等同于全网事实核查。
- 默认开启跨批次终审，合并跨媒体、跨栏目的同一事件并校正分类，决策记录在 `output/editorial_review.json`；终审失败会停止生成和发信。不同产品和实质新进展分别保留。
- 默认14天历史位于 `state/sent.json`，预览不写；历史损坏不静默重置。
- 抓取异常、地区缺口和截止时间在邮件中显示；分析缺项停止发信，正常分析后全部过滤可生成空栏。
- 没有候选且来源异常时停止发信，避免把采集失败误报为没有新闻。
- 主栏评分门槛5.5分；补充新闻至少6.5分，每栏最多2条，不能挤掉合格的24小时新闻。
- `--hours`调整主窗口；采集窗口取主窗口与 `supplement_window_hours` 的较大值。无合格补充时不凑数。
- 已有成功试发回执会按收件人、文件摘要与日期验证，回执中的原文链接参与排重，避免昨天试发内容被重新补入。试发回执只支持链接排重，跨媒体事件仍依赖模型标识。

## 配置与部署

`filtering.max_per_track` 当前为 `{ai: 12, data: 8}`；`min_per_region` 控制国内外目标数量。
`supplement_window_hours: 48`、`supplement_max_per_track: 2`、`supplement_score_threshold: 6.5`控制补充规则。
默认配置多数来源最多25条候选，IT之家40条、开源中国30条；两者按标题主题词优先取候选，再交模型判断，避免通用科技资讯先占满候选名额。源条目仍可由模型重新分类。
网页源使用 `type: html`、`selector`、`link_pattern`、`timezone_hours`、`max_articles`。
`kind: official` 只是来源提示，官方转载不自动为一手。

```powershell
docker compose run --rm tribrief --no-email
```

Docker挂载配置、`output/`和`state/`，凭据通过 `.env` 注入。
GitHub Actions每日北京时间08:00（日本09:00）运行，支持手动触发。在仓库Secrets设置上述三个环境变量。
Actions Cache恢复/保存发送历史，工作流串行执行。缓存过期或被清除可能丢失历史，本地和远端历史不自动共享。
SMTP与历史文件不是原子事务：发送成功后崩溃、响应不明或缓存保存失败仍可能重复，需查运行日志。
配置文件随仓库提交，注意收件人隐私。更新本地工作流不代表已经远端部署或验证邮件投递。

## 测试

另装pytest后运行 `python -m pytest -q`。
覆盖旧闻、无日期、未来日期、历史去重与新进展、历史过期/损坏、国内外配额、模型缺项/全过滤、预览不发送不记账。
实际邮件客户端投递仍需单独验证。

`tribrief-build-spec.md`保留为历史构建记录，当前范围以本README和配置为准。许可证见 LICENSE。
