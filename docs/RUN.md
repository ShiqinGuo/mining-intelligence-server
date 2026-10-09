# 本地运行

本项目独立于通用 client。以下命令在 `D:/code/code4interview-server` 执行，需要 Docker Compose、Python 3.12 和 uv。两个应用容器为 `mining-backend`、`mcp-gateway`，PostgreSQL 和 RabbitMQ 是另计的基础设施容器。后端容器内运行 API、Celery prefork worker 和单实例 Beat。

`apps/backend` 与 `apps/mcp_gateway` 各自维护源码、依赖、测试和 Dockerfile，公共 HTTP 契约位于 `packages/contracts`。Compose 从仓库根目录构建两个独立镜像，分别使用各服务的 Dockerfile；Gateway 镜像不包含后端源码与 ORM、Celery、PDF 依赖。根目录的 uv workspace 安装两个服务用于开发和联调；一份 lockfile 不代表两个生产镜像共享全部依赖。

## 启动

```powershell
uv sync --frozen --all-packages
uv run python scripts/init_env.py
docker compose -p code4interview-server up -d --build
docker compose -p code4interview-server ps
```

`init_env.py` 仅在 `.env` 不存在时创建随机密钥、口令和独立 OAuth host identity，已有文件会拒绝覆盖。当前工作区已初始化，重复启动跳过初始化命令。不要提交 `.env` 或将其内容粘贴到日志。备份数据库时同时安全备份 master key，否则无法解密已保存的模型连接。

首次启动自动执行 Alembic 迁移并安装默认信源与行情规格。首次下载镜像、依赖的时间取决于网络；镜像就绪后的启动验收与下载耗时分开计算。

| 入口 | 地址 |
| --- | --- |
| HTTP 文档与配置 | `http://127.0.0.1:28110/docs` |
| 后端存活 / 就绪 | `/health/live` / `/health/ready` |
| MCP Gateway 存活 | `http://127.0.0.1:28111/health/live` |
| 新闻 MCP | `http://127.0.0.1:28111/mcp/news/` |
| 文档 MCP | `http://127.0.0.1:28111/mcp/documents/` |
| 行情 MCP | `http://127.0.0.1:28111/mcp/market/` |

所有宿主端口默认仅绑定 loopback。Gateway 只接收 service token；业务读取接受 service 或 admin token，配置修改需要 admin token。Swagger 的 Authorize 中填入相应 token。`mcp-config.json` 是通用连接示例，替换占位 token 后按所用 client 的配置格式导入；不修改另一个 client 项目，也不假定它消费此 JSON 格式。

## Codex 本地接入

