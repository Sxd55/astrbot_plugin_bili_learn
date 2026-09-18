# Savage Evolution（astrbot_plugin_bili_learn）

从 B 站**公开视频**自动学习短摘要，写入 AstrBot 官方知识库；用户发链接/BV 号时 AI 可**按需读取单个视频**，也可以**主动查询已学知识**（检索、读全文、看概览）并用自己的人设回复。Cookie 选填，不点赞、不评论、不私信、不投币、不改人格文件。

当前版本 `v1.11.1`。需要 AstrBot `>= 4.5.7`（官方知识库 + LLM 工具；兴趣词表格需要 `>= 4.10.4`）。纯标准库实现，无第三方依赖。

和 Savage Type 的分工：本插件管「世界知识」；Savage 管「谁说过什么」。本插件**不写** Savage 事实库，主链检索走 AstrBot 知识库 RAG。

---

## 一、功能总览

### 1. 自动学习（后台）

- 按**兴趣词列表顺序**搜索公开视频：第一个词刷够每轮目标，再刷下一个词。
- 拉取标题、UP、分区、简介、时长；读多 P 字幕（默认前 3 P，可配 0=全部），优先中文轨；超长字幕取开头/中间/结尾三段。
- 用模型生成结构化短文档：概括标题 + 分区 + 相关度评分 + 3–7 条要点（标明「视频摘要，非亲历」）。
- 写入 AstrBot 官方知识库，文档名 `分区｜概括标题.md`，同类型自动聚在一起。
- **关键词门禁**：模型判为「其他」或相关度低于阈值（默认 80）的视频不入库，标 `off_topic` 且不重试。
- **三层去重**：bvid 状态机、知识库同名文档查重、延期视频复用缓存摘要——同一视频只学一次。

### 2. 按需读（对话中）

- 注册 `bilibili_read` 工具：用户发 B 站链接、BV/av 号、b23.tv 短链，AI 自动调用。
- 工具返回摘要素材，由 AI 按人设自然回复（不是机械贴摘要）。
- 同一视频默认也写入知识库（可关），重复发链接直接返回缓存摘要，不再调模型。
- 非关键词视频只回复、不入库。

### 3. 知识查询（对话中）

- 注册 `bilibili_knowledge` 工具：用户问某个主题、让你总结学过的知识、问「你学过什么」时，AI 主动调用。
- 三种用法：`query` 语义检索（知识库向量检索 + 重排）、`doc_name` 读单篇文档全文、两者留空看知识库概览（已学主题列表）。
- 可连续调用：概览找主题 → 检索 → 读全文，直到能回答用户；返回内容带「非亲历」声明，要求基于返回内容回答、不许编造。
- **兜底**：知识库检索不可用（没配 Embedding、KB 未创建、接口报错）或没有命中时，自动回退本地 SQLite 摘要库做关键词匹配，并给出 BV 链接。
- 管理员可用 `/bilearn search` 手动验证入库效果。

### 4. 知识组织（汇总）

- 某关键词「未并入」的视频达到阈值（默认 10 条）后自动汇总，也可 `/bilearn digest` 手动触发。
- 汇总文档 `【汇总】分区｜主题知识.md`，结构固定：文件头 + 每轮 `## 第 N 轮新增（日期，来源：BV…）`。
- **只增不删**：模型只被允许输出「已有汇总里没有的新知识」，代码负责机械追加，旧内容永不重写、永不删除。
- 生成后由模型**复审**一次，有问题整轮不写入并冷却 24 小时。
- 成功后按配置删除原视频文档（本地 SQLite 仍留摘要和素材，可重建）。

### 5. 可信度审核

- 入库时留存素材（送进摘要模型的字幕摘录/简介）。
- 定期（默认 7 天）对照素材检查文档有没有虚构事实，只标记 `ok/suspect/error` 不修改内容。
- 每天限审 N 篇摊平成本；`/bilearn audit` 查看统计和可疑列表。

### 6. 调度与配额

- **每日定时**：默认每天凌晨 1 点（北京时间）触发，从该时间起循环轮次，直到每个关键词的当日配额刷完或没有新候选才停。
- **配额模式**（默认）：每个关键词每天入库上限（默认 3，可单关键词覆盖），配额按最终归类计数，超出转「延期」第二天直接入库。
- **无限模式**：不设每日上限，开启后**立即开始**，一轮接一轮直到某轮 0 新增；有 `run_max_videos`（默认 100 条/次）和 20 轮安全上限防失控。

### 7. 监控与运维

