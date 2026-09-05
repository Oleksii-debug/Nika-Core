(() => {
  "use strict";

  const statusNode = document.getElementById("app-status");
  const activityLog = document.getElementById("activity-log");
  const keymapBody = document.getElementById("keymap-body");
  const keymapJson = document.getElementById("keymap-json");
  const commandInput = document.getElementById("command-input");
  const tasksList = document.getElementById("tasks-list");
  const agentsList = document.getElementById("agents-list");
  const workspacesList = document.getElementById("workspaces-list");
  const tasksEmpty = document.getElementById("tasks-empty");
  const agentsEmpty = document.getElementById("agents-empty");
  const workspacesEmpty = document.getElementById("workspaces-empty");
  const productProjectEmpty = document.getElementById("product-project-empty");
  const productProjectSummary = document.getElementById("product-project-summary");
  const productProjectFields = Object.freeze({
    title: document.getElementById("product-project-title"),
    project_id: document.getElementById("product-project-id"),
    goal: document.getElementById("product-project-goal"),
    state: document.getElementById("product-project-state"),
    spec_version: document.getElementById("product-project-spec-version"),
    blocker_count: document.getElementById("product-project-blocker-count"),
    status_count: document.getElementById("product-project-status-count"),
    decision_count: document.getElementById("product-project-decision-count"),
  });
  const teamTaskEmpty = document.getElementById("team-task-empty");
  const teamTaskSummary = document.getElementById("team-task-summary");
  const teamMembersList = document.getElementById("team-members-list");
  const teamEventsList = document.getElementById("team-events-list");
  const teamEventsEmpty = document.getElementById("team-events-empty");
  const teamRosterNote = document.getElementById("team-roster-note");
  const teamFinalEmpty = document.getElementById("team-final-empty");
  const teamFinalSummary = document.getElementById("team-final-summary");
  const teamTaskFields = Object.freeze({
    task_id: document.getElementById("team-task-id"),
    command: document.getElementById("team-task-command"),
    task_state: document.getElementById("team-task-state"),
    team_id: document.getElementById("team-id"),
    team_state: document.getElementById("team-state"),
    roster_count: document.getElementById("team-roster-count"),
  });
  const teamFinalFields = Object.freeze({
    status: document.getElementById("team-final-status"),
    text: document.getElementById("team-final-text"),
    task_id: document.getElementById("team-final-task-id"),
    team_id: document.getElementById("team-final-team-id"),
  });
  const productProjectUnavailableMessage = "Стан поточного ProductProject недоступний.";
  const teamTaskUnavailableMessage = "Стан командного завдання недоступний.";
  const teamRoleLabels = Object.freeze({
    supervisor: "Координатор",
    worker: "Виконавець",
    checker: "Перевіряльник",
  });
  const allowedMemberStates = new Set([
    "spawned",
    "running",
    "waiting_approval",
    "paused",
    "completed",
    "failed",
    "cancelled",
  ]);
  const allowedTeamStates = new Set(["active", "completed", "failed", "cancelled"]);
  const allowedOperations = new Set([
    "Очікує підтвердження.",
    "Роботу призупинено.",
    "Роботу завершено.",
    "Роботу завершено з помилкою.",
    "Роботу скасовано.",
    "Очікує запуску.",
    "Координує командне завдання.",
    "Перевіряє результат виконавця.",
    "Виконує командне завдання.",
  ]);
  const eventMessages = Object.freeze({
    "worker.assigned": "Завдання передано виконавцю.",
    "checker.assigned": "Перевірку передано перевіряльнику.",
    "worker.result": "Виконавець зберіг результат операції.",
    "checker.result": "Перевіряльник зберіг результат операції.",
    "worker.error": "Виконавець завершив операцію з помилкою.",
    "checker.error": "Перевіряльник завершив операцію з помилкою.",
  });
  const finalMessages = Object.freeze({
    completed: "Командне завдання завершено; збережені результати учасників доступні.",
    failed: "Командне завдання завершено з помилкою; доступний безпечний стан учасників.",
    cancelled: "Командне завдання скасовано; збережений стан доступний після перезапуску.",
  });
  let actions = [];
  let actionsReady = false;
  let bridgeInitializationStarted = false;
  let statePollHandle = null;
  let teamStateSignature = null;

  function announce(message, assertive = false) {
    statusNode.setAttribute("aria-live", assertive ? "assertive" : "polite");
    statusNode.textContent = message || "Готово.";
  }

  function appendLog(message) {
    if (!message) return;
    const item = document.createElement("li");
    item.textContent = message;
    activityLog.appendChild(item);
  }

  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function isEditable(target) {
    if (!(target instanceof Element)) return false;
    if (target.matches("input, textarea, select")) return true;
    return target instanceof HTMLElement && target.isContentEditable;
  }

  function eventBinding(event) {
    const parts = [];
    if (event.ctrlKey) parts.push("ctrl");
    if (event.altKey) parts.push("alt");
    if (event.shiftKey) parts.push("shift");
    if (event.metaKey) parts.push("win");
    const key = event.key.toLowerCase();
    if (["control", "alt", "shift", "meta"].includes(key)) return null;
    parts.push(key);
    return parts.join("+");
  }

  function normalizedBinding(binding) {
    return String(binding || "").split("+").map((part) => part.trim().toLowerCase()).filter(Boolean).join("+");
  }

  function focusElementById(focusId) {
    if (!focusId) return false;
    const element = document.getElementById(focusId);
    if (!(element instanceof HTMLElement)) return false;
    element.focus({ preventScroll: false });
    return document.activeElement === element;
  }

  function keymapControlId(actionId, control) {
    return `keymap-${control}-${encodeURIComponent(String(actionId))}`;
  }

  function keymapAccessibleActionLabel(action) {
    return `${action.label} (${action.action_id})`;
  }

  function renderItems(list, emptyNode, items, formatter) {
    list.replaceChildren();
    emptyNode.hidden = items.length > 0;
    for (const item of items) {
      const row = document.createElement("li");
      row.textContent = formatter(item);
      list.appendChild(row);
    }
  }

  function validProductProject(project) {
    if (!project || typeof project !== "object" || Array.isArray(project)) return false;
    const stringFields = ["title", "project_id", "goal", "state"];
    if (stringFields.some((field) => typeof project[field] !== "string" || !project[field].trim())) {
      return false;
    }
    if (!Number.isInteger(project.spec_version) || project.spec_version < 1) return false;
    const countFields = ["blocker_count", "status_count", "decision_count"];
    return countFields.every((field) => Number.isInteger(project[field]) && project[field] >= 0);
  }

  function clearProductProjectFields() {
    for (const node of Object.values(productProjectFields)) node.textContent = "";
  }

  function renderProductProjectUnavailable(message) {
    productProjectEmpty.textContent = message || productProjectUnavailableMessage;
    productProjectEmpty.hidden = false;
    productProjectSummary.hidden = true;
    clearProductProjectFields();
  }

  function reportStateUnavailable() {
    renderProductProjectUnavailable(productProjectUnavailableMessage);
    renderTeamTaskUnavailable();
    announce(productProjectUnavailableMessage, true);
    appendLog(productProjectUnavailableMessage);
  }

  function renderProductProject(project) {
    if (project == null) {
      productProjectEmpty.textContent = "Поточний ProductProject не вибрано.";
      productProjectEmpty.hidden = false;
      productProjectSummary.hidden = true;
      clearProductProjectFields();
      return true;
    }
    if (!validProductProject(project)) {
      renderProductProjectUnavailable(
        "Стан поточного ProductProject недоступний або пошкоджений.",
      );
      appendLog("Некоректний bounded ProductProject state відхилено інтерфейсом.");
      return false;
    }
    for (const [field, node] of Object.entries(productProjectFields)) {
      node.textContent = String(project[field]);
    }
    productProjectEmpty.hidden = true;
    productProjectSummary.hidden = false;
    return true;
  }

  function clearTeamTaskFields() {
    for (const node of Object.values(teamTaskFields)) node.textContent = "";
    for (const node of Object.values(teamFinalFields)) node.textContent = "";
    teamMembersList.replaceChildren();
    teamEventsList.replaceChildren();
    teamRosterNote.textContent = "";
    teamEventsEmpty.hidden = false;
    teamFinalEmpty.hidden = false;
    teamFinalSummary.hidden = true;
  }

  function renderTeamTaskUnavailable() {
    teamTaskEmpty.textContent = teamTaskUnavailableMessage;
    teamTaskEmpty.hidden = false;
    teamTaskSummary.hidden = true;
    clearTeamTaskFields();
    teamStateSignature = "unavailable";
  }

  function validTeamMember(member) {
    if (!member || typeof member !== "object" || Array.isArray(member)) return false;
    if (
      typeof member.member_id !== "string"
      || !member.member_id.trim()
      || !(member.role in teamRoleLabels)
      || !allowedMemberStates.has(member.state)
      || !allowedOperations.has(member.current_operation)
    ) {
      return false;
    }
    if (member.safe_error == null) return true;
    return (
      typeof member.safe_error === "object"
      && !Array.isArray(member.safe_error)
      && member.safe_error.code === "member_failed"
    );
  }

  function validTeamEvent(event) {
    return Boolean(
      event
      && typeof event === "object"
      && !Array.isArray(event)
      && typeof event.code === "string"
      && Object.prototype.hasOwnProperty.call(eventMessages, event.code)
      && typeof event.time === "string"
      && event.time.trim(),
    );
  }

  function validFinalResult(result, taskId, teamId) {
    if (result == null) return true;
    return Boolean(
      result
      && typeof result === "object"
      && !Array.isArray(result)
      && Object.prototype.hasOwnProperty.call(finalMessages, result.status)
      && result.task_id === taskId
      && result.team_id === teamId
      && Number.isInteger(result.terminal_member_count)
      && result.terminal_member_count >= 0
      && Number.isInteger(result.result_record_count)
      && result.result_record_count >= 0,
    );
  }

  function validTeamTaskProjection(projection) {
    if (!projection || typeof projection !== "object" || Array.isArray(projection)) return false;
    if (projection.available !== true) return false;
    const { task, team, members, events, final_result: finalResult } = projection;
    if (
      !task
      || typeof task !== "object"
      || Array.isArray(task)
      || typeof task.task_id !== "string"
      || !task.task_id.trim()
      || typeof task.state !== "string"
      || !task.state.trim()
      || (task.command != null && (typeof task.command !== "string" || !task.command.trim()))
    ) {
      return false;
    }
    if (
      !team
      || typeof team !== "object"
      || Array.isArray(team)
      || typeof team.team_id !== "string"
      || !team.team_id.trim()
      || !allowedTeamStates.has(team.state)
      || !Number.isInteger(team.member_count)
      || team.member_count < 2
      || team.member_count > 3
      || team.expected_member_count !== 3
      || typeof team.roster_complete !== "boolean"
      || team.roster_complete !== (team.member_count === 3)
    ) {
      return false;
    }
    if (!Array.isArray(members) || members.length !== team.member_count || !members.every(validTeamMember)) {
      return false;
    }
    const roles = members.map((member) => member.role);
    if (new Set(roles).size !== roles.length || !roles.includes("supervisor") || !roles.includes("worker")) {
      return false;
    }
    if ((team.roster_complete && !roles.includes("checker")) || (!team.roster_complete && roles.includes("checker"))) {
      return false;
    }
    if (!Array.isArray(events) || !events.every(validTeamEvent)) return false;
    return validFinalResult(finalResult, task.task_id, team.team_id);
  }

  function appendDefinitionItem(list, term, value) {
    const dt = document.createElement("dt");
    dt.textContent = term;
    const dd = document.createElement("dd");
    dd.textContent = value;
    list.append(dt, dd);
  }

  function renderTeamMember(member) {
    const item = document.createElement("li");
    const heading = document.createElement("h4");
    heading.textContent = teamRoleLabels[member.role];
    const details = document.createElement("dl");
    appendDefinitionItem(details, "Стан", member.state);
    appendDefinitionItem(details, "Поточна операція", member.current_operation);
    item.append(heading, details);
    if (member.safe_error?.code === "member_failed") {
      const error = document.createElement("p");
      error.textContent = "Виконання учасника завершилося помилкою.";
      item.appendChild(error);
    }
    return item;
  }

  function teamProjectionSignature(projection) {
    if (projection == null) return "none";
    return JSON.stringify({
      task_id: projection.task.task_id,
      team_id: projection.team.team_id,
      team_state: projection.team.state,
      roster_complete: projection.team.roster_complete,
      members: projection.members.map((member) => [
        member.member_id,
        member.role,
        member.state,
        member.safe_error?.code || null,
      ]),
      events: projection.events.map((event) => [event.code, event.time]),
      final_status: projection.final_result?.status || null,
    });
  }

  function renderTeamTask(projection) {
    if (projection == null) {
      const nextSignature = "none";
      const changed = teamStateSignature !== null && teamStateSignature !== nextSignature;
      teamStateSignature = nextSignature;
      teamTaskEmpty.textContent = "Реального командного завдання ще немає.";
      teamTaskEmpty.hidden = false;
      teamTaskSummary.hidden = true;
      clearTeamTaskFields();
      return { ok: true, changed };
    }
    if (
      projection
      && typeof projection === "object"
      && !Array.isArray(projection)
      && projection.available === false
    ) {
      const changed = teamStateSignature !== null && teamStateSignature !== "unavailable";
      renderTeamTaskUnavailable();
      return { ok: true, changed };
    }
    if (!validTeamTaskProjection(projection)) {
      renderTeamTaskUnavailable();
      appendLog("Некоректний bounded team state відхилено інтерфейсом.");
      return { ok: false, changed: false };
    }

    const nextSignature = teamProjectionSignature(projection);
    const changed = teamStateSignature !== null && teamStateSignature !== nextSignature;
    teamStateSignature = nextSignature;
    const { task, team, members, events, final_result: finalResult } = projection;
    teamTaskFields.task_id.textContent = task.task_id;
    teamTaskFields.command.textContent = task.command || "Команда не збережена у bounded projection.";
    teamTaskFields.task_state.textContent = task.state;
    teamTaskFields.team_id.textContent = team.team_id;
    teamTaskFields.team_state.textContent = team.state;
    teamTaskFields.roster_count.textContent = `${team.member_count} з ${team.expected_member_count}`;
    teamRosterNote.textContent = team.roster_complete
      ? "Усі три реальні учасники підтверджені durable state."
      : `Підтверджено ${team.member_count} з ${team.expected_member_count} реальних учасників; відсутня роль не підставляється.`;

    teamMembersList.replaceChildren();
    for (const member of members) teamMembersList.appendChild(renderTeamMember(member));

    teamEventsList.replaceChildren();
    for (const event of events) {
      const item = document.createElement("li");
      item.textContent = `${event.time}: ${eventMessages[event.code]}`;
      teamEventsList.appendChild(item);
    }
    teamEventsEmpty.hidden = events.length > 0;

    if (finalResult == null) {
      teamFinalEmpty.hidden = false;
      teamFinalSummary.hidden = true;
      for (const node of Object.values(teamFinalFields)) node.textContent = "";
    } else {
      teamFinalFields.status.textContent = finalResult.status;
      teamFinalFields.text.textContent = finalMessages[finalResult.status];
      teamFinalFields.task_id.textContent = finalResult.task_id;
      teamFinalFields.team_id.textContent = finalResult.team_id;
      teamFinalEmpty.hidden = true;
      teamFinalSummary.hidden = false;
    }

    teamTaskEmpty.hidden = true;
    teamTaskSummary.hidden = false;
    return { ok: true, changed };
  }

  async function refreshState({ announceTeamTransitions = true } = {}) {
    if (!globalThis.pywebview?.api?.get_state) {
      reportStateUnavailable();
      return false;
    }
    let response;
    try {
      response = await globalThis.pywebview.api.get_state();
    } catch {
      reportStateUnavailable();
      return false;
    }
    if (!response?.ok) {
      reportStateUnavailable();
      return false;
    }
    const state = response.state || {};
    renderItems(tasksList, tasksEmpty, state.tasks || [], (item) => `${item.command || "Без назви"} — ${item.state}`);
    renderItems(agentsList, agentsEmpty, state.agents || [], (item) => `${item.name} — ${item.goal}`);
    renderItems(workspacesList, workspacesEmpty, state.workspaces || [], (item) => `${item.name} — ${item.description || "Без опису"}`);
    const productReady = renderProductProject(state.product_project ?? null);
    const teamRender = renderTeamTask(state.v01_team_task ?? null);
    if (!teamRender.ok) {
      announce(teamTaskUnavailableMessage, true);
      return false;
    }
    if (!productReady) return false;
    if (announceTeamTransitions && teamRender.changed) {
      announce("Стан командного завдання оновлено.");
    }
    return true;
  }

  async function dispatch(actionId, trigger = null) {
    if (!globalThis.pywebview?.api?.dispatch) {
      announce("Міст Nika ще не готовий.", true);
      return;
    }
    const payload = {};
    if (actionId === "task.create") payload.command = commandInput.value.trim();
    const result = await globalThis.pywebview.api.dispatch({ request_id: requestId(), action_id: actionId, payload });
    const failed = result.status === "failed" || result.status === "rejected";
    announce(result.message || (result.status === "completed" ? "Виконано." : result.status), failed);
    appendLog(result.message);
    const stateReady = await refreshState();
    document.documentElement.dataset.nikaReady = stateReady ? "true" : "false";
    const focusId = result.focus_id || (failed ? trigger?.dataset?.errorFocusTarget : trigger?.dataset?.focusTarget);
    if (focusId) focusElementById(focusId);
    else trigger?.focus?.();
  }

  async function refreshKeymap() {
    if (!globalThis.pywebview?.api?.list_actions) {
      actionsReady = false;
      return false;
    }
    actions = await globalThis.pywebview.api.list_actions();
    keymapBody.replaceChildren();
    for (const action of actions) {
      const accessibleActionLabel = keymapAccessibleActionLabel(action);
      const row = document.createElement("tr");
      const labelCell = document.createElement("th");
      labelCell.scope = "row";
      labelCell.textContent = action.label;
      const bindingCell = document.createElement("td");
      const input = document.createElement("input");
      input.type = "text";
      input.id = keymapControlId(action.action_id, "binding");
      input.value = action.binding || "";
      input.dataset.actionId = action.action_id;
      input.setAttribute("aria-label", `Комбінація для ${accessibleActionLabel}`);
      bindingCell.appendChild(input);
      const controlCell = document.createElement("td");
      const save = document.createElement("button");
      const saveFocusId = keymapControlId(action.action_id, "save");
      save.type = "button";
      save.id = saveFocusId;
      save.textContent = action.may_be_unbound ? "Зберегти / очистити" : "Зберегти";
      save.setAttribute(
        "aria-label",
        action.may_be_unbound
          ? `Зберегти або очистити комбінацію для ${accessibleActionLabel}`
          : `Зберегти комбінацію для ${accessibleActionLabel}`,
      );
      save.addEventListener("click", async () => {
        const response = await globalThis.pywebview.api.set_binding(action.action_id, input.value.trim() || null);
        announce(response.message, !response.ok);
        if (response.ok) {
          await refreshKeymap();
          focusElementById(saveFocusId);
        } else input.focus();
      });
      const restore = document.createElement("button");
      const restoreFocusId = keymapControlId(action.action_id, "restore");
      restore.type = "button";
      restore.id = restoreFocusId;
      restore.textContent = "За замовчуванням";
      restore.setAttribute(
        "aria-label",
        `Відновити комбінацію за замовчуванням для ${accessibleActionLabel}`,
      );
      restore.addEventListener("click", async () => {
        const response = await globalThis.pywebview.api.restore_default(action.action_id);
        announce(response.message, !response.ok);
        if (response.ok) {
          await refreshKeymap();
          focusElementById(restoreFocusId);
        }
      });
      controlCell.append(save, document.createTextNode(" "), restore);
      row.append(labelCell, bindingCell, controlCell);
      keymapBody.appendChild(row);
    }
    actionsReady = true;
    return true;
  }

  function startStatePolling() {
    if (statePollHandle !== null || typeof window.setInterval !== "function") return;
    statePollHandle = window.setInterval(async () => {
      if (document.hidden) return;
      const ready = await refreshState();
      document.documentElement.dataset.nikaReady = ready ? "true" : "false";
    }, 1500);
  }

  async function initializeBridge() {
    if (bridgeInitializationStarted) return;
    bridgeInitializationStarted = true;
    announce("Завантаження команд Nika Core…");
    try {
      const ready = await refreshKeymap();
      if (!ready) throw new Error("Action Registry bridge unavailable");
    } catch {
      actionsReady = false;
      bridgeInitializationStarted = false;
      document.documentElement.dataset.nikaReady = "false";
      announce("Не вдалося завантажити команди Nika Core.", true);
      appendLog("Не вдалося ініціалізувати міст Nika Core.");
      return;
    }

    let stateReady = false;
    try {
      stateReady = await refreshState({ announceTeamTransitions: false });
    } catch {
      reportStateUnavailable();
    }
    if (!stateReady) {
      bridgeInitializationStarted = false;
      document.documentElement.dataset.nikaReady = "false";
      return;
    }
    document.documentElement.dataset.nikaReady = "true";
    announce("Nika Core готова до роботи.");
    startStatePolling();
  }

  document.getElementById("keymap-export").addEventListener("click", async () => {
    const response = await globalThis.pywebview.api.export_keymap();
    announce(response.message, !response.ok);
    if (response.ok) {
      keymapJson.value = response.data;
      keymapJson.focus();
    }
  });

  document.getElementById("keymap-import").addEventListener("click", async () => {
    const response = await globalThis.pywebview.api.import_keymap(keymapJson.value);
    announce(response.message, !response.ok);
    if (response.ok) await refreshKeymap();
    else keymapJson.focus();
  });

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest?.("[data-action-id]");
    if (!trigger) return;
    void dispatch(trigger.dataset.actionId, trigger);
  });

  document.addEventListener("keydown", (event) => {
    if (isEditable(event.target)) return;
    if (!actionsReady) return;
    const pressed = eventBinding(event);
    if (!pressed) return;
    const action = actions.find((candidate) => normalizedBinding(candidate.binding) === pressed);
    if (!action) return;
    event.preventDefault();
    void dispatch(action.action_id, event.target instanceof HTMLElement ? event.target : null);
  });

  window.addEventListener("beforeunload", () => {
    if (statePollHandle !== null && typeof window.clearInterval === "function") {
      window.clearInterval(statePollHandle);
      statePollHandle = null;
    }
  });
  window.addEventListener("pywebviewready", () => { void initializeBridge(); });
  if (globalThis.pywebview?.api) void initializeBridge();
})();