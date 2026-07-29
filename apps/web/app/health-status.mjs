/**
 * @typedef {"online" | "degraded" | "model-missing" | "offline"} HealthState
 */

/**
 * Convert the API health envelope into the small set of UI states the shell can render.
 * Unknown or malformed payloads fail closed as offline instead of throwing in React.
 *
 * @param {unknown} payload
 * @returns {HealthState}
 */
export function classifyHealthEnvelope(payload) {
  if (!payload || typeof payload !== "object" || !("data" in payload)) {
    return "offline";
  }

  const data = payload.data;
  if (!data || typeof data !== "object") {
    return "offline";
  }

  const status = "status" in data ? data.status : undefined;
  const database = "database" in data ? data.database : undefined;
  const modelConfigured = "model_configured" in data ? data.model_configured : undefined;

  if (status === "degraded" || database === "unavailable") {
    return "degraded";
  }

  if (status !== "ok" || database !== "connected" || typeof modelConfigured !== "boolean") {
    return "offline";
  }

  return modelConfigured ? "online" : "model-missing";
}