- 内置新拟物风格监控页（5 套配色 + 自定义）：
  - 运行记录与阶段时间线（含北京时间戳）
  - 视频处理状态（归类、尝试次数、doc_id、审核状态）
  - 汇总文档列表、审核统计
  - 兴趣词可视化编辑（每行一个词 + 每轮数量，保存写回插件配置）
  - 健康检查（只读）、无限模式开关、手动试跑（后台排队）
  - 页面 30 秒自动刷新，编辑兴趣词时不会被打断
- 管理员命令：状态、手动运行、单视频读取、配额查看/重置、汇总、审核。

---

## 二、技术实现

### 1. 代码结构

| 文件 | 职责 |
| --- | --- |
| `main.py` | 插件生命周期、命令、监控页 Web API、LLM 工具注册、定时任务、配置保存、Provider 解析 |
| `bili/client.py` | B 站 HTTP 客户端：WBI 签名、浏览器指纹、搜索/view/字幕接口、节流与风控 |
| `bili/throttle.py` | 请求间隔抖动 + 全局冷却 |
| `bili/reference.py` | 从消息里提取 BV/av/b23/bili2233/`bilibili://`/QQ 小程序链接；命令参数解析 |
| `bili/pipeline.py` | 主编排：顺序轮次刷取、配额与延期、摘要入库、汇总、审核 |
| `bili/ingest.py` | 提示词、模型输出解析、长文采样、视频文档与汇总文档渲染 |
| `bili/store.py` | SQLite 审计库：状态机、重试退避、每日配额计数、汇总表、审核字段、自动迁移 |
| `bili/tool.py` | `bilibili_read` / `bilibili_knowledge` 两个 FunctionTool |
| `bili/query.py` | 查询结果处理：工具参数解包（兼容 arguments 多层包装）、给模型/人看的格式化 |
| `bili/runlog.py` | run_id、北京时间工具 |
| `pages/monitor/` | 监控页（HTML/CSS/JS，新拟物风格） |

### 2. B 站接口层（不依赖第三方库）

- **WBI 签名**：先从 `/x/web-interface/nav` 拿 `img_key/sub_key`，按当前混合表生成 mixin key，再对参数排序拼接 + `wts` 计算 `w_rid`（MD5）。搜索等接口必须签名，否则 412。
- **浏览器指纹**：启动时调官方 `/x/frontend/finger/spi` 获取 `buvid3/buvid4`，失败则生成标准 `UUID+infoc` 回退；与 `SESSDATA` 放进**同一个 CookieJar**统一发送——避免「有 Cookie 缺指纹」被风控拦截（也修掉了手动 Cookie 头会覆盖 jar 指纹的坑）。
- **字幕三级保障**：优先 WBI 签名 `player/wbi/v2`（带 `fnval`）；412 或缺 URL 自动回退 `player/v2`；兼容 `subtitle_url_v2`。多轨按中文优先排序，单轨下载失败换轨；可选**标题-字幕重叠校验**，拦下 B 站偶发的「指向其他视频的缓存字幕」。
- **多 P 合并**：读取前 N P 字幕，按 `[P1 标题]` 分段合并。
- **节流与风控**：最小间隔 + 随机抖动；请求串行化（异步锁）；命中 `-799/-509` 或 HTTP 412 触发全局冷却并立即中止本轮，不硬打。

### 3. 调度与状态机

- **SQLite 状态机**：`ingested / excluded / no_subtitle / failed / deferred` 五种终态或可重试态，记录 `attempts`、`next_retry_at`。
- **分级重试**：网络/模型失败退避 1h/6h/24h 最多 3 次；无字幕 7 天后最多 2 次；知识库故障无限重试但按退避冷却（修掉了每轮重抓重调模型的浪费）；已排除/已入库永不重试。
- **延期机制**：归类配额已满时，把摘要、文档名、素材一起缓存为 `deferred`，次日直接入库，**不再重复调模型**。
- **每日配额**：`daily_stats(keyword, day, ingested)` 按北京时间日切；`/bilearn quota reset` 可重置。
- **定时任务**：注册 AstrBot cron `0 {daily_start_hour} * * *`（`Asia/Shanghai` 时区，失败回退系统时区），启动时清理同名旧任务。
- **运行锁 + 后台队列**：同一时间只跑一轮；手动试跑/无限模式走后台任务排队，HTTP 请求立即返回，不会卡死页面。

### 4. 知识库集成

