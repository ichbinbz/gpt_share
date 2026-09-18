CWS Codex Linux 服务端部署包
============================

适用系统：
- 使用 systemd 的 Linux 服务器
- Python 3.10 或更高版本
- 已安装 python3-venv

安全说明：
- 本包不包含任何 ChatGPT auth.json、员工设备令牌、管理员令牌或运行数据。
- Broker 默认只监听 127.0.0.1:8765，不应直接暴露到公网。
- 首次安装不会自动启动服务，完成凭据和环境变量配置后再手动启动。

安装：
1. 解压部署包并进入目录。
2. 使用 root 执行：

   ./install-server.sh

3. 把第一个已登录账号的 auth.json 安全复制到：

   /var/lib/cws-codex/auth.json

4. 编辑 /etc/cws-codex/broker.env，将 CWS_CODEX_ADMIN_TOKEN 替换为随机长令牌。
   同时将 CWS_CODEX_USER_TOKEN_SECRET 替换为另一个至少 32 字符的随机令牌；该值用于重建
   用户自助查询的设备令牌，创建用户后必须保持稳定。
   可用以下命令生成：

   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'

5. 检查权限并启动：

   chmod 600 /var/lib/cws-codex/auth.json /etc/cws-codex/broker.env
   systemctl enable --now cws-codex-broker
   systemctl status cws-codex-broker

6. 本机健康检查：

   curl http://127.0.0.1:8765/healthz

常用管理命令：
- 新建设备令牌：

  /opt/cws-codex-venv/bin/python /opt/cws-codex/codex_device_tokens.py create '员工或设备标签'

- 查看设备令牌状态：

  /opt/cws-codex-venv/bin/python /opt/cws-codex/codex_device_tokens.py list

- 查看服务日志：

  journalctl -u cws-codex-broker -f

- 管理员额度与用户管理页面：

  http://codex.cws.internal:8765/quota

- 员工 Token 自助查询入口：

  http://codex.cws.internal:8765/token

  管理员在额度页面输入“名字首字母.姓氏”的大写用户名（例如 YX.GUO）和工号增加用户。员工使用
  该用户名、工号作为初始密码查询自己的设备令牌。服务器仅保存密码派生哈希和设备令牌哈希，
  不保存明文。

新增 Plus 账号、批量导入员工令牌、代理访问和额度页面的完整说明见包内：
- CODEX_PLUS_SHARE.zh-CN.md
- SERVER_ACCOUNT_MANAGEMENT.zh-CN.md

升级：
重新执行 ./install-server.sh。安装器会更新程序和 systemd 服务文件，但不会覆盖现有
/etc/cws-codex/broker.env、auth.json、设备令牌注册表或用量数据。

租约均衡：
- 默认每个设备最多连续使用同一账号 24 小时，之后重新参与调度。
- 可在 /etc/cws-codex/broker.env 中通过 CWS_CODEX_LEASE_ROTATION_SECONDS 调整，默认 86400。
- 同等额度可用性下优先选择活动租约较少的账号，再比较额度重置时间和剩余额度。
- Broker 每 15 分钟探测一次每个账号的 Codex 文本模型；某模型“容量超限”后默认冷却 30 分钟。
  新租约会避开所有已知可用模型均被阻塞的账号；探测未知时不会排除账号。

模型健康探测配置（写入 /etc/cws-codex/broker.env 后重启服务）：
- CWS_CODEX_MODEL_PROBE_INTERVAL_SECONDS：探测间隔，默认 900（15 分钟）。
- CWS_CODEX_MODEL_COOLDOWN_SECONDS：容量超限后的冷却时间，默认 1800（30 分钟）。
- CWS_CODEX_MODEL_PROBE_CONCURRENCY：同时探测的账号数，默认 2。
- CWS_CODEX_MODEL_PROBE_TIMEOUT_SECONDS：单次模型探测超时，默认 45 秒。
- CWS_CODEX_MODEL_FALLBACKS：模型发现失败时逗号分隔的回退模型列表。
- CWS_CODEX_MODEL_HEALTH_FILE：仅保存非敏感探测摘要的状态文件，默认 /var/lib/cws-codex/model-health.json。

额度页面的模型健康状态含义：模型可用、容量超限、额度耗尽、鉴权失败、不支持、探测异常、探测未知。
其中容量超限会在冷却结束后重新探测；额度耗尽和鉴权失败会阻止调度；探测异常或未知不自动排除账号。
