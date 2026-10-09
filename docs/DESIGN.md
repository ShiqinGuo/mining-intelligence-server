# 矿业业务服务与 MCP Gateway 设计

日期：2026-10-08。本文是 mining-intelligence-server 独立项目的设计入口。服务端与通用 client 是两个项目，不共享源码、依赖锁文件、迁移或部署配置，仅通过 MCP 协议互通。PostgreSQL 是本项目业务数据、任务与检查点的事实来源。状态：首版已在本地 Docker 联调，三份真实报告的订阅模型抽取已通过基准记录校验，并经 HTTP 与 MCP 读取验证；范围与限制见第 10 节。

## 1 目标与已决事项

建立具有独立业务价值的矿业 HTTP 服务，提供新闻、资源报告、行情三类能力。MCP Gateway 通过封装的 HTTP Client 调用业务服务；通用 client 负责理解用户目标、选择证据并生成带引用的 Markdown 简报。

已确定：Gateway 与业务后端位于本独立仓库，两个应用 svc，FastAPI 异步接口，PostgreSQL + SQLAlchemy asyncio，Celery + RabbitMQ + Beat，显式 workflow + LLM，检查点存 PG。首版不引入 Redis、LangChain、LangGraph，不建设注册、多租户、计费或完整管理前端。信源及日常运行配置通过 HTTP 管理，OpenAPI 提供操作说明。后续服务器沿用本地 Docker 的部署结构。

源码先按服务划分，再在后端内部按职责分层。`apps/backend` 包含 API、Service、内部领域类型、Repository、采集与模型适配器、Worker 和迁移；`apps/mcp_gateway` 包含 MCP 适配、HTTP Client、自有配置与错误处理。两个服务分别声明依赖、分别构建镜像，互不导入实现。`packages/contracts` 是双方消费的公开 HTTP 模型、错误响应及相关枚举，仅依赖 Pydantic；执行状态机、任务租约、模型凭据与 ORM 仍属于后端。根 uv workspace 以一份 lockfile 固定版本，生产镜像只安装指定服务的依赖闭包。

后端测试放在 `apps/backend/tests`，Gateway 测试放在 `apps/mcp_gateway/tests`，跨服务边界检查放在 `tests/integration`。后端继续使用 `mining_server` 包名，Gateway 使用 `mining_gateway`；已发布迁移内容保持不变，其中仍消费的 `StepKind` 导入由后端领域模块显式保留。迁移与命名卷位置保持连续，目录重构不改变数据库表、HTTP 路径、MCP 入口或任务载荷。

2026-10-09 服务目录拆分已通过 Linux 完整回归 273 项、零跳过，包含新增的三个导入边界检查。37 个公开模型的输入校验与输出序列化 Schema 与原版本一致；外部依赖版本未改变。两个独立镜像已构建并部署本地，Gateway 镜像不含后端源码、SQLAlchemy、Celery、PyMuPDF、asyncpg、Alembic 或 FastAPI。后端与 Gateway 健康检查、三个官方 MCP SDK 握手、迁移差异检查及实际行情查询通过。CI 已同步新的测试、样本与迁移目录。

第 2 题要求至少三个 MCP server：新闻 `search(query, days)`、`fetch_article(url)`，文档 `extract_resources(pdf_url)`，行情 `get_price(commodity, date)`、`get_trend(commodity, days)`；另含自写 Agent、引用充分的 Markdown、mcp-config.json、docs/RUN.md、Docker Compose 和五分钟启动。其他题目的采集数量、向量库和多 Agent 审核轮数不纳入本节。

Gateway svc 内承载三个独立 MCP server 实例，入口为 `/mcp/news`、`/mcp/documents`、`/mcp/market`。各自拥有 server identity、发现结果和工具目录，在 mcp-config.json 中配置成三个可独立连接的 server；不是把一个 server 的三个工具组计作三个 server。业务后端仍是一个服务，不另拆三个业务微服务。

## 2 部署与调用边界

~~~mermaid
flowchart TD
    Client[Agent Client] --> Gateway[MCP Gateway svc]
    Operator[HTTP 调用方] --> API[业务后端 svc：FastAPI]
    Gateway -->|具名 HTTP Client| API
    API --> PG[(PostgreSQL)]
    Publisher[后端待投递记录发布器] --> PG
    Publisher --> MQ[RabbitMQ]
    Beat[后端 Celery Beat] --> MQ
    MQ --> Worker[后端 Celery worker]
    Worker --> PG
    Worker --> Sources[免费信源]
    Worker --> Model[OpenAI 兼容 API / 可选 ChatGPT 订阅]
    API --> Files[持久化文件卷]
    Worker --> Files
~~~

- `mcp-gateway`：MCP SDK、工具契约和业务 HTTP Client；不访问业务数据库，不持有模型 OAuth 令牌，不运行采集或文档 Agent。
- `mining-backend`：由进程管理器分别运行 FastAPI、Celery worker、单实例 Beat。待投递记录发布器随 API 生命周期运行，各进程有独立健康与退出状态，停止时有界清理。
- `postgres`、`rabbitmq`：基础设施 svc，另计；默认 Compose 为四个服务条目，其中两个是应用 svc。PG、MQ、文件均使用持久化卷。
- 开发阶段先使用本地 Docker Linux 容器；API 初始单 worker，Beat 单实例。不增加独立 worker svc、搜索引擎或对象存储服务。

BackendClient 使用具名方法，集中连接池、认证、超时、请求追踪、Response 校验与错误映射；不接受任意目标 URL 充当通用代理。Gateway 不透明重放任务提交或模型调用；只读重试也有次数和总体预算。服务间不共享 ORM、Session 或可写数据库连接。

MCP 使用 `2026-07-28` 新协议及官方 Python SDK 已核验的 v2.3.0 基线；实施时锁定依赖。首版采用普通工具返回业务 task_id，不要求 MCP Tasks 扩展。支持其他 Agent 的范围以实际客户端互通测试为准，不承诺所有历史版本。

公网接入沿用本地 Docker，通过可选 `compose.tunnel.yaml` 启动独立 cloudflared 连接器。Tunnel token 从仓库外文件挂载为 Docker secret；Gateway 仅持有业务 service token。`MINING_PUBLIC_ORIGINS` 接受精确 HTTPS origin，构造时拒绝路径、用户信息、查询、fragment 和通配符；保留 SDK 的 Host/Origin 校验与 Bearer 认证。公网路由仅匹配三个 MCP 入口，管理 API 和健康接口不发布。PG 与 RabbitMQ 设置 `unless-stopped`，Docker 重启后恢复基础设施容器，持久化卷保持不变。