- 通过 AstrBot `kb_manager` 按名查找/创建知识库，`upload_document` 写入，`list_documents` 按名查重，`delete_document` 在汇总合并后删除原文档。
- 只有拿到 `doc_id` 才算入库成功；同名文档存在时复用其 `doc_id`，不重复写入。
- 删除前检查 `doc_id` 引用计数，多个视频共用同一文档时不误删。
- 查询走 `kb_manager.retrieve`（语义检索 + 可选重排）；读全文走 `list_documents` + `get_chunks_by_doc_id` 按 chunk 顺序拼接；不可用时回退 SQLite `search_local`（LIKE 已转义通配符）。

### 5. LLM 调用设计

- **结构化输出**：提示词要求固定格式（标题/分区/相关度/要点），解析失败逐级兜底（标题用视频原标题、分区用关键词命中）。
- **双模型路由**：摘要与汇总生成走 `summary_provider_id`；汇总复审、可信度审核这类机械核对走 `utility_provider_id`（建议配便宜模型），留空跟随摘要模型。
- **任务分档**（v1.11.0）：新增「精准模型 `quality_provider_id`」与「快速模型 `fast_provider_id`」两个档位，分别覆盖摘要/汇总与复审/审核；显式单任务配置优先于档位，都留空则跟随当前会话模型，「实际用了哪个」会记账到面板。
- **Token 预算闸**（v1.11.0）：`daily_token_limit` 硬限额达到后停止一切内部调用，本轮以 `budget_limited` 收尾（不把视频标成失败）；`soft_token_limit` 达到后只停复审/审核，摘要与汇总继续；`single_call_token_cap` 预估输入超限时改用 `fallback_provider_id`。消耗按任务记账（含 Provider 无 usage 时的本地估算），面板与 `/bilearn status` 可见今日 Token、限额与「预算跳过 N 次」。
- **拒答重试**（v1.11.0）：模型返回「无法协助/作为一个AI」这类拒绝短语时，自动换 `fallback_provider_id` 重试一次，避免把拒答当摘要写进知识库。
- **成本控制**：字幕头中尾采样（默认 12000 字）、审核素材限长（默认 4000）、复审来源采样 8000 字、汇总来源最多 30 条。

### 6. 汇总的「只增不删」实现

- 汇总内容存 SQLite `digests` 表，正文按「轮次小节」存储。
- 每轮：模型只看「已有汇总 + 新摘要」，只输出新要点；代码把 `## 第 N 轮新增` 追加到正文末尾，文件头按最新轮次重新渲染——**知识正文不做任何重写或删除**，不依赖模型自觉。
- 复审通过才写入；失败记录原因并 24 小时冷却，自动模式会跳过冷却中的关键词继续处理其他词。

### 7. 监控页技术

- AstrBot 插件 Pages + bridge API（`apiGet/apiPost`），后端注册 11 个 Web API。
- 新拟物风格：双阴影（左上亮/右下暗）、同色系表面、hover 阴影缩小、active 内凹、输入框内凹且 focus 变浅、无渐变/无纯白黑背景/无位移，`prefers-reduced-motion` 与键盘焦点可见性。
- 配色 5 套预设 + 自定义（背景/暗阴影/高光/强调色），持久化在插件配置（iframe 沙箱不能用 localStorage）；深色自定义自动反色文字与状态色。
- 兴趣词编辑器直接写回 `_conf_schema.json` 的 `template_list` 配置（`save_config`），失败会明确提示。

---

## 三、解决了什么问题

| 问题 | 方案 |
| --- | --- |
| B 站搜索接口 412（缺少 WBI 签名） | 动态获取 WBI 密钥并签名所有相关请求 |
| 有 Cookie 但缺浏览器指纹被风控 | SPI 获取 buvid3/buvid4 + 统一 CookieJar |
| 字幕接口不稳、返回错轨缓存 | `player/wbi/v2` + 回退 + 多轨排序 + 标题校验 |
| 限流 -799/-509 后任务硬打 | 全局冷却 + 立即中止，冷却期内自动等待 |
| 同一视频被重复学习、重复烧 token | bvid 状态机 + 知识库同名查重 + 延期缓存复用 |
| 一次临时错误导致视频永久跳过 | 分级重试状态机（退避 + 上限） |
| 知识零散、检索碎片化 | 按关键词汇总成主题知识文档 |
| AI 汇总改写会丢知识 | 机械追加式「只增不删」结构 |
| 文档可能编造内容 | 留存素材 + 定期对照审核（只标记不篡改） |
| 兴趣词混刷、配额失控 | 顺序刷取 + 每日配额 + 无限模式安全上限 |
| 无限模式开关点了不生效 | 开启即排队启动（重载/监控页开关都触发） |
| 管理黑盒、出问题难查 | 监控页运行时间线 + 审计库 + 只读健康检查 |
| 用户发链接 bot 无反应 | `bilibili_read` 工具 + 人设化回复 |
| AI 答不出「你学过的内容」 | `bilibili_knowledge` 工具：语义检索 + 读全文 + 概览，检索不可用回退本地摘要库 |
| Token 成本高 | 双模型路由 + 多层采样 + 去重 + 配额调度 |

