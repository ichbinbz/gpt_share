# CWS Codex 模型健康探测与双源更新设计

## 目标

在 CWS Codex Broker 中加入按账号、按模型的主动健康探测，使模型容量超限的账号不会继续被错误分配；同时让 Windows 和 Linux 员工端优先从 Broker 获取更新，在 Broker 不可用时回退 GitHub Release，并在启动时明确显示当前版本和更新检查结果。

目标发布版本为 `v0.1.7`。Broker 仍只监听 `127.0.0.1:8765`，员工通过现有受控代理/VLESS 访问，不开放公网端口。

## 已确认约束

- Broker 每 15 分钟探测全部账号。
- 每个账号动态发现其可用 Codex 文本模型；发现失败时使用配置的兜底模型列表。
- 对发现的全部可用模型发送最小化模型请求。
- 容量超限状态冷却 30 分钟。
- 一个账号至少有一个可用模型时仍可参与调度；全部模型不可用时停止分配。
- 管理页面显示每个模型的健康状态、错误和冷却信息。
- 客户端更新采用 Broker 优先、GitHub 兜底。
- Windows 和 Linux 客户端软件都镜像到服务器。

## 方案选择

### 模型探测

采用 Broker 直接调用 Codex 客户端实际使用的 ChatGPT OAuth 后端，而不在服务器为每次探测启动 Codex CLI 子进程。

备选方案一是为每个账号和模型运行 `codex exec`。它最接近真实客户端，但需要服务器安装并维护匹配版本的 Codex CLI，进程启动成本高，错误解析也更脆弱。备选方案二是由员工端上报容量错误，成本最低，但不能在账号被分配前发现问题，而且用户已明确选择服务器主动探测。

直接 HTTP 探测复用现有服务端 `auth.json`、OAuth 刷新和 `httpx.AsyncClient`，依赖更少，也能把超时、HTTP 状态、Codex 错误码和错误消息结构化保存。正式启用前必须用服务器上的一个正常账号和已知异常账号 `chatgpt024` 做一次真实请求验证；如果实际请求协议与预期不一致，部署不得继续。

官方 OpenAI Models API 定义模型列表接口用于返回当前可用模型，Responses API 定义模型响应和结构化错误对象。ChatGPT OAuth 的 Codex 后端不是公开 Platform API 合约，因此实现会把端点、超时和兜底模型列表做成配置，并用集成测试固定当前 Codex 行为：

- https://developers.openai.com/api/reference/resources/models
- https://developers.openai.com/api/reference/cli/resources/responses/methods/create

### 更新源

采用 Broker 优先、GitHub 兜底。Broker 是员工端已经连通并鉴权的内部入口，速度和可达性更稳定；GitHub 保留为灾备源和公开发布源。

## 模型健康组件

### 配置

`BrokerSettings` 增加：

- `CWS_CODEX_MODEL_PROBE_INTERVAL_SECONDS`，默认 `900`。
- `CWS_CODEX_MODEL_COOLDOWN_SECONDS`，默认 `1800`。
- `CWS_CODEX_MODEL_PROBE_CONCURRENCY`，默认 `2`。
- `CWS_CODEX_MODEL_PROBE_TIMEOUT_SECONDS`，默认 `45`。
- `CWS_CODEX_MODEL_FALLBACKS`，逗号分隔的兜底模型列表。
- `CWS_CODEX_MODEL_HEALTH_FILE`，默认 `/var/lib/cws-codex/model-health.json`。
- 可覆盖的 Codex 模型列表和响应端点；默认使用当前官方 Codex 客户端的 ChatGPT 后端地址。

兜底列表必须随 `v0.1.7` 的实际 Codex 客户端支持模型确定，不在设计文档中硬编码容易过期的模型名。

### 状态模型

`model-health.json` 使用原子写入，权限为 `0600`。顶层按账号别名保存：

```json
{
  "version": 1,
  "updated_at": 1789700000,
  "accounts": {
    "chatgpt024": {
      "available": false,
      "discovery_source": "remote",
      "last_probe_at": 1789700000,
      "next_probe_at": 1789700900,
      "models": {
        "example-model": {
          "status": "capacity",
          "available": false,
          "last_probe_at": 1789700000,
          "cooldown_until": 1789701800,
          "http_status": 503,
          "error_code": "server_overloaded",
          "error_message": "Selected model is at capacity. Please try a different model."
        }
      }
    }
  }
}
```

