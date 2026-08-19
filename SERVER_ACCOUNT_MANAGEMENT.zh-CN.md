# CWS Codex 服务器账号管理使用说明

本文用于在现有 CWS Codex Broker 中新增、检查、重新授权或停用 ChatGPT 订阅账号。

## 一、当前目录约定

首个账号使用：

```text
/var/lib/cws-codex/auth.json
```

后续账号每个使用一个独立目录：

```text
/var/lib/cws-codex/accounts/account-02/auth.json
/var/lib/cws-codex/accounts/account-03/auth.json
/var/lib/cws-codex/accounts/account-04/auth.json
```

新增账号必须使用新的编号和独立 `CODEX_HOME`。不要在已有账号目录中登录另一个账号，
否则会覆盖该目录原来的登录凭据。

## 二、新增账号

以下示例添加 `account-03`。添加后续账号时，将所有 `account-03` 替换为新的连续编号。

### 1. 准备新账号

在准备添加的 ChatGPT 账号中打开安全设置，启用 Codex 设备代码授权。建议在后续浏览器
授权时使用无痕窗口，避免误用浏览器中已经登录的其他 ChatGPT 账号。

OpenAI 官方说明：无头或远程服务器可以运行 `codex login --device-auth`，然后在浏览器中
打开官方地址并输入一次性代码：

- <https://learn.chatgpt.com/docs/auth#login-on-headless-devices>

### 2. 登录服务器

```bash
ssh root@173.242.116.11
```

### 3. 创建独立账号目录

```bash
install -d -m 700 /var/lib/cws-codex/accounts/account-03
```

### 4. 发起官方设备代码登录

```bash
CODEX_HOME=/var/lib/cws-codex/accounts/account-03 \
codex login --device-auth
```

终端会显示 OpenAI 官方地址和一次性设备代码：

1. 在管理员自己的电脑上打开终端显示的官方地址。
2. 确认登录的是准备添加的新 ChatGPT 账号。
3. 输入一次性代码并确认授权。
4. 等待服务器终端显示 `Successfully logged in`。

设备代码只用于这一次官方登录，不是员工端使用的 CWS 设备令牌，不要将它发送给员工。

### 5. 检查登录状态

```bash
CODEX_HOME=/var/lib/cws-codex/accounts/account-03 \
codex login status
```

正常结果应包含：

```text
Logged in using ChatGPT
```

确认鉴权文件已经生成：

```bash
test -s /var/lib/cws-codex/accounts/account-03/auth.json \
  && echo "account-03 auth.json exists"
```

不要打印或复制 `auth.json` 内容。该文件包含可刷新登录凭据，应按密码管理。

### 6. 设置文件权限

```bash
chown -R root:root /var/lib/cws-codex/accounts/account-03
chmod 700 /var/lib/cws-codex/accounts/account-03
chmod 600 /var/lib/cws-codex/accounts/account-03/auth.json
```

### 7. 重启 Broker

Broker 在启动时扫描账号目录，因此新增账号后必须重启服务：

```bash
systemctl restart cws-codex-broker.service
sleep 2
systemctl is-active cws-codex-broker.service
curl -fsS http://127.0.0.1:8765/healthz
```

如果原来是两个账号，成功添加后应看到：

```json
{"ok":true,"accounts":3}
```

## 三、检查账号额度与可用状态

### 方式一：直接打开只读额度网页

员工端或管理员电脑先连接公司内网代理/VLESS，然后打开：

```text
http://codex.cws.internal:8765/quota
```

输入服务器 `/etc/cws-codex/broker.env` 中配置的 `CWS_CODEX_ADMIN_TOKEN` 后即可查询。令牌只保存在
当前浏览器标签页的 `sessionStorage` 中；关闭标签页后清除。页面每 30 秒自动更新，也可以点击
“刷新数据”立即绕过 Broker 的额度缓存。页面下半部分显示按员工设备令牌 ID/标签归属的 Token
使用量、会话数和最后上报时间；超过 15 分钟未上报显示“数据过期”，被吊销的令牌显示“已吊销”。
新版员工端还显示本机局域网 IP 和 Broker 看到的代理源 IP。

该地址只能通过现有内网代理/VLESS 访问，Broker 仍只监听服务器 `127.0.0.1:8765`，不要将 8765
开放到公网。

### 方式二：CWS 管理后台

新版 CWS 管理后台的“系统管理”页面包含“Codex 官方账号额度”和“Codex 员工 Token 使用量”卡片。
CWS 后端需要配置：

```bash
CWS_CODEX_BROKER_URL=http://127.0.0.1:8765
CWS_CODEX_ADMIN_TOKEN=与Broker相同的管理令牌
```

