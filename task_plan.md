# 常态申请募选实现计划

## 目标
为现有 `/募选` 模块新增低嵌入、可复用的常态申请投票功能：
- 长期入口，申请人选择一个岗位并填写报名宣言。
- 按申请身份组限制提交，按投票身份组限制投票/改票。
- 每个申请在投票频道生成一体式消息，投票期间不显示实时票数。
- 到期或手动结束后按最低总票数和同意比例结算。
- 未通过、进行中退出、通过后退出均触发冷却；通过名单可公开按岗位查询。
- 结果动态发布到公示频道。

## 阶段
1. [complete] 梳理现有募选模块接口、数据层和 slash command 约束。
2. [complete] 新增常态申请独立数据层、embeds、views、service。
3. [complete] 接入 `/募选 常态 ...` 命令与 scheduler 自动结算。
4. [complete] 补测试和 README，运行验证。

## 约束
- 保留现有未提交改动，不回退用户已有变更。
- 新功能尽量独立文件承载，减少对现有统一募选流程的侵入。
- 使用现有时间解析、身份组解析、文本清洗、审计/权限风格。

## 错误记录
| 错误 | 尝试次数 | 解决方案 |
|------|---------|----------|
| `groups cannot have more than 25 commands` | 1 | 将常态申请命令从一级 `/募选 常态创建` 等改为嵌套 `/募选 常态 创建/入口/...` 子组。 |

---

# 常态通过名单内置 API 计划

## 目标
在现有 Discord Bot 进程内启动一个只读 HTTP API 子服务，用于跨 VPS 的其他 Bot 通过 Cloudflare Tunnel 安全获取常态申请通过名单：
- 与 Bot 同进程、同 systemd 服务启停。
- 仅监听 `127.0.0.1`，由 Cloudflare Tunnel 暴露域名。
- 提供健康检查和通过名单 JSON 查询。
- 使用 Bearer Token 做应用层鉴权，可叠加 Cloudflare Access Service Token。
- 不改变现有 Discord 指令、投票、结算和通过名单写入逻辑。

## 阶段
1. [complete] 梳理 Bot 生命周期、配置加载和常态名单查询接口。
2. [complete] 新增 API 配置项和内置 aiohttp 服务模块。
3. [complete] 在 `CourtBot.setup_hook()` / `close()` 接入 API 启停。
4. [complete] 补充测试，覆盖鉴权、过滤和 JSON 输出。
5. [complete] 更新 `.env.example` 与 README，写明 systemd + Cloudflare Tunnel 部署方式。

## 约束
- 保持单进程部署，避免新增第二个 systemd service。
- API 默认关闭，必须显式设置环境变量启用。
- API 默认只绑定 `127.0.0.1`，避免绕过 Cloudflare 直接暴露。
- 输出 Discord snowflake ID 使用字符串，避免其他语言整数精度丢失。
- 查询只读、短事务、有限制分页，避免影响 Bot 主事件循环。
- 不把 Cloudflare Access 校验写进应用代码；Access 在 Cloudflare 边缘处理，应用只校验自己的 Bearer Token。

## 成功标准
- 未启用 API 时，现有 Bot 行为不变。
- 启用 API 时，`/healthz` 返回 JSON 健康状态。
- 有效 Bearer Token 可以 GET 常态通过名单。
- 缺失或错误 Bearer Token 返回 401。
- `guild_id` 必填，`config_id`、`field_name`、`limit` 可选。
- 测试和编译检查通过。

## 验证记录
- `python -m unittest tests.test_approved_api tests.test_election_core`：通过 37 项。
- `python -m unittest discover`：通过 39 项。
- `python -m compileall court_bot tests`：通过。
