# Aion OpenAI Image MCP

独立的 Streamable HTTP 生图服务。一个进程可以提供多个供应商模型入口，供应商的认证、请求格式和响应解析由各自的 adapter 处理。Aion 继续使用统一的 `image_generate_async`，图片由后台写回原会话。

## 配置与启动

本项目统一使用 Python 3.12 和 uv。依赖、启动和测试都通过 uv 管理。

```sh
uv sync --locked
mkdir -p ~/.aion-openai-image-mcp
cp -n config.example.toml ~/.aion-openai-image-mcp/config.toml
chmod 600 ~/.aion-openai-image-mcp/config.toml
```

将示例中的 Base URL 和模型 ID 换成真实值。在启动终端设置配置指定的两个环境变量 `AION_IMAGE_MCP_TOKEN` 和 `EXAMPLE_IMAGE_API_KEY`。前者是自选的本机 MCP 访问令牌，后者由供应商提供。每个供应商连接可以被多个 profile 引用。配置和凭据放在源码目录之外。

```sh
./scripts/service.sh
./scripts/service.sh status
./scripts/service.sh restart
./scripts/service.sh stop
```

管理命令支持 `--config PATH`。服务默认监听 `127.0.0.1:8790`，与 Aion Web 独立启停。默认数据在 `~/.aion-openai-image-mcp/`，管理日志在 `service/mcp.log`。健康接口是 `/healthz`。启动和重启需要相同的供应商环境变量，停止和状态检查无需密钥。

脚本默认执行 `start`，通过 uv 加载私有 `~/.aion-openai-image-mcp/.env`。可用 `AION_IMAGE_MCP_ENV_FILE` 指定其他环境文件。服务在独立进程会话中运行，关闭启动终端或收到 `SIGHUP` 后继续运行。改完配置用 `restart` 生效。

## 导入 Aion

在「设置 → 工具 → MCP」导入以下 JSON，将占位令牌替换成实际的 MCP 访问令牌，并启用入口。URL 最后一段是 profile 名，多个 profile 分别导入。导入只配置连接。全局默认生图后端在 Aion 的「设置 → 工具」单独选择。

```json
{
  "mcpServers": {
    "example-image": {
      "type": "streamable_http",
      "url": "http://127.0.0.1:8790/mcp/example",
      "headers": {"Authorization": "Bearer REPLACE_WITH_MCP_TOKEN"},
      "purpose": "async_image"
    }
  }
}
```

## 适配与恢复

- 首版 `openai_images` 使用 Base URL 下的 `/images/generations` 和 `/images/edits`。开启 `supports_edits` 才接受 PNG 参考图。尺寸支持 `auto` 或 `WIDTHxHEIGHT`，不传尺寸时由供应商决定。可选的 `quality` 映射将 Aion 的 `fast` 和 `quality` 转成上游值。没有命中映射时可以通过 profile 的 `options.default_quality` 设置默认质量。
- `aiwave_grok` 用于 AiWave 的 Grok Imagine 文生图，`supports_edits` 必须为 `false`。普通档和质量档配置为不同的模型 ID。适配器请求 Base64 结果，将 JPEG 或 WebP 转为 PNG，并将尺寸转换为宽高比。
- 输出接收单张 PNG 的 `b64_json` 或 URL。下载只允许供应商来源及显式配置的 CDN 来源，不转发供应商令牌，也不跟随重定向。供应商连接默认使用 HTTPS，环回地址可用于本机模拟和本地服务。
- 增加供应商时，在 `adapters/` 实现 profile 校验、请求和结果映射，在显式注册表加入 adapter ID，再为它配置 profile。专用参数可放在 profile 的 `options` 表，额外凭据用连接的 `secret_env` 表映射到环境变量。MCP 的三个工具和 SQLite 任务处理可复用。
- Aion 重启不会影响独立 worker。MCP 自己重启后继续处理未发出的请求。对已发出且无法确认结果的付费请求返回 `submission_uncertain`，不重发。图片在 MCP 保留七天，已被 Aion 取回的图片继续由原会话保存。过期任务保留轻量幂等记录。

```sh
uv run --locked pytest
```