允许的模型状态：

- `available`：最小响应成功。
- `capacity`：错误码为 `server_overloaded`，或规范化消息包含 `selected model is at capacity`。
- `quota`：明确的账号额度耗尽或速率窗口耗尽。
- `auth`：刷新后仍为 401/403。
- `unsupported`：模型不存在或账号无权使用；不计入账号的可用模型集合。
- `probe_error`：网络、超时或无法分类的临时故障。

只有 `capacity`、`quota` 和 `auth` 是阻断状态。`probe_error` 不得因一次网络失败把原本健康的账号下线：若有最近一次成功结果，则沿用到下一轮；若从未成功探测，则账号保持现有额度调度逻辑，并在管理页标记“探测未知”。

### 模型发现与探测

每轮按以下顺序执行：

1. 调用账号的 `ensure_fresh` 获取有效访问令牌。
2. 调用 Codex 模型列表接口，携带 `Authorization` 和 `ChatGPT-Account-Id`。
3. 仅保留可用于 Codex 文本响应的模型，排除图像、音频、Realtime、Embedding、Moderation 和已停用模型。
4. 发现失败时使用 `CWS_CODEX_MODEL_FALLBACKS`。
5. 以最大并发 2 为每个模型发送最小请求：`store=false`、禁用工具、最小合理输出上限、固定短输入。
6. 解析 HTTP 状态、JSON/SSE 错误码和错误消息，更新模型状态。
7. 至少一个模型 `available` 时账号整体 `available=true`；所有受支持模型都处于阻断状态时为 `false`。

容量错误设置 `cooldown_until = now + 1800`。冷却期内仍会在下一轮统一探测，但不会因为管理页手动刷新而高频重复调用。探测成功立即清除该模型的容量冷却。

### 生命周期

FastAPI 使用 lifespan 管理一个后台任务：服务启动时先加载持久状态，然后立即安排首轮探测；之后每 15 分钟运行。服务关闭时取消任务并关闭 `httpx.AsyncClient`。同一时间最多运行一轮，管理页刷新只读取状态，不触发模型请求。

### 调度集成

新租约候选集先排除 `model-health.json` 中明确 `available=false` 且状态仍在有效期内的账号，再沿用现有额度、重置时间和活动租约排序。

如果客户端带来的亲和租约指向已明确不可用的账号，Broker 删除该旧租约并重新选择账号，而不是继续返回原账号。若所有账号都被明确阻断，返回 `503 all Codex accounts are unavailable`，不得退回已知容量超限账号。

未完成首轮探测或只有临时网络错误时，继续使用现有额度调度，避免 Broker 启动后出现全员不可用。

### 管理页面

`GET /v1/admin/accounts` 的每个账号增加：

- `model_probe_available`
- `model_probe_status`
- `model_probe_last_at`
- `model_probe_next_at`
- `model_probe_source`
- `model_health[]`，包含模型名、状态、可用性、最近探测、冷却截止、错误码和经过截断的错误消息。

额度卡片新增整体标签“模型可用 / 容量超限 / 探测未知”，并在可展开区域列出每个模型。错误文本用 `textContent` 渲染，不插入 HTML。管理员令牌和账号访问令牌绝不进入页面响应。

## 双源更新组件

### 服务器文件布局

服务器目录固定为 `/var/lib/cws-codex/releases/`，权限 `0755`，发布文件 `0644`，清单 `0644`：

```text
/var/lib/cws-codex/releases/
  latest.json
  CWS-Codex-Setup-v0.1.7.exe
  CWS-Codex-Windows-v0.1.7.zip
  CWS-Codex-Linux-v0.1.7.tar.gz
  CWS-Codex-Server-v0.1.7.tar.gz
```

`latest.json` 由发布脚本生成，不能手工拼接。每个资产记录平台、文件名、字节数和 SHA-256。清单只引用同目录的简单文件名。

### Broker 接口

- `GET /v1/client/releases/latest`：需要有效设备 Bearer 令牌，返回版本、发布时间和当前平台资产元数据。
- `GET /v1/client/releases/download/{filename}`：需要有效设备 Bearer 令牌，只允许下载 `latest.json` 中列出的文件，使用 `FileResponse`，设置固定内容类型、`Content-Disposition`、`Content-Length` 和 `Cache-Control: private`。

