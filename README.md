# 自序 · 个人知识工作台

D1 建立两周 MVP 的可运行基础，D2 打通不可变来源导入，D3 加入可重试的真实文档解析、版本化解析产物与可信引用定位，D4 实现结构化 AI 提炼与可追踪 Extraction Job，D5 交付候选编辑、人工决策与正式知识入库事务，D6 已加入 PostgreSQL 持久化知识树、Revision 编辑、词法搜索和不可漂移的来源深链。D7 交付 Chunk、Embedding、Keyword/Vector 检索调试与离线检索评测；D8 在相同范围边界上加入 Hybrid RRF、可选 Reranker、有界 Context、结构化回答/拒答和服务端引用校验。

## 当前 D1–D8 能力

- Next.js 应用与 FastAPI 服务固定为 Web `http://localhost:3000`、API `http://localhost:8000`；使用单个预置个人知识空间，数据模型与接口仍显式携带 `space_id`。
- PostgreSQL/pgvector 保存业务事实，Redis 作为 Celery broker/短期基础设施，MinIO 保存不可变来源原件与解析产物；Alembic 管理迁移。
- 文件导入支持 PDF、Markdown 和纯文本，浏览器校验扩展名、MIME、非空及 25 MiB 上限，并通过预签名 multipart `POST` 直传 MinIO。
- 来源创建、上传预留、完成、重试和重解析使用幂等键；Job、JobAttempt 与 Transactional Outbox 协调至少一次执行，不依赖 Celery Result Backend 保存业务状态。
- D3 将入库后的来源交给独立 `source-parse` 队列。Docling 运行时按锁定版本解析 PDF/Markdown/HTML，受源文件大小、页数、OCR、租约、心跳和总超时配置约束。
- 解析产物按 source version 和 parse revision 写入 MinIO，并在 PostgreSQL 保存 canonical sections。section 保留稳定 ordinal、heading path、页码、段落索引、可用时的 bbox、精确 UTF-8 quote hash 及冻结到 source/version/artifact revision 的 locator。
- 来源详情、分页 section 查询和幂等 reparse API 暴露真实解析状态；旧 revision 不覆盖当前可信引用所需的版本信息。
- AI Gateway 默认使用无需凭据的确定性 Fake Provider；设置 `AI_PROVIDER=anthropic` 与服务端 `ANTHROPIC_API_KEY` 后，通过官方 Anthropic Python SDK 调用 Claude Opus 5。
- D4 将 ready parse artifact 分批送入 `source-extract` worker，以严格 Pydantic Structured Output 生成标题、原子正文、标签、Evidence ID、置信度和待验证原因。Evidence 必须属于同一来源版本，否则整次提炼安全失败且不落候选。
- Extraction Job 记录 Provider、Model、Prompt Version、Token、Latency 和 Request ID；来源详情显示进度与可重试失败。
- D5 Candidate Queue 保持 ORIGINAL SOURCE 与 AI CANDIDATE 身份分离，支持编辑最终标题、正文、标签与目标位置，以及接受、待验证和拒绝。接受会在同一事务内创建正式知识节点、revision、冻结 Evidence、复习卡、审查记录、活动与 Outbox；待验证和拒绝不会创建正式知识。
- D5 写接口要求 `Idempotency-Key` 与 `expected_version`，支持结果重放、冲突检测和不确定响应 reconciliation；接受来源 Evidence 前会重新校验版本、解析产物、locator 与 quote hash。
- D6 以邻接表和 PostgreSQL `ltree` 持久化 `root/folder/document/point/source` 五类节点；支持创建目录/文档、追加 Revision、移动子树、软删除、乐观锁、幂等重放和写结果 reconciliation。追加 Revision 会保留历史并显式继承当前冻结 Evidence；编辑界面要求保存前确认修改后的正文仍由这些来源支持。
- `/knowledge` 提供可折叠 WAI-ARIA Tree、服务端标题/正文/显示路径/关联来源标题搜索、详情维护和 Citation drawer；引用固定到精确 SourceVersion、ParseArtifact 与 Section，并可深链到历史原文高亮位置。
- D7 通过真实 HTTP debug API 分别暴露 Keyword 与 Vector Top-K 结果。`evals/` 的 32 条中文 seeds 覆盖 scope、内容 identity、section、困难负样本和拒答；retrieval runner 对 27 条可回答样本计算 Recall@5、可选 MRR 与 Scope Leakage。
- D8 生产问答执行 `Scope → Keyword/Vector → RRF → optional rerank → bounded Context → structured answer/abstain → citation validation → persistence`。`/qa` 只发布经服务端验证的答案与冻结引用；空证据、范围外证据或引用不完整时安全拒答/失败关闭，`/qa/debug` 保留 D7 原始通道调试。
- 合成 parser fixture 覆盖 UTF-8 中文/Unicode、静态 HTML、单页/多页 PDF、表格、纯图片、空文件和畸形 PDF，并冻结来源哈希与期望 quote hash。

