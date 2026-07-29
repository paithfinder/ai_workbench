import assert from "node:assert/strict";
import test from "node:test";

import { classifyHealthEnvelope } from "./health-status.mjs";

test("classifies a fully ready health envelope as online", () => {
  assert.equal(
    classifyHealthEnvelope({
      data: { status: "ok", database: "connected", model_configured: true },
      error: null,
      request_id: "request-1",
    }),
    "online",
  );
});

test("keeps a connected service visible when the model is not configured", () => {
  assert.equal(
    classifyHealthEnvelope({
      data: { status: "ok", database: "connected", model_configured: false },
      error: null,
      request_id: "request-2",
    }),
    "model-missing",
  );
});

test("reports database unavailability as degraded even with HTTP 200", () => {
  assert.equal(
    classifyHealthEnvelope({
      data: { status: "degraded", database: "unavailable", model_configured: false },
      error: null,
      request_id: "request-3",
    }),
    "degraded",
  );
});

test("fails closed for malformed or unknown envelopes", () => {
  assert.equal(classifyHealthEnvelope(null), "offline");
  assert.equal(classifyHealthEnvelope({ data: null }), "offline");
  assert.equal(
    classifyHealthEnvelope({ data: { status: "ok", database: "connected" } }),
    "offline",
  );
  assert.equal(
    classifyHealthEnvelope({ data: { status: "mystery", database: "connected", model_configured: true } }),
    "offline",
  );
});