2026-10-09 已创建专用 Tunnel `mining-intelligence-local`，DNS 与 `mining-mcp.charworkservice.site` 路由已保存，路径限定 `^/mcp/(news|documents|market)/?$`，目标 `http://mcp-gateway:8001`。当前公网通道尚未上线：本机常规 DNS 缺少 Tunnel SRV 结果，绕过发现步骤的有界诊断仍在 edge TLS 握手收到 EOF，QUIC 超时；现有 SOCKS 出口也复现 TLS EOF。已撤下诊断覆盖配置，连接器使用正式 Compose 配置。需网络链路恢复后完成公网 SDK 握手、认证与业务查询验收，不能将路由创建计作接通。

## 3 工程契约与数据

源码和测试全英文，不写注释或 docstring；设计文档中文。封闭状态、类别、判别字段与错误码使用枚举，Literal 只引用枚举成员子集。开放的用户正文、URL 等仍是经校验的字符串。跨层 Request、Response、任务载荷、步骤结果、检查点和错误详情在 domain 定义具体 BaseModel，单文件内部结构使用 dataclass。JSON 仅存在于请求、响应和数据库边界，读取后立即校验成具体类型；不以 JsonValue、字典包装或 JSON 字符串掩盖未定义的业务契约。方法内确需协议映射时使用明确的 TypedDict，不跨方法传递；不使用反射、字典 get 或猜测字段的兼容分支。

任务按 TaskKind 选择具体输入与结果模型；步骤恢复按该步骤声明的结果类型读取 PG。TaskView.result 直接返回具体业务结果，steps 仅返回步骤键与完成时间，不公开任意检查点载荷。模型 SSE 按事件类型判别联合解析，工具参数在调用入口解析为该工具的参数模型；协议中 JSON Schema 的动态属性只在序列化边界保留，业务中使用具名类型。

字段构造与外部边界 fail-fast，无效显式配置不替换成默认值。平行分派用 match/case，独立校验保持 guard；泛型用 Python 3.12 `[T]`，不用 TypeVar。后端绝对导入从 `mining_server` 开始，Gateway 从 `mining_gateway` 开始，共享公开契约从 `mining_contracts` 开始。后端业务实现放 `mining_server.application`，内部跨层类型放 `mining_server.domain`；共享 HTTP 契约放 `mining_contracts.domain`。两个服务不导入对方实现，也不导入通用 client 的实现。

FastAPI 路由通过 Depends 注入 Service，Service provider 通过 Depends 组装 Repository 和 UnitOfWork；路由不绕过 Service 执行业务。后台任务用同一套显式依赖工厂，不调用 FastAPI Depends。每个并发 task 独立 AsyncSession，事务由 Service 组织，Repository 不隐藏提交。统一 ErrorCode、DomainError 和错误构造接口由 Service 使用，HTTP/MCP 转换仅在边界完成。

Server 使用独立 Alembic 迁移目录和版本表。先从模型生成迁移，再人工审查并补充 backfill；文件名含日期、revision 和用途。大数据回填分批、有游标、可重入，不在数据库事务中等待模型或网络，不修改已发布迁移。第三方 API 写代码前通过 Context7 核对实际版本，索引混版时看对应 tag 源码；依赖、容器版本在实施时锁定。

PG 保存 Source/SourceRevision、CollectionRun、Article/ArticleRevision/ArticleAnalysis、Project、Document/DocumentPage、ResourceExtraction/ResourceRecord、PriceInstrument/PriceObservation、TaskRun/StepRun/Checkpoint、ModelInvocation、OutboxMessage 与 ModelConnection。建表和关系只围绕真实消费者，不预建租户和动态插件平台。

PDF、页图和大正文放持久化文件卷，PG 保存 hash、相对对象标识、长度与类型。先写临时文件并原子定稿，再提交数据库引用；无引用孤立文件延迟清理。检查点引用不可变内容版本，不反复复制整份文档；Gateway 通过受认证 HTTP 读取内容，不挂载业务文件卷。

## 4 Workflow、队列与 PG 检查点

新闻采用采集、正文、相关性、实体、摘要和关联的显式工作流；行情采用采集、口径校验和确定性计算；PDF 工作流内包含有界文档 Agent。Service 控制步骤和条件分支，Celery 控制任务执行，不建设通用动态工作流引擎。参考 AIHOT 的手写业务编排，不复制其 pg-boss、评分门槛和付费请求策略。[SREF1][SREF2]

Beat 定时触发“检查到期信源”和“检查到期恢复任务”。信源间隔、启停、next_due_at 存 PG，经 HTTP 修改后下一轮生效，无需重新生成 Beat 配置。Beat 只触发检查，不在调度进程内抓网页或调用模型。

任务提交在一个 PG 短事务中保存 TaskRun、输入版本和 OutboxMessage。发布器在事务外向 RabbitMQ 投递，broker 确认后标记已投递；确认丢失时允许重投，由消费者按业务 task_id、版本与 generation 去重。相同 Celery task_id 本身不保证只执行一次。MQ 故障时保留待投递状态，HTTP 返回可查询的 task_id，不谎称开始执行。

消息只携带已校验的标识、契约版本与关联字段，不传全文、PDF 或凭据。业务状态由 TaskService 和 PG 决定，不使用 Celery result backend 作为任务事实来源。消费者以 PG 短事务领取 owner、generation、lease_until 并续租；提交校验 generation，旧 worker 不能覆盖新结果。

Checkpoint 保存 workflow_version、input_revision、checkpoint_seq、已完成步骤结果引用、下一步、Agent 必要上下文和工具结果、剩余预算。步骤结果、步骤状态、下一步检查点与后续 outbox 同一事务提交。升级提示词、模型或解析器不改变旧任务输入；显式重新处理创建新 revision。

模型调用先提交意图，再在事务外调用，完整响应保存后提交结果。已发送但结果丢失时记为 UNKNOWN，不因队列重投自动重发；完整响应已经保存的步骤可以继续解析，确定性步骤可以幂等重做。UNKNOWN 的重新调用通过显式 HTTP 恢复操作产生新 attempt。消息至少一次投递不等于业务或模型 exactly-once。[SREF10]

任务状态用枚举：QUEUED、RUNNING、WAITING_AUTH、WAITING_QUOTA、RETRY_WAIT、PARTIAL、SUCCEEDED、FAILED、CANCEL_REQUESTED、CANCELLED、UNKNOWN。操作区分 resume、retry_failed_step、reprocess、cancel；成功任务不被 resume 重跑。预算耗尽保留已有证据并返回 PARTIAL，不将未完成误标成功。

六类任务共用执行生命周期，合法转换与失败分类集中于 `domain.task_lifecycle.TaskLifecycle`；Service 负责事务、执行代次、owner/租约及跨领域协调，Repository 负责持久化。模型调用 intent/complete/rejected/unknown、OAuth 连接与刷新租约、Document stored/ready/failed、Source enabled/disabled/archived 和 Outbox 投递租约分别保留各自语义，不建立万能状态机。

