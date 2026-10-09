# 项目协作与工程约定

本文件仅适用于 `D:/code/code4interview-server`。这是独立 server 项目，与 `D:/code/code4interview` 的通用 client 项目只通过 MCP 协议互通。不得修改 client 仓库的文档、工程规则、代码、依赖或部署文件；也不得依赖其本地源码或导入 `agent_client`。

## Mandatory Python rules

- Use enums for states, discriminators, error codes, and other closed vocabularies. `Literal` may contain only a subset of members from an enum class.
- JSON exists only at request/response or database boundaries. Immediately validate it into concrete field types and fail fast. Use named Pydantic `BaseModel` contracts in `domain` across layers, and explicit `dataclass` types for structures confined to one file. Do not pass raw dictionaries or broad `JsonValue` payloads through business methods. Do not hide untyped payloads in JSON strings or wrapper models.
- A dictionary may exist only inside one method and must have an explicit `TypedDict` type. Prefer the typed contracts above; external serialization belongs at the boundary. Remove compatibility fallbacks, reflection, and repeated defensive probing that become unnecessary after boundary validation.
- Do not use reflection, including `getattr`, `setattr`, `hasattr`, or `vars`. Do not use dictionary or mapping `.get(key)` access; use typed model attributes or explicit membership and indexed access inside a local typed mapping.
- Use enums for closed string vocabularies. Do not embed unexplained integer literals in executable logic; put operational limits in named configuration or parameter/model defaults and use standard enums for protocol status codes. Do not disguise magic numbers as meaningless numeric aliases.
- Define error codes and domain error construction centrally. Services use that API; protocol adapters translate errors to HTTP or MCP responses.
- Write source code and tests in English. Do not add comments or docstrings.
- Use `match/case` for parallel dispatch branches. Keep independent validation guards independent.
- Use asynchronous FastAPI endpoints and SQLAlchemy typed models and expressions. Do not write business SQL strings.
- Use Python 3.12 parameter syntax such as `[T]`; do not use `TypeVar`.
- Use absolute imports rooted at `mining_server` for Backend, `mining_gateway` for Gateway, and `mining_contracts` for public contracts.
- Define explicit Request/Response models. Use `Depends` to assemble Services and Repositories for HTTP requests. Routes must not bypass Services for business operations. Background tasks use the same explicit composition factories without invoking FastAPI dependency resolution.
- Generate Alembic schema migrations from models, then manually review and complete data backfills. Include the date in migration filenames. Never edit released migrations.
- Validate fields at construction and external boundaries, fail immediately on invalid input, and never silently replace invalid explicit configuration with defaults.

## 业务与执行边界

- 根目录仅保留 `README.md` 与 `AGENTS.md`；其他开发文档统一在 `docs/` 维护。当前唯一设计入口为 `docs/DESIGN.md`；按用户确认的范围同步契约、取舍与验收证据，不另建重复设计。
- 实现独立矿业业务 HTTP 服务与 MCP Gateway。Backend 在 `apps/backend`、Gateway 在 `apps/mcp_gateway`，各自维护源码、测试、依赖与 Dockerfile。Gateway 只通过具名异步 HTTP Client 调用业务 API，不导入后端实现，不共享 ORM、业务数据库或模型凭据。`packages/contracts` 只维护公开 HTTP 模型、错误响应及相关枚举，不导入任一服务；根 uv workspace 统一锁定依赖，各镜像只安装本服务的依赖闭包。
- 两个应用 svc 为 MCP Gateway 与业务后端；后端管理 FastAPI、Celery worker、单实例 Beat。PostgreSQL、RabbitMQ 为基础设施，另计。开发先在本地 Docker 验收，未经请求不创建或发布远端资源。
- PostgreSQL 是业务、任务、步骤结果、模型执行记录与检查点的事实来源。Celery 执行，Beat 定时调度，RabbitMQ 传递任务；首版不引入 Redis，其他中间件先与用户讨论。
- 采用显式 workflow + LLM 和有界文档 Agent，编排自行实现，不使用 LangChain/LangGraph。协议、模型、TUI 等参考项目的机制可借鉴，不复制其全局状态、宽泛异常兜底或无消费者兼容层。
- 事务由 Service 组织，Repository 不隐藏提交；每个并发 task 独立 AsyncSession，不在事务内等待外部 I/O。阻塞文件、PDF 和渲染操作不能阻塞 FastAPI 事件循环。
- 步骤结果、状态、检查点与后续待投递记录在 PG 同一事务提交。队列允许重投，执行由任务身份、generation 与真实结果去重。模型或副作用结果未知不自动重放，恢复不得伪造成功。
- 不做注册、租户、计费或完整管理后台；信源、运行配置、任务与模型连接状态提供 HTTP 接口。
- 正式模型接入支持环境变量注入 OpenAI 兼容 API 的 base URL、API key 与协议，管理 HTTP 接口选择模型渠道与模型。ChatGPT 订阅作为本地验收及可选渠道保留，参考 gongkao-hub 的独立 OAuth、身份校验、刷新与 Responses 实现；不读取其他应用现有凭据，不在渠道失败时静默切换或调用付费 API。密钥不进入任务快照、查询响应或日志。
- 数据源仅选免费渠道，由主 Agent 根据题目和实证仲裁。区分可读页面、已打通取数、授权范围和历史覆盖；不将现货、期货、发行人售价或不同频率数据混用。
- PDF 接受公开 URL 与直接上传，使用多份不同布局真实报告验收。资源量与储量、NI 43-101 与 JORC、矿体/日期/品位/含量/单位分别记录，所有结果可追溯至文档与页码。

## 协作与验证

- 用户当前明确指令优先；讨论先收敛方案，已授权开发持续完成实施、验证与交付。默认简体中文沟通，设计文档中文，源码与测试英文。
- 开发和调研子智能体使用用户指定的 `gpt-6.1-sol`，主 Agent 负责契约、集成、验收与调度。分配独立文件范围，避免多智能体同时编辑同一文件。
- 第三方 API 写代码前先用 Context7 核对实际版本，混版或索引过时时查对应 tag 源码和官方文档；依赖使用 lockfile。
- 搜索优先 `rg`，独立读取批量执行；测试验证真实边界、异常、并发与恢复，不复述实现。持久化和队列故障用真实临时 PostgreSQL、RabbitMQ 和文件。
- 调研源码、临时脚本、日志、截图和中间文件放仓库外当前会话的 outputs，默认 `C:/Users/20977/.codex/outputs/01a11b34-1c27-77c1-bf47-7d8d5cf371f1/`；收尾删除不再需要的临时文件。正式测试、迁移与设计按项目目录维护，用户数据放独立持久化目录。
