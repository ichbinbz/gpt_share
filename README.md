# CWS GPT Share

<div align="center">

面向团队的 ChatGPT Plus 与 Codex Plus 共享方案，提供账号池、额度感知调度、员工设备授权和用量统计。

[![Build](https://github.com/ichbinbz/gpt_share/actions/workflows/docker-image.yml/badge.svg)](https://github.com/ichbinbz/gpt_share/actions/workflows/docker-image.yml)
[![CodeQL](https://github.com/ichbinbz/gpt_share/actions/workflows/codeql.yml/badge.svg)](https://github.com/ichbinbz/gpt_share/actions/workflows/codeql.yml)
[![License](https://img.shields.io/github/license/ichbinbz/gpt_share)](LICENSE)

[English](README.en.md) · [Codex Plus 部署说明](CODEX_PLUS_SHARE.zh-CN.md) · [服务器账号管理](SERVER_ACCOUNT_MANAGEMENT.zh-CN.md)

</div>

> [!IMPORTANT]
> ChatGPT 共享当前使用已登录的真实 Chromium 页面作为传输层，不依赖原项目已经失效的直接 Web API 鉴权链路。部署方式和功能边界请阅读 [ChatGPT Plus 浏览器共享模式](BROWSER_TRANSPORT.zh-CN.md)。

## 当前版本

- Windows / Ubuntu 员工端：`v0.1.2`
- Codex Broker 服务端：`v0.1.3`

## 主要功能

- **ChatGPT 浏览器共享**：员工只登录 CWS，由服务端管理多个相互隔离的 ChatGPT 浏览器账号，支持负载均衡和会话账号亲和。
- **Codex Plus 账号池**：员工设备通过设备令牌换取短期 Codex 凭据，长期账号令牌只保存在服务端。
- **额度感知调度**：综合剩余额度、距离重置的时间和当前活跃租约选择账号；优先消耗即将重置且余量充足的账号。
- **账号状态可视化**：管理后台显示账号真实邮箱、别名、主/次额度窗口、重置时间、健康状态和调度使用分。
- **跨平台员工端**：提供 Windows 图形安装器和 Ubuntu/Linux 客户端，支持自动同步、用量上报、诊断和卸载。
- **复用原生 VS Code 体验**：不创建独立用户配置目录，不改变主题、扩展、设置、历史目录和窗口恢复行为，仅通过 `CODEX_HOME` 切换公司 Codex 登录凭据。

## 发布包

构建产物位于 [`dist`](dist) 目录：

| 文件 | 用途 |
| --- | --- |
| `CWS-Codex-Release-v0.1.2.zip` | Windows 推荐分发包，包含图形安装器和简明使用说明 |
| `CWS-Codex-Setup-v0.1.2.exe` | Windows 员工端安装器 |
| `CWS-Codex-Windows-v0.1.2.zip` | Windows 脚本版及故障排查备用包 |
| `CWS-Codex-Linux-v0.1.2.tar.gz` | Ubuntu/Linux 员工端 |
| `CWS-Codex-Server-v0.1.3.tar.gz` | Codex Broker 服务端部署包 |

Windows 员工通常只需解压 Release 包、双击安装器并填写管理员分配的设备令牌。Linux 员工端解压后以普通用户运行 `./install.sh`。

## 部署概览

1. 在服务器部署 CWS 和 Codex Broker，并录入可用账号。
2. 保持 Broker 监听 `127.0.0.1:8765`，通过现有的 VLESS/REALITY 或其他受控隧道转发员工请求。
3. 将内部域名 `codex.cws.internal` 在服务器侧解析到 `127.0.0.1`，并配置客户端代理将该域名送入隧道。
4. 为员工创建或分配设备令牌，并分发对应平台的客户端。
5. 员工首次启动前完全退出所有 VS Code 窗口，再通过“公司 Codex（VS Code）”启动。第一个窗口加载凭据后，后续窗口可按平时方式打开。

> [!WARNING]
> `8765` 是 Broker 的内部鉴权与凭据分发端口，不要直接开放到公网。仅修改员工电脑的 `hosts` 不能让远端服务器的回环服务自动可达，域名解析和代理/隧道路由需要配套配置。

完整的服务端环境变量、账号导入、员工端安装和代理示例见 [CWS 官方 Codex Plus 共享方案](CODEX_PLUS_SHARE.zh-CN.md)。

## 开发与验证

后端与打包测试：

```bash
pytest -q
```

前端构建：

```bash
cd frontend
pnpm install
pnpm build
```

## 文档

- [CWS 官方 Codex Plus 共享方案](CODEX_PLUS_SHARE.zh-CN.md)
- [ChatGPT Plus 浏览器共享模式](BROWSER_TRANSPORT.zh-CN.md)
- [服务器账号管理使用说明](SERVER_ACCOUNT_MANAGEMENT.zh-CN.md)
- [项目特点](https://cws-docs.pages.dev/zh/)
- [快速部署指南](https://cws-docs.pages.dev/zh/guide/quick-start.html)
- [演示截图](https://cws-docs.pages.dev/zh/demo/screenshots.html)

## 声明

本项目仅供学习和研究使用，不鼓励用于商业用途。使用本项目可能违反相关服务的用户协议，请在部署前自行了解并承担相应风险。项目维护者不对使用本项目造成的损失负责。
