# ADR-0005：D3 解析隔离与可复现运行时

- 状态：接受
- 日期：2026-08-02

## 背景

D3 在 D2 的不可变原件导入之后增加文档解析。Docling 的 PDF/OCR 运行时比来源入库任务消耗更多 CPU、内存和时间；Celery 采用至少一次投递，解析还必须在进程丢失或超时后安全恢复。解析结果需要支持重跑、审计和稳定引用，不能覆盖原件或依赖 Celery Result Backend 保存业务事实。

## 决策

- 保留 `source-ingest` worker；另设只消费 `source-parse` 队列的 worker，并固定 `--concurrency=1`。两类任务不争用同一 worker 并发池。
- parse worker 显式连接 PostgreSQL、Redis 和 MinIO。PostgreSQL 中的 Job、JobAttempt、Outbox、parse artifact 和 section 是权威状态；Redis 只承担 broker，MinIO 保存不可变原件及按 revision/attempt 隔离的解析产物。只有仍持有数据库租约的获胜 attempt 能提交 artifact 指针；迟到 attempt 写入的对象不会覆盖获胜产物，并在可控退出时清理。
- `PARSE_ATTEMPT_LEASE_SECONDS` 控制作业尝试租约，`PARSE_HEARTBEAT_SECONDS` 在长解析期间续租，`PARSE_TIMEOUT_SECONDS` 限制单次解析耗时。Docling 在可终止子进程内执行；worker 丢失、重复投递和重试都通过数据库租约、fencing token 与幂等 artifact revision 协调。
- `PARSE_MAX_SOURCE_BYTES` 和 `PARSE_MAX_PAGES` 在进入解析器前/解析时限制资源；OCR 及语言通过 `PARSE_ENABLE_OCR`、`PARSE_OCR_LANGUAGES` 配置。
- 网页来源通过受限静态抓取器获取：逐跳校验 URL、DNS 与目标 IP，将连接固定到已验证地址并保留 Host/TLS SNI；每个重定向 hop 使用独立连接池，防止不同 HTTPS hostname 共用 IP 时复用前一主机已认证的 TLS 连接。抓取器拒绝压缩传输，并限制 URL、重定向、超时、Content-Type 与响应体大小。
- Docling 版本同时锁定在 Python 依赖锁文件和 `PARSER_VERSION` 元数据中。Docling 重型导入与 converter/model 初始化仅在 parse task 实际调用适配器时发生；API、迁移、relay、ingest worker 和镜像启动不预热模型。
- CI 单独执行合成 fixture manifest、确定性 parser contract 与真实 Docling fixture 回归，然后执行完整后端单元测试。真实 Docling 回归覆盖 Markdown 精确 quote hash、PDF 页码/bbox provenance 与空文件失败路径；当前不启用 OCR，因此不声称验证 OCR 模型转换。

## 影响

解析吞吐量有意限制为每个 parse worker 一次一个文档；需要扩容时应增加独立 worker 实例，而不是提高单实例并发。首次需要特定 Docling 模型的解析可能发生受上游模型分发影响的下载；生产部署应将模型准备作为显式构建/发布步骤，并把 `DOCLING_ARTIFACTS_PATH` 指向受控缓存或只读制品，不应把下载隐式塞入服务启动。

D3 只生成 canonical document、解析产物、section、页码/段落/边界框等来源定位信息。D4 的 AI 摘要、知识点或知识候选抽取不在本决策范围内，也尚未实现。