如果 CWS 后端运行在 Docker 容器中，必须确保容器能够访问宿主机 Broker；在当前 Broker 仅绑定
回环地址的配置下，建议让 CWS 容器使用 host 网络，而不是把 8765 暴露到公网。管理员网页只接收
经过归一化的额度摘要，不接收 Broker 管理令牌或任何 OAuth 凭据。

员工端向 Broker 的 `POST /v1/usage/report` 上报绝对累计值，Broker 管理接口
`GET /v1/admin/device-usage` 返回按设备令牌汇总的只读结果。归属身份取自 Bearer 设备令牌在服务器
注册表中的 ID 和标签，客户端提交的电脑名不能改变统计归属。服务器用同一会话各字段的最大值更新，
因此定时重复上报是幂等的。

批量员工令牌从签发清单导入时，服务器注册表仍只保存 SHA-256 哈希和员工元数据。一次性明文分发
表必须保存为 `0600`、只允许 root 读取，分发完成后应删除或转移到公司密码管理系统。示例：

```bash
python /opt/cws-codex/import_employee_tokens_xlsx.py \
  /root/员工令牌配置表.xlsx \
  --distribution /root/cws-codex-employee-tokens.csv
```

### 方式三：服务器命令行

在服务器执行：

```bash
set -a
. /etc/cws-codex/broker.env
set +a

curl -fsS \
  -H "Authorization: Bearer ${CWS_CODEX_ADMIN_TOKEN}" \
  http://127.0.0.1:8765/v1/admin/accounts \
  | python3 -m json.tool

unset CWS_CODEX_ADMIN_TOKEN
```

重点检查每个账号的以下字段：

- `alias`：服务器账号目录名称。
- `available`：当前是否能刷新凭据并查询官方额度。
- `plan_type`：登录令牌中识别的订阅类型。
- `usage_score`：调度器用于负载均衡的当前使用分数。
- `usage`：官方额度窗口返回的数据。

新账号会自动参加后续的新租约分配。已经运行的员工会话保持原账号亲和，不会因为新增账号
立即跳转；重新获取租约或原租约到期后，才会重新参加负载选择。

## 四、账号需要重新登录

如果状态接口显示账号不可用、刷新令牌失效或管理员主动退出了该账号，应在原目录重新登录：

```bash
CODEX_HOME=/var/lib/cws-codex/accounts/account-03 \
codex login --device-auth

chmod 600 /var/lib/cws-codex/accounts/account-03/auth.json
systemctl restart cws-codex-broker.service
```

必须继续使用原来的 `account-03` 目录，这样调度器中的账号别名不会变化。

## 五、临时停用或恢复账号

需要立即停止给某个账号分配新租约时，可将其鉴权文件改名并重启 Broker：

```bash
mv /var/lib/cws-codex/accounts/account-03/auth.json \
   /var/lib/cws-codex/accounts/account-03/auth.json.disabled
systemctl restart cws-codex-broker.service
```

恢复账号：

```bash
mv /var/lib/cws-codex/accounts/account-03/auth.json.disabled \
   /var/lib/cws-codex/accounts/account-03/auth.json
chmod 600 /var/lib/cws-codex/accounts/account-03/auth.json
systemctl restart cws-codex-broker.service
```

停用只影响后续凭据签发。员工电脑上已经取得的官方短期访问令牌在自身过期前仍可能继续有效。

## 六、常见问题

### `codex login --device-auth` 不显示代码

确认目标 ChatGPT 账号已经在安全设置中启用设备代码授权，并检查服务器能否通过现有网络访问
OpenAI/ChatGPT 官方站点。

### 登录成功但账号数量没有增加

依次检查：

```bash
test -s /var/lib/cws-codex/accounts/account-03/auth.json
systemctl restart cws-codex-broker.service
systemctl --no-pager --full status cws-codex-broker.service
journalctl -u cws-codex-broker.service -n 50 --no-pager
```

### 新账号没有立刻分配给员工

这是会话账号亲和的正常表现。新增账号只参与新租约选择；不建议直接删除服务器租约状态文件。

## 七、操作检查表

- [ ] 新账号已启用 Codex 设备代码授权。
- [ ] 使用了新的独立 `account-XX` 目录。
- [ ] `codex login status` 显示使用 ChatGPT 登录。
- [ ] 目录权限为 `0700`，`auth.json` 权限为 `0600`。
- [ ] Broker 已重启且状态为 `active`。
- [ ] `/healthz` 中账号数量正确。
- [ ] 管理接口中新增账号 `available` 为 `true`。
- [ ] 没有打印、复制或分发服务器 `auth.json`。
