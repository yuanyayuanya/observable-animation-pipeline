# 动画素材处理任务管线 MVP

这是题目 D「任务管线可观测方向」的可运行实现。系统不训练真实模型，而是用确定性的 Mock AI 步骤，集中证明耗时任务的持久化状态、执行历史、自动与手动重试、Worker 中断恢复、租约防重、缓存复用和产物校验。

## 已实现功能

- FastAPI 上传与查询接口，上传过程流式写入磁盘。
- SQLite WAL 持久化任务、步骤、attempt、事件、产物和资源租约。
- 独立 Worker 原子领取步骤，使用 lease token 阻止旧 Worker 迟到提交。
- 五步动画处理：输入校验、均匀抽帧、Mock AI 分析、预览拼图、ZIP 打包。
- staging 校验后同卷原子提交，SHA-256 缓存键与损坏缓存隔离。
- 临时错误自动退避重试、最终失败人工重试、过期租约恢复。
- Mock GPU 单槽资源租约。
- 浏览器控制台：任务列表、五步时间线、进度、attempt、事件、错误和产物下载。
- API 不可用时自动进入 Mock 数据模式，便于单独评审页面。

## 架构

```text
Browser -> FastAPI -> SQLite WAL
                  -> 本地 uploads / artifacts

Worker  -> SQLite 原子领取 / 心跳 / 恢复扫描
        -> OpenCV + Pillow 五步处理
        -> staging 校验后原子提交
```

API 不执行视频处理，Worker 不持有唯一内存状态；两个进程都可以重启，事实来源始终是 SQLite 和 Artifact Store。

## 快速运行

将源码解压或克隆到空间充足的磁盘，进入项目根目录后首次安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

打开第一个 PowerShell：

```powershell
.\scripts\run_api.ps1
```

打开第二个 PowerShell：

```powershell
.\scripts\run_worker.ps1
```

浏览器访问：

```text
http://127.0.0.1:8000
```

页面可以直接上传 MP4/MOV，也可以先生成演示视频：

```powershell
.\.venv\Scripts\python.exe scripts\generate_sample_video.py .runtime\temp\sample.mp4 --seconds 3
```

## 故障演示

创建任务时可选：

| 计划 | 预期行为 |
|---|---|
| `none` | 五步正常完成 |
| `analyze_fail_once_at_50` | AI 步骤在 50% 临时失败，退避后自动成功 |
| `analyze_timeout_once` | AI 步骤第一次模拟超时，随后自动重试 |
| `contact_sheet_permanent_fail` | 预览图首次最终失败，点击人工重试后成功 |
| `worker_exit_at_40` | Worker 在 AI 进度 40% 直接退出；租约到期后重启 Worker 可恢复 |

故障注入只有在 `APP_ENABLE_FAULT_INJECTION=1` 时生效；运行脚本默认开启，便于测试题演示。

## 测试

```powershell
.\scripts\run_tests.ps1
```

覆盖内容包括状态迁移、缓存键与路径边界、五步处理器、SQLite 并发领取、租约过期、迟到提交、缓存复用、API/UI 契约，以及真实短视频的正常、自动重试和人工重试端到端流程。

API 和至少一个 Worker 启动后，可用一个命令依次复现四个核心验收场景：

```powershell
.\.venv\Scripts\python.exe scripts\demo_acceptance.py all
```

顺序为正常执行、自动重试、缓存复用、Worker 中断恢复。最后一个场景会按题意终止领取任务的 Worker，并由脚本启动临时替代 Worker 完成恢复；演示结束后需要重新运行 `scripts/run_worker.ps1`。也可把 `all` 换为 `normal`、`auto-retry`、`cache-hit` 或 `worker-crash` 单独演示。

## 运行数据

所有可增长数据默认位于项目下的 `.runtime`。若系统盘空间较少，请把整个项目放在空间充足的磁盘，或通过 `APP_RUNTIME_ROOT` 指向其他目录：

```text
.runtime/
├─ uploads/
├─ staging/
├─ artifacts/
├─ quarantine/
├─ logs/
├─ temp/
└─ app.db
```

项目脚本会把视频、数据库和测试临时文件写入项目的 `.runtime`。删除该目录会清空全部本地任务状态和产物，操作前请确认不再需要。

## 设计文档

- [需求规格](docs/01-requirements.md)
- [系统架构](docs/02-architecture.md)
- [数据模型与 API](docs/03-data-and-api.md)
- [恢复策略与验收计划](docs/04-recovery-and-acceptance.md)
- [阶段二实现与真实失败记录](docs/05-implementation-notes.md)
- [阶段三验收、需求追踪与四场景复现](docs/06-stage3-acceptance.md)
- [提交审查记录](docs/07-submission-review.md)
- [技术选型决策](docs/adr/ADR-001-technology-stack.md)
