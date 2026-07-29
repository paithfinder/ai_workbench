"use client";

import { useEffect, useState } from "react";

import { createApiClient, type HealthData } from "./api-client";

type ApiState = "checking" | "online" | "degraded" | "model-missing" | "offline";

const labels: Record<ApiState, string> = {
  checking: "检查 API",
  online: "服务就绪",
  degraded: "数据库不可用",
  "model-missing": "模型未配置",
  offline: "API 未连接",
};

export function classifyHealth(data: HealthData): ApiState {
  if (data.status === "degraded" || data.database === "unavailable") return "degraded";
  if (data.status !== "ok" || data.database !== "connected") return "offline";
  return data.model_configured ? "online" : "model-missing";
}

export function HealthStatus({ apiBaseUrl }: { apiBaseUrl: string }) {
  const [state, setState] = useState<ApiState>("checking");

  useEffect(() => {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 3500);

    createApiClient(apiBaseUrl).health(controller.signal)
      .then((data) => setState(classifyHealth(data)))
      .catch(() => setState("offline"))
      .finally(() => window.clearTimeout(timeout));

    return () => {
      controller.abort();
      window.clearTimeout(timeout);
    };
  }, [apiBaseUrl]);

  return (
    <div className="api-status" aria-live="polite" data-health-state={state}>
      <span className={`status-mark status-mark--${state}`} aria-hidden="true"><span /></span>
      <span>{labels[state]}</span>
    </div>
  );
}
