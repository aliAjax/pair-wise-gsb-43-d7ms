# 公共采购密封投标与评审系统

标准库实现的招标发布、密封投标、开标校验收、评分基准快照与待生效修订、规则评分、利益冲突、澄清、废标、投诉重评和授标快照服务。

## 评分基准（快照与修订）

开标后评分口径不能被采购员临时改动而追溯影响已有评分：

- `POST /api/baselines/confirm`：**监督员确认**后生成不可变基准快照（版本、口径哈希、确认人）；确认前不能评分，同一项目只能有一个生效基准（部分唯一索引兜底）。
- `POST /api/baselines/revisions`：采购员调整评分项/权重/分值只生成 `pending` 待生效修订，携带其基于的基准版本（`expected_version`）；提交时若基准已前进则 409 版本冲突。
- `POST /api/baselines/revisions/review`：监督员复核，`approved` 时旧基准归档为 `superseded`、生成新版本基准并进入下一评审轮次（旧评分冻结、按旧基准回读）；`rejected` 保留复核人和原因；晚到修订复核撞版本会被标记 `conflicted`（409）。
- `GET /api/baselines/pending`：待生效修订待办，服务重启后据此接着未完成修订（启动时也会打印待办数量；修订持久化在 SQLite）。
- 评分行绑定 `baseline_id/baseline_version`；授标只接受生效基准，且要求无待生效修订、无旧基准评分、全部按现基准评完、无未结投诉，否则阻断并把阻断原因写入时间线（`award.blocked`）。
- 详情页（`GET /api/tenders/{id}`，procurement/supervisor/auditor）返回生效基准、基准历史、修订记录（含复核人和阻断原因）、各轮评分按当时基准的回读。

## 运行

要求 Python 3.11+（当前 Python 3.9 环境亦可）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址 `http://127.0.0.1:8209`，数据库默认 `public_procurement.db`（旧库启动时自动迁移评分基准相关表与列）。

## 主要接口

使用 `X-User`、`X-Role` 请求头。角色有 `procurement`、`vendor`、`evaluator`、`supervisor`、`auditor`、`public`。

- `GET /health`、`GET /api/state`、`GET /api/tenders/{id}`
- `POST /api/vendors`、`POST /api/tenders`、`POST /api/tenders/publish`
- `POST /api/bids`、`POST /api/bids/withdraw`、`POST /api/bids/disqualify`
- `POST /api/tenders/open`：截止后开标并核验承诺哈希
- `POST /api/baselines/confirm`、`POST /api/baselines/revisions`、`POST /api/baselines/revisions/review`、`GET /api/baselines/pending`
- `POST /api/conflicts`、`POST /api/evaluations`
- `POST /api/clarifications`、`POST /api/clarifications/answer`
- `POST /api/complaints`、`POST /api/complaints/resolve`
- `POST /api/tenders/award`：只接受生效基准，锁定评分轮次并保存排名快照

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整开标授标、截止前正文隐藏、利益冲突、重复评分覆盖、投诉重评、角色权限，以及评分基准确认门控、待生效修订不影响既有评分、晚到修订版本冲突、修订生效后旧分冻结与重评、候选顺序随口径翻转、修订驳回留痕、重启续办和旧库迁移。

## 局限

供应商与请求用户没有绑定校验，身份仍依赖请求头；投标正文虽然按接口阶段隐藏，但数据库本身未加密；评分规则适合演示，不覆盖复杂资格预审、保证金、电子签名和采购法规差异。
