# CWS 官方 Codex Plus 共享方案

本方案不配置自定义 Provider。员工端 Codex 始终使用内置 `openai` Provider，访问官方 ChatGPT Codex 后端。

## 鉴权边界

- 服务端保存每个 Plus 账号的 OAuth 刷新令牌。
- 员工端只获得短期官方访问令牌；写入的 `auth.json` 使用 Codex 原生 `chatgptAuthTokens` 模式，`refresh_token` 固定为空。
- 访问令牌仍属于敏感凭据，员工端可以在有效期内读取和使用；这个风险无法在本地运行官方 Codex 插件的前提下完全消除。
- 第一版按 VS Code 会话固定账号。启动时根据额度和活跃租约选择账号；需要跨账号时重新同步并重载 VS Code 窗口。

## 服务端目录

首个账号沿用：

```text
/var/lib/cws-codex/auth.json
```

增加账号时，每个账号使用独立 `CODEX_HOME` 完成一次官方登录，然后保存为：

```text
/var/lib/cws-codex/accounts/account-02/auth.json
/var/lib/cws-codex/accounts/account-03/auth.json
```

目录和文件权限应分别为 `0700` 和 `0600`。

新增、重新授权、额度检查和停用账号的完整命令见
[服务器账号管理使用说明](SERVER_ACCOUNT_MANAGEMENT.zh-CN.md)。

## 启动 Broker

配置逐设备令牌注册表和一个随机管理令牌：

```bash
export CWS_CODEX_DEVICE_TOKEN_FILE='/var/lib/cws-codex/device-tokens.json'
export CWS_CODEX_ADMIN_TOKEN='仅管理员使用的随机长令牌'
python scripts/codex_plus_broker.py
```

`CWS_CODEX_DEVICE_TOKENS` 只为兼容早期静态令牌保留，新部署应通过下述管理工具逐台签发。

默认只监听 `127.0.0.1:8765`。当前服务器通过 VLESS + REALITY 的 `freedom` 出站访问 Broker：员工端把内部目标 `codex.cws.internal:8765` 发送给本地 VLESS HTTP/Mixed 代理，服务器将该内部主机名解析到回环地址。不要把 8765 开放到公网。

额度来自官方 Codex usage 接口。调度分数以主窗口和次窗口中较高的 `used_percent` 为基础，并对当前活跃租约增加惩罚，优先分配余量较多、负载较低的账号。

管理员可通过 `http://codex.cws.internal:8765/quota` 打开独立只读页面，或在 CWS 管理后台的
“系统管理”页面查看账号额度和按员工令牌归属的 Token 使用量。独立页面仅能通过当前内网代理/VLESS 访问。

## 员工端同步

管理员先在服务器为每台员工电脑签发独立令牌。明文只显示一次，服务器仅保存 SHA-256 哈希：

```bash
python /opt/cws-codex/codex_device_tokens.py create '研发-PC01'
python /opt/cws-codex/codex_device_tokens.py list
python /opt/cws-codex/codex_device_tokens.py revoke '研发-PC01'
python /opt/cws-codex/codex_device_tokens.py rotate '研发-PC01'
```

员工安装时输入管理员签发的本机令牌，不应输入其他电脑的令牌，也不应共用服务器测试令牌。

员工端只需 Python 3：

```bash
export CWS_CODEX_BROKER_URL='http://codex.cws.internal:8765'
export CWS_CODEX_DEVICE_TOKEN='该设备的随机令牌'
python scripts/codex_plus_sync.py --codex-home "$HOME/.cws-codex" --watch
```

未设置 `CWS_CODEX_PROXY_URL` 时，员工端默认使用公司内网 HTTP/Mixed 代理 `http://192.168.2.38:7897`。

同步成功后必须完全退出所有 VS Code 窗口，再通过项目提供的启动器打开。直接执行 `CODEX_HOME=... code` 在 macOS 上可能被已经存在的 VS Code 后台进程接管，导致新环境变量不生效。

macOS：

