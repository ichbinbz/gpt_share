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

新增 Plus 账号、批量导入员工令牌、代理访问和额度页面的完整说明见包内：
- CODEX_PLUS_SHARE.zh-CN.md
- SERVER_ACCOUNT_MANAGEMENT.zh-CN.md

升级：
重新执行 ./install-server.sh。安装器会更新程序和 systemd 服务文件，但不会覆盖现有
/etc/cws-codex/broker.env、auth.json、设备令牌注册表或用量数据。
