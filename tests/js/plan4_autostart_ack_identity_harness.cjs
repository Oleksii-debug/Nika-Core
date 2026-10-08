"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");

const app = fs.readFileSync(process.argv[2], "utf8");
const start = app.indexOf("  function renderAutostart(snapshot) {");
const end = app.indexOf("  function renderSourceSetup(selection) {", start);
assert(start >= 0 && end > start, "autostart source boundary must exist");
const snippet = app.slice(start, end);
const factory = new Function("context", `
  const {
    autostartInput, autostartSave, autostartStatus, globalThis, requestId,
    announce, appendLog, refreshState, renderStateUnavailable, focusElementById,
  } = context;
  let autostartPending = false;
  let autostartEffectUncertain = false;
  let autostartGeneration = 0;
  let autostartDirty = true;
  ${snippet}
  return {
    dispatchAutostart, renderAutostart,
    dirty: () => autostartDirty,
    uncertain: () => autostartEffectUncertain,
  };
`);

function makeHarness(reply) {
  const requests = [];
  const messages = [];
  let sequence = 0;
  let ui;
  const autostartInput = {
    checked: true, disabled: false,
    addEventListener() {}, focus() {},
  };
  const autostartSave = { disabled: false };
  const autostartStatus = { textContent: "" };
  const context = {
    autostartInput, autostartSave, autostartStatus,
    globalThis: {pywebview: {api: {dispatch: async (request) => {
      requests.push(request);
      return reply(request, requests.length);
    }}}},
    requestId: () => `req-${++sequence}`,
    announce: (message, failed) => { messages.push([message, failed]); },
    appendLog: () => {},
    refreshState: async () => {
      ui.renderAutostart({schema_version: 1, state: "disabled", can_change: true});
      return true;
    },
    focusElementById: () => true,
  };
  ui = factory(context);
  return {ui, requests, messages, autostartInput, autostartSave, autostartStatus};
}

async function main() {
  const save = "settings.autostart.configure";
  const refresh = "settings.autostart.refresh";

  const clean = makeHarness((request) => ({
    request_id: request.request_id, status: "completed", message: "Збережено.",
  }));
  await clean.ui.dispatchAutostart(save);
  assert.equal(clean.requests[0].payload.enabled, true);
  assert.equal(clean.ui.dirty(), false);
  assert.equal(clean.ui.uncertain(), false);
  assert.equal(clean.autostartSave.disabled, false);
  console.log("PASS: correlated successful OS write unlocks after state readback");

  const swapped = makeHarness(() => ({
    request_id: "stale-other-request", status: "completed", message: "FALSA_ACK",
  }));
  await swapped.ui.dispatchAutostart(save);
  assert.equal(swapped.ui.uncertain(), true);
  assert.equal(swapped.ui.dirty(), true);
  assert.equal(swapped.autostartSave.disabled, true);
  assert(!JSON.stringify(swapped.messages).includes("FALSA_ACK"));
  await swapped.ui.dispatchAutostart(save);
  assert.equal(swapped.requests.length, 1);
  assert(swapped.autostartStatus.textContent.includes("заблоковано"));
  console.log("PASS: misrouted success never confirms or repeats Windows write");

  await swapped.ui.dispatchAutostart(refresh);
  assert.equal(swapped.requests.length, 2, "read-only refresh remains available");
  assert.equal(swapped.requests[1].action_id, refresh);
  assert.equal(swapped.autostartSave.disabled, true);
  console.log("PASS: read-only refresh cannot clear uncertain-write lock");

  const missing = makeHarness(() => ({status: "completed", message: "bad"}));
  await missing.ui.dispatchAutostart(save);
  assert.equal(missing.ui.uncertain(), true);
  assert.equal(missing.ui.dirty(), true);
  console.log("PASS: missing acknowledgement identity fails closed");

  const rejected = makeHarness((request, n) => ({
    request_id: request.request_id,
    status: n === 1 ? "rejected" : "completed",
    message: "Стан.",
  }));
  await rejected.ui.dispatchAutostart(save);
  assert.equal(rejected.ui.uncertain(), false);
  assert.equal(rejected.ui.dirty(), true);
  await rejected.ui.dispatchAutostart(save);
  assert.equal(rejected.requests.length, 2);
  assert.equal(rejected.ui.dirty(), false);
  console.log("PASS: verified pre-effect rejection permits safe correction and retry");

  const failed = makeHarness((request) => ({
    request_id: request.request_id, status: "failed", message: "Помилка.",
  }));
  await failed.ui.dispatchAutostart(save);
  assert.equal(failed.ui.uncertain(), true);
  await failed.ui.dispatchAutostart(save);
  assert.equal(failed.requests.length, 1);
  console.log("PASS: failed handler is treated as possibly committed OS write");

  const disconnected = makeHarness(() => {
    throw new Error("SECRET_OS_TRANSPORT_TOKEN");
  });
  await disconnected.ui.dispatchAutostart(save);
  assert.equal(disconnected.ui.uncertain(), true);
  assert(!JSON.stringify(disconnected.messages).includes("SECRET_OS_TRANSPORT_TOKEN"));
  console.log("PASS: transport failure locks duplicate OS write and redacts error");
}

main().catch((error) => {console.error(error); process.exitCode = 1;});
