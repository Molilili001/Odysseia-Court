# 常态通过名单 API 部署手册

这份手册用于把“常态通过名单只读 API”部署到 VPS，并通过 Cloudflare Tunnel 暴露给其他 Bot 调用。

## 0. 先上传哪些文件

如果你不是整仓库 `git pull`，而是手动上传文件，至少上传这些文件：

```text
court_bot/api.py
court_bot/config.py
court_bot/bot.py
requirements.txt
```

推荐一起上传这些文档/示例文件，方便以后维护：

```text
.env.example
README.md
APPROVED_API_DEPLOY.md
```

测试文件只在 VPS 上要跑测试时才需要上传：

```text
tests/test_approved_api.py
```

如果你用 `git pull` 部署，直接同步整个仓库即可，不需要逐个挑文件。

注意：`court_bot/bot.py` 是必传文件，因为 API 的同启同关接入点在这里。当前工作树里这个文件还包含你此前已有的议诉频道标题/topic 相关改动；如果你的 VPS 代码没有这些改动，手动覆盖前要确认这部分差异也是你想部署的内容。

## 1. VPS 前置检查

进入项目目录：

```bash
cd /path/to/your/bot
```

确认当前 systemd 服务名：

```bash
systemctl list-units --type=service | grep -i bot
```

下面示例假设服务名是：

```text
court-bot.service
```

如果你的服务名不同，把后续命令里的 `court-bot` 换成你的服务名。

## 2. 同步代码

方式 A：如果 VPS 上是 git 仓库：

```bash
git pull
```

方式 B：如果手动上传：

1. 先停 Bot：

```bash
sudo systemctl stop court-bot
```

2. 上传第 0 节列出的必需文件到对应路径。

3. 确认文件存在：

```bash
ls -l court_bot/api.py court_bot/config.py court_bot/bot.py requirements.txt
```

## 3. 更新依赖

如果你使用虚拟环境，先激活：

```bash
source .venv/bin/activate
```

安装依赖：

```bash
pip install -r requirements.txt
```

这一步会确保 `aiohttp` 可用。

## 4. 生成 API Token

在 VPS 上生成一个长随机 token：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

复制输出结果，后面写入 `.env`。

建议每个外部 Bot 使用不同 token。第一版也可以先只放一个 token。

## 5. 修改 `.env`

打开 `.env`：

```bash
nano .env
```

加入或修改以下配置：

```env
APPROVED_API_ENABLED=true
APPROVED_API_HOST=127.0.0.1
APPROVED_API_PORT=8787
APPROVED_API_TOKENS=把这里替换成第4步生成的token
APPROVED_API_MAX_LIMIT=500
```

关键点：

- `APPROVED_API_HOST` 保持 `127.0.0.1`。
- 不要改成 `0.0.0.0`。
- `APPROVED_API_TOKENS` 不能为空，否则 Bot 会拒绝启动。

保存后可以检查配置是否写入：

```bash
grep APPROVED_API .env
```

不要把真实 token 发到公开聊天、GitHub、截图或日志里。

## 6. 重启 Bot

```bash
sudo systemctl restart court-bot
```

查看状态：

```bash
sudo systemctl status court-bot --no-pager
```

查看最近日志：

```bash
journalctl -u court-bot -n 100 --no-pager
```

正常时应看到类似日志：

```text
Approved API listening on http://127.0.0.1:8787
```

如果看到：

```text
APPROVED_API_ENABLED=true 时必须设置 APPROVED_API_TOKENS
```

说明 `.env` 里启用了 API，但没有填 token。

## 7. 在 VPS 本机测试 API

健康检查：

```bash
curl http://127.0.0.1:8787/healthz
```

预期返回：

```json
{"ok":true,"service":"approved-api"}
```

测试未带 token 的名单接口：

```bash
curl -i "http://127.0.0.1:8787/v1/continuous/approved?guild_id=你的服务器ID"
```

预期返回 `401 Unauthorized`。

测试带 token 的名单接口：

```bash
curl "http://127.0.0.1:8787/v1/continuous/approved?guild_id=你的服务器ID" \
  -H "Authorization: Bearer 你的token"
```

预期返回类似：

```json
{
  "ok": true,
  "guild_id": "你的服务器ID",
  "config_id": null,
  "field_name": null,
  "limit": 100,
  "count": 0,
  "items": []
}
```

`count` 为 `0` 不一定是错误，只表示当前筛选条件下没有通过名单。

如果你知道常态配置 ID，可以测试：

```bash
curl "http://127.0.0.1:8787/v1/continuous/approved?guild_id=你的服务器ID&config_id=配置ID" \
  -H "Authorization: Bearer 你的token"
```

