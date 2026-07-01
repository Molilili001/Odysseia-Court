# 常态申请募选发现记录

## 已确认需求
- 一个服务器可有多个常态配置，每个配置独立岗位、冷却、通过名单。
- 一次申请一个岗位。
- 申请只填报名宣言，但投票/公示中展示 Discord 自动信息。
- 投票允许改票，结算只按最新投票。
- 申请和投票身份组分别配置；申请提交时检查，投票/改票时检查。
- 投票期间不显示实时票数。
- 公示频道发布通过、未通过、退出等结果动态。
- 通过名单默认所有人可查，可按岗位过滤。
- 进行中退出和通过后退出都触发冷却。

## 代码现状
- 现有 `/募选` 是周期性多岗位统一投票系统，表前缀为 `pe_`。
- 现有 `ElectionRepo` 在 `court_bot/election/database.py`，可新增表和 repo 方法。
- 现有 `ElectionScheduler` 每 60 秒 tick，可额外处理常态申请到期。
- 现有 `RegistrationEntryView`、`VoteEntryView` 已注册为持久 View，新常态 View 也应在 cog load 注册。
- 仓库中没有二级 `app_commands.Group` 示例；为降低兼容风险，常态命令先作为 `/募选 常态创建`、`/募选 常态入口` 这类一级子命令实现。
- 数据库封装支持 `execute_close`、`fetchone`、`fetchall` 和直接使用 `db.conn` 事务；独立 repo 可复用同一连接和自己的 lock。
