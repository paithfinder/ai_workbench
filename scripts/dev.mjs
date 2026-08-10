#!/usr/bin/env node
// Unified local dev launcher for solo development.
// Starts API + 3 Celery workers + outbox relay + Web in one terminal.
//
// Prerequisites (must be running BEFORE you `pnpm dev`):
//   - PostgreSQL on 5432 (with pgvector + ltree extensions, DB = knowledge_workbench)
//   - Redis on 6379
//   - MinIO on 9000 (bucket: knowledge-sources)
//   - Alembic migrations applied (`alembic -c apps/api/alembic.ini upgrade head`)
//
// To skip a service you don't need today (e.g. parse while doing API-only work),
// just comment out its entry in `services` below.
//
// Usage:   pnpm dev     (or: node scripts/dev.mjs)
// Stop:    Ctrl+C once  — kills all child processes

import { spawn } from "node:child_process";
import { platform } from "node:os";

const isWindows = platform() === "win32";
const useColor = process.stdout.isTTY && process.stderr.isTTY;

// Order matches the README preview: api → 3 workers → relay → web.
const services = [
  {
    name: "api",
    color: "\x1b[36m", // cyan
    cmd: "uv run --package knowledge-workbench-api uvicorn knowledge_workbench.main:app --app-dir apps/api/src --reload --host 0.0.0.0 --port 8000",
  },
  {
    name: "ingest",
    color: "\x1b[34m", // blue
    cmd: "uv run --package knowledge-workbench-api celery -A knowledge_workbench.worker.celery_app:celery_app worker --loglevel=INFO --queues=source-ingest",
  },
  {
    name: "parse",
    color: "\x1b[35m", // magenta
    cmd: "uv run --package knowledge-workbench-api celery -A knowledge_workbench.worker.celery_app:celery_app worker --loglevel=INFO --queues=source-parse --concurrency=1",
  },
  {
    name: "extract",
    color: "\x1b[33m", // yellow
    cmd: "uv run --package knowledge-workbench-api celery -A knowledge_workbench.worker.celery_app:celery_app worker --loglevel=INFO --queues=source-extract --concurrency=1",
  },
  {
    name: "relay",
    color: "\x1b[32m", // green
    cmd: "uv run --package knowledge-workbench-api python -m knowledge_workbench.worker.outbox_relay",
  },
  {
    name: "web",
    color: "\x1b[37m", // white
    cmd: "pnpm dev:web",
  },
];

const RESET = "\x1b[0m";
// Width to which `[name]` is padded so all prefixes align. Longest name is "extract" → `[extract]` = 9.
const TAG_WIDTH = 9;

function makePrefix(svc) {
  const tag = `[${svc.name}]`.padEnd(TAG_WIDTH);
  if (!useColor) return `${tag} `;
  return `${svc.color}${tag}${RESET} `;
}

function startService(svc) {
  // shell:true lets Windows resolve `pnpm.cmd`; detached:true on POSIX creates a
  // process group so we can SIGTERM the whole tree on exit.
  const child = spawn(svc.cmd, {
    stdio: ["ignore", "pipe", "pipe"],
    shell: true,
    detached: !isWindows,
  });

  const prefix = makePrefix(svc);

  // Line-buffered prefixing. stdout/stderr data events can split mid-line, so
  // we hold the trailing partial line and flush it when the next chunk arrives.
  function makeWriter(stream) {
    let pending = "";
    return (chunk) => {
      pending += chunk.toString();
      const lines = pending.split(/\r?\n/);
      pending = lines.pop();
      for (const line of lines) {
        stream.write(line.length ? prefix + line : "");
        stream.write("\n");
      }
    };
  }

  child.stdout.on("data", makeWriter(process.stdout));
  child.stderr.on("data", makeWriter(process.stderr));

  child.on("exit", (code, signal) => {
    process.stderr.write(`${prefix}exited code=${code} signal=${signal}\n`);
  });

  child.on("error", (err) => {
    process.stderr.write(`${prefix}failed to start: ${err.message}\n`);
  });

  return child;
}

console.log("Starting 6 services. Press Ctrl+C once to stop all.");
console.log(
  "Make sure PostgreSQL (5432), Redis (6379), MinIO (9000) are running.\n"
);

const children = services.map((svc) => ({ svc, child: startService(svc) }));
let shuttingDown = false;

function killAll() {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const { svc, child } of children) {
    try {
      if (isWindows) {
        // /T kills the whole process tree (uv/celery spawn children).
        spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], {
          stdio: "ignore",
        }).unref();
      } else {
        // Negative pid = kill the process group created by detached:true.
        process.kill(-child.pid, "SIGTERM");
      }
    } catch {
      // Already gone — ignore.
    }
  }
}

process.on("SIGINT", () => {
  killAll();
  process.exit(130);
});
process.on("SIGTERM", () => {
  killAll();
  process.exit(143);
});