授权恢复采用立即触发与定时检查：凭据导入及模型连接/渠道检查成功后尝试唤醒对应 WAITING_AUTH；Beat 逐渠道领取到期等待任务，事务外实际刷新凭据和检查模型目录，确认任务模型与固定端点可用后，再在锁定事务内更新状态、generation 与 Outbox。失败检查退避 300 秒，旧快照缺少渠道字段仍解释为原订阅渠道。并发检查通过任务代次与状态复核防止重复投递；任何最新模型调用结果未知均禁止自动重放。

明确 retryable 且结果已知的失败使用独立 `task_retry_budgets` 预算，默认最多三次，退避 30、60、120 秒；generation 只承担执行隔离。BUSY、WAITING_AUTH、WAITING_QUOTA 不消耗业务失败预算，人工恢复重置失败预算。到期 RETRY_WAIT/WAITING_QUOTA、过期运行租约与长期未领取的已投递 QUEUED 延续已有恢复路径；CANCEL_REQUESTED 租约失效完成取消。FAILED 永久失败、UNKNOWN、CANCELLED、SUCCEEDED、PARTIAL 不被定时盲重投。PARTIAL 已保存 complete=false 结果，若需改善应明确调整预算/输入并 reprocess。

Celery 使用 Linux prefork。同步 task 入口仅承接异步 runner；异步 client、event loop、AsyncEngine 生命周期一致，不跨 fork 或跨事件循环复用连接池。首版可在每个任务内创建并关闭异步资源，以正确性优先。阻塞 PDF 解析和渲染放受控 worker 进程，不能阻塞 FastAPI 事件循环。late ack、worker 丢失与任务重投配置用真实故障验证。[SREF10][SREF11]

## 5 HTTP 配置和操作接口

所有接口有 Request/Response Model 和稳定错误契约。管理入口使用部署管理员凭据，Gateway 使用独立服务凭据，不建设账号系统。配置只接收已支持类型的结构化参数，不接收 Python、shell 或 eval。密钥与连接字符串由部署 secrets 管理，HTTP 管理运行配置但不公开凭据。

MCP 的新闻搜索、任务轮询、抽取状态与结果读取、品种列表及行情读取显式声明 `ToolAnnotations.read_only_hint=True`；可能投递后台任务的 `fetch_article` 与 `extract_resources` 声明 False。此标注提供调用效果契约，客户端仍负责权限与故障分类。验收同时检查 SDK 工具发现与 HTTP `tools/list` 的 `readOnlyHint` 序列化，避免只读查询超时被误判为副作用结果未知。

| 接口族 | 操作 |
| --- | --- |
| `/api/v1/sources` | 新增、查询、更新、启停、归档，显示 last_success、next_due_at 和错误 |
| `/api/v1/sources/{id}/preview` | 小规模试抓，不进入正式发布链路 |
| `/api/v1/sources/{id}/runs` | 手动创建采集任务，查询运行记录 |
| `/api/v1/settings` | 调度默认值、并发、时间窗口、文档与模型预算 |
| `/api/v1/model-connection` | 连接与授权状态、检查、重新授权安装请求、断开；不返回 token |
| `/api/v1/tasks/{id}` | 查询步骤、进度、错误、结果引用；独立恢复、重试和取消操作 |
| `/api/v1/news`、`/api/v1/articles` | 收录范围内搜索；按 URL 获取正文或提交获取任务 |
| `/api/v1/documents` | multipart 上传、登记公开 URL、文件与解析状态 |
| `/api/v1/resource-extractions` | 对 URL 或 document_id 提交抽取、查询结果 |
| `/api/v1/price-instruments`、`/api/v1/prices`、`/api/v1/price-trends` | 明确报价口径、单点和序列查询 |

更新配置用 expected_revision 防覆盖；同一幂等键不同参数拒绝。已领取任务使用原配置快照，新任务采用新版本；停用阻止后续调度，当前任务另用 cancel。归档信源不删除历史证据。

统一错误含 code、英文 message、request_id 和具名 details，按需返回 retryable、retry_after、task_id。参数无效、无报价、未授权、上游失败、标准不符与结果未知分别表达，不能都转为空列表。任务接受使用 HTTP 202，已经存在的可复用完整结果可直接返回；连接断开不自动取消已受理的持久任务。

## 6 新闻流程与信源裁决

首批围绕 PLS/Pilbara Minerals/Pilgangoora 的锂矿动态，增加少量全球锂背景。来源与渠道由 Agent 根据本轮实证裁决，均不依赖付费账号：

| 来源 | 获取方式和初始频率 | 用途与边界 |
| --- | --- | --- |
| `https://www.pls.com/news` | HTML 列表与详情，默认禁用 | 原调研可读，后端直接 HTTP 现遇访问挑战；由官方 ASX 渠道补充 |
| ASX 官方 PLS 公告列表 | DOCUMENT_URLS，每 60 分钟，初次回填 30 天 | 官方列表提供公告时间、标题与公开 PDF；全公司公告不预设矿山归属 |
| `https://sigmalithiumresources.com/feed/` | RSS，每 60 分钟 | 全球锂背景，保留已确认的官方新域名映射 |
| `https://mining.com.au/feed/?s=PLS` | RSS 全文，每 60 分钟 | PLS 相关报道，保留公司与矿山的区别 |
| `https://im-mining.com/feed/` | RSS 全文，每 60 分钟 | 近期矿业背景，不自动归为 Pilgangoora 项目 |
| `https://www.australianmining.com.au/` | HTML 列表与详情，默认禁用 | 403 拒绝程序访问，日常报道改用已验证 RSS 渠道 |
| `https://www.miningweekly.com/page/lithium` | HTML 栏目，默认禁用 | Cloudflare 403 验证页，配置与历史保留 |
| ASX 官方公开 PDF | 已验证 URL，经配置接口登记或从官方新闻发现 | 资源与季度价格证据；公告列表已通过 DOCUMENT_URLS 接入 |

首版渠道类型为 RSS、HTML_LIST、DOCUMENT_URLS，暂不实现 X、公众号、付费阅读器或全网搜索。新闻首次回填默认 7 天、每源最多 50 条，后续按游标增量采集；登记的历史 PDF 独立于新闻回填窗口。原文时间、发现时间和更新时间分别保存，缺失发布日期不以抓取时间替代。

RSS 正文策略通过枚举 `rules.feed_body_policy` 配置：`webpage` 沿用链接正文采集；`feed_full_text` 使用 RSS `content:encoded` 或 Atom `content`，清除脚本与模板内容后直接入库，保留文章链接与 feed 发布时间，不再依赖详情页可访问。只有 RSS 类型允许全文策略；正文缺失或过短明确失败，不能拿 description 摘要补成全文。新增两个渠道默认使用全文策略，首次最多十条、回填七天。