架构决策见 [`docs/adr/`](docs/adr/)，D3 worker/runtime 决策见 [`ADR-0005`](docs/adr/0005-d3-parse-worker-and-runtime.md)。

## 一键 Compose 启动

需要 Docker Compose。首次启动前复制环境变量示例：

```bash
cp .env.example .env
docker compose -f infra/compose/docker-compose.yml up --build
```

Compose 会启动 PostgreSQL、Redis、MinIO，执行迁移，然后启动 API、Web、outbox relay、保留的 `source-ingest` worker，以及独立的 `source-parse` 和 `source-extract` worker。parse/extract worker 固定 `concurrency=1`，显式连接数据库、Redis 与 MinIO，以免重型解析或模型调用阻塞入库任务。

Docling 依赖由 `pyproject.toml` 和 `uv.lock` 锁定。镜像构建与容器启动不会主动初始化或下载模型；重型 converter/model 初始化仅在 parse worker 实际解析时发生。首次 OCR 解析可能需要取得上游模型，生产环境应预置受控模型缓存并配置 `DOCLING_ARTIFACTS_PATH`。

健康检查：

```bash
curl http://localhost:8000/health/live
curl http://localhost:3000/api/health
```

停止服务：

```bash
docker compose -f infra/compose/docker-compose.yml down
```

如需同时删除本地数据卷，追加 `--volumes`。

## D3 运行配置

`.env.example` 列出全部当前 Settings 环境变量。重点限制如下：

| 配置 | 默认值 | 用途 |
| --- | ---: | --- |
| `MAX_UPLOAD_SIZE_BYTES` | `26214400` | 浏览器文件来源上限 |
| `MAX_PASTED_TEXT_SIZE_BYTES` | `1048576` | 粘贴文本 UTF-8 字节上限 |
| `WEB_FETCH_CONNECT_TIMEOUT_SECONDS` | `5.0` | 安全网页抓取连接超时 |
| `WEB_FETCH_TOTAL_TIMEOUT_SECONDS` | `15.0` | 单次网页抓取总超时 |
| `WEB_FETCH_MAX_BODY_BYTES` | `5242880` | 网页快照响应体上限 |
| `WEB_FETCH_MAX_REDIRECTS` | `5` | 安全重定向上限 |
| `PARSE_ATTEMPT_LEASE_SECONDS` | `1800` | PostgreSQL parse attempt 租约 |
| `PARSE_HEARTBEAT_SECONDS` | `30` | 长解析租约续期周期 |
| `PARSE_TIMEOUT_SECONDS` | `1800` | 单次 parser 调用总超时 |
| `PARSE_MAX_PAGES` | `200` | 单文档解析页数上限 |
| `PARSE_MAX_SOURCE_BYTES` | `26214400` | parse worker 下载/解析字节上限 |
| `PARSE_ENABLE_OCR` | `true` | 是否允许 PDF OCR |
| `PARSE_OCR_LANGUAGES` | `["en","zh"]` | JSON 数组形式的 OCR 语言 |
| `PARSE_ARTIFACT_PREFIX` | `artifacts` | MinIO 解析产物前缀 |
| `PARSER_NAME` / `PARSER_VERSION` | `docling` / `2.117.0` | 解析 artifact 的预定 parser 元数据 |

