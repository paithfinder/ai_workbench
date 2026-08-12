# ADR-0006：PostgreSQL 混合检索索引

- 状态：接受
- 日期：2026-08-10

## 决策

D7 以 PostgreSQL 作为检索索引事实源：结构化 Chunk、`simple` FTS 和 pgvector dense embedding 保存在同一事务边界。每次来源版本索引由不可变 `RetrievalIndexRun` 标识，Chunk 的 `content_identity` 基于索引配置与内容生成，重建通过 Job、Outbox、唯一约束和 worker lease 保证幂等及可恢复。

Embedding 通过应用层 `EmbeddingGateway` 端口隔离；默认 Fake 适配器用于无凭据开发，BGE-M3 使用有界超时的 HTTP 适配器。检索 Scope 必须先解析为服务端快照，再同时应用到关键词与向量通道；不得仅依赖客户端传入的路径。

## 理由与影响

单库实现降低个人项目的运维成本，并让范围过滤、索引状态和业务数据保持一致。HNSW 为近似向量召回，FTS 与 Vector 在 D7 仅暴露原始独立排名；融合与重排留到 D8。索引维度固定为 1024，切换不同维度模型需要新迁移与索引版本。
