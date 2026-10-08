"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");

const source = fs.readFileSync(process.argv[2], "utf8");
assert(source.includes("  let stateReadGeneration = 0;"));
const readStart = source.indexOf("  async function refreshState(");
const readEnd = source.indexOf("  async function dispatch(", readStart);
const pollStart = source.indexOf("  function startStatePolling() {");
const pollEnd = source.indexOf("  async function initializeBridge()", pollStart);
assert(readStart > 0 && readEnd > readStart && pollStart > 0 && pollEnd > pollStart);

const readFactory = new Function("ctx",
  "const {globalThis, renderAutostart, reportStateUnavailable, renderSourceSetup, "
  + "renderItems, tasksList, tasksEmpty, renderTaskTargets, agentsList, agentsEmpty, "
  + "workspacesList, workspacesEmpty, renderProductProject, renderTeamTask, announce}=ctx;"
  + "let stateReadGeneration=0, autostartGeneration=0, stateUnavailableReported=false;"
  + source.slice(readStart, readEnd) + "return refreshState;"
);

async function main() {
  const pending = [];
  const displayed = [];
  let outages = 0;
  const refreshState = readFactory({
    globalThis: {pywebview: {api: {get_state: () => new Promise((resolve, reject) => {
      pending.push({resolve, reject});
    })}}},
    renderAutostart: () => {},
    reportStateUnavailable: () => {outages += 1;},
    renderSourceSetup: () => {},
    renderItems: () => {},
    tasksList: {}, tasksEmpty: {},
    renderTaskTargets: (items) => displayed.push(items),
    agentsList: {}, agentsEmpty: {}, workspacesList: {}, workspacesEmpty: {},
    renderProductProject: () => true,
    renderTeamTask: () => ({ok: true, changed: false}),
    announce: () => {},
  });

  let older = refreshState();
  let latest = refreshState();
  pending[1].resolve({ok: true, state: {tasks: [{task_id: "newest"}]}});
  assert.equal(await latest, true);
  pending[0].resolve({ok: true, state: {tasks: [{task_id: "older"}]}});
  assert.equal(await older, null);
  assert.deepEqual(displayed.map((items) => items[0].task_id), ["newest"]);
  console.log("PASS: late older success cannot roll back semantic task state");

  older = refreshState();
  latest = refreshState();
  pending[3].resolve({ok: true, state: {tasks: [{task_id: "recovered"}]}});
  assert.equal(await latest, true);
  pending[2].reject(new Error("STALE_BACKEND_INTERNAL_DETAIL"));
  assert.equal(await older, null);
  assert.equal(outages, 0);
  assert.deepEqual(displayed.map((items) => items[0].task_id), ["newest", "recovered"]);
  console.log("PASS: stale failure cannot replace recovered accessible state");

  older = refreshState();
  latest = refreshState();
  pending[5].resolve({ok: true, state: []});
  assert.equal(await latest, false, "malformed successful envelope must fail closed");
  pending[4].resolve({ok: true, state: {tasks: [{task_id: "stale"}]}});
  assert.equal(await older, null);
  assert.equal(outages, 1);
  assert.equal(displayed.length, 2);
  console.log("PASS: malformed state fails closed without late false recovery");

  const pollFactory = new Function("ctx",
    "const {window, document, refreshState, inFlightActions}=ctx;"
    + "let statePollHandle=null;"
    + source.slice(pollStart, pollEnd)
    + "return startStatePolling;"
  );
  let tick = null;
  let calls = 0;
  let reply = true;
  const inFlightActions = new Set();
  const document = {hidden: false, documentElement: {dataset: {nikaReady: "unknown"}}};
  pollFactory({
    window: {setInterval: (callback) => {tick = callback; return 123;}},
    document, inFlightActions,
    refreshState: async () => {calls += 1; return reply;},
  })();
  inFlightActions.add("task-control");
  await tick();
  assert.equal(calls, 0);
  assert.equal(document.documentElement.dataset.nikaReady, "unknown");
  inFlightActions.clear();
  await tick();
  assert.equal(calls, 1);
  assert.equal(document.documentElement.dataset.nikaReady, "true");
  reply = null;
  await tick();
  assert.equal(document.documentElement.dataset.nikaReady, "true");
  console.log("PASS: polling defers to in-flight command and ignores stale completion");
}

main().catch((error) => {console.error(error); process.exitCode = 1;});