租约应覆盖正常解析窗口；心跳周期必须显著短于租约。parse timeout 和资源限制用于停止单次尝试，数据库租约与 Celery 重试用于恢复，而不是以提高 worker 并发绕过限制。

## D7 检索边界与本地 BGE-M3

D7 只负责确定性 chunk、embedding/index、Keyword/Vector 检索、scope 隔离和检索调试评测。真实调试端点为：

```text
POST /api/v1/knowledge-spaces/{space_id}/retrieval/debug-search
```

请求包含 `query`、`scope.scope_node_id`、`scope.include_descendants`、`top_k` 与 `channels`；响应分别返回 `keyword_hits`、`vector_hits`，并报告 `embedding`、`channel_errors` 和 `timings_ms`。该 debug API 只用于开发/评测，不是面向最终用户的问答接口。

本地原生运行 BGE-M3 时，先启动暴露 OpenAI-compatible `/v1/embeddings` 的 HTTP 服务并加载 `BAAI/bge-m3`，再配置 API：

```dotenv
EMBEDDING_PROVIDER=bge_m3_http
EMBEDDING_URL=http://127.0.0.1:<port>/v1/embeddings
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSIONS=1024
EMBEDDING_TIMEOUT_SECONDS=30
```

这里 `EMBEDDING_URL` 是完整 endpoint，不是服务 origin；当前 adapter 不需要 API key。API 在主机原生运行时使用 `127.0.0.1`；只有 API 在 Compose 容器中、embedding 服务在宿主机时才使用 `http://host.docker.internal:<port>/v1/embeddings`。模型服务必须返回真实 1024 维向量，并与数据库索引维度一致。chunk/index 还受 `CHUNKER_VERSION`、`CHUNK_TARGET_CHARACTERS`、`CHUNK_OVERLAP_CHARACTERS`、`INDEX_VERSION` 与 `RETRIEVAL_DEFAULT_TOP_K` 控制；所有默认值以最终 `.env.example` 为准。

Fake Provider 仅用于确定性单元测试、HTTP transport smoke 和失败路径，不代表 BGE-M3，也不能作为“真实 D7 基线”。runner 默认拒绝 Fake；只有显式 `--allow-fake-smoke` 才允许无 fixture map 执行，此时必须用 `--smoke-scope-node-id` 指向测试空间内真实存在的范围节点 UUID，并将报告标记为 `baseline_eligible=false` 与 `run_kind=fake-smoke`。真实 baseline 还必须同时满足：`embedding.provider=bge_m3_http`、model 明确为 BGE-M3、`embedding.dimensions=1024`、响应含 `scope_summary.index_config_version`，且整次运行 metadata 不漂移。

### 准备可执行评测 fixture

seeds 中的 `scope.key`、content identity 和 section ID 是稳定的评测键，不伪装成数据库 UUID。真实 baseline 前必须显式启用 BGE-M3，并用专用装载器创建隔离的评测空间：

```dotenv
EMBEDDING_PROVIDER=bge_m3_http
EMBEDDING_URL=http://127.0.0.1:<port>/v1/embeddings
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIMENSIONS=1024
```

```bash
uv run --package knowledge-workbench-api python evals/prepare_retrieval_fixture.py
```

装载器会先探测真实 BGE-M3、确认 PostgreSQL 已迁移到 D7 当前 head（含 `0009_d7_cjk_fts`），然后按 dataset/version 的稳定 UUID 创建专用评测空间、范围节点、ready 来源版本和 Section，直接复用 `IndexingService` 与 `SourceIndexWorker` 生成正式 Chunk。它不会写入默认个人空间，也不会经过 Redis、MinIO 或 Docling。成功后生成被 Git 忽略的 `evals/fixture-map.local.json`，其中包含真实评测空间 UUID、完整映射以及实际 embedding/index metadata；相同 dataset/version 重复执行是幂等的，数据内容变化必须先提升 dataset version。

只检查 BGE、数据库和已装载 fixture，不写数据：

