# 恢复策略与验收计划

## 1. 错误分类

| 类型 | 示例 | 自动重试 | 处理方式 |
|---|---|---:|---|
| `PermanentInputError` | 格式不支持、视频为空、时长超限 | 否 | 立即失败并提示更换输入 |
| `RetryableStepError` | 模拟服务抖动、短暂文件占用 | 是 | 退避后重新执行 |
| `StepTimeoutError` | Mock AI 超出步骤时限 | 是 | 终止 attempt，按策略重试 |
| `WorkerLostError` | Worker 被强制结束、进程崩溃 | 是 | 租约过期后恢复 |
| `ArtifactValidationError` | 输出缺失、哈希不匹配 | 是 | 丢弃 staging 并重算 |
| `InternalInvariantError` | 非法状态迁移、重复有效租约 | 否 | 失败并记录高优先级事件 |

默认退避序列为 2 秒、4 秒；测试可注入虚拟时钟，避免自动测试真实等待。

## 2. 故障场景行为

### 步骤抛出临时异常

1. attempt 记录为 `FAILED`。
2. 步骤进入 `RETRY_WAIT`，事件记录下一次执行时间。
3. 到期后步骤回到 `READY`。
4. 前置成功步骤和产物不变。

### Worker 执行中被结束

1. 步骤暂时保持 `RUNNING`，页面显示心跳变旧。
2. `lease_expires_at` 到期后 Recovery Sweeper 取得恢复锁。
3. 原 attempt 标记为 `LOST`。
4. 未完成 staging 被标记为待清理。
5. 步骤在有剩余次数时回到 `READY`。
6. 新 Worker 使用新的 attempt 和 lease token 执行。

### API 进程重启

API 不持有任务执行状态，因此 Worker 不受影响。API 恢复后直接从 SQLite 返回当前快照。

### 缓存记录存在但文件损坏

缓存校验失败，系统写入 `cache_invalidated`，不使用该产物，重新运行对应步骤。旧文件移动到隔离目录，避免被再次命中。

### 旧 Worker 迟到提交

数据库比较 lease token。令牌已经变化时拒绝提交，记录 `stale_worker_commit_rejected`，不会覆盖新 attempt 的结果。

## 3. 故障注入计划

| 故障计划 | 行为 | 预期用途 |
|---|---|---|
| `none` | 正常执行 | 基准流程 |
| `analyze_fail_once_at_50` | 第一次 AI 分析到 50% 抛临时错误 | 自动重试 |
| `contact_sheet_permanent_fail` | 拼图步骤持续抛永久错误 | 手动重试界面 |
| `analyze_timeout_once` | 第一次 AI 分析超过时限 | 超时恢复 |
| `worker_exit_at_40` | Worker 在分析到 40% 直接退出 | 租约恢复 |
| `corrupt_cached_artifact` | 缓存命中前修改校验结果 | 缓存失效 |

故障计划只在 `ENABLE_FAULT_INJECTION=1` 时生效，生产模式忽略客户端传入的故障参数。

## 4. 验收用例

| 编号 | 场景 | 操作 | 通过标准 |
|---|---|---|---|
| AT-01 | 正常执行 | 上传示例视频并运行 | 五步成功，进度 100%，可下载 zip |
| AT-02 | 临时失败 | 使用 `analyze_fail_once_at_50` | 第一次失败、第二次成功；抽帧不重跑 |
| AT-03 | Worker 中断 | 使用 `worker_exit_at_40` 后重启 Worker | 20 秒内回收租约并继续，出现 `LOST` attempt |
| AT-04 | API 重启 | 运行中结束并重启 API | Worker 继续执行，页面恢复后状态正确 |
| AT-05 | 手动重试 | 制造最终失败后点击重试 | 只重试失败步骤，下游随后继续 |
| AT-06 | 缓存复用 | 相同输入与参数创建第二个任务 | 可复用步骤进入 `SKIPPED`，耗时明显下降 |
| AT-07 | 缓存损坏 | 修改一个缓存文件后 rerun | 校验失败并重新计算，不返回损坏结果 |
| AT-08 | 并发领取 | 启动两个 Worker | 一个步骤只有一个有效 attempt |
| AT-09 | 迟到提交 | 令租约过期后让旧 Worker 提交 | 提交被拒绝，新结果不被覆盖 |
| AT-10 | 非法输入 | 上传超长或损坏视频 | 永久失败，不自动重试，错误可读 |

## 5. 自动化测试分层

### 单元测试

- 状态迁移表。
- 任务进度权重。
- 缓存键稳定性。
- 错误分类与退避时间。
- 路径边界和参数规范化。

### 集成测试

- SQLite 事务中的原子领取。
- 两个 Worker 并发竞争。
- 租约过期与恢复扫描。
- staging 到 artifact 的提交。
- API 创建、重试和 rerun。

### 端到端测试

- 启动 API 和 Worker 子进程。
- 创建真实短视频任务。
- 等待状态变化并检查产物。
- 强制结束 Worker，再启动新 Worker。
- 验证页面需要的完整响应字段。

## 6. 可观测指标

| 指标 | 定义 | 用途 |
|---|---|---|
| Task duration | 从创建到终态的总时间 | 比较正常与恢复任务 |
| Step duration | 每个 attempt 的耗时 | 找出瓶颈和超时 |
| Retry count | 各步骤重试次数 | 验证错误策略 |
| Recovery latency | 最后心跳到新 attempt 开始的时间 | 验证中断恢复 SLA |
| Cache hit ratio | 缓存命中步骤 / 可缓存步骤 | 证明结果复用 |
| Duplicate claim rejection | 被拒绝的并发领取次数 | 证明并发安全 |
| Stale commit rejection | 旧租约提交被拒绝次数 | 证明 fence token 有效 |

MVP 页面展示任务和步骤指标；全局聚合指标可以通过 `/api/metrics/summary` 返回 JSON，不引入 Prometheus。

## 7. 演示脚本

### 演示一：正常流程

上传示例视频，展示五步依次运行、进度变化、产物预览和最终压缩包。

### 演示二：临时失败

选择 `analyze_fail_once_at_50`，展示第一次 attempt 的错误、退避倒计时和第二次成功，指出抽帧结果没有重算。

### 演示三：Worker 中断

在 AI 分析进行中结束 Worker，展示心跳变旧、租约过期、attempt 变为 `LOST`，重新启动 Worker 后继续运行。

### 演示四：缓存复用

使用相同视频和参数 rerun，展示命中缓存的步骤、显著缩短的总耗时和 `cache_hit` 事件。

## 8. 失败案例记录模板

每个开发期间出现的真实失败都按以下格式记录：

```text
现象：用户或测试观察到什么
触发条件：输入、状态和操作
根因：为什么会发生
影响：错误状态、重复计算或数据风险
修复：代码或设计如何调整
验证：新增了什么测试
仍有限制：修复后还有什么边界
```

提交文档至少保留两个真实失败案例，不把开发过程包装成一次成功。

## 9. 阶段一验收检查

- [x] 项目目标与非目标明确。
- [x] 五步任务及输入输出明确。
- [x] 任务、步骤、attempt 和资源租约状态明确。
- [x] 中断、重试、超时、缓存损坏和迟到提交行为明确。
- [x] 数据模型和 API 边界明确。
- [x] 自动测试和人工演示标准明确。
- [x] 技术选型与后续扩展路线明确。
- [x] 阶段二开始前确认依赖版本并建立代码骨架。