按岗位过滤：

```bash
curl --get "http://127.0.0.1:8787/v1/continuous/approved" \
  --data-urlencode "guild_id=你的服务器ID" \
  --data-urlencode "config_id=配置ID" \
  --data-urlencode "field_name=管理组" \
  -H "Authorization: Bearer 你的token"
```

## 8. 配置 Cloudflare Tunnel

进入 Cloudflare Zero Trust 控制台：

```text
Zero Trust -> Networks -> Tunnels -> 选择你的 Tunnel -> Public Hostname
```

新增 Public Hostname：

```text
Subdomain: approved-api
Domain: 你的域名
Type: HTTP
URL: 127.0.0.1:8787
```

保存后，外部访问地址类似：

```text
https://approved-api.你的域名
```

Cloudflare Tunnel 指向的是 VPS 本机的 `127.0.0.1:8787`，所以 VPS 防火墙不需要开放 8787 端口。

## 9. 从外部测试

在另一台机器上执行：

```bash
curl "https://approved-api.你的域名/healthz"
```

再测试名单接口：

```bash
curl "https://approved-api.你的域名/v1/continuous/approved?guild_id=你的服务器ID" \
  -H "Authorization: Bearer 你的token"
```

如果健康检查通，但名单接口返回 401，说明 Tunnel 正常，应用层 token 没带对。

如果健康检查都不通，优先检查 Cloudflare Tunnel Public Hostname 是否指向 `http://127.0.0.1:8787`，以及 VPS 本机 `curl http://127.0.0.1:8787/healthz` 是否正常。

## 10. 可选：启用 Cloudflare Access Service Token

第一轮部署可以先不启用 Access，等 API 和 Tunnel 跑通后再加。

启用后，其他 Bot 请求时需要同时携带：

```http
CF-Access-Client-Id: xxx
CF-Access-Client-Secret: yyy
Authorization: Bearer 你的应用token
```

这样有两层保护：

1. Cloudflare Access 拦截非授权机器请求。
2. Bot API 自己校验 Bearer Token。

## 11. 其他 Bot 调用示例

Python 示例：

```python
import requests

url = "https://approved-api.你的域名/v1/continuous/approved"
headers = {
    "Authorization": "Bearer 你的token",
}
params = {
    "guild_id": "你的服务器ID",
    "config_id": "配置ID",
}

resp = requests.get(url, headers=headers, params=params, timeout=10)
resp.raise_for_status()
data = resp.json()

approved_user_ids = [item["user_id"] for item in data["items"]]
print(approved_user_ids)
```

如果启用了 Cloudflare Access：

```python
headers = {
    "CF-Access-Client-Id": "你的client id",
    "CF-Access-Client-Secret": "你的client secret",
    "Authorization": "Bearer 你的token",
}
```

## 12. 常见故障排查

### Bot 启动失败

查看日志：

```bash
journalctl -u court-bot -n 100 --no-pager
```

常见原因：

- `APPROVED_API_ENABLED=true` 但 `APPROVED_API_TOKENS` 为空。
- 端口 `8787` 被其他程序占用。
- 依赖没有更新，缺少 `aiohttp`。

检查端口占用：

```bash
ss -ltnp | grep 8787
```

### 本机 healthz 不通

确认 Bot 是否运行：

```bash
sudo systemctl status court-bot --no-pager
```

确认 `.env` 是否启用 API：

```bash
grep APPROVED_API .env
```

### 外部域名不通，但本机通

检查 Cloudflare Tunnel：

```bash
cloudflared tunnel list
```

如果你的 Tunnel 是 systemd 管理，也可以看：

```bash
sudo systemctl status cloudflared --no-pager
journalctl -u cloudflared -n 100 --no-pager
```

### 返回 401

说明请求到达 API 了，但 Bearer Token 错误或缺失。

确认 header 格式必须是：

```http
Authorization: Bearer 你的token
```

不是：

```http
Authorization: 你的token
```

也不是：

```http
token=你的token
```

### 返回空名单

空名单不一定是错误。检查：

- `guild_id` 是否是 Discord 服务器 ID。
- `config_id` 是否正确。
- `field_name` 是否和岗位名完全一致。
- 服务器里是否真的有状态为“已通过且未退出”的常态申请。

## 13. 回滚方式

如果部署后想临时关闭 API，不需要回滚代码。

把 `.env` 改成：

```env
APPROVED_API_ENABLED=false
```

然后重启：

```bash
sudo systemctl restart court-bot
```

这样 Discord Bot 继续运行，但不会启动 HTTP API。
