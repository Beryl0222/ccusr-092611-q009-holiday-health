# 假期健康提醒编排

面向公共卫生团队的旅行健康提醒编排服务：保存旅行计划、家庭成员授权、预防措施
（洗手、疫苗、防蚊、回程症状监测）与症状报告，按可注入的当前时间生成提醒，并在
疫区返回者出现症状时把就医路径置于普通提示之前。

## 核心保证

- **幂等重复同步**：所有写操作接受 `request_key`；已完成措施为终态，重复同步
  （同键重放或换键再扫）不再产生修订、事件或提醒；风险窗口以相同等级重复登记
  不提升版本。
- **风险增量重算**：每个措施窗口带“覆盖日期 × 风险版本”摘要，只有受影响窗口
  在风险变化时产生新修订；洗手、日常监测、成人建议等与风险无关的窗口不重算；
  风险下调时门槛措施（疫苗/防蚊/儿童暂缓）未完成条目自动作废。
- **儿童与成人分开记录**：儿童的“暂缓集体活动”（`child_group_hold`）与成人的
  “回程健康建议”（`adult_advisory`）是不同 kind/audience 的独立措施，互不影响。
- **跨时区与监测窗口**：出发/回程按目的地 IANA 时区的当地日期锚定，提醒到期时刻
  换算为 UTC；回程后监测固定一个自然月（日历进位，如 1/31 + 1 月 = 2/28），
  早段（第 1–14 天）与晚段（第 15 天至满月前一天）不重不漏。
- **可解释、可恢复**：所有状态变化追加到只增的事件表；权限撤销、计划取消或服务
  重启后，值班人员可通过历史事件与 `/handoff` 交接视图解释历史提醒、处置待处理
  升级、继续完成未竟事项。症状升级独立于措施生命周期，取消计划或撤销授权都不
  清除临床升级。
- **就医优先**：疫区（行程内 moderate+，回程后继承行程末日暴露）返回者出现
  发热、腹泻、皮疹、黄疸等预警症状时立即开立 `urgent_care` 升级与分步就医路径；
  待办视图把该成员的提醒整体排在普通提示之前。

## 目录

- src/holiday_health/domain.py：时钟、时区/日期约定、风险画像与措施模板（纯函数）。
- src/holiday_health/service.py：SQLite 事务、事件表、幂等键、授权、增量重算、
  提醒投递、症状升级与值班交接。
- src/holiday_health/api.py：标准库 HTTP 边界（写接口均支持 `request_key`）。
- tests/：幂等、增量重算、年龄分流、时区窗口、权限/取消、重启恢复、就医优先、
  HTTP 端到端测试。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests

## 运行 HTTP 服务

    HOLIDAY_HEALTH_DB=holiday_health.db PYTHONPATH=src \
      python3 -m holiday_health.api

主要接口（JSON）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/plans` | 创建计划（owner、目的地、IANA 时区、当地出发/回程日期） |
| POST | `/plans/{id}/dates` `/cancel` `/members` `/sync` | 改期、取消、加成员、同步措施 |
| POST | `/members/{id}/consent` `/consent/revoke` | 授予/撤销成员授权 |
| POST | `/risk/{目的地}` | 登记/变更风险窗口（等级变化才升版本） |
| POST | `/measures/{uid}/complete` | 完成措施（重复完成幂等） |
| POST | `/reminders/generate` | 按当前时间投递到期提醒（每修订仅一次） |
| GET  | `/reminders/pending` | 待办视图（就医相关成员置顶） |
| POST | `/members/{id}/symptoms` | 症状报告；疫区+预警症状返回就医路径与升级 ID |
| POST | `/escalations/{id}/ack` `/resolve` | 升级确认与结案 |
| GET  | `/events` `/handoff` `/escalations` | 历史解释与值班交接 |
