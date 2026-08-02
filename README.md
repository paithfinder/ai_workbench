# 自序 · 个人知识工作台

D1 建立两周 MVP 的可运行基础，D2 打通不可变来源导入，D3 在此基础上加入可重试的真实文档解析、版本化解析产物与可信引用定位。当前优先服务个人知识闭环；D4 的 AI 摘要与知识候选抽取尚未实现。

## 当前 D1–D3 能力

- Next.js 应用与 FastAPI 服务固定为 Web `http://localhost:3000`、API `http://localhost:8000`；使用单个预置个人知识空间，数据模型与接口仍显式携带 `space_id`。
- PostgreSQL/pgvector 保存业务事实，Redis 作为 Celery broker/短期基础设施，MinIO 保存不可变来源原件与解析产物；Alembic 管理迁移。
- 文件导入支持 PDF、Markdown 和纯文本，浏览器校验扩展名、MIME、非空及 25 MiB 上限，并通过预签名 multipart `POST` 直传 MinIO。
- 来源创建、上传预留、完成、重试和重解析使用幂等键；Job、JobAttempt 与 Transactional Outbox 协调至少一次执行，不依赖 Celery Result Backend 保存业务状态。
- D3 将入库后的来源交给独立 `source-parse` 队列。Docling 运行时按锁定版本解析 PDF/Markdown/HTML，受源文件大小、页数、OCR、租约、心跳和总超时配置约束。
- 解析产物按 source version 和 parse revision 写入 MinIO，并在 PostgreSQL 保存 canonical sections。section 保留稳定 ordinal、heading path、页码、段落索引、可用时的 bbox、精确 UTF-8 quote hash 及冻结到 source/version/artifact revision 的 locator。
- 来源详情、分页 section 查询和幂等 reparse API 暴露真实解析状态；旧 revision 不覆盖当前可信引用所需的版本信息。
- AI Gateway 边界仍使用无需凭据的 Fake Provider；OpenAPI 到前端类型保持单向生成。
- 合成 parser fixture 覆盖 UTF-8 中文/Unicode、静态 HTML、单页/多页 PDF、表格、纯图片、空文件和畸形 PDF，并冻结来源哈希与期望 quote hash。

架构决策见 [`docs/adr/`](docs/adr/)，D3 worker/runtime 决策见 [`ADR-0005`](docs/adr/0005-d3-parse-worker-and-runtime.md)。

## 一键 Compose 启动

需要 Docker Compose。首次启动前复制环境变量示例：

```bash
cp .env.example .env
docker compose -f infra/compose/docker-compose.yml up --build
```

Compose 会启动 PostgreSQL、Redis、MinIO，执行迁移，然后启动 API、Web、outbox relay、保留的 `source-ingest` worker，以及独立的 `source-parse` worker。parse worker 固定 `concurrency=1`，显式连接数据库、Redis 与 MinIO，以免重型解析阻塞入库任务。

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
# RAG JSON + Schema（无需第三方 Python 包）
python evals/validate_seeds.py

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

- D4 AI 摘要、知识点/知识候选抽取及人工审核；D3 只做确定性解析与可信来源定位。
- 向量化、知识树、全文/向量混合检索和带引用问答。
- 间隔复习、活动中心的完整业务能力与 RAG 指标评分流水线。
- 多空间创建/切换、团队协作、用户身份、权限与配额管理。
- 真实 AI Provider 调用、生产密钥接入、成本/限流策略；当前仅使用 Fake Provider。
- 类 Claude Code 的受控改码及 Git/Shell 执行；本期仅保留只读代码理解与可信引用方向。
- 生产部署、高可用、备份恢复、模型制品供应与完整可观测性。