Codex 使用用户目录的 `.codex/config.toml`，桌面端与 CLI 共用配置。三个 Streamable HTTP 地址分别配置为 `mining-news`、`mining-documents`、`mining-market`。认证使用本项目 `.env` 的 `MINING_SERVICE_TOKEN`，可通过各 server 的 `http_headers.Authorization` 设置 Bearer 值，或用 `bearer_token_env_var` 引用启动 Codex 时可见的环境变量。不要使用管理员 token，也不要将真实 token 放入仓库示例。当前开发机已配置三个入口；桌面会话未加载时在 MCP 设置中 Restart，配置方式见 [Codex 官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。

自然语言验收使用“给我生成一份关于 Pilbara（Pilgangoora）锂矿的今日简报”，要求中文 Markdown 包含新闻摘要、资源量数据、价格走势、风险提示与来源链接。仅从三个 MCP 获取业务事实，保留实际日期、规格、标准和缺口。Codex 自行选择工具并轮询异步抽取；不向提示词注入现成抽取 ID 或数值。CLI 非交互测试需要为本次授权使用的矿业工具设置调用许可，`approval_policy=never` 不会自动批准默认要求确认的 MCP 工具；测试许可不修改用户全局工具审批设置。

资源报告必须已进入可搜索语料，或由请求提供公开 PDF URL。仅接入最近新闻可能只能发现季度经营报告，不能从没有分类资源表的报告中捏造资源量。当前实例已通过现有 `POST /api/v1/articles/fetch` 收录官方 [2025-06-11 Pilgangoora 资源报告](https://announcements.asx.com.au/asxpdf/20250611/pdf/06kmc3l7r1bjsm.pdf)，供新闻搜索发现并由文档 MCP 抽取；报告日期仍是 2025 年，不作为今日资源变更。

## HTTP 操作

PowerShell 从本项目 `.env` 构造管理请求头，不输出口令：

```powershell
$adminLine = Get-Content -LiteralPath .env | Where-Object { $_.StartsWith('MINING_ADMIN_TOKEN=') }
$adminToken = $adminLine.Split('=', 2)[1]
$headers = @{ Authorization = "Bearer $adminToken" }
$base = 'http://127.0.0.1:28110/api/v1'
Invoke-RestMethod "$base/sources" -Headers $headers
Invoke-RestMethod "$base/price-instruments" -Headers $headers
```

信源使用 `POST /sources` 创建、`PUT /sources/{id}` 更新、`POST /sources/{id}/state` 启停，修改传入当前 revision；`POST /sources/{id}/preview` 预览，`POST /sources/{id}/runs` 提交采集。完整 Request/Response 见 OpenAPI。修改来源不需要重新部署。

`GET /news` 与 MCP `search` 返回每篇最多 1200 字符的 `excerpt`，逐条 `truncated` 明确是否为节选，同时保留元数据与已有分析。需要完整正文时调用 `POST /articles/fetch` 或 MCP `fetch_article`；已有文章直接返回全文，不再重新抓取。

行情明确选择规格 slug：

```powershell
$slug = 'lithium_carbonate_battery_99_5_china_am'
$run = Invoke-RestMethod "$base/price-instruments/$slug/runs" -Method Post -Headers $headers -ContentType 'application/json' -Body '{"start":"2026-09-28","end":"2026-10-08"}'
Invoke-RestMethod "$base/tasks/$($run.id)" -Headers $headers
Invoke-RestMethod "$base/prices?commodity=$slug&date=2026-10-08&mode=exact" -Headers $headers
```

提交返回 202 与任务 ID，必须轮询任务到成功再查询结果。`exact` 不回填缺失日期；`latest_available` 显式允许获取指定日期之前的最近观测。期货使用独立 slug `lithium_carbonate_futures_lc2701`，不能当作现货。锂辉石公开文章是稀疏历史观测，氢氧化锂尚无合格自动来源。

PDF 使用 `POST /documents/from-url`，请求体 `{"pdf_url":"https://public.example/report.pdf"}`；或 `POST /documents/upload` 的 multipart `file` 字段。随后 `POST /resource-extractions` 传入 `document_id` 或 `pdf_url`，只能二选一。URL 导入和抽取提交必须携带 `Idempotency-Key`。`standard` 可为 `auto`、`ni_43_101` 或 `jorc`。结果由 `/resource-extractions/{id}` 和 `/resource-extractions/{id}/result` 读取。

文档文件上限 250 MiB、1000 页；上传总请求体上限 251 MiB，每个 API 进程最多两个并发上传。超出请求体上限返回 413，并发占满返回 503；PDF 下载不接受 HTTP 压缩编码。上传响应的 `task_id` 用于查询解析任务，资源不足或解析失败时应读取该任务的错误，不要仅轮询文档的 `stored` 状态。文件先落盘，解析结果按页写清单、每批最多 20 页入库，Agent 按需读取。Linux Docker 的解析子进程最多使用 512 MiB 地址空间，超出资源预算的文档明确失败；后台容器整体上限 2 GiB。需为数据库、消息队列、后端和文件留出磁盘与内存，不能把文件大小上限当作任意 PDF 成功解析的承诺。

运行预算通过 `GET/PUT /settings` 管理；`source_interval_seconds` 控制行情源轮询周期，新闻采集周期由各信源的 `interval_minutes` 单独设置。任务操作通过 `POST /tasks/{id}/actions`，`operation` 为 `resume`、`retry_failed_step`、`reprocess` 或 `cancel`。恢复复用已完成检查点；重处理创建独立任务，新抽取结果尚未产生时先查询新任务 ID，完成后从任务结果取得新的 extraction ID。`unknown` 表示外部结果无法确定，不自动重放；明确接受重复调用风险后才用 `acknowledge_unknown` 执行人工恢复操作。

`model_channel` 和 `model_name` 也由该 HTTP 接口管理，应使用选定渠道模型目录中的名称。新任务固定当时的渠道、模型、端点、协议与预算快照，修改配置不改变旧任务；`reprocess` 新建任务并采用当前配置。模型并发准入始终读取当前全局上限，旧任务不能绕过新上限。部署环境仅用于首次创建运行配置，已有 PG 配置后以 HTTP 配置为准。

默认启用 Mining.com.au PLS RSS、International Mining RSS、Sigma RSS 和 PLS 官方 ASX 公告列表。前两个 RSS 使用 `rules.feed_body_policy=feed_full_text`，从 RSS `content:encoded` 或 Atom `content` 读取正文，不再请求可能被拦截的文章网页；缺少正文明确失败，不把摘要当全文。原有默认策略 `webpage` 继续获取文章网页。PLS 公司报道和行业报道均不预设为 Pilgangoora 矿山事实。

ASX 的 `DOCUMENT_URLS` 信源解析公开 PDF 地址，原子登记文档解析与新闻正文任务，再异步进行模型分析。原公告时间与最终 PDF URL 都会保留。PLS 官网、Australian Mining、Mining Weekly 的原 HTML 源保持禁用，作为配置历史保留。公开下载器在连接失败时尝试其他已验证公网地址，403 和响应读取失败不会触发地址重试；错误 `details.reason` 保留 HTTP 状态或安全异常类型。

锂辉石行情从 Mysteel 新能源首页动态发现锂电早读和锂相关日报，按文章路径时间排序并限制抓取数，不再追加固定历史文章。已有历史报价保留；未发现文章或所有文章都没有可验证报价时明确报错。实际观察日与发布时间分别记录，区间中值标记为派生值，不能据此承诺连续日度历史。

## OpenAI 兼容模型配置

新部署默认渠道为 `openai_compatible`。在私有 `.env` 中设置以下变量；示例 key 是占位符，应替换后再启动：

```dotenv
MINING_MODEL_CHANNEL=openai_compatible
MINING_MODEL_NAME=your-model-name
MINING_OPENAI_BASE_URL=https://api.openai.com/v1
MINING_OPENAI_PROTOCOL=chat_completions
MINING_OPENAI_API_KEY=replace-with-your-key
```

`MINING_OPENAI_PROTOCOL` 支持 `chat_completions` 和 `responses`，默认前者。base URL 应是供应商 API 根路径，程序追加 `/chat/completions` 或 `/responses`。模型需支持业务使用的函数工具；报告页图还需要视觉能力。未提供 API Key 时可运行采集、查询与 PDF 解析，模型任务等待配置；不要用空字符串代替未配置。

模型请求独立配置超时：`MINING_MODEL_READ_TIMEOUT_SECONDS` 默认 300 秒，连接、写入和连接池等待分别由 `MINING_MODEL_CONNECT_TIMEOUT_SECONDS`、`MINING_MODEL_WRITE_TIMEOUT_SECONDS`、`MINING_MODEL_POOL_TIMEOUT_SECONDS` 配置，默认各 30 秒。读取超时限制相邻网络数据到达的间隔，不替代文档 Agent 的总体执行预算。普通采集和授权请求仍使用各自超时；模型传输结果未知时不自动重放，任务错误会保留具体传输失败类型。

修改环境后运行 `docker compose -p code4interview-server up -d --force-recreate mining-backend`。密钥仅注入后端，不进入 Gateway、任务快照或 HTTP 查询结果。环境变量设置不会覆盖已有 PG 运行配置；已有实例还需通过管理接口选择渠道和模型：

```powershell
Invoke-RestMethod "$base/model-channels" -Headers $headers
Invoke-RestMethod "$base/model-channels/openai_compatible/check" -Method Post -Headers $headers
```

目录检查只发 GET 模型目录请求，不发起推理。通过 Swagger 的 `GET/PUT /settings` 保留其余配置与当前 revision，并修改 `model_channel` 和 `model_name`。老任务缺少渠道字段时沿用原有订阅渠道；恢复不会自动换渠道。端点或协议修改后应重处理旧任务，采用新快照。API Key 可轮换，不写进数据库快照。

## 可选 ChatGPT 订阅连接

订阅适用于本地验收或主动选择该渠道的部署，配置 `MINING_OAUTH_HOST_ID` 后按下列步骤独立授权。在运行设置中选择 `chatgpt_subscription` 和账号支持的模型。未连接时订阅模型任务进入 `waiting_auth`，不会切换到 API Key；数据采集、PDF 解析和行情查询无需模型登录。凭据导入或渠道检查成功后立即尝试恢复对应渠道的等待任务；Beat 定期兜底，先实际检查凭据与模型目录，失败后退避 300 秒。恢复遵循任务固定的渠道、模型和端点；不因为当前全局模型改变而切换旧任务。

明确可重试的已知失败最多自动重试三次，默认退避 30、60、120 秒，计数独立保存在任务重试预算表；手动 `resume` 重置这项预算。模型繁忙、授权等待和额度等待不消耗业务失败预算。永久失败、`unknown`、已取消、已成功和已有 `partial` 结果不定时重投；`partial` 改善结果需调整预算或输入后明确 `reprocess`，单纯恢复会复用已有检查点和结果。

```powershell
$connection = Invoke-RestMethod "$base/model-connection" -Headers $headers
$installBody = @{ expected_revision = $connection.revision } | ConvertTo-Json
$installation = Invoke-RestMethod "$base/model-connection/install" -Method Post -Headers $headers -ContentType 'application/json' -Body $installBody
$pairingDirectory = Join-Path $env:CODEX_HOME 'outputs/mining-server-pairing'
New-Item -ItemType Directory -Force -Path $pairingDirectory | Out-Null
$installationPath = Join-Path $pairingDirectory 'installation.json'
$installation | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $installationPath -Encoding utf8
uv run python -m mining_server.infrastructure.auth.local_helper --installation $installationPath --handoff-command docker compose -p code4interview-server exec -T mining-backend python -m mining_server.infrastructure.auth.import_helper
```

打开 helper 输出的官方登录链接，浏览器授权回调到本机 loopback。helper 通过 Docker stdin 将凭据交给后端，后端独占刷新，业务 HTTP 不提供明文 token 导入接口。授权完成后通过 `/model-connection/check` 检查实际账号与模型可用性；body 使用当前 `expected_revision`。通过任务 `resume` 继续等待授权的工作。真实账号授权与真实模型抽取需要本人完成登录后验收；脚本化模型测试不能替代这一项。

远程部署时可以用 SSH stdin 替换 Docker handoff 命令，仍由本机浏览器完成授权；本项目当前交付范围为本地 Docker。目标实例的 `.env`、安装 ticket 和 host identity 必须对应同一实例。

## 真实报告抽取验收

`apps/backend/tests/fixtures/document-golden.json` 固定三份报告的 SHA-256、资源类别、原始单位、日期、截止品位与证据页。验收器会逐字段比较，不能用仅成功解析页数代替模型抽取通过。登录完成后，在仓库外准备上述三份 PDF，显式启动会调用订阅模型的验收：

```powershell
$serviceLine = Get-Content -LiteralPath .env | Where-Object { $_.StartsWith('MINING_SERVICE_TOKEN=') }
$env:MINING_ACCEPTANCE_SERVICE_TOKEN = $serviceLine.Split('=', 2)[1]
uv run python -m mining_server.infrastructure.documents.acceptance --mode live --golden apps/backend/tests/fixtures/document-golden.json --fixture-dir $env:MINING_REPORT_FIXTURE_DIR --output-dir "$env:CODEX_HOME/outputs/mining-server-acceptance" --backend-url http://127.0.0.1:28110 --authorize-model-calls --run-id report-acceptance-1
```

同一个 `run-id` 复用已提交任务；遇 `waiting_auth`、`waiting_quota` 或 `unknown` 明确停止，不自动重放。授权问题解决后使用任务 HTTP 操作恢复已知等待任务，再运行相同验收命令。修正模型或预算后需要 `reprocess` 或显式使用新的 `run-id`。结果 JSON 与逐字段判定写入 `output-dir`；可用 `--mode verify --result-dir <结果目录>` 对已保存结果再次比对，此模式不调用模型。

文档请求的 `budget.context_bytes` 限制累计模型上下文字节，页图只作为图像输入发送一份；证据标为 image 的页必须实际渲染过。扫描标准识别允许引用渲染页中的标准标记，但图像语义仍需真实模型验收，程序不能仅凭引用文本证明其正确性。

## 验证与维护

GitHub Actions 在 push、pull request 和手动触发时运行 `.github/workflows/ci.yml`。`test` job 使用 Python 3.12、固定 uv 版本与 lockfile，启动 PostgreSQL/RabbitMQ，创建独立测试库及迁移检查库，下载并校验三份公开 PDF 后运行全部 pytest。任何环境缺失导致的 skip 都使 CI 失败。PDF 缓存位于 runner 临时目录，每次仍校验固定 SHA-256；项目仓库不包含报告二进制或执行日志。

`docker-smoke` job 生成临时 `.env`，构建并启动独立 Compose 部署，检查后端 live/ready、Gateway live、未认证请求拒绝，以及三个 MCP 服务的实际 SDK 握手和工具发现。CI 的凭据与数据库仅用于该次运行。模型协议与任务恢复测试使用脚本化响应，实际订阅模型抽取验收仍按上节单独执行。

CI helper 使用 `uv run python scripts/ci.py fixtures` 准备 `MINING_REPORT_FIXTURE_DIR`，`uv run python scripts/ci.py test` 执行不允许跳过的完整测试，`uv run python scripts/ci.py smoke` 验证已启动实例。测试命令需预先配置下述独立 PG/MQ 环境；Linux 资源限制回归必须在 Linux 执行。

```powershell
uv run ruff check apps packages tests scripts
uv run pytest
docker compose -p code4interview-server logs --tail 100 mining-backend mcp-gateway
docker compose -p code4interview-server exec -T mining-backend alembic check
docker compose -p code4interview-server stop
```

单独运行服务测试使用 `uv run pytest apps/backend/tests` 或 `uv run pytest apps/mcp_gateway/tests`；服务边界检查使用 `uv run pytest tests/integration`。在根目录对空库核验迁移使用 `uv run alembic -c apps/backend/alembic.ini upgrade head` 与 `uv run alembic -c apps/backend/alembic.ini check`，必须先配置独立数据库；容器已在后端目录执行，继续使用上面的 `alembic check` 命令。

数据库测试只使用独立测试数据库；配置 `MINING_TEST_DATABASE_URL`、`MINING_NEWS_TEST_DATABASE_URL`、`MINING_AUTH_TEST_DATABASE_URL`、`MINING_DOCUMENTS_TEST_DATABASE_URL`、`MINING_TYPED_MIGRATION_TEST_DATABASE_URL` 和 `MINING_TEST_BROKER_URL`，不对运行数据库执行测试。文档测试数据库名须为 `mining_documents_test`，迁移测试数据库名须为 `mining_typed_migration_test`。无测试数据库时相关集成测试会跳过，不能把跳过计作通过。真实 PDF 验证样本和已知数值、报告体系、页码见 [设计与边界](DESIGN.md)。将三份报告分别保存为 `sigma-2023.pdf`、`falchani-2023.pdf`、`pilgangoora-2025.pdf`，通过 `MINING_REPORT_FIXTURE_DIR` 指定仓库外目录；未设置时这三项会明确跳过。PDF 的 Linux 进程资源限制须在 Docker 中验证。

普通停止或 `docker compose down` 保留命名卷。数据在 `postgres_data`、`rabbitmq_data`、`mining_data` 中；不要用 `down -v` 作为常规重启。修改数据库模型后自动生成带日期的 Alembic 迁移，再审查并补 backfill；已发布迁移只追加修正。