补充核验已打通 `https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode=PLS&timeframe=D&period=M6`。DOCUMENT_URLS 从公开公告页解析最终 PDF 地址，同一事务登记文档导入、PDF 新闻正文任务与采集游标，后续分析独立执行。PDF 新闻复用受控解析子进程并限制页数与文本体积。PLS 官网仍返回访问挑战，保持禁用；ASX 默认启用，保留公告原时间与最终 PDF URL，不将整个公司的公告全部归到 Pilgangoora。

Source 包含 publisher、类型、入口、项目/矿种关联、域名约束、列表/正文选择器、时间解析及时区、间隔、上限与 revision。URL 规范化、来源标识和内容 hash 去重；抓取失败不推进游标。采集结果先保存原始版本，再排队加工。

模型只参与行业相关性、主体与证据抽取、摘要和必要事件判断；输入 revision、模型和提示词版本留存。首版不照搬 AIHOT 双重热度评分，不以热度屏蔽特定矿山的重要公告。不同项目不能因同属锂行业而合并事实。

`search(query, days)` 查询库内材料，支持项目、时间、矿种与关键词，返回来源范围、最新成功采集时间、结果截断信息。默认覆盖相关有效材料而不是只查精选。无结果只表示本库无匹配，不等于全网无此事；查询本身不启动模型。`fetch_article(url)` 可受控获取库外公开正文，不扩展为自主网页研究。

### 新闻首版实际覆盖（2026-10-08）

已实现 Source/SourceRevision、采集运行、文章版本、ArticleAnalysis 与任务联动。来源增改、expected_revision 冲突检测、启停/归档、preview、手动运行和查询有 HTTP 接口。网络步骤不持有数据库事务；原文版本、去重、游标、分析任务 outbox 与采集检查点在同一 PG 短事务提交。分析单独执行，先保存完整 ModelResponse，再核对实体与摘要引用属于对应原文 revision；未连接订阅时分析进入 WAITING_AUTH，采集及原文查询仍可用。

本机安全 HTTP 实采确认 Sigma RSS 返回 10 项，最新样本正文 15762 字符，发布时间从 RSS 保留为 2026-10-06T20:51:17Z；配置支持旧官网入口重定向到 sigmalithiumcorp.com，正文采用实际 Elementor/b2i 容器。Sigma RSS 与已打通的 PLS 官方 ASX 公告种子默认启用。PLS 官网、Australian Mining、Mining Weekly 在当前固定 DNS/IP/SNI 的公开读取路径返回非成功状态，默认禁用并记录真实原因；保留配置与 preview，操作方核验恢复后可启用，不把失败写成已成功采集。没有绕访问挑战。

当前库内搜索包含 source coverage、截断标志、原文及已完成分析。发布日期缺失仍为 unknown，筛选时间窗口时使用发现时间并在响应说明，不将其填入发布日期。现有新闻正文与版本保存在受认证 PG TEXT；大正文文件卷分离尚未实施，当前网络响应上限 2 MB，不宣称已完成文件化正文存储。

## 7 PDF 双输入与文档 Agent

公开 URL 与 multipart 上传最终形成同一种不可变 Document。文件分块落盘，校验真实格式、大小、可解析性，不信任扩展名和原文件名。文档入口上限为 250 MiB、1000 页；上传成功返回 document_id，不同步等待全文理解。URL 仅允许公开 HTTP(S)，逐次校验重定向与目标地址，限制大小、超时，拒绝本机/私网地址；不接受服务器任意路径。

大文件处理采用明确资源预算。URL 下载以 64 KiB 块直接写文件，不拼接完整 bytes；PDF 下载要求 identity HTTP 编码，避免解码器在业务计数前展开压缩响应。上传在 multipart 解析前按实际请求体计数，251 MiB 包含 multipart 开销，Content-Length 可提前拒绝但不替代实际计数；每个 API 进程最多同时处理两次上传。业务文件仍按 250 MiB 限制，异常和取消清理临时文件。

解析进程逐页生成 NDJSON 磁盘清单，每页最多 20 万字符、总计 2000 万字符，单行最多 1 MiB。父进程只接收小型清单描述，按最多 20 页一批入库；每批结果与任务检查点在同一 fenced 事务提交。Agent 的候选页、搜索、阅读和引用校验在数据库按范围查询，生产路径不加载整个 ParsedDocument。新闻 PDF 正文有单独 200 万字符预算，逐批读取时提前检查。

Linux Docker 中，解析、表格和渲染子进程共用跨进程锁，单实例只运行一个；在导入解析库前设置 512 MiB 地址空间上限和 120 秒 CPU 上限，父进程同时限制墙钟时间。stdout/stderr 在接收过程中限量，超限或取消终止子进程。后端容器内存上限 2 GiB、禁止额外 swap，Gateway 上限 512 MiB；这限制部署资源消耗，不保证所有合法大小的复杂 PDF 都能成功解析。原生 Windows 开发测试没有 Linux RLIMIT 的同等承诺，正式资源限制在 Docker 中验收。

流程为下载/上传、hash、按页文本与布局提取、候选章节定位、有界文档 Agent、证据校验、结果提交。工具限本文件的 search_pages、read_pages、read_table、render_page。扫描或复杂表格可以调用具备视觉能力的订阅模型读取页图，不接付费 OCR；不支持的模型能力明确报错，不静默换模型。

首版必需覆盖为报告采用的现行基准资源量声明，按矿体、资源类别和 in-situ/stockpile/total 分开。历史对比和备选截止品位情景不要求穷举；若返回，必须单列日期、范围与截止品位。complete 表示必需资源表覆盖完成，不等于全文每页都读过或原报告没有矛盾。原报告的日期冲突、缺失字段和算术不一致应保留原值或 null，并明确 limitations；核心资源表仍未检查或证据不足时返回 PARTIAL。

吨位只表示资源物料总质量，字段构造时接受明确的 ore 或 mineralized material 标签并保留原标签；Li、Li2O 等金属或化合物含量不能填入吨位。文本引用必须是指定物理页中的连续原文，可将表头、表行和脚注拆成多条证据，不得拼接省略内容冒充一句原文。

finish_extraction 的确定性证据校验通过后才能提交结果。引用、数量或范围校验拒绝时，记录带具体字段位置的具名验证结果作为工具检查点，反馈给同一个有界 Agent 继续修正；反馈也计入轮次、工具数和上下文预算。恢复复用已保存的模型完成响应及验证结果，不重复旧调用；UNKNOWN、租约、数据库异常和真实标准不符仍按原错误语义停止，不伪装成可纠正的候选问题。被拒绝的候选不会作为成功结果发布。

建议初始预算：20 轮模型决策、80 次文档工具调用、10 分钟活动执行时间、30 张模型可见页图，经 HTTP 可调整并用真实样本校准。PDF 内容是不可信外部材料，不能扩展工具权限。达到预算返回已验证记录与未完成范围。