---

## 四、安装与配置

目录名保持 `astrbot_plugin_bili_learn`。拷到 `AstrBot/data/plugins/` 后重载。

配置顺序：

1. 在 AstrBot 配好 Embedding Provider（知识库创建时必填）。Rerank 可选，有预算建议配。
2. 插件设置里填写 Embedding（以及可选 Rerank）、兴趣词与每轮数量。
3. Cookie 可空。要读登录可见的 AI 字幕再填 SESSDATA，当最高权限保管，不要发到群或日志。
4. 知识库名称默认 `Bili Learn`。选定 Embedding 后不要改该库的向量维度。
5. 聊天配置里把这个知识库绑到要用的会话，否则主链不会自动检索。

### 常用配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | true | 总开关，关闭后定时与工具全部停用 |
| `kb_name` | Bili Learn | 知识库名称；选定 Embedding 后不要改该库的向量维度 |
| `sessdata` | 空 | B 站 SESSDATA（选填），只用于读登录可见的 AI 字幕 |
| `embedding_provider_id` / `rerank_provider_id` | 空 | Embedding（知识库必填）与 Rerank；留空自动选第一个可用 |
| `exclude_keywords` | 空 | 标题/简介/字幕含这些词的视频直接排除，逗号分隔 |
| `request_interval_seconds` | 3 | B 站请求最小间隔秒数（0.5–30），风控冷却另算 |
| `interest_quotas` | 空 | 兴趣词 + 每轮入库数量（表格，顺序即刷取顺序）；留空回退到 `keywords` |
| `keywords` | 聊天技巧,暧昧拉扯技巧,AI,SKILLS,提示词,科技,数码,构图,审美,艺术 | 回退用关键词；同时作为知识库分区名 |
| `unlimited_mode` | false | 无限模式：不设每日上限，一轮接一轮；**开启后立即开始刷取**，token 消耗大 |
| `run_max_videos` | 100 | 单次运行最多处理条数（含无限模式多轮），防失控 |
| `daily_start_hour` | 1 | 每天几点开始自动刷取（0-23）；-1=只手动 |
| `daily_per_keyword` | 3 | 配额模式下每关键词每天最多入库数；0=不限 |
| `daily_quota_overrides` | 空 | 单关键词配额覆盖，如 `AI:5,科技:2` |
| `summary_provider_id` | 空 | 摘要与汇总生成用的模型；留空跟随当前会话 |
| `utility_provider_id` | 空 | 复审/审核用的便宜模型；留空跟随摘要模型 |
| `quality_provider_id` / `fast_provider_id` | 空 | 任务档位：精准档（摘要/汇总）、快速档（复审/审核）；显式配置优先于档位 |
| `fallback_provider_id` | 空 | 单次请求超 Token 上限或模型拒答时的备用模型 |
| `daily_token_limit` / `soft_token_limit` | 0 | 每日 Token 硬限额 / 软限额（0=不限）；软限只停复审与审核 |
| `single_call_token_cap` | 0 | 单次请求预估 Token 上限（0=不限），超出改用备用模型 |
| `category_min_score` | 80 | 模型给分区的相关度低于该值时归入「其他」不入库；0=关闭 |
| `subtitle_page_limit` | 3 | 多 P 读取数量，0=全部 |
| `subtitle_max_chars` | 12000 | 送入模型的字幕上限，超长取头/中/尾 |
| `subtitle_verify` | true | 标题-字幕匹配校验，拦截错轨字幕 |
| `missing_subtitle` | desc_only | 无字幕时用简介兜底（skip=跳过并限次重试） |
| `max_duration_minutes` | 120 | 后台学习时长上限，按需读不受限 |
| `on_demand_enabled` | true | 注册 `bilibili_read` 工具，按需读视频 |
| `on_demand_ingest` | true | 按需读的视频也写入知识库（复用去重） |
| `query_enabled` | true | 注册 `bilibili_knowledge` 工具，AI 可查询已学知识 |
| `query_top_k` | 5 | 每次检索返回的知识片段条数（上限 10） |
| `consolidate_enabled` | true | 达阈值自动汇总（生成+复审两次调用） |
| `consolidate_threshold` | 10 | 某关键词未并入文档达到该数量触发汇总 |
| `consolidate_delete_sources` | true | 汇总成功后删除原视频文档（SQLite 留档） |
| `audit_enabled` | true | 定期可信度审核 |
| `audit_interval_days` | 7 | 超过该天数未审的文档进入审核队列 |
| `audit_daily_limit` | 20 | 每天最多审核篇数 |
| `audit_excerpt_chars` | 4000 | 审核时素材上限，越小越省 |

