# ChatGPT Plus 浏览器共享模式

这个分支把 CWS 的默认 OpenAI Web 传输层改成了“真实浏览器桥接”：后端通过 Chrome DevTools Protocol（CDP）连接一个独立 Chromium 配置目录，由浏览器本人完成 ChatGPT 登录、页面请求和站点挑战，CWS 负责排队、驱动输入框并把回复流式转发给各用户。

当前核心范围：

- 多个 CWS 用户共享一个或多个已登录的 ChatGPT Plus 浏览器会话；
- 多账号按健康状态、忙闲、权重与本地额度估算自动负载均衡；
- 新会话自动选账号，已有会话固定回到创建它的账号；
- 用户、会话归属、限额、排队和管理后台继续沿用原 CWS；
- 支持新建文本对话、连续文本对话、历史读取、改名和删除；
- 支持 `Auto / Instant / Thinking / Pro` 的可配置页面标签；
- 每个浏览器账号串行执行一条对话；全局 `max_completion_concurrency` 不得超过已启用账号数；
- 暂不支持上传、图片输入、旧插件模式和 `:continue`。

## 1. 启动专用 Chrome

不要复用日常 Chrome 配置目录。单账号在 macOS 仓库根目录运行：

```bash
chmod +x scripts/start-chatgpt-browser-macos.sh
./scripts/start-chatgpt-browser-macos.sh
```

在打开的专用 Chrome 中登录 `https://chatgpt.com/`，并保持浏览器运行。CDP 端口可完全控制该浏览器，因此只能暴露在可信主机/网络中。

多账号使用不同端口和不同配置目录分别启动：

```bash
CWS_CDP_PORT=9222 CWS_BROWSER_PROFILE_DIR="$PWD/data/chrome-account-a" \
  ./scripts/start-chatgpt-browser-macos.sh

CWS_CDP_PORT=9223 CWS_BROWSER_PROFILE_DIR="$PWD/data/chrome-account-b" \
  ./scripts/start-chatgpt-browser-macos.sh
```

分别在两个窗口登录不同的 ChatGPT 账号。浏览器也可以运行在不同的内部工作节点，只要中央 CWS 能访问对应 CDP 地址。

## 2. 源码运行

创建配置时生成随机密钥，并把 CDP 地址设为本机：

```bash
cd backend
../.venv/bin/python manage.py create_config \
  --output-dir ./data/config \
  --generate-secrets \
  --initial-admin-password '请替换为强密码' \
  --browser-cdp-url http://127.0.0.1:9222
```

`config.yaml` 的关键项应为：

```yaml
openai_web:
  enabled: true
  transport: browser
  browser_cdp_url: http://127.0.0.1:9222
  browser_chatgpt_url: https://chatgpt.com
  enabled_models:
    - chatgpt_auto
  browser_model_labels:
    chatgpt_auto: ''
    chatgpt_instant: Instant
    chatgpt_thinking: Thinking
    chatgpt_pro: Pro
  browser_accounts:
    - id: plus-a
      name: Plus 账号 A
      cdp_url: http://127.0.0.1:9222
      weight: 1
      quota_limits:
        chatgpt_thinking: 100
      quota_window_seconds:
        chatgpt_thinking: 10800
    - id: plus-b
      name: Plus 账号 B
      cdp_url: http://127.0.0.1:9223
      weight: 1
      quota_limits:
        chatgpt_thinking: 100
      quota_window_seconds:
        chatgpt_thinking: 10800
  max_completion_concurrency: 2
  disable_uploading: true
```

页面语言或账号显示的模式名称不同时，修改 `browser_model_labels`；空字符串表示不切换模型，直接使用 ChatGPT 当前/自动模式。

安装依赖并启动：

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd backend
CWS_CONFIG_DIR=./data/config ../.venv/bin/uvicorn main:app \
  --host 127.0.0.1 --port 8000 --log-config logging_config.yaml
```

前端仍按原项目方式运行或构建。管理员登录后可请求以下接口检查桥接状态：

```text
GET /api/system/check-openai-web-browser
```

正常结果包含 `connected: true` 和 `logged_in: true`。

账号池实时状态接口：

```text
GET /api/system/openai-web-browser-pool?refresh=true
GET /api/system/openai-web-browser-pool?refresh=true&refresh_quota=true
```

管理后台“系统管理”页面每 10 秒刷新账号健康、忙闲、成功/失败数与本地额度估算。点击“刷新官网状态/额度提示”时，会临时打开每个账号的模型菜单，读取官网当时实际显示的重置/额度文字。

### 额度字段的含义

- `remaining_estimate`：管理员配置的 `quota_limits` 减去经 CWS 成功完成的请求，只是本地窗口估算；服务重启后从零开始，账号在官网或其他客户端的用量不会被计入。
- `remote_quota_hint`：ChatGPT 官网模型菜单实际显示的原始文字；官网不显示数字时，本系统也不会伪造数字。
- 未配置限额时显示“未知”，但账号仍可依据登录态、忙闲、失败冷却和权重参与负载均衡。
- 官网返回 401/403/429/503/504 时，该账号会暂时冷却，新会话自动选择其他可用账号；已有会话不会跨账号迁移。

公司终端只需访问中央 CWS 并登录 CWS 账号，不需要安装或保存 ChatGPT 官网凭据。若员工绕过 CWS 直接使用官网，那部分消耗无法进入本地额度估算。

## 3. Docker 运行

Docker 容器访问宿主 Chrome 时，配置使用：

```yaml
browser_cdp_url: http://host.docker.internal:9222
```

Linux 宿主还需要在容器启动参数中加入：

```text
--add-host=host.docker.internal:host-gateway
```

镜像已从 Alpine 改为 Debian slim，以便 Playwright 的 CDP 驱动正常工作。数据库、MongoDB、数据卷和反向代理暴露方式继续沿用原 CWS 部署结构。

## 4. 故障定位

- `Cannot connect ... 9222`：专用 Chrome 未启动、端口不可达，或容器没有宿主机映射。
- `not signed in`：在专用 Chrome 中完成登录后重试，无需重启 CWS。
- `composer was not found`：ChatGPT 页面结构已变化，需要更新 `openai_web_browser.py` 中的选择器。
- 找不到 `Instant / Thinking / Pro`：该模式未向账号开放，或页面语言与配置标签不一致；先只启用 `chatgpt_auto`。
- 回复已经出现但超时：适当增大 `ask_timeout` 或 `browser_stable_seconds`，并检查页面是否停在确认/错误提示上。