ResourceRecord 保存 project、reporting_standard、report_date、effective_date、deposit/scope、category、tonnage、grade、contained_material、单位、cutoff、ownership_basis、inclusive_of_reserves、文档 hash、PDF 页码、印刷页码、表号和证据。数值用 Decimal；未知不填零。不同矿体、截止品位、日期与统计范围不直接相加；M&I 合计不拆成 Indicated，Li、Li2O、LCE 不混成一个含量。

NI 43-101 是题面必需，JORC 是 Pilgangoora 的明确扩展，分别保存实际标准。严格指定标准时不匹配明确拒绝；自动识别返回真实标准。Indicated/Inferred 按资源量分类处理，不能直接称可采储量。

结果按文档 hash、请求选项、workflow/解析器/提示词版本与模型复用；重新处理产生新版本。MCP `extract_resources` 保留 pdf_url 并增加 document_id 替代输入，必须恰好一种。已有完整结果直接返回，否则返回业务 task_id；增加 `get_extraction`、`get_extraction_result` 工具，让 client 查询完成状态。Gateway 不占住长 HTTP 请求等待全文解析。

## 8 免费行情与时间口径

进一步派出三个 gpt-6.1-sol 子智能体并行核验后，首版采用以下相互独立的价格系列。免费可读是技术事实，不代表取得商业再分发许可；当前为个人使用与本地开发，所有业务 API 受认证，不将第三方行情建设为匿名公共数据分发服务。不依赖付费源或绕过登录、挑战、TLS 校验。

| 系列 | 已验证获取方式 | 口径与覆盖 |
| --- | --- | --- |
| 碳酸锂现货 | Mysteel公开历史JSON `https://openapi.mysteel.com/publishd/index/chart/dateData`，参数indexCodes/startTime/endTime；公开页为 `https://www.mysteel.com/mmlc/` | ID01551919电池级≥99.5%早盘、ID01551906工业级≥99.2%早盘、ID01720085电池晚盘。中国、CNY/t、含13%VAT；电池送到、工业自提。早盘实测2021-07-01起1309点，晚盘2022-07-12起1052点，截至2026-10-08。外层response为JSON字符串，xAxis/yAxis按索引配对，严格检查code/unit/date/长度。 |
| 锂辉石精矿现货 | Mysteel公开早读正文，已核实三篇历史文章 | 澳洲SC6 CIF中国USD/t；2026-01-27区间2360–2550，2026-03-12为2120–2190/中2155，2026-03-16为2130–2190/中2160。属于公开文章披露的稀疏日度序列，报价日与文章日分别存；马里周度行不能混为日度。 |
| 碳酸锂期货 | 新浪公开JSONP `https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20_LC2701=/InnerFuturesNewService.getDailyKLine?symbol=LC2701` | 具体合约、CNY/t，o/h/l/c分别OHLC，s为结算，v/p为成交量/持仓。实测LC2701为2026-01-19至2026-10-08共172行；到期合约另有历史。JSONP只提取JSON，不执行脚本。LC0主连与具体合约分开，不拿主连拼逐合约基差。 |
| PLS发行人季度售价 | ASX免费公开季报PDF | 平均估计实现售价，原品位和发行人SC6等价各自保留；不属于日现货。 |

2026-10-08实测碳酸锂电池早盘122000、工业早盘120250、电池晚盘121000 CNY/t；同期LC2701收盘117300、结算121540 CNY/t，二者不是同一价格。公开页与历史接口可能不同步，每条记录保留其真实日期、session、来源与抓取时间。Mysteel方法论版本V2.2注明交易日10:20/16:30、最小30吨；接口的历史范围不代表永久可用承诺。[SREF13]

锂辉石公开文章仍不能保证连续历史或最新每一天都有报价。2026-10-08早读未含精矿价，但同日20:50公开日报包含澳洲SC6 CIF中国1670–1720 USD/t，区间计算中值1695，标记 derived。原发现逻辑仅筛早读而遗漏日报，已改为动态发现早读与锂相关日报，按文章路径时间排序、去重并限制抓取数量，移除固定历史文章补抓。没有可发现文章或没有可验证报价时明确报错；不把网站获取故障当作正常空行情。报价日期和发布时间仍分别保留。氢氧化锂尚无同时满足取数、规格和来源条件的合格自动渠道，首版返回未支持原因。GFEX官方接口本次证书/挑战失败，期货必须标第三方新浪来源，不宣称官方行情接口已接通。SMM、长江有色等有明确限制或授权要求的渠道不纳入默认采集。[SREF14][SREF15]

PriceInstrument 定义发行人/项目、产品、规格、价格类型、频率、币种单位与交易条件；PriceObservation 保存统计期、公告时间、估计截至日、估计/修订状态、数值及原始证据。后续修订留历史，不覆盖出处。

`get_price(commodity, date)` 使用明确品种，简称有歧义时返回候选。EXACT 模式匹配实际观察日或统计期结束日并标注公告时间；LATEST_AVAILABLE 显式查询截至指定日期已经披露的最近数据，返回真实统计期。按统计期查报告与按历史时点查已知价格分开表达，不能用后来披露的数据冒充当时已知行情。

`get_trend(commodity, days)` 只返回真实观测点、频率与缺口，少于两个可比较点不计算涨跌；不把季度均价前向填充成日线。碳酸锂现货按所选早晚盘查询；锂辉石缺日期时显式返回不完整覆盖，季度售价单独呈现。已知非交易日、报价缺失与上游获取失败分别表达，不能用期货填补现货缺口。

真实历史回归数据为 PLS 2025-07-30、2025-10-24 季报的 2025Q1/Q2/Q3 SC6 等价估计售价 851/703/841 USD/t；这不是当前报价。原产品品位不同，使用发行人自己的 SC6 等价列比较，原报价和脚注同时保留。[SREF6][SREF12]

### 行情首版实际覆盖（2026-10-08）

已实现六个默认 instruments 与独立采集任务、价格版本留存、EXACT/LATEST_AVAILABLE、known_at 查询和真实观测趋势。默认包含三个 Mysteel 碳酸锂系列与新浪 LC2701；本机直接 HTTP 请求及解析均已成功。Mysteel JSON 不提供真实发布时间，所以 published_at 保持 unknown，不能把方法论的计划发布时间用作当时已知证明；asof_at 是本服务实际取得数据的时间，known_at 过滤不泄露后来取得的值。新浪保留收盘/结算/OHLC、成交量及持仓的不同字段，缺交易日不填零。

SC6 可选源解析公开早读正文，优先内容容器以免标题日期覆盖实际报价日期；三个真实研究样本分别返回 2026-01-27、2026-03-12、2026-03-16，后两点保留明确中价，首点计算区间均值并标记 derived。首页发现最近早读，结合三个已核实历史入口小规模回填；2026-10-08正文无报价返回真实覆盖缺口。仍是稀疏观测，未承诺完整历史。

