"""Execute the packaged Web client in Node with fake DOM and streamed responses.

This tests browser-side bounds/correlation without claiming a real browser or NVDA pass.
"""
from __future__ import annotations

import shutil
import subprocess
from importlib.resources import files

import pytest


_NODE_PROOF = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");

(async () => {
  const scenario = process.argv[1];
  const script = fs.readFileSync(process.argv[2], "utf8");
  let submit;
  let fetchCalls = 0;
  let cancelCalls = 0;
  class Element {
    constructor() {
      this.textContent = "";
      this.value = "";
      this.hidden = true;
      this.disabled = false;
      this.attrs = {};
    }
    addEventListener(type, callback) {
      assert.equal(type, "submit");
      submit = callback;
    }
    setAttribute(name, value) { this.attrs[name] = value; }
    removeAttribute(name) { delete this.attrs[name]; }
    focus() {}
  }
  const nodes = {};
  for (const id of [
    "inspect-form", "task-id", "inspect-submit", "app-status",
    "task-details", "result-id", "result-state"
  ]) nodes[id] = new Element();
  nodes["task-id"].value = "task-123";

  const result = {
    request_id: scenario === "mismatch" ? "other-request" : "fixed-request",
    status: "completed",
    code: "ok",
    data: { task_id: "task-123", state: "CREATED" }
  };
  let responseBytes = Buffer.from(JSON.stringify(result), "utf8");
  if (scenario === "oversize") responseBytes = Buffer.alloc(81921, 65);
  if (scenario === "invalid-utf8") responseBytes = Buffer.from([255]);
  const contentType = scenario === "wrong-mime" ? "text/html" : "application/json";
  const lengthHeader = scenario === "declared-oversize" ? "999999" : null;
  let readCalls = 0;
  const fetch = async (url, init) => {
    fetchCalls++;
    assert.equal(url, "/v1/commands");
    assert.equal(init.credentials, "omit");
    assert.equal(init.redirect, "error");
    assert.equal(JSON.parse(init.body).request_id, "fixed-request");
    return {
      ok: true,
      headers: { get: name => name === "content-type" ? contentType :
        name === "content-length" ? lengthHeader : null },
      body: { getReader: () => ({
        async read() {
          if (readCalls++ === 0) {
            return { done: false, value: Uint8Array.from(responseBytes) };
          }
          return { done: true };
        },
        async cancel() { cancelCalls++; },
        releaseLock() {}
      }) }
    };
  };
  vm.runInNewContext(script, {
    document: { getElementById: id => nodes[id] },
    crypto: { randomUUID: () => "fixed-request" },
    fetch, Uint8Array, TextDecoder, AbortController, setTimeout, clearTimeout
  }, { filename: "packaged-client.js" });
  assert.equal(typeof submit, "function");
  await submit({ preventDefault() {} });
  const visible = !nodes["task-details"].hidden;
  assert.equal(visible, scenario === "ok");
  assert.equal(fetchCalls, 1);
  assert.equal(nodes["inspect-submit"].disabled, false);
  assert.ok(!("aria-busy" in nodes["inspect-form"].attrs));
  if (scenario === "ok") {
    assert.equal(nodes["result-state"].textContent, "CREATED");
    assert.equal(nodes["result-id"].textContent, "task-123");
  }
  if (scenario === "mismatch") {
    assert.match(nodes["app-status"].textContent, /Стан недоступний/u);
  }
  if (scenario === "oversize") assert.equal(cancelCalls, 1);
  console.log("PASS", scenario);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(
    "scenario",
    ["ok", "mismatch", "oversize", "declared-oversize", "invalid-utf8", "wrong-mime"],
)
def test_packaged_web_client_bounds_and_correlates_response(scenario: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for actual Web JavaScript runtime proof")
    asset = files("nika_core.web_api.client").joinpath("client.js")
    completed = subprocess.run(
        [node, "-e", _NODE_PROOF, scenario, str(asset)],
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
    )
    assert completed.returncode == 0, (
        f"Node Web client {scenario} failed: {completed.stdout} {completed.stderr}"
    )
    assert "PASS" in completed.stdout
