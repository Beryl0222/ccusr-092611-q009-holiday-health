# 假期健康提醒编排

面向公共卫生团队的假期/旅行健康提醒编排服务，负责保存旅行计划、成员
授权、目的地风险窗口、预防措施与症状报告，并以**可注入的当前时间**
驱动提醒与超时升级。核心写入使用 SQLite 事务，服务接口不依赖页面，
便于值班人员在现场或后台系统中核对状态与历史。

## 目录

- `src/holiday_health/domain.py`：领域对象、时间约定与纯派生规则
  （风险分级、措施期望、症状分诊）。
- `src/holiday_health/travel.py`：旅行健康编排服务（事务、幂等、
  措施同步、提醒/升级扫描、症状报告）。
- `src/holiday_health/service.py`：旧版通用记录服务（保持兼容）。
- `src/holiday_health/api.py`：本地 HTTP 接口。
- `tests/`：时区、状态、版本、权限、幂等、重启恢复与分诊测试。

## 关键约定

- **时间锚定**：内部时刻一律为时区感知 UTC；对家庭成员的日期锚定
  IANA 时区（行程中按目的地、返程后按居家地）。回程后监测期为
  **一个自然月**（如 1/31 → 2/28），不是固定 30 天。
- **措施分类**：洗手、疫苗、防蚊为通用建议；儿童“暂缓集体活动”
  （返程后 14 个自然日）与成人“健康自评”（持续一个自然月）分别
  派生、分别记录（`advice_class`）。
- **风险变化只重算受影响窗口**：风险窗口按 `window_id` 收敛重算，
  其他窗口的措施完全不触碰；返程监测类常规建议因依赖“是否暴露”
  的聚合判断会联动重算。重叠窗口共享同一天的物理措施（唯一槽位），
  窗口删除时改挂到仍需要该措施的其他窗口。
- **完成即静默**：完成记录按「成员+措施+日期+疾病」去重；已完成
  措施在任何重复同步后都不复活、不重复提醒。
- **撤销/取消/重启可解释、可续办**：授权撤销冻结待办（`held`）并
  撤回未发提醒；计划取消作废未发提醒、停止新升级，但**历史提醒与
  open 升级全部保留**，值班人员仍可解释并显式办结。服务重启后
  提醒与升级完全由持久化状态扫描重建，幂等不重复。
- **症状就医路径优先**：疫区暴露者在行程中或返程监测期内报告症状
  时，发热门诊、旅行史申报、检疫热线等就医路径整体排在普通提示
  之前；红旗症状/蚊媒发热直接给出急诊指令并立即生成 open 升级。

## HTTP 接口（摘要）

写接口均需在 JSON 体中提供 `request_key`（重复提交幂等）与
`operator_id`。

- `POST /plans` / `POST /plans/{id}/activate` / `POST /plans/{id}/cancel`
- `PUT /plans/{id}/members/{mid}`（`category`、`consent_status`）
- `PUT|DELETE /plans/{id}/risk-windows/{wid}`
- `POST /plans/{id}/sync`（可选 `only_window`）
- `POST /plans/{id}/members/{mid}/complete`、`.../symptoms`
- `GET /plans/{id}/members|risk-windows|measures|reminders|symptoms|events`
- `POST /reminders/due?send=1`、`POST /escalations/scan`、
  `GET /escalations`、`POST /escalations/{id}/resolve`

`serve(host, port, database=":memory:")` 默认使用内存库；把
`database` 指向文件（如 `"travel_health.db"`）时，进程重启后可
完整恢复计划、提醒与升级。

## 测试

    PYTHONPATH=src python3 -m unittest discover -s tests

## 编译检查

    python3 -m compileall -q src tests
