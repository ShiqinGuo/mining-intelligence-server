# 运行与接入

需要 Docker Compose、Python 3.12 和 uv。以下命令在克隆后的仓库根目录执行，示例使用 PowerShell。

## 启动

```powershell
uv run python scripts/init_env.py
docker compose -p code4interview-server up -d --build
```

`uv run` 自动准备依赖。初始化脚本生成私有 `.env`，重复执行保留已有配置。首次启动自动迁移数据库并安装默认信源与行情规格。请保留 Compose 项目名 `code4interview-server`，已有数据卷与该名称关联。不要提交 `.env`，备份数据库时也要安全保存其中的 master key。

采集、查询和 PDF 解析可先使用；新闻分析和资源量抽取需要完成下方模型配置。

| 入口 | 地址 |
| --- | --- |
| HTTP 文档与管理配置 | `http://127.0.0.1:28110/docs` |
| 新闻 MCP | `http://127.0.0.1:28111/mcp/news/` |
| 文档 MCP | `http://127.0.0.1:28111/mcp/documents/` |
| 行情 MCP | `http://127.0.0.1:28111/mcp/market/` |

端口默认只绑定本机。Swagger 的 Authorize 填入 `.env` 中的 `MINING_ADMIN_TOKEN`，用于管理配置。

## Agent 接入 MCP

支持 Streamable HTTP 的 Agent 均可连接。参照 [MCP 配置示例](../mcp-config.json)，将三个地址添加到所用 Agent 的 MCP 配置，并设置请求头 `Authorization: Bearer <MINING_SERVICE_TOKEN>`。token 来自本项目私有 `.env`，不要使用管理员 token；示例 JSON 的结构需按实际客户端格式调整。

不想自行部署项目和运行定时采集任务，可以直接接入作者通过 Cloudflare Tunnel 提供的公网 MCP 服务：将三个 MCP 地址的 `http://127.0.0.1:28111` 替换为 `https://mining-mcp.charworkservice.site`，service token 向作者获取。

可以把下面的提示词交给能够修改自身配置的 Agent，并通过其私有凭据方式提供 service token：

```text
请按照你当前客户端的 MCP 配置格式，添加以下三个 Streamable HTTP 服务：
mining-news：http://127.0.0.1:28111/mcp/news/
mining-documents：http://127.0.0.1:28111/mcp/documents/
mining-market：http://127.0.0.1:28111/mcp/market/
各服务使用 Authorization: Bearer <我单独提供的 service token>。
请将凭据保存在私有配置中，然后连接服务并读取工具列表。
```

接入后可以询问“给我生成一份关于 Pilbara（Pilgangoora）锂矿的今日简报，包含新闻摘要、资源量、价格走势、风险提示和来源链接”。报告需已被新闻搜索发现，或在请求中提供公开 PDF URL；没有可用资源表时应说明缺口。Agent 应轮询异步任务，保留新闻、资源报告和行情各自的日期、规格、标准与来源。

## 管理请求

下方模型配置和订阅授权示例共用以下请求头：

```powershell
$adminLine = Get-Content -LiteralPath .env | Where-Object { $_.StartsWith('MINING_ADMIN_TOKEN=') }
$adminToken = $adminLine.Split('=', 2)[1]
$headers = @{ Authorization = "Bearer $adminToken" }
$base = 'http://127.0.0.1:28110/api/v1'
```

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

修改环境后运行 `docker compose -p code4interview-server up -d --force-recreate mining-backend`。密钥仅注入后端，不进入 Gateway、任务快照或 HTTP 查询结果。环境变量设置不会覆盖已有 PG 运行配置；已有实例还需通过管理接口选择渠道和模型：

```powershell
Invoke-RestMethod "$base/model-channels" -Headers $headers
Invoke-RestMethod "$base/model-channels/openai_compatible/check" -Method Post -Headers $headers
```

通过 Swagger 的 `GET/PUT /api/v1/settings` 保留其余配置与当前 revision，并修改 `model_channel` 和 `model_name`。新任务使用提交时的模型配置；旧任务需要采用新配置时，使用 `reprocess` 创建新任务。

## 可选 ChatGPT 订阅连接

订阅渠道通过本项目独立授权，不读取其他应用凭据。初始化已生成 `MINING_OAUTH_HOST_ID`。在 Swagger 的运行设置中选择 `chatgpt_subscription` 和账号支持的模型；未授权的模型任务会等待授权。

```powershell
$connection = Invoke-RestMethod "$base/model-connection" -Headers $headers
$installBody = @{ expected_revision = $connection.revision } | ConvertTo-Json
$installation = Invoke-RestMethod "$base/model-connection/install" -Method Post -Headers $headers -ContentType 'application/json' -Body $installBody
$pairingDirectory = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
New-Item -ItemType Directory -Force -Path $pairingDirectory | Out-Null
$installationPath = Join-Path $pairingDirectory 'installation.json'
$installation | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $installationPath -Encoding utf8
try {
    uv run python -m mining_server.infrastructure.auth.local_helper --installation $installationPath --handoff-command docker compose -p code4interview-server exec -T mining-backend python -m mining_server.infrastructure.auth.import_helper
} finally {
    Remove-Item -LiteralPath $installationPath
    Remove-Item -LiteralPath $pairingDirectory
}
```

打开 helper 输出的官方登录链接，浏览器授权回调到本机 loopback。helper 通过 Docker stdin 将凭据交给后端，后端独占刷新，业务 HTTP 不提供明文 token 导入接口。授权完成后通过 `/model-connection/check` 检查实际账号与模型可用性；body 使用 `GET /api/v1/model-connection` 返回的当前 revision 作为 `expected_revision`。通过任务 `resume` 继续等待授权的工作。