```bash
uv run --package knowledge-workbench-api python evals/prepare_retrieval_fixture.py --check-only
```

然后启动 development API 和 Web，使用映射文件中的 `space_id` 运行：

```bash
uv run --package knowledge-workbench-api python evals/run_retrieval.py \
  --base-url http://localhost:8000 \
  --space-id <fixture-map.local.json 中的 space_id> \
  --fixture-map evals/fixture-map.local.json \
  --mrr \
  --output evals/retrieval-report.json
```

runner 会在发请求前拒绝不完整或仍含 `<placeholder>` 的映射。Scope Leakage 只按已映射的 `out_of_scope_content_identities` 命中计算；所有范围外对照内容都存放在评测空间的独立兄弟子树中，用于验证 `space_id + ltree` 过滤，而不是根据不存在于冻结 hit 契约的 scope 字段猜测。`evals/fixture-map.example.json` 只展示格式，不是可执行 baseline 数据，也不得直接作为报告输入。

## D8 Hybrid RAG、Reranker 与可信问答

生产端点为：

```text
POST /api/v1/knowledge-spaces/{space_id}/qa/turns
GET  /api/v1/knowledge-spaces/{space_id}/qa/turns/{turn_id}
GET  /api/v1/knowledge-spaces/{space_id}/qa/turns/by-idempotency-key/{key}
```

POST 必须携带 `Idempotency-Key`。相同 key 和请求体返回同一 Turn；相同 key 配合不同请求体返回冲突，不会重复调用模型。Reranker 默认关闭；启用本地 `bge-reranker-v2-m3` HTTP 服务时配置：

```dotenv
RERANKER_PROVIDER=bge_http
RERANKER_URL=http://127.0.0.1:8081/rerank
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
RERANKER_TIMEOUT_SECONDS=15
```

Reranker 超时、连接失败、429、5xx 或协议错误会记录 warning 并回退到 RRF；不会自动切换到 Fake。`AI_PROVIDER=fake` 只验证流水线和拒答路径，不能作为答案质量 baseline。正式 D8 QA 报告要求真实 BGE-M3 fixture 和非 Fake QA Provider：

```bash
uv run --package knowledge-workbench-api python evals/run_qa.py \
  --base-url http://localhost:8000 \
  --space-id <fixture-map.local.json 中的 space_id> \
  --fixture-map evals/fixture-map.local.json \
  --output evals/qa-report.json
```

报告包含 Citation Validity、Claim Citation Coverage、Correct Abstention Rate、Scope Leakage、失败样本和运行时模型/索引 metadata。Fake smoke 必须显式添加 `--allow-fake-smoke`，且报告固定为 `baseline_eligible=false`。

## 本地开发

要求 Python 3.12、uv、Node.js 20 和 pnpm 10。

```bash
uv sync --frozen --all-packages --all-groups
pnpm install --frozen-lockfile

# API（默认 http://localhost:8000）
uv run --package knowledge-workbench-api uvicorn knowledge_workbench.main:app \
  --app-dir apps/api/src --reload --host 0.0.0.0 --port 8000

# source-ingest worker
uv run --package knowledge-workbench-api celery \
  -A knowledge_workbench.worker.celery_app:celery_app worker \
  --loglevel=INFO --queues=source-ingest

# D3 source-parse worker（独立终端，固定并发 1）
uv run --package knowledge-workbench-api celery \
  -A knowledge_workbench.worker.celery_app:celery_app worker \
  --loglevel=INFO --queues=source-parse --concurrency=1

# D4 source-extract worker（独立终端，固定并发 1）
uv run --package knowledge-workbench-api celery \
  -A knowledge_workbench.worker.celery_app:celery_app worker \
  --loglevel=INFO --queues=source-extract --concurrency=1

# Outbox relay（独立终端）
uv run --package knowledge-workbench-api python \
  -m knowledge_workbench.worker.outbox_relay

# Web（默认 http://localhost:3000）
pnpm dev:web
```

本地直接运行进程时，`.env` 中 PostgreSQL、Redis、MinIO 地址需指向 `localhost`；Compose 内使用 `postgres`、`redis`、`minio` 服务名。