### 配额与调度说明

- 刷取顺序 = 兴趣词列表顺序：前一个词刷到每轮目标（或每日剩余额度）后再刷下一个。
- 配额按**最终归类**计数；处理完归类后还有一道强制拦截，超出额度的视频转「延期」。
- 按需读（工具/`/bilearn read`）不占配额；`/bilearn once` 批量运行占配额。
- 配额模式：每天到点后循环轮次，每轮每个关键词的目标 = min(每轮数量, 当日剩余)，直到刷完或没有新候选。
- 无限模式：开启后立即开始，一轮结束立即下一轮，直到某轮 0 新增；中途关闭，正在跑的任务会在下一轮按配额模式收敛停止。
- 文档命名规则：`分区｜概括标题.md`；已入库的旧文档不会自动改名。

### 省 token 设计

- 双模型路由（生成用主模型，复审/审核用工具模型）。
- 查询走知识库检索（Embedding/Rerank）不回灌模型；本地兜底是 SQLite 匹配，零 token。
- 工具返回内容限长（单条 900 字、总计 6000 字），防止塞爆上下文。
- 去重三层：bvid 状态机、知识库同名文档、延期缓存复用。
- 多层限长：字幕采样 12000、审核素材 4000、复审来源 8000。
- 配额刷完即停；无限模式有单次条数与轮次上限。

---

## 五、命令（管理员）

| 命令 | 说明 |
| --- | --- |
| `/bilearn status` | 知识库、入库/失败/汇总/审核统计、定时与下次运行时间 |
| `/bilearn once` | 立刻跑一轮 |
| `/bilearn read <BV号或链接>` | 不经 AI，直接读单个视频并返回摘要 |
| `/bilearn search [关键词]` | 查询知识库学过的内容；不带参数列出概览（分区、汇总主题、最近入库） |
| `/bilearn recent [n]` | 最近处理的 bvid（带时间戳） |
| `/bilearn quota` | 查看今日各关键词配额用量与每轮目标 |
| `/bilearn quota reset` | 重置今日配额计数 |
| `/bilearn digest [关键词]` | 立即汇总（不带参数自动挑未并入最多的关键词） |
| `/bilearn audit` | 查看审核统计和可疑列表 |
| `/bilearn audit run [n]` | 立即审核 n 篇（默认按每日上限） |

定时：`daily_start_hour`（默认 1，即每天凌晨 1 点，北京时间）注册 cron。到点后按兴趣词顺序刷取，直到每个关键词的当日配额都刷完或没有新候选；-1 表示只手动跑。

---

## 六、明确不做

评论、弹幕、私信、投币、点赞、收藏、视频下载、ASR 语音转写、HTML/思维导图、人格进化、把全文当 Savage 事实。Cookie 只用于读字幕，仍不发评。

---

## 七、参考与许可

学习流水线参考 [astrbot_plugin_b-](https://github.com/mjy1113451/astrbot_plugin_b-)（MIT）的「看 → 理解 → 归档」与字幕/风控实践；链接提取、多 P 字幕与采样参考 [astrbot_plugin_bilibili_parser](https://github.com/xiaowan138/astrbot_plugin_bilibili_parser)（MIT）；按需读的工具形态参考 [astrbot_plugin_biliread](https://github.com/SodaCodeSave/astrbot_plugin_biliread)（AGPL-3.0，仅思路，未复制代码）。本插件未移植其互动、下载与人格模块。

---

## 八、测试

```text
python tests/test_core.py -v
```

102 个测试，只用 Python 3.11+ 标准库，不联网。集成测试（23 个：真实 AstrBot 框架 + 假 KB/假 B 站/脚本化假模型，覆盖插件加载、全部命令、两个 LLM 工具、面板接口、配额/延期/汇总/审核全链路，以及任务分档、Token 预算闸、拒答重试；需 `pip install astrbot`）：

```text
python tests/test_integration.py -v
```

另有 1 个真实 B 站网络探针（只读：搜索 + 详情 + 字幕），默认跳过，`BILI_LIVE=1` 才跑。
实机测试流程见 `SOAK.md`。`tests/verify_readme.py` 核查文档里的命令、配置、默认值与版本号是否和代码一致。