PLS 可选适配器已实现两份已核实 2025 季报 PDF 第 1 页的 SC6 等价估计实现售价表解析，回归值 Q1/Q2/Q3 为 851/703/841 USD/t，默认禁用；当前未建立原产品品位独立报价系列。报告只给公告日期，publication_date 保存日期精度，published_at 保持 unknown，不伪造公告时刻。所有系列保留来源和使用说明；氢氧化锂没有合格种子，查询缺失明确报错。趋势不前向填充，少于两点不算涨跌。

验证包含 24 项负责范围测试：最小解析样本、HTTP 大小/挑战处理、MCP 三实例发现及结构化 isError、独立真实 PG schema 下的游标/原文/检查点/分析 outbox 原子提交、版本冲突、价格修订及 known_at、scripted model 证据核验与完整响应复用、未授权分析等待。没有调用未授权模型。

## 9 模型渠道与配置

正式部署默认使用 `openai_compatible`，由环境变量注入 `MINING_OPENAI_API_KEY`、`MINING_OPENAI_BASE_URL`、`MINING_OPENAI_PROTOCOL`。默认协议为覆盖面较广的非流式 `chat_completions`，可显式选择 `responses`；兼容范围为业务使用的文本、图片与函数工具调用，不承诺所有厂商的扩展字段或模型能力一致。API Key 缺失时应用与免费采集仍可启动，模型步骤进入等待授权；显式空值、非法地址与协议立即拒绝。

管理 HTTP `GET /api/v1/model-channels` 返回非敏感配置状态，`POST /api/v1/model-channels/{channel}/check` 读取模型目录而不发起推理。`PUT /api/v1/settings` 选择 `model_channel`、`model_name`。密钥只从进程环境读取，不写入运行设置、任务快照或返回值；Gateway 不持有模型密钥。更换部署密钥后需重建后端容器。

任务固定渠道、模型、base URL 与协议快照，恢复旧任务不会切换渠道。已有任务与旧 PG 设置缺少渠道字段时明确解释为原有 `chatgpt_subscription`；新部署的初始设置采用环境默认。端点或协议与旧任务快照不一致时明确拒绝执行，可重处理生成采用新配置的任务；密钥轮换不改变业务快照。不存在失败后自动切换订阅或付费 API 的逻辑。

Chat Completions 适配器将开发者消息转换为 system 消息、namespace 工具转换为函数列表，并保留同轮调用组、call ID、工具结果与图片。截断、非法工具调用、响应过大或结果不确定时不执行工具；已知拒绝与未知结果遵循相同的任务恢复规则。

### 可选 ChatGPT 订阅

参考 gongkao-hub `model_connections/oauth.py`、`local_helper.py`、`service.py`、`adapter.py`，读取时 HEAD 为 `29f7a1d9ab93809ae3a2222d7141f7badbe078a8`，不据此假定工作树没有修改。借鉴独立授权、PKCE、身份校验、刷新和 Responses 流处理，不引入其 LangChain 适配层。[SREF7][SREF8]

连接状态、安装请求、指引和重新授权操作通过 HTTP 管理。首次远程授权需本地浏览器回调助手，沿用通过 Docker/SSH stdin 安全交接凭据的方式；不要求在服务器日常使用 CLI。127.0.0.1 回调到达用户电脑，不是远端服务，不能承诺改成任意公网 callback。

后端独占导入会话的刷新权，不能与 client 或本地助手并发刷新同一 rotating token。凭据由部署主密钥保护，不进入队列、普通结果、日志或浏览器存储。刷新先记录意图与 generation，再在事务外访问身份服务；结果未知进入重新授权状态。

使用订阅支持的 Responses 路径、store=false、stream=true，不复制 AIHOT Chat Completions 的请求字段。上传 PDF 是自有业务 API，不依赖该订阅路径不支持的 OpenAI Files API；向模型发送的文本或页图以实际能力为准。[SREF9]

额度不足记 WAITING_QUOTA，认证失效记 WAITING_AUTH，保留步骤。采集与已有结果查询可继续，模型步骤按恢复时间或 HTTP resume 继续，不忙轮询、不切 API key 计费。后端模型并发初始建议 1，模型与预算可配置。真实账户授权和推理权限在实施验收时验证，本轮不读取现有凭据。

首次建运行配置时读取部署默认值，此后 PG 是配置来源；任务提交固定模型与业务预算快照，重处理新任务采用当前配置，恢复旧任务保持原快照。全局模型并发准入实时读取被锁定的当前上限。明确的 HTTP 拒绝或 terminal failed 与网络结果不确定分开记录，后者仍禁止自动重放；订阅 `store=false` 下保留完整输出供后续请求重建上下文。[SREF16]

## 10 验收与实施顺序

| 验收 | 必须证明 |
| --- | --- |
| 本地 Docker | 两个应用 svc 与 PG/MQ 就绪；迁移、worker、Beat 和 HTTP 健康可查 |
| HTTP 配置 | Request/Response、Depends、版本冲突、非法值 fail-fast；运行配置无需 SSH 改文件 |
| 新闻 | Sigma RSS 实采；PLS 等当前取数失败来源明确禁用并暴露原因；去重、时间与项目准确，失败不推进游标 |
| PDF 双输入 | 同内容身份与一致结果；坏文件、超限、跨页表格、取消与恢复明确 |
| 真实报告 | Sigma Grota do Cirilo 2023、Falchani 2023 的 NI 43-101；Pilgangoora 2025 JORC，多矿体/日期/截止品位不混算 |
| 扫描路径 | 已核实报告可提取文本；额外用明确标记为人工栅格化的样本页验证视觉路径，不冒充真实扫描报告 |
| 价格 | Mysteel碳酸锂真实日度与历史、公开早读精矿稀疏报价、新浪具体合约期货、ASX季度价分别验收；单位、规格、日期与历史可得时间正确，缺失不造值 |
| 故障恢复 | 真实 PG/MQ，提交未投递、确认丢失、重投、worker 被杀、租约失效、旧 generation 拒绝提交 |
| 检查点 | 步骤结果和游标原子提交；恢复复用完整模型结果；未完成响应不执行工具 |
| 订阅 | 刷新并发、结果未知、额度不足、重新授权与流中断，不切付费 API |
| MCP 与日报 | 本项目验收三个入口各自发现和调用、证据与缺失返回；日报生成属于独立 client 的联调范围，不在本仓库实现 |

五分钟启动指环境和接口就绪，不承诺首次授权、全部历史采集或几百页 PDF 理解五分钟完成。抽取回归使用人工核对字段和证据位置，不让模型自评代替验收。Server 事务与恢复测试使用真实临时 PG/MQ/文件，不以全 mock 证明可靠性。

