> [!IMPORTANT]
> 本分支正在恢复 ChatGPT Plus 共享核心功能。默认使用已登录的真实 Chromium
> 页面作为传输层，不再依赖原项目已失效的直接 Web API 鉴权链路。部署与当前功能边界见
> [ChatGPT Plus 浏览器共享模式](BROWSER_TRANSPORT.zh-CN.md)。

当前分支支持公司终端只登录 CWS、多个独立 ChatGPT 浏览器账号自动负载均衡、会话账号亲和、管理后台账号健康和额度状态查询，以及按员工设备令牌归属的 Codex Token 使用量统计。

<h1 align="center">ChatGPT Web Share</h1>

<div align="center">

[![GitHub tag (latest by date)](https://img.shields.io/github/v/tag/chatpire/chatgpt-web-share?label=container&logo=docker)](https://github.com/chatpire/chatgpt-web-share/pkgs/container/chatgpt-web-share)
[![Github Workflow Status](https://img.shields.io/github/actions/workflow/status/chatpire/chatgpt-web-share/docker-image.yml?label=build)](https://github.com/chatpire/chatgpt-web-share/actions)
[![License](https://img.shields.io/github/license/chatpire/chatgpt-web-share)](https://github.com/chatpire/chatgpt-web-share/blob/main/LICENSE)

适用于个人、组织或团队的 ChatGPT 共享方案。共享一个 ChatGPT Plus 账号给多人使用，提供完善的管理和限制功能。

[English Readme](README.en.md)

</div>

## 文档

- [CWS Codex Plus 共享方案](CODEX_PLUS_SHARE.zh-CN.md)
- [服务器账号管理使用说明](SERVER_ACCOUNT_MANAGEMENT.zh-CN.md)
- GitHub Release 同时提供 Windows 员工端安装包和 Linux 服务端部署包。
- 项目特点：https://cws-docs.pages.dev/zh/
- 快速部署指南：http://cws-docs.pages.dev/zh/guide/quick-start.html
- 演示截图：http://cws-docs.pages.dev/zh/demo/screenshots.html

> [!IMPORTANT]
> 部署 CWS 需要海外 VPS。项目文档内推荐了一些高性价比的服务器，低至 $11/年。请移步：[VPS推荐](https://cws-docs.pages.dev/zh/support/vps.html)

## 声明

本项目仅供学习和研究使用，不鼓励用于商业用途。您应当知悉使用本项目可能会违反相关用户协议，并了解相关的风险。我们不对任何因使用本项目而导致的任何损失负责。
