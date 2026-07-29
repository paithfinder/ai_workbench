import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { HealthStatus } from "./health-status";

function response(data: unknown) {
  return new Response(JSON.stringify({ data, error: null, request_id: "health-request" }), { status: 200 });
}

describe("HealthStatus", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("shows ready only for a runtime-valid ready response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      app: "ai-workbench",
      database: "connected",
      model_configured: true,
      status: "ok",
      version: "0.1.0",
    })));
    render(<HealthStatus apiBaseUrl="http://api.test/api/v1" />);
    expect(await screen.findByText("服务就绪")).toBeTruthy();
  });

  it("fails closed for malformed, inconsistent, and error-bearing 200 responses", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: "degraded", database: "unavailable", model_configured: "no" }))
      .mockResolvedValueOnce(response({ app: "ai-workbench", status: "ok", database: "unavailable", model_configured: true, version: "0.1.0" }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        data: { app: "ai-workbench", status: "ok", database: "connected", model_configured: true, version: "0.1.0" },
        error: { code: "unexpected", message: "not ready", details: null },
        request_id: "health-error",
      }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    const first = render(<HealthStatus apiBaseUrl="http://api.test/api/v1" />);
    await waitFor(() => expect(screen.getByText("API 未连接")).toBeTruthy());
    first.unmount();
    const second = render(<HealthStatus apiBaseUrl="http://api.test/api/v1" />);
    await waitFor(() => expect(screen.getByText("API 未连接")).toBeTruthy());
    second.unmount();
    render(<HealthStatus apiBaseUrl="http://api.test/api/v1" />);
    await waitFor(() => expect(screen.getByText("API 未连接")).toBeTruthy());
  });

  it("fails closed when the request cannot complete", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    render(<HealthStatus apiBaseUrl="http://api.test/api/v1" />);
    await waitFor(() => expect(screen.getByText("API 未连接")).toBeTruthy());
  });
});