实现已覆盖 HTTP 契约、PG 迁移、两个应用 svc、订阅连接模块、Celery/Beat/outbox/检查点、新闻、文档 Agent、免费价格与 MCP Gateway。业务运行于本地 Docker；公网 Tunnel 的接入状态见第 2 节。

实际 Docker 验证使用 Python 3.12.15、RabbitMQ 4.3.0、PostgreSQL 18.6 与锁定依赖。Celery 禁用本项目不使用的 remote control、gossip、mingle 和事件广播，避免 RabbitMQ 4.3 已禁用的临时非独占队列。`/health/ready` 检查 PG、outbox 最近轮询、实际队列探针消费与 Beat 消息证据；`/health/live` 单独报告 API 存活。

已通过实际 HTTP 提交行情任务、Linux prefork 执行、PG 入库及 MCP 查询：2026-10-08 碳酸锂电池早盘 122000 CNY/t、LC2701 期货收盘 117300 CNY/t。Sigma RSS 已采集两篇带原发布时间的完整正文。83 页 Pilgangoora PDF 经上传、异步解析后 READY；抽取任务未登录时 WAITING_AUTH，保留两个已完成检查点。三个 MCP 入口经官方 Client 发现并协商为 `2026-07-28`。

行情调度消费 `source_interval_seconds`，按时间桶去重，避免零点采集后整日不再更新；首次补历史，有数据后重叠抓取近七天。新闻周期使用各信源 `interval_minutes`。三份真实报告均已完成实际订阅模型抽取，正式结果保存在 PG，页码、原文引用和限制说明随结果返回。

Windows AMQP socket 超时采用平台分支，避免 py-amqp 的 POSIX timeval 被 Winsock 解释成毫秒；Linux 保留 socket 超时。实际故障实验在 Sigma URL 导入任务 RUNNING 时 SIGKILL 后端容器，重启后约 133 秒完成租约恢复，generation 从 1 升到 3，四个步骤检查点完成并得到 568 页文档。恢复过程中未调用模型。

健康反向实验暂停实际 Celery 主进程与两个 prefork 子进程后，`/health/live` 仍为 200，`/health/ready` 为 503 且 worker=false；恢复进程后四项依赖均恢复为 true。最终镜像已重建，两个应用与两项基础设施均 healthy，Alembic 检查无差异。

后续差距审查补上租约在事务提交前的再次校验、旧 owner 禁止写失败状态、仅检查最新 attempt 的 UNKNOWN、重复取消保持取消过程，以及已确认投递但未领取的 QUEUED 看门狗。存在 pending outbox 不重复登记；同 generation 重投仍由领取事务去重。

三份报告的最低验收记录固定在 `apps/backend/tests/fixtures/document-golden.json`，原图表头、单位和列位置已人工复核，并包含体积误当吨位的反例。文档 Agent 限制累计上下文字节，消除页图重复输入，允许以已渲染页证据识别扫描标准；每轮超过 100 个工具会明确 PARTIAL，避免旧检查点键发生碰撞。真实模型验收使用独立命令并输出逐字段结果，不将脚本化响应或 PDF 页数测试计为真实模型通过。

实际 Docker 已采入七条 PLS 官方 ASX 公告，MCP 新协议查询返回最多 1200 字符的逐条摘要；七条结果的 MCP 响应约 28 KB，其中一篇超过 116 万字符的全文仍能由 `fetch_article` 完整读取，公告时间与公司范围没有丢失。运行模型配置的非法空白名称实际返回 422 且未修改 revision。

真实验收修复了流输出重复比较、reasoning.content 具体类型遗漏、吨位物料标签校验、PyMuPDF 提示文字污染子进程 stdout，以及模型与采集共用短超时等问题。用户明确继续后，通过任务接口恢复 UNKNOWN；完整响应复用，无确定结果的调用才在人工恢复后再次发送。模型读取超时独立为默认 300 秒，文档总体预算仍约束执行。

尚未解决的行情覆盖：连续且最新的免费日度锂辉石现货、合格氢氧化锂源。当前证据只说明已核验渠道尚未满足接入条件，不证明不存在可用免费来源。商业使用与再分发许可不在本次只读调研中取得。

验证反馈流程加入后的 Linux 完整回归为 **197 项通过、零跳过**，使用专用 PostgreSQL 测试库、真实 RabbitMQ 独立队列与三份外部 PDF，Ruff 通过。随后模型独立超时和 PDF stdout 隔离修复的 Linux 定向回归 **61 项通过、零跳过**。OpenAI 兼容协议使用本地 MockTransport 验证，没有配置真实 API Key 或调用付费接口。订阅真实最小推理确认完成事件可能返回空 `output`。按用户要求已移除前后重复输出的内容比较，以流中完成的输出项为准，收到 `response.completed` 后返回；缺终态或输出项索引缺口仍明确失败。

严格目录校验暴露并修正了 visibility 枚举值误写：官方实际返回 list/hide，原代码误写为 list/hidden；修复后仍拒绝未定义值，新增四项聚焦回归通过。最终镜像已部署，真实订阅目录检查为 ready、返回七个可见模型，四个容器 healthy，Alembic 无差异。MCP 新闻查询再次验证七条结果、1200 字符摘要上限及 1168644 字符全文读取。

迁移 `20261008_136e253238b6_typed_task_payloads.py` 将旧步骤、检查点、任务和模型结果包装转换为具体结构，并重新规范化任务输入及幂等摘要。先备份运行库，在恢复副本验证升级与 Alembic 无差异，再部署升级；UNKNOWN 状态、任务 generation 和凭据不被修改。应用代码只读取新契约，不保留旧包装兼容路径。

部署后的 HTTP 验证中，104858460 字节的有效附件 PDF 解析成功，1000 页 PDF 解析成功，1001 页 PDF 以 invalid_input 失败；之后数据库、worker、Beat 和 publisher 仍就绪。前两个样本用于资源边界验证，不计作报告语义验收。Linux 子进程测试另验证 512 MiB 地址空间限制及跨进程互斥，容器实际配置为后端 2 GiB、禁止额外 swap。

真实订阅模型 `gpt-6.1-sol` 的最终基准记录验收如下。所有任务为 SUCCEEDED，三份结果均由 MCP `get_extraction_result` 读取，与 HTTP 结果完整相等，协议为 `2026-07-28`。

| 报告 | 正式任务 ID | 返回记录 | 最低基准记录 |
| --- | --- | --- | --- |
| Sigma 2023，568 页，NI 43-101 | `61b6e58f-d30f-4c59-81e7-4ddbd1f92cca` | 19 | 2/2 通过 |
| Falchani 2023，106 页，NI 43-101 | `a26023c2-df61-4f0c-bc5e-5e483eb2abad` | 4 | 2/2 通过 |
| Pilgangoora 2025，83 页，JORC | `c4957f42-1ea4-4540-9622-c2528dc58c3c` | 18 | 5/5 通过 |

