CWS Codex Ubuntu/Linux 员工端 v0.1.2
=====================================

系统要求：
- Ubuntu 22.04/24.04 或其他带 Python 3.10+ 的桌面 Linux
- Visual Studio Code（code 命令、Deb 包或 Snap 版均可）
- systemd 用户服务（建议，用于每 5 分钟自动同步）

安装：
1. 解压 CWS-Codex-Linux-v0.1.2.tar.gz。
2. 打开终端进入解压后的目录，不要使用 sudo。
3. 执行：

   ./install.sh

4. 输入管理员分配给本机的 cwsdt_ 设备令牌。
5. 完全退出已有 VS Code，再从应用程序菜单或桌面打开“公司 Codex（VS Code）”。

默认代理为 http://192.168.2.38:7897。使用本机 7897 代理安装：

   ./install.sh --proxy-url http://127.0.0.1:7897

常用命令：
- 诊断：cws-codex diagnose
- 同步凭据：cws-codex sync
- 重新分配账号：cws-codex sync --new-lease
- 修改代理：cws-codex configure-proxy
- 启动 VS Code：cws-codex launch
- 卸载：~/.local/share/cws-codex/uninstall.sh

行为说明：
- 启动 VS Code 时不传额外窗口或用户目录参数，复用用户原有设置、扩展和历史目录。
- 仅通过 CODEX_HOME=~/.cws-codex 切换公司 Codex 登录凭据。
- 设备令牌保存在 ~/.local/share/cws-codex/device-token，目录权限 700、文件权限 600。
- systemd 用户定时器每 5 分钟同步凭据和上报散列后的会话 Token 累计值。
- 不上传对话、提示词、代码或文件内容。

如果应用菜单未立即刷新，可注销后重新登录，或直接运行 cws-codex launch。
