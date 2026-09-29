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
- `GET /api/items/{id}/blockers`，查看事件关闭阻塞原因
- `GET /api/items/{id}/segments` / `POST`，岸线段列表/登记（段编号、位置、敏感等级）
- `POST /api/items/{id}/segments/{sid}/claim`，队伍领段占用，必须提交`expected_version`
- `POST /api/items/{id}/segments/{sid}/complete`，完成并记录实测`film_thickness_um`和`cleaned_quantity`
- `POST /api/items/{id}/segments/{sid}/review`，复核`{"passed": true|false}`
- `POST /api/items/{id}/segments/{sid}/reoil`，涨潮返油登记
- `GET /api/items/{id}/segments/{sid}`，含历次作业台账`jobs`
- `GET /api/audit`

允许角色：observer, response_commander, operations, viewer。估算油量、海况和未完成任务数影响响应等级；关闭前必须完成回收和岸线监测记录。

## 岸线段台账规则

- 按事件登记岸线段，敏感等级为 low/moderate/high/critical，对应残余油膜厚度阈值 100/50/20/5 微米。
- 段状态：`open`待领取 → `claimed`清理中 → `completed`已完成；完成时资料不全或实测油膜超阈值则进入`reinspection`待复检；`reoil`登记后段变为`reoiled`，原完成作业标记失效，须重新领段清理。
- 同一段同一时间只允许一项未结束作业（乐观版本 + 段作业唯一索引双重保证，两个队并发领取只有一个成功）。
- 领段占用、完成、复核、返油重开全部写入 SHA-256 审计链。
- 存在未领取/清理中/待复检/复油段时，事件不能关闭，原因可在`/blockers`查看。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
