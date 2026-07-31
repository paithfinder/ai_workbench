# 自序 · 个人知识工作台

D1 建立两周 MVP 的可运行基础，优先服务个人知识闭环，同时为后续检索、评测与可信引用保留清晰边界。

## D1 范围

- Next.js 应用壳层和 FastAPI 基础服务，固定本地地址为 Web `http://localhost:3000`、API `http://localhost:8000`。
- 单个预置个人知识空间；所有数据模型与后续接口仍显式携带 `space_id`。
- PostgreSQL/pgvector、Redis 与 MinIO 本地基础设施，以及 Alembic 初始迁移。
- AI Gateway 边界和无需密钥的 Fake Provider 基础。
- OpenAPI 到前端类型的单向契约生成链路。
- 20 条 `status=draft` 的中文 RAG 评测种子、JSON Schema 和本地校验器。
- Playwright Web/API 可达性 smoke 测试与 CI 基线。

架构决策见 [`docs/adr/`](docs/adr/)。

## 一键 Compose 启动

需要 Docker Compose。首次启动前复制环境变量示例：

```bash
cp .env.example .env
docker compose -f infra/compose/docker-compose.yml up --build
```

Compose 会启动 PostgreSQL、Redis、MinIO，执行迁移，然后启动 API 与 Web。健康检查：

```bash
curl http://localhost:8000/health/live
curl http://localhost:3000/api/health
```

停止服务：

```bash
docker compose -f infra/compose/docker-compose.yml down
```

如需同时删除本地数据卷，追加 `--volumes`。Docker 本机不可用时，可执行下述非容器检查；完整 Compose 与 smoke 流程由 CI 覆盖。

## 本地开发

要求 Python 3.12、uv、Node.js 20 和 pnpm 10。

```bash
uv sync --frozen --all-packages
pnpm install --frozen-lockfile

# API（默认 http://localhost:8000）
uv run --package knowledge-workbench-api uvicorn knowledge_workbench.main:app \
  --app-dir apps/api/src --reload --host 0.0.0.0 --port 8000

# Web（默认 http://localhost:3000）
pnpm dev:web
```

本地直接运行 API 时，`.env` 中的依赖地址需指向 `localhost`；Compose 内则使用服务名 `postgres`、`redis`、`minio`。

## 数据库迁移

```bash
# 升级到最新版本
uv run --package knowledge-workbench-api alembic -c apps/api/alembic.ini upgrade head

# 检查当前版本
uv run --package knowledge-workbench-api alembic -c apps/api/alembic.ini current

# 回退一版
uv run --package knowledge-workbench-api alembic -c apps/api/alembic.ini downgrade -1
```

## API 契约

FastAPI 是 OpenAPI 的定义来源，生成方向固定为 FastAPI → OpenAPI → TypeScript。根命令会先从应用工厂导出规范，再更新生成类型：

```bash
# 从 FastAPI 导出 OpenAPI 并生成 TypeScript 类型
pnpm contract:generate

# 重新生成并检查是否存在未提交的契约漂移
pnpm contract:check
```

生成文件不得手工修改；`contract:check` 用于检测未提交的生成差异。

## 测试与检查

```bash
# RAG JSON + Schema（无需第三方 Python 包）
python evals/validate_seeds.py

# 后端单元测试、静态检查与迁移回放
uv run --package knowledge-workbench-api ruff check apps/api
uv run --package knowledge-workbench-api mypy apps/api/src
uv run --package knowledge-workbench-api pytest apps/api/tests/unit
uv run --package knowledge-workbench-api python scripts/verify_migrations.py

# 前端
pnpm lint
pnpm typecheck
pnpm test
pnpm build

# Web/API 已运行后执行 smoke
pnpm exec playwright test --config tests/e2e/playwright.config.ts
```

CI 会使用 PostgreSQL 服务执行迁移与后端检查，并通过 Compose 启动预期服务后执行 Playwright smoke。

## 尚未实现

- 文件导入、解析、切分、向量化和可重试的异步处理流水线。
- 知识候选的人工审核、知识树、全文/向量混合检索和带引用问答。
- 间隔复习、活动中心的完整业务能力与 RAG 指标评分流水线。
- 多空间创建/切换、团队协作、用户身份、权限与配额管理。
- 真实 AI Provider 调用、生产密钥接入、成本/限流策略；D1 仅使用 Fake Provider。
- 类 Claude Code 的受控改码及 Git/Shell 执行；本期仅保留只读代码理解与可信引用方向。
- 生产部署、高可用、备份恢复与完整可观测性。