## 数据库迁移

```bash
uv run --package knowledge-workbench-api alembic -c apps/api/alembic.ini upgrade head
uv run --package knowledge-workbench-api alembic -c apps/api/alembic.ini current
uv run --package knowledge-workbench-api alembic -c apps/api/alembic.ini downgrade -1
```

## API 契约

FastAPI 是 OpenAPI 定义来源，生成方向固定为 FastAPI → OpenAPI → TypeScript：

```bash
pnpm contract:generate
pnpm contract:check
```

生成文件不得手工修改；`contract:check` 用于检测未提交的生成差异。

## 测试与检查

```bash
# D7 RAG JSON + Schema（无需第三方 Python 包）
python evals/validate_seeds.py

# validator、D7 retrieval runner 与 D8 QA runner 单元/自测
python -m unittest discover -s evals -p "test_*.py" -v
python -m py_compile evals/validate_seeds.py evals/run_retrieval.py evals/run_qa.py evals/test_eval_tools.py

# 在已导入且已索引评测内容的真实 D7 API 上运行 Recall@5、MRR、Scope Leakage
python evals/run_retrieval.py \
  --base-url http://localhost:8000 \
  --space-id <真实评测空间UUID> \
  --fixture-map evals/fixture-map.local.json \
  --mrr \
  --output evals/retrieval-report.json

# Fake 只能做连通性 smoke，输出不会被标记为真实 baseline
python evals/run_retrieval.py \
  --base-url http://localhost:8000 \
  --space-id <测试空间UUID> \
  --smoke-scope-node-id <测试空间内范围节点UUID> \
  --allow-fake-smoke

# D8 Hybrid QA、引用有效性、claim coverage、拒答和 scope leakage
python evals/run_qa.py \
  --base-url http://localhost:8000 \
  --space-id <真实评测空间UUID> \
  --fixture-map evals/fixture-map.local.json \
  --output evals/qa-report.json

# D3 fixture、确定性 parser contract 与真实 Docling fixture 回归
uv run --package knowledge-workbench-api pytest \
  apps/api/tests/unit/test_d3_fixture_manifest.py \
  apps/api/tests/unit/test_source_parsing.py \
  apps/api/tests/integration/test_docling_parser.py

# 完整后端单元测试、静态检查与迁移回放
uv run --package knowledge-workbench-api ruff check apps/api
uv run --package knowledge-workbench-api mypy apps/api/src
uv run --package knowledge-workbench-api pytest apps/api/tests/unit
uv run --package knowledge-workbench-api python scripts/verify_migrations.py

# 前端
pnpm lint
pnpm typecheck
pnpm test
pnpm build

# Compose 服务已运行后执行 Web/API smoke 与导入 E2E
pnpm exec playwright test --config tests/e2e/playwright.config.ts
```

CI 会显式执行 D3 fixture、确定性 parser contract 与真实 Docling fixture 回归，再执行完整后端单元测试、迁移与 OpenAPI 漂移检查。真实 Docling 回归覆盖 Markdown quote hash、PDF 页码/bbox provenance 和空文件失败路径；它不启用 OCR，因此不声称验证 OCR 模型转换。Compose Playwright job 负责构建并启动容器服务。

## 尚未实现

- D8 的带引用问答、RRF/reranker、query rewrite、答案拒答判定、Citation Validity 与 Claim Citation Coverage；这些不是 D7 retrieval runner 的目标，不能由 Recall@5 结果代替。
- LangGraph/Agent，以及 D10 的 FSRS 调度字段与完整间隔复习。
- 多空间创建/切换、团队协作、用户身份、权限与配额管理。
- 生产 AI 密钥托管、预算/成本告警和更完整的限流策略；本地默认使用 Fake Provider，真实 Anthropic 调用需要显式服务端配置。
- 类 Claude Code 的受控改码及 Git/Shell 执行；本期仅保留只读代码理解与可信引用方向。
- 生产部署、高可用、备份恢复、模型制品供应与完整可观测性。