验收覆盖现行基准资源声明，不等于穷举历史情景、备选截止品位或所有资源子集。Sigma 的原 PARTIAL 结果保留，新任务覆盖五矿体基准表；Barreiro 日期冲突留 null 并列明不同出处，不改写原表算术不一致。Pilgangoora 的不连续引用经过具名反馈修正后才入库，原文匹配规则未放宽。完整结果、基准验证和独立复核记录保存在本会话仓库外 `outputs/01a11b34-1c27-77c1-bf47-7d8d5cf371f1/live-report-acceptance/`。

最低基准之外，三份正式输出共 41 条记录已独立逐条对照原表复核。Pilgangoora 最终 18 条记录的数值、单位、日期、矿体、范围和分类在修正引用前后未改变，108 条文本证据全部匹配连续原文；堆存 Measured 的真实零值保留，破折号对应的 Inferred 堆存行未伪造，九条 South/Central/North 区域记录与全矿汇总分开。此三份样本最终均使用文本证据，不将其验收扩大解释为扫描图像语义已全面验证。

用户确认本期 PDF 功能以这三份真实报告通过为验收标准，扫描件和复杂图像不再作为本期未完成项。更多扫描布局可作为后续扩展测试，不扩大本期交付范围，也不将已有文本证据验收表述为任意扫描件识别保证。

2026-10-09 来源与恢复修复已部署本地 Docker：Mining.com.au PLS RSS 首次入库 1 篇，International Mining RSS 首次入库 10 篇，均保留 RSS 发布时间并直接使用正文。此前七条 PLS 公告在登录前进入 WAITING_AUTH，因旧实现不自动唤醒而滞留，本次已全部恢复分析成功。授权后自动唤醒、周期兜底、渠道与模型检查、并发去重、独立失败重试预算和终态保护已补充真实 PG 回归。迁移 `3a39f510590c` 自动生成后手动回填既有任务零重试计数，验证旧库升级及全新建库均通过，原已部署迁移未修改。

同日锂辉石任务已通过实际 Worker 完成发现、下载、解析与入库，HTTP 和 MCP 返回 2026-10-08 澳洲 SC6 CIF 中国 1670–1720 USD/t，计算中值 1695、derived=true、发布时间20:50。除了补充日报发现，也修复了行情注册处理器向步骤函数多传参数的问题；新增真实 PG 回归从注册处理器执行到行情落库，并验证重复消息不重复采集。全套 Linux 验证 268 项通过、无跳过；最终 API Key 边界修复后 53 项相关测试通过，行情入口修复后 35 项相关测试通过。细节和实际 HTTP/MCP 结果保存在本会话 `recent-source-audit/` 输出目录。

交付 CI 使用 GitHub Actions，push、pull request 和手动触发运行固定 uv/lockfile 的 Ruff 与 pytest；当前收集 288 个测试。测试 job 使用真实 PostgreSQL、RabbitMQ 和固定哈希的三份公开报告，并对独立数据库执行完整 Alembic 升级与模型差异检查。所有 skip 均视为失败；Linux PDF 资源限制、队列确认与恢复测试不得因缺少环境被计作通过。独立 Docker job 验证两个应用 svc 与基础设施健康、MCP 认证和三个官方 SDK 连接。凭据按运行生成，实际模型推理仍使用已有独立本地验收流程，不将脚本化模型响应等同真实抽取结果。

2026-10-09 已完成真实 Codex 宿主联调：本机 Codex 共享配置接入三个 Streamable HTTP MCP 入口，Codex CLI 0.162.0-alpha.2 使用 gpt-6.1-sol，根据“给我生成一份关于 Pilbara 锂矿的今日简报”自主选择三个服务的工具并生成 Markdown。业务事实仅来自 MCP 返回，输出包含新闻、分类资源量、现货与期货走势、风险和来源链接。本次验证证明 Codex 与本地 Gateway 实际兼容，不据此推断 Codex 握手协商的具体协议版本，也不替代独立通用 client 项目的实现验收。

联调前通过现有文章导入 HTTP 接口将已验收的 Pilgangoora 2025 资源公告纳入搜索语料；原始自然语言提示未提供报告 URL、资源数字或任务 ID。Codex 自主找到报告并提交新抽取请求 `24e11488-30d9-5329-9f96-bf4e29fc2b60`，对应后台任务 `46bf8694-892f-4236-8fe4-1445ccf5e1f4`。首轮在异步抽取完成前结束，随后使用其已有请求引用续接，读取成功结果后更新简报；并非单轮无人续接完成。新结果基准 5/5 通过，独立对照原表核实 17 条数值记录及 1 条全破折号记录，后者保留为空。最终资源总计 446 Mt、1.28% Li2O、含 Li2O 5.7 Mt，与公告一致；缺失当日报价和稀疏锂辉石数据均明确披露。提示词、工具调用记录、最终简报与独立复核保存在本会话仓库外 `codex-integration/` 输出目录。

[SREF1]: https://github.com/KKKKhazix/AIHOT/blob/6e67a9d9e8d87b95b8118a8a0b328a9bebd2bb48/docs/architecture.md
[SREF2]: https://github.com/KKKKhazix/AIHOT/blob/6e67a9d9e8d87b95b8118a8a0b328a9bebd2bb48/packages/backend/src/editorial/analyze.ts
[SREF3]: https://sigmalithiumresources.com/wp-content/uploads/2023/05/2023-01-SGML-Updated-Technical-Report-1.pdf
[SREF4]: https://americanlithiumcorp.com/wp-content/uploads/2025/05/Falchani-NI43-101_Technical_Report_Final_12-14-23.pdf
[SREF5]: https://announcements.asx.com.au/asxpdf/20250611/pdf/06kmc3l7r1bjsm.pdf
[SREF6]: https://announcements.asx.com.au/asxpdf/20251024/pdf/06qz382w4nt85r.pdf
[SREF7]: https://developers.openai.com/siwc/token-sharing-open-source/sign-in
[SREF8]: https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms
[SREF9]: https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations
[SREF10]: https://docs.celeryq.dev/en/v5.6.3/userguide/tasks.html
[SREF11]: https://docs.celeryq.dev/en/v5.6.3/userguide/periodic-tasks.html
[SREF12]: https://announcements.asx.com.au/asxpdf/20250730/pdf/06m8x0wx5j7r91.pdf
[SREF13]: https://a.mysteelcdn.com/common/mysteel/dataIndex/tansuanli/file/tsl-methodology.pdf?v=20260421
[SREF14]: https://xny.mysteel.com/a/26031217/D51843650B60AF0D.html
[SREF15]: https://webnewslt1.pobo.net.cn/Data/JINSHU/6773403.HTML
[SREF16]: https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference
