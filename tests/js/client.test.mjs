import test from "node:test";
import assert from "node:assert/strict";
import { createClient, StudioError } from "../../scripts/okf_loom/viewer/static/client.js";

test("client encodes queries, scopes credentials and lets the host cancel", async () => {
  const calls = [];
  const fetchImpl = async (...args) => { calls.push(args); return new Response("{}", { status: 200 }); };
  const client = createClient({ baseUrl: "https://host.example/loom", token: "session", fetchImpl });
  const controller = new AbortController();
  await client.document("a/b & c", { signal: controller.signal });
  assert.match(calls[0][0], /id=a%2Fb\+%26\+c/);
  assert.equal(calls[0][1].headers["X-OKF-Token"], undefined);
  assert.equal(calls[0][1].signal, controller.signal);
  await client.comment({ concept: "a", body: "ask" }, { idempotencyKey: "retry" });
  assert.equal(calls[1][1].headers["X-OKF-Token"], "session");
  assert.equal(calls[1][1].headers["Idempotency-Key"], "retry");
  assert.equal(calls[1][1].redirect, "error");
});

test("client preserves conflict details and rejects external routes", async () => {
  const client = createClient({ fetchImpl: async () => new Response('{"conflict":true}', { status: 409 }) });
  await assert.rejects(client.apply({}), (error) => error instanceof StudioError && error.status === 409 && error.payload.conflict);
  await assert.rejects(client.request("https://other.example"), /Invalid studio route/);
});
