# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、评分基准快照与待生效修订、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 评分基准快照

开标后评分口径不再允许临时改写，核心规则：

- **基准确认**：`POST /api/baselines/confirm`（仅监督员），开标后冻结当前评分项/权重/分值为生效基准快照，记录确认人、确认时间和口径哈希。同一项目同一时刻只有一个生效基准（数据库部分唯一索引强制保证）。
- **待生效修订**：`POST /api/baselines/revisions`（仅采购员）对评分项、权重、分值的调整只形成 `pending` 修订，生效基准与既有排名不变；同一项目只允许一条待生效修订。
- **复核与版本冲突**：`POST /api/baselines/revisions/review`（仅监督员）批准时校验修订所基于的基准版本（`expected_version`）：通过则旧基准置 `superseded` 并生成新生效基准，旧基准永久保留；晚到修订（基准版本已变化）置 `blocked` 并保留阻断原因；驳回必须填写说明，记录复核人。
- **重启恢复**：服务启动时自动扫描 `pending` 修订并在时间线登记 `baseline.recovered`；悬挂在失效基准上的修订自动阻断，恢复后可继续复核。
- **旧评分按当时基准回读**：每条评分记录 `baseline_id`，排名按指定基准快照计算；基准更新后旧分仍可按旧基准查看，但授标要求全部有效投标在生效基准下完成评分（`stale_baseline_scores` 阻断，须按新基准重评）。
- **授标只接受生效基准**：未确认基准、存在待生效修订、评分未按生效基准完成、有未处理投诉等均会阻断授标，阻断原因结构化为 `award_blockers` 在 `GET /api/tenders/{id}` 详情中展示（含修订记录、复核人、阻断原因）。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/baselines/confirm`：监督员确认评分基准，冻结为生效快照
- `POST /api/baselines/revisions`：采购员提交评分项/权重/分值调整（待生效修订）
- `POST /api/baselines/revisions/review`：监督员批准/驳回复核，版本冲突会阻断
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：锁定评分轮次并保存排名快照

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评和角色权限。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
