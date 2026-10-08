"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");

const app = fs.readFileSync(process.argv[2], "utf8");
const begin = app.indexOf('  document.addEventListener("click", (event) => {');
const end = app.indexOf("\n  });", begin);
assert(begin >= 0 && end > begin, "command delegation must exist");
const snippet = app.slice(begin, end + "\n  });".length);
const listeners = new Map();
const calls = [];
const document = {
  addEventListener(event, callback) { listeners.set(event, callback); },
};
new Function("document", "dispatch", snippet)(
  document,
  (action, target) => { calls.push({ action, target }); },
);
const click = listeners.get("click");
assert.equal(typeof click, "function");

// A keymap editing input carries an action ID for editing, but it must not
// trigger the registered command when focused or clicked.
const keymapField = {
  dataset: { actionId: "task.create" },
  closest(selector) { return selector === "[data-action-id]" ? this : null; },
};
click({ target: keymapField });
assert.equal(calls.length, 0, "clicking a keymap field must be side-effect free");
console.log("PASS: keymap editor never dispatches a task");

// Keyboard Enter/Space activation of an actual button generates a click.
const button = {
  dataset: { actionId: "task.create" },
  closest(selector) { return selector === "button[data-action-id]" ? this : null; },
};
click({ target: button });
assert.equal(calls.length, 1);
assert.equal(calls[0].action, "task.create");
assert.equal(calls[0].target, button);
console.log("PASS: semantic command button dispatches");

// Nested label elements still route to their containing semantic button.
const nested = {
  closest(selector) { return selector === "button[data-action-id]" ? button : null; },
};
click({ target: nested });
assert.equal(calls.length, 2);
assert.equal(calls[1].action, "task.create");
console.log("PASS: child of semantic button dispatches");

// Non-element click targets must remain inert instead of throwing.
click({ target: { nodeType: 3 } });
assert.equal(calls.length, 2);
console.log("PASS: non-element target does not dispatch");
