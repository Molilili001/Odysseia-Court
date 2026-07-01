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
