# Mining Server

独立的矿业 HTTP 业务服务与 MCP Gateway。新闻、技术报告和行情由业务后端处理，通用 Agent 通过三个 MCP server 接入。

- [设计与边界](DESIGN.md)
- [运行与验收](RUN.md)
- [工程约定](AGENTS.md)
- [持续集成](.github/workflows/ci.yml)

两个应用 svc 为 `mining-backend` 和 `mcp-gateway`；PostgreSQL、RabbitMQ 为基础设施。检查点与任务事实存 PostgreSQL，不使用 Redis。Client 是另一个项目，不依赖其本地源码。

使用 Python 3.12、FastAPI、SQLAlchemy asyncio、Celery、RabbitMQ、PostgreSQL 和官方 MCP SDK。正式模型渠道支持环境变量注入 OpenAI 兼容 API Key、base URL 与协议，通过管理 HTTP 接口选择渠道和模型。ChatGPT 订阅保留为可选渠道，本地真实报告验收使用订阅；缺少模型凭据时，数据查询与已配置的免费采集仍可使用。

行情明确区分碳酸锂现货、锂辉石稀疏现货、具体合约期货与发行人季度售价，保留规格、时间、单位和来源。

测试使用 pytest 与 pytest-asyncio。GitHub Actions 在 push、pull request 和手动触发时运行代码检查、真实 PostgreSQL/RabbitMQ 集成测试、真实 PDF 回归和 Docker 启动验证；依赖按 uv.lock 安装。模型协议测试采用脚本化响应，实际订阅模型抽取验收见 RUN.md。
