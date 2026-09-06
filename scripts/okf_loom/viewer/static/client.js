/* Reusable ES module for browsers and modern Node hosts. No studio DOM needed. */
export class StudioError extends Error {
  constructor(status, payload) {
    super(payload.error || `Studio HTTP ${status}`);
    this.name = "StudioError";
    this.status = status;
    this.payload = payload;
  }
}

export function createClient({ baseUrl = "", token = "", fetchImpl = fetch } = {}) {
  const base = baseUrl.replace(/\/$/, "");
  async function request(path, { body, query, signal, idempotencyKey } = {}) {
    if (!path.startsWith("/__") || path.includes("://")) throw new Error("Invalid studio route");
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(query || {})) {
      if (value !== undefined && value !== null) params.set(key, String(value));
    }
    const headers = { Accept: "application/json" };
    if (token && (body !== undefined || path === "/__diff")) headers["X-OKF-Token"] = token;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
    const response = await fetchImpl(base + path + (params.size ? "?" + params : ""), {
      method: body === undefined ? "GET" : "POST", headers,
      body: body === undefined ? undefined : JSON.stringify(body), signal,
      redirect: "error", credentials: "same-origin",
    });
    const raw = await response.text();
    let payload;
    try { payload = JSON.parse(raw); } catch (_) { payload = { error: raw }; }
    if (!response.ok) throw new StudioError(response.status, payload);
    return payload;
  }
  return Object.freeze({
    request,
    discover: (options) => request("/__api/v1", options),
    health: (options) => request("/__health", options),
    document: (id, options = {}) => request("/__data/doc", { ...options, query: { id } }),
    search: (q, mode = "lexical", options = {}) => request("/__search", { ...options, query: { q, mode, format: "json" } }),
    comments: (options) => request("/__comments", options),
    events: (since, options = {}) => request("/__data/events", { ...options, query: { since, order: "asc" } }),
    comment: (body, options = {}) => request("/__comment", { ...options, body }),
    claim: (body, options = {}) => request("/__claim", { ...options, body }),
    resolve: (body, options = {}) => request("/__resolve", { ...options, body }),
    apply: (body, options = {}) => request("/__apply", { ...options, body }),
    undo: (body, options = {}) => request("/__undo", { ...options, body }),
  });
}
