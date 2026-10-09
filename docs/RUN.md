# 运行与接入

需要 Docker Compose、Python 3.12+ 和 uv。

## 启动

```powershell
uv run python scripts/init_env.py
docker compose -p code4interview-server up -d --build
```

初始化生成 `.env`，启动时自动迁移数据库并安装信源与行情规格。

| 入口 | 地址 |
| --- | --- |
| HTTP 管理 | http://127.0.0.1:28110/docs |
| 新闻 MCP | http://127.0.0.1:28111/mcp/news/ |
| 文档 MCP | http://127.0.0.1:28111/mcp/documents/ |
| 行情 MCP | http://127.0.0.1:28111/mcp/market/ |

## MCP 接入

[配置示例](../mcp-config.json)使用环境变量注入 service token：

```powershell
$serviceLine = Get-Content .env | Where-Object { $_.StartsWith('MINING_SERVICE_TOKEN=') }
$env:MINING_SERVICE_TOKEN = $serviceLine.Split('=', 2)[1]
$env:MCP_SDK_GENERATION = 'v2'
$env:MCP_PROTOCOL_NEGOTIATION = 'auto'
claude --mcp-config ./mcp-config.json --strict-mcp-config
```

公网服务将上述地址的 `http://127.0.0.1:28111` 换为 `https://mining-mcp.charworkservice.site`，测试 Token 见交付 RUN.md。客户端配置与 [mining-brief Skill](https://github.com/ShiqinGuo/e/tree/main/examples/mining) 位于客户端仓库。

## 后端模型

将一组默认配置写入 `.env`，再启动服务。

ChatGPT / OpenAI API：

```dotenv
MINING_MODEL_CHANNEL=openai_compatible
MINING_MODEL_NAME=gpt-6.1-sol
MINING_OPENAI_BASE_URL=https://api.openai.com/v1
MINING_OPENAI_PROTOCOL=responses
MINING_OPENAI_API_KEY=your_openai_api_key
```

DeepSeek：

```dotenv
MINING_MODEL_CHANNEL=openai_compatible
MINING_MODEL_NAME=deepseek-flash
MINING_OPENAI_BASE_URL=https://api.deepseek.com
MINING_OPENAI_PROTOCOL=chat_completions
MINING_OPENAI_API_KEY=your_deepseek_api_key
```

已有部署修改后执行 `docker compose -p code4interview-server up -d --force-recreate mining-backend`，并在 `/docs` 的 `PUT /api/v1/settings` 选择渠道和模型。管理授权使用 `.env` 中的 `MINING_ADMIN_TOKEN`。

## ChatGPT 订阅

在 `/docs` 的运行设置中选择 `chatgpt_subscription` 和 `gpt-6.1-sol`，然后执行独立登录：

```powershell
$adminLine = Get-Content .env | Where-Object { $_.StartsWith('MINING_ADMIN_TOKEN=') }
$headers = @{ Authorization = 'Bearer ' + $adminLine.Split('=', 2)[1] }
$base = 'http://127.0.0.1:28110/api/v1'
$connection = Invoke-RestMethod "$base/model-connection" -Headers $headers
$body = @{ expected_revision = $connection.revision } | ConvertTo-Json
$installation = Invoke-RestMethod "$base/model-connection/install" -Method Post -Headers $headers -ContentType 'application/json' -Body $body
$authFile = Join-Path ([IO.Path]::GetTempPath()) ('mining-login-' + [guid]::NewGuid() + '.json')
try {
    $installation | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $authFile -Encoding utf8
    uv run python -m mining_server.infrastructure.auth.local_helper --installation $authFile --handoff-command docker compose -p code4interview-server exec -T mining-backend python -m mining_server.infrastructure.auth.import_helper
} finally {
    Remove-Item -LiteralPath $authFile
}
```