接口不接受路径、绝对路径、斜杠、反斜杠或清单外文件。服务端在返回前重新检查文件大小和 SHA-256 与清单一致；不匹配时返回 503。发布目录中不得出现 `auth.json`、设备令牌库或环境文件。

### Windows 客户端

启动窗口首先输出当前 `config.json.client_version`。更新器显式把 TLS 1.2 加入 `ServicePointManager.SecurityProtocol`，同时保留系统已有协议。

每次启动按以下顺序：

1. 解密本机 DPAPI 设备令牌。
2. 通过配置的 Broker URL 和代理请求服务器清单。
3. 若服务器请求、鉴权或清单验证失败，则请求 GitHub `releases/latest`。
4. 明确输出使用的更新源、当前版本、最新版本或双源失败原因。
5. 发现新版后显示现有确认对话框。
6. 从选定源下载安装器；服务器源请求必须携带设备令牌。
7. 校验清单或 GitHub asset digest 中的 SHA-256，然后以 `/S /AUTOUPDATE` 执行。

服务器检查每次启动执行，不沿用旧的 24 小时负缓存。GitHub 只在服务器失败时访问。`Launch-CwsCodex.ps1` 增加可测试的 `-StatusOnly`，只输出版本且不检查更新、不同步凭据、不启动 VS Code。

### Linux 客户端

Linux 使用相同的服务器优先、GitHub 兜底策略和设备令牌鉴权。控制台输出当前版本和更新源；图形确认逻辑保持不变。下载后校验 SHA-256，再调用 `install.sh --auto-update`。

### 旧客户端引导

0.1.5/0.1.6 客户端尚不知道服务器更新接口，因此 `v0.1.7` 仍必须发布到 GitHub。当前测试电脑由管理员手动运行一次 0.1.7 安装器完成引导；其他无法通过 GitHub 自动升级的旧客户端需要同样的一次性安装。升级到 0.1.7 后，后续版本可直接从服务器更新。

## 安全与隐私

- 模型探测只发送固定字符串，不包含用户输入、会话内容或公司数据。
- 探测响应不保存正文，只保存状态、时间、错误码和截断后的错误消息。
- 账号访问令牌只保存在服务器内存和现有 `auth.json`，不写入健康状态文件或日志。
- 更新接口沿用设备令牌鉴权；管理员接口仍使用管理员令牌。
- 所有更新文件执行前必须校验 SHA-256。
- 8765 继续仅监听回环地址，通过现有隧道访问。

## 测试策略

### 单元与集成测试

- 模型发现过滤，只保留支持的 Codex 文本模型。
- 成功、容量超限、额度耗尽、鉴权失败、模型不支持、网络错误的分类。
- 一次网络错误不覆盖最近健康状态。
- 全模型阻断时账号被排除；至少一个模型成功时账号仍可选。
- 亲和租约指向不可用账号时自动换号。
- 管理接口不泄露访问令牌，页面安全渲染模型状态。
- 更新清单鉴权、白名单、目录穿越、文件缺失、大小和哈希不匹配。
- Windows TLS 1.2 保留式启用、`-StatusOnly` 输出和双源回退。
- Linux 双源回退和哈希校验。
- 服务端发布包包含管理页和更新接口代码，但不包含真实凭据。

### 线上验证

部署前备份 Broker、网页、systemd 和环境文件。先用临时命令验证正常账号至少一个模型成功、`chatgpt024` 能复现并分类 `server_overloaded`。部署后验证：

- 服务仍只监听 `127.0.0.1:8765`。
- `/healthz` 正常。
- 未授权更新接口返回 401。
- 有效设备令牌可读取清单、下载文件且 SHA-256 一致。
- 管理页显示账号及模型状态。
- 新租约不再选择所有模型均阻断的 `chatgpt024`。
- 本机 0.1.5 经一次性安装升级到 0.1.7，启动时显示版本和更新源。

## 发布与回滚

发布 `v0.1.7`，上传 Windows 推荐 ZIP、安装器、Windows 脚本包、Linux 客户端和服务端包；同一批文件复制到服务器发布目录并生成清单。

回滚时恢复部署前 Broker 和管理页备份、重启 systemd。模型健康文件可以保留；旧 Broker 会忽略它。更新发布目录不参与 Broker 核心凭据和租约状态，可独立回滚或清空到上一版清单。
