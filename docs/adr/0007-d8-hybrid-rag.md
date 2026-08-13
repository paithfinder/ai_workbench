# ADR-0007：D8 Hybrid RAG 与可审计引用问答

- 状态：接受
- 日期：2026-08-12

## 决策

D8 在 D7 的范围安全检索之上实现一次性问答流水线：服务端解析并冻结 Scope，分别执行 Keyword 与 Vector 召回，按 Chunk 去重后使用 Reciprocal Rank Fusion（RRF）融合，可选调用独立 HTTP Reranker，再构造有界 Context。回答通过现有结构化 AI Gateway 生成，只允许引用服务端分配的短 Evidence ID；服务端必须验证 Evidence 属于本次 Context、当前 Scope 及冻结的 KnowledgeRevision 或 SourceVersion/ParseArtifact/Section，验证通过后才可发布 `answered` Turn。

`qa_turns`、`qa_retrieval_hits` 与 `qa_citations` 保存问题、范围与配置快照、原始及融合排名、实际 Context、模型元数据、Token、延迟、回答状态和冻结引用。Turn 创建使用请求哈希与 Idempotency-Key；AI 调用不持有长数据库事务，但先持久化 `processing` 占位，确保网络重试不会重复调用模型。

Reranker 默认禁用；超时、限流、服务不可用或协议错误时记录降级原因并继续使用 RRF。Vector 暂时不可用时可使用 Keyword；两个通道均无可用证据时不调用生成模型并安全拒答。D7 原始检索调试接口继续只在 development/test 开放，生产 QA 复用 application service 而不是调用 debug HTTP API。

## 边界

D8 不实现 LangGraph、Query Rewrite、多轮检索循环、SSE/断线恢复、引用自动修复或 Evidence Agent。D8 的引用校验覆盖身份、范围、锚点与事实 claim 覆盖率；语义蕴含质量进入评测，不引入自动修复循环。

## 影响

QA Turn 与 Citation 是审计事实，不是可重建派生索引；迁移 downgrade 在表中存在记录时失败关闭。模型只能看到短 Evidence ID，不能构造 UUID 或来源 URL。所有已发布 `answered` Turn 必须达到 Citation Validity 100% 和事实 claim coverage 100%，否则保存为失败而不是可信回答。
