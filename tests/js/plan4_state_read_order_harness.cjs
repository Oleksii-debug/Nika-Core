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
  + "workspacesList, workspacesEmpty, renderProductProject, renderTeamTask, announce,"
  + "validProductProject,validTeamTaskProjection}=ctx;"
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
    // These small doubles isolate the ordered UI commit from the already
    // separately-tested canonical project/team structural validators.
    validProductProject: (item) => typeof item.title === "string" && item.title.length > 0,
    validTeamTaskProjection: (item) => item.available === true && item.task?.task_id === "valid",
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

  older = refreshState();
  pending[6].resolve({ok: true, state: {tasks: "malformed-not-an-empty-list"}});
  assert.equal(await older, false);
  assert.equal(outages, 2);
  assert.equal(displayed.length, 2, "broken tasks must not clear explicit target selection");
  console.log("PASS: malformed task-list snapshot preserves the last authorized task selection");

  older = refreshState();
  pending[7].resolve({ok: true, state: {tasks: [null]}});
  assert.equal(await older, false);
  assert.equal(outages, 3);
  assert.equal(displayed.length, 2, "null task entry must not clobber target DOM");
  console.log("PASS: malformed individual task projection fails closed without DOM mutation");


  older = refreshState();
  pending[8].resolve({ok: true, state: {
    tasks: [{task_id: "newer"}], agents: [{name: "bad agent"}],
  }});
  assert.equal(await older, false);
  assert.equal(outages, 4);
  assert.equal(displayed.length, 2,
    "malformed agent collection must not replace the keyboard task target");
  console.log("PASS: invalid agent projection cannot partly update task controls");

  older = refreshState();
  pending[9].resolve({ok: true, state: {
    tasks: [{task_id: "newer"}], workspaces: [{name: "space", description: {unsafe: true}}],
  }});
  assert.equal(await older, false);
  assert.equal(outages, 5);
  assert.equal(displayed.length, 2,
    "malformed workspace collection must not replace the keyboard task target");
  console.log("PASS: invalid workspace projection cannot partly update task controls");

  older = refreshState();
  pending[10].resolve({ok: true, state: {
    tasks: [{task_id: "duplicate"}, {task_id: "duplicate"}],
  }});
  assert.equal(await older, false);
  assert.equal(outages, 6);
  assert.equal(displayed.length, 2, "duplicate IDs must not change the explicit target");
  console.log("PASS: duplicate task identities cannot create ambiguous keyboard targets");

  older = refreshState();
  pending[11].resolve({ok: true, state: {
    tasks: [{task_id: "newer"}], product_project: {bad: "projection"},
  }});
  assert.equal(await older, false);
  assert.equal(outages, 7);
  assert.equal(displayed.length, 2, "bad product state must not partly publish tasks");
  console.log("PASS: malformed project sibling fails before task control DOM changes");

  older = refreshState();
  pending[12].resolve({ok: true, state: {
    tasks: [{task_id: "newer"}], v01_team_task: {available: true, bad: true},
  }});
  assert.equal(await older, false);
  assert.equal(outages, 8);
  assert.equal(displayed.length, 2, "bad team state must not partly publish tasks");
  console.log("PASS: malformed team sibling fails before task control DOM changes");

  older = refreshState();
  pending[13].resolve({ok: true, state: {
    tasks: [{task_id: "safe-after-corruption"}], product_project: null, v01_team_task: null,
  }});
  assert.equal(await older, true);
  assert.equal(outages, 8);
  assert.equal(displayed.length, 3);
  assert.equal(displayed[2][0].task_id, "safe-after-corruption");
  console.log("PASS: clean snapshot recovers semantic task state after rejected siblings");

  older = refreshState();
  pending[14].resolve({ok: true, state: {
    tasks: [{task_id: "foreign", workspace_id: "another-workspace", agent_id: "nika.default"}],
  }});
  assert.equal(await older, false);
  assert.equal(outages, 9);
  assert.equal(displayed.length, 3);
  console.log("PASS: foreign-workspace task is not announced through desktop state");

  older = refreshState();
  pending[15].resolve({ok: true, state: {
    tasks: [{task_id: "foreign-agent", workspace_id: "default", agent_id: "other-agent"}],
  }});
  assert.equal(await older, false);
  assert.equal(outages, 10);
  assert.equal(displayed.length, 3);
  console.log("PASS: foreign-agent task is not announced through desktop state");

  older = refreshState();
  pending[16].resolve({ok: true, state: {
    tasks: [{task_id: "authorized", workspace_id: "default", agent_id: "nika.default"}],
  }});
  assert.equal(await older, true);
  assert.equal(outages, 10);
  assert.equal(displayed.length, 4);
  assert.equal(displayed[3][0].task_id, "authorized");
  console.log("PASS: authorized local task resumes after foreign task rejection");

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
