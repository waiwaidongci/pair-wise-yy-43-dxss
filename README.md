# 溢油应急响应与任务追踪

围控、回收、岸线保护和废弃物处置任务，按证据和监测结果闭环。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8320
```

默认端口为`8320`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/segments`
- `POST /api/items/{id}/segments`，按事件登记岸线段（段名、敏感等级）
- `POST /api/items/{id}/segments/{seg_id}/claim`，领段占用；同一段已有未结束作业时返回 409
- `POST /api/items/{id}/segments/{seg_id}/complete`，完成并记录实测油膜厚度与清理量；油膜超阈值或资料不全则转待复检
- `POST /api/items/{id}/segments/{seg_id}/reinspect`，复核（pass/fail）
- `POST /api/items/{id}/segments/{seg_id}/reoil`，返油重开，原完成失效
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录，且不存在未完成或复油岸线段。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
