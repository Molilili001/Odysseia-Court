# 常态申请募选进度

## 2026-06-30
- 创建实现计划文件。
- 当前阶段：梳理现有接口并准备低嵌入实现。
- 已确认命令层、数据库 helper、时间/文本工具和现有持久 View 注册方式。
- 阶段 1 完成；开始新增常态申请独立模块文件。
- 已新增常态申请 constants、纯逻辑、独立数据库 repo、embeds、views 和 service。
- 运行测试时发现 Discord `app_commands.Group` 子命令上限为 25；直接添加 7 个 `/募选 常态...` 一级命令会超限。改为嵌套 `/募选 常态 ...` 子组。
- 已改为嵌套 `/募选 常态 ...` 子组；`python -m unittest tests.test_election_core` 通过 23 项。
- README 已补常态申请说明。
- 最终验证：`python -m unittest discover` 通过 24 项；`python -m compileall court_bot tests` 通过；显式实例化 `ElectionGroup` 确认为父级 24 个命令、`continuous` 子组 7 个命令。

## 2026-07-08
- 开始规划“常态通过名单内置 API”功能。
- 已确认用户希望 API 与 Discord Bot 同一个 systemd 服务同启同关。
- 已读取现有 Bot 生命周期：`setup_hook()` 适合启动 API，`close()` 适合关闭 API。
- 已确认常态通过名单有现成 repo 查询函数，可复用 `list_approved_applications()`。
- 已新增内置 `aiohttp` API 服务、配置项、Bot 生命周期接入、测试和 README 部署说明。
- 验证通过：`python -m unittest tests.test_approved_api tests.test_election_core`、`python -m unittest discover`（39 项）、`python -m compileall court_bot tests`。
- 已新增 `APPROVED_API_DEPLOY.md`，按手动上传、VPS 配置、Cloudflare Tunnel、外部 Bot 调用和故障排查拆成部署步骤。