```bash
export CWS_CODEX_BROKER_URL='http://codex.cws.internal:8765'
export CWS_CODEX_DEVICE_TOKEN='该设备的随机令牌'
export CWS_CODEX_PROXY_URL='http://127.0.0.1:1082'
./scripts/start_vscode_cws_codex_macos.sh
```

Windows PowerShell：

```powershell
$env:CWS_CODEX_BROKER_URL='http://codex.cws.internal:8765'
$env:CWS_CODEX_DEVICE_TOKEN='该设备的随机令牌'
.\scripts\start_vscode_cws_codex_windows.ps1
```

Windows 员工日常使用建议直接分发标准图形安装器 `dist/CWS-Codex-Setup-v0.1.1.exe`，员工无需
手工解压即可双击安装，并可在 Windows“已安装的应用”中卸载。`dist/CWS-Codex-Windows-v0.1.1.zip`
保留用于管理员排障。员工输入管理员
为本机签发的令牌后，安装器会使用 Windows DPAPI 保存令牌、安装/检查
官方 `openai.chatgpt` 扩展、创建独立 `%USERPROFILE%\.cws-codex`，并在桌面生成 `公司 Codex（VS Code）`
快捷方式。安装器还会在当前 Windows 用户的启动目录创建隐藏后台任务；该任务不依赖 CMD 窗口或
VS Code 是否打开，每 5 分钟同步短期凭据并上报 Token 累计值。后台使用互斥锁避免重复运行，电脑
休眠或断网恢复后会从本地累计记录补报。

员工统计以服务器签发的设备令牌 ID 和标签为唯一身份，不信任客户端自行填写的电脑名。同一个
员工令牌若错误地安装到多台电脑，用量会合并到该令牌名下。员工端只上传散列后的会话 ID，以及
`input_tokens`、`cached_input_tokens`、`output_tokens`、`reasoning_output_tokens` 和 `total_tokens`
等累计计数，不上传提示词、回复、代码或文件内容。服务端对同一令牌、同一会话保存最大累计值，
重复上报不会重复计数；令牌吊销后不能继续上报，历史统计仍保留。

该统计反映 Codex 本地会话记录中的模型 Token 数，不是 OpenAI 官方账单，也不能直接换算为 Plus
额度窗口的剩余百分比。本地记录被首次上报前删除、后台任务被本机管理员停用，或 Codex 将来改变
本地记录格式时，都可能造成漏计；后台以最后上报时间提示数据是否过期。

员工端每次凭据同步和用量上报还会发送本机默认网络的局域网 IPv4。Broker 分别保存客户端声明的
`client_ip` 和连接实际来源 `source_ip`，并为每个员工令牌保留最近 20 个地址的出现历史。经过
VLESS 时 `source_ip` 可能只是回环或代理地址，因此后台以 `client_ip` 为主要设备定位信息。IP
由客户端声明，可被本机管理员修改，只能用于内部追踪，不能作为鉴权依据。

若需要立即重新选择账号：

```bash
python scripts/codex_plus_sync.py --new-lease
```

`--watch` 默认每 5 分钟同步一次，并保持同一租约的账号粘性。同步助手更新同一账号的新访问令牌后，Codex 遇到 401 会重新读取鉴权文件；切换到不同账号时，应执行“Developer: Reload Window”或完全重启 VS Code。

本次验证使用 macOS Shadowrocket 的 HTTP/Mixed 端口 `1082`。Windows v2rayN 等客户端常见端口为 `10809`，应以客户端实际的 HTTP/Mixed 端口为准。若客户端只开放 SOCKS5，需要同时开启 HTTP/Mixed 入站。

员工可按网络环境覆盖代理：

```bash
# 公司内网默认值，可不设置
export CWS_CODEX_PROXY_URL='http://192.168.2.38:7897'

# macOS Shadowrocket
export CWS_CODEX_PROXY_URL='http://127.0.0.1:1082'

# 明确不走代理
python scripts/codex_plus_sync.py --direct
```
