(() => {
  "use strict";

  const statusNode = document.getElementById("app-status");
  const activityLog = document.getElementById("activity-log");
  const keymapBody = document.getElementById("keymap-body");
  const keymapJson = document.getElementById("keymap-json");
  const commandInput = document.getElementById("command-input");
  const sourceInputs = Object.freeze({
    root: document.getElementById("source-root"),
    source_a: document.getElementById("source-a"),
    source_b: document.getElementById("source-b"),
  });
  const sourceStatus = document.getElementById("source-setup-status");
  let sourceRevision = 0;
  let sourceDirty = false;
  const autostartInput = document.getElementById("autostart-enabled");
  const autostartSave = document.getElementById("autostart-save");
  const autostartStatus = document.getElementById("autostart-status");
  let autostartDirty = false;
  let autostartPending = false;
  let autostartGeneration = 0;
  const tasksList = document.getElementById("tasks-list");
  const taskTarget = document.getElementById("task-target");
  let taskTargetSignature = null;
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
  // One outstanding durable task command per UI session: a second click must not mint a new request ID.
  const taskMutationActions = new Set(["task.create", "task.pause", "task.resume", "agent.stop"]);
  const inFlightActions = new Set();
  let keymapMutationPending = false;
  let bridgeInitializationStarted = false;
  let statePollHandle = null;
  let stateReadGeneration = 0;
  let teamStateSignature = null;
  let stateUnavailableReported = false;
  const maxActivityItems = 200;

  function announce(message, assertive = false) {
    statusNode.setAttribute("aria-live", assertive ? "assertive" : "polite");
    statusNode.textContent = message || "Готово.";
  }

  function appendLog(message) {
    if (!message || activityLog.lastElementChild?.textContent === message) return;
    const item = document.createElement("li");
    item.textContent = message;
    activityLog.appendChild(item);
    if (activityLog.childElementCount > maxActivityItems) {
      activityLog.firstElementChild.remove();
      activityLog.setAttribute("aria-label", "Журнал активності: останні 200 повідомлень");
    }
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

  function renderTaskTargets(items) {
    if (!taskTarget) return;
    const targets = Array.isArray(items) ? items.filter(
      (item) => item && typeof item.task_id === "string"
        && item.task_id.length > 0
        && item.workspace_id === "default"
        && item.agent_id === "nika.default"
    ).map((item) => ({
      id: item.task_id,
      label: `${typeof item.command === "string" ? item.command.slice(0, 120) : "Без назви"} — ${typeof item.state === "string" ? item.state : "невідомий стан"} — ${item.task_id}`,
    })) : [];
    const signature = JSON.stringify(targets);
    if (taskTargetSignature === signature) return;
    const previousId = taskTarget.value;
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Без явного вибору — тільки одне відповідне завдання";
    const options = [placeholder];
    for (const item of targets) {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.label;
      options.push(option);
    }
    taskTarget.replaceChildren(...options);
    if (targets.some((item) => item.id === previousId)) taskTarget.value = previousId;
    taskTargetSignature = signature;
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
    if (stateUnavailableReported) return;
    stateUnavailableReported = true;
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
    const memberIds = members.map((member) => member.member_id);
    if (new Set(memberIds).size !== memberIds.length) return false;
    const count = (role) => roles.filter((item) => item === role).length;
    const legacyRoster = count("supervisor") === 1 && count("worker") === 1
      && count("checker") === (team.roster_complete ? 1 : 0);
    const sourceRoster = count("supervisor") === 0 && count("checker") === 1
      && count("worker") === (team.roster_complete ? 2 : 1);
    if (!legacyRoster && !sourceRoster) return false;
    if (!team.roster_complete
        && (team.state === "completed" || finalResult?.status === "completed")) return false;
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

  function renderAutostart(snapshot) {
    if (!autostartInput || !autostartSave || !autostartStatus || autostartPending) return;
    const messages = {
      enabled: "Автозапуск увімкнено для цього застосунку.",
      disabled: "Автозапуск вимкнено.",
      stale: "Збережено застарілий або інший запис автозапуску. Позначте прапорець і збережіть, щоб прив’язати поточний застосунок, або зніміть позначку і збережіть, щоб прибрати запис.",
      unavailable: "Автозапуск доступний лише у зібраному застосунку Windows.",
      error: "Не вдалося прочитати автозапуск. Перечитайте стан або перевірте доступ Windows.",
    };
    const valid = snapshot?.schema_version === 1
      && Object.hasOwn(messages, snapshot.state)
      && snapshot.can_change === ["enabled", "disabled", "stale"].includes(snapshot.state);
    const current = valid ? snapshot.state : "error";
    const canChange = valid && snapshot.can_change;
    autostartInput.disabled = !canChange;
    autostartSave.disabled = !canChange;
    if (!canChange) autostartDirty = false;
    if (!autostartDirty) autostartInput.checked = current === "enabled";
    autostartStatus.textContent = messages[current]
      + (autostartDirty ? " Позначку змінено, але ще не збережено." : "");
  }

  autostartInput?.addEventListener("change", () => {
    autostartDirty = true;
    autostartStatus.textContent = "Позначку змінено, але ще не збережено. Натисніть «Зберегти автозапуск».";
  });

  async function dispatchAutostart(actionId, trigger) {
    if (autostartPending) return;
    const save = actionId === "settings.autostart.configure";
    if (save && (!autostartInput || autostartInput.disabled)) return;
    const payload = save ? { enabled: autostartInput.checked } : {};
    autostartPending = true;
    autostartGeneration += 1;
    autostartInput.disabled = true;
    autostartSave.disabled = true;
    try {
      const result = await globalThis.pywebview.api.dispatch({ request_id: requestId(), action_id: actionId, payload });
      if (!["completed", "failed", "rejected"].includes(result?.status)) throw new Error("Invalid acknowledgement");
      const failed = result.status !== "completed";
      if (!failed || !save) autostartDirty = false;
      announce(result.message, failed);
      appendLog(result.message);
    } catch {
      // The OS write may have completed before the bridge disconnected. No blind retry.
      announce("Немає підтвердження зміни автозапуску. Перечитайте стан перед повтором.", true);
    } finally {
      autostartPending = false;
      autostartGeneration += 1;
      if (!await refreshState({ announceTeamTransitions: false })) renderAutostart(null);
      if (!autostartInput.disabled) autostartInput.focus();
      else if (trigger && !trigger.disabled) trigger.focus();
      else focusElementById("autostart-heading");
    }
  }

  function renderSourceSetup(selection) {
    if (!sourceStatus || selection == null) return;
    if (!["ready", "missing"].includes(selection.status)
        || !Number.isSafeInteger(selection.revision) || selection.revision < 0
        || !Object.keys(sourceInputs).every((key) => typeof selection[key] === "string")) {
      sourceStatus.textContent = "Налаштування джерел недоступні або несумісні.";
      return;
    }
    sourceStatus.textContent = selection.status === "ready"
      ? "Джерела збережено. Можна створити нове командне завдання."
      : "Спочатку вкажіть папку та два файли й натисніть «Зберегти джерела».";
    if (sourceDirty) return;
    sourceRevision = selection.revision;
    for (const [key, input] of Object.entries(sourceInputs)) {
      if (input) input.value = selection[key];
    }
  }

  for (const input of Object.values(sourceInputs)) {
    input?.addEventListener("input", () => { sourceDirty = true; });
  }
  document.getElementById("source-reload")?.addEventListener("click", async () => {
    sourceDirty = false;
    if (await refreshState()) announce("Збережені налаштування перечитано.");
  });

  async function refreshState({ announceTeamTransitions = true } = {}) {
    // A stale poll must never overwrite a more recent task-control readback.
    const readGeneration = ++stateReadGeneration;
    const autostartReadGeneration = autostartGeneration;
    if (!globalThis.pywebview?.api?.get_state) {
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      reportStateUnavailable();
      return false;
    }
    let response;
    try {
      response = await globalThis.pywebview.api.get_state();
    } catch {
      if (readGeneration !== stateReadGeneration) return null;
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      reportStateUnavailable();
      return false;
    }
    if (readGeneration !== stateReadGeneration) return null;
    if (response?.ok !== true || !response.state || typeof response.state !== "object"
        || Array.isArray(response.state)) {
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      reportStateUnavailable();
      return false;
    }
    const state = response.state || {};
    if (autostartReadGeneration === autostartGeneration) renderAutostart(state.autostart ?? null);
    renderSourceSetup(state.v01_sources ?? null);
    const taskItems = Array.isArray(state.tasks) ? state.tasks : [];
    renderItems(tasksList, tasksEmpty, taskItems, (item) => `${item.command || "Без назви"} — ${item.state}`);
    renderTaskTargets(taskItems);
    renderItems(agentsList, agentsEmpty, state.agents || [], (item) => `${item.name} — ${item.goal}`);
    renderItems(workspacesList, workspacesEmpty, state.workspaces || [], (item) => `${item.name} — ${item.description || "Без опису"}`);
    const productReady = renderProductProject(state.product_project ?? null);
    const teamRender = renderTeamTask(state.v01_team_task ?? null);
    if (!teamRender.ok) {
      announce(teamTaskUnavailableMessage, true);
      return false;
    }
    if (!productReady) return false;
    stateUnavailableReported = false;
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
    if (["settings.autostart.configure", "settings.autostart.refresh"].includes(actionId)) {
      await dispatchAutostart(actionId, trigger);
      return;
    }
    // Group task controls: pause/resume/stop must not race an unacknowledged task creation.
    const durableMutation = taskMutationActions.has(actionId) || actionId === "team.sources.configure";
    const lockKey = taskMutationActions.has(actionId) ? "task-control" : actionId;
    if (inFlightActions.has(lockKey)) {
      announce("Попередню команду ще обробляють. Дочекайтеся підтвердження.", false);
      return;
    }
    inFlightActions.add(lockKey);
    let keepLocked = false;
    const reconcileUncertain = async (message) => {
      announce(message, true);
      appendLog(message);
      let stateReady = false;
      try {
        stateReady = await refreshState();
      } catch {
        reportStateUnavailable();
      }
      document.documentElement.dataset.nikaReady = stateReady ? "true" : "false";
      if (durableMutation) keepLocked = true;
      if (stateReady) {
        const reconciled = durableMutation
          ? "Стан перечитано після непідтвердженої дії. Повтор заблоковано до перезапуску вікна; перевірте результат."
          : "Стан перечитано після непідтвердженої дії. Перевірте результат перед повтором.";
        announce(reconciled, true);
        appendLog(reconciled);
      } else {
        keepLocked = true;
        announce(
          "Немає безпечного підтвердження поточного стану. Повтор цієї дії заблоковано до перезапуску вікна.",
          true,
        );
      }
      trigger?.focus?.();
    };
    try {
      const payload = {};
      if (actionId === "task.create") payload.command = commandInput.value.trim();
      if (["task.pause", "task.resume", "agent.stop"].includes(actionId) && taskTarget?.value) {
        payload.task_id = taskTarget.value;
      }
      if (actionId === "team.sources.configure") {
        payload.revision = sourceRevision;
        for (const [key, input] of Object.entries(sourceInputs)) payload[key] = input?.value ?? "";
      }
      let result;
      try {
        result = await globalThis.pywebview.api.dispatch({
          request_id: requestId(), action_id: actionId, payload,
        });
      } catch {
        // The durable effect may have committed before the bridge disconnected. Never retry blindly.
        await reconcileUncertain(
          "Немає підтвердження виконання дії. Стан буде перечитано перед можливим повтором.",
        );
        return;
      }
      if (!result || !["accepted", "completed", "failed", "rejected"].includes(result.status)) {
        await reconcileUncertain(
          "Міст повернув непідтверджений результат. Стан буде перечитано перед можливим повтором.",
        );
        return;
      }
      const failed = ["failed", "rejected"].includes(result.status);
      const message = typeof result.message === "string" && result.message
        ? result.message
        : (failed
          ? "Дію відхилено."
          : (result.status === "accepted" ? "Дію прийнято до виконання." : "Виконано."));
      if (actionId === "team.sources.configure" && result.status === "completed") {
        sourceDirty = false;
      }
      announce(message, failed);
      appendLog(message);
      let stateReady = false;
      try {
        stateReady = await refreshState();
      } catch {
        reportStateUnavailable();
      }
      document.documentElement.dataset.nikaReady = stateReady ? "true" : "false";
      if (!stateReady) {
        if (!failed && durableMutation) keepLocked = true;
        announce(
          failed
            ? "Не вдалося оновити стан після відхиленої дії. Причина є в журналі."
            : (result.status === "accepted"
              ? (durableMutation
                ? "Дію прийнято, але оновлений стан недоступний. Повтор заблоковано до перезапуску вікна."
                : "Дію прийнято, але оновлений стан недоступний. Не повторюйте її без перевірки.")
              : (durableMutation
                ? "Дію підтверджено, але оновлений стан недоступний. Повтор заблоковано до перезапуску вікна."
                : "Дію підтверджено, але оновлений стан недоступний. Перечитайте стан.")),
          true,
        );
      }
      const focusId = result.focus_id
        || (failed ? trigger?.dataset?.errorFocusTarget : trigger?.dataset?.focusTarget);
      if (focusId) focusElementById(focusId);
      else trigger?.focus?.();
    } finally {
      if (!keepLocked) inFlightActions.delete(lockKey);
    }
  }

  async function mutateKeymap(operation, focusId = null, failureTarget = null) {
    if (keymapMutationPending) {
      announce("Зміна карти клавіш ще виконується. Дочекайтеся підтвердження.", false);
      return;
    }
    keymapMutationPending = true;
    let keepPending = false;
    const reconcileUncertainKeymap = async (message) => {
      announce(message, true);
      appendLog(message);
      let keymapReady = false;
      try {
        keymapReady = await refreshKeymap();
      } catch {
        keymapReady = false;
      }
      if (keymapReady) {
        const reconciled = "Карту клавіш перечитано після непідтвердженої зміни. Перевірте її перед повтором.";
        announce(reconciled, true);
        appendLog(reconciled);
      } else {
        actionsReady = false;
        keepPending = true;
        announce(
          "Немає безпечного підтвердження карти клавіш. Повтор змін заблоковано до перезапуску вікна.",
          true,
        );
      }
      failureTarget?.focus?.();
    };
    try {
      let response;
      try {
        response = await operation();
      } catch {
        await reconcileUncertainKeymap(
          "Немає підтвердження зміни клавіш. Карта буде перечитана перед можливим повтором.",
        );
        return;
      }
      if (!response || typeof response.ok !== "boolean") {
        await reconcileUncertainKeymap(
          "Міст повернув непідтверджену зміну клавіш. Карта буде перечитана перед можливим повтором.",
        );
        return;
      }
      const message = typeof response.message === "string" && response.message
        ? response.message : (response.ok ? "Зміни збережено." : "Зміну відхилено.");
      announce(message, !response.ok);
      appendLog(message);
      if (!response.ok) {
        failureTarget?.focus?.();
        return;
      }
      try {
        if (!await refreshKeymap()) throw new Error("keymap unavailable");
      } catch {
        actionsReady = false;
        keepPending = true;
        announce(
          "Зміну підтверджено, але карту клавіш не вдалося перечитати. Повтор змін заблоковано до перезапуску вікна.",
          true,
        );
        failureTarget?.focus?.();
        return;
      }
      if (focusId) focusElementById(focusId);
    } finally {
      if (!keepPending) keymapMutationPending = false;
    }
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
        await mutateKeymap(
          () => globalThis.pywebview.api.set_binding(action.action_id, input.value.trim() || null),
          saveFocusId,
          input,
        );
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
        await mutateKeymap(
          () => globalThis.pywebview.api.restore_default(action.action_id),
          restoreFocusId,
          restore,
        );
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
      // A durable command's post-acknowledgement reconciliation takes priority.
      if (document.hidden || inFlightActions.size > 0) return;
      const ready = await refreshState();
      if (ready !== null) {
        document.documentElement.dataset.nikaReady = ready ? "true" : "false";
      }
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
    if (keymapMutationPending) {
      announce("Дочекайтеся збереження карти клавіш перед експортом.");
      return;
    }
    try {
      const response = await globalThis.pywebview.api.export_keymap();
      if (!response || typeof response.ok !== "boolean"
          || (response.ok && typeof response.data !== "string")) {
        throw new Error("invalid keymap export acknowledgement");
      }
      const message = typeof response.message === "string" && response.message
        ? response.message : (response.ok ? "Карту експортовано." : "Не вдалося експортувати карту.");
      announce(message, !response.ok);
      if (response.ok) {
        keymapJson.value = response.data;
        keymapJson.focus();
      }
    } catch {
      announce("Не вдалося підтвердити експорт карти клавіш. Повторіть після перевірки мосту.", true);
    }
  });

  document.getElementById("keymap-import").addEventListener("click", async () => {
    await mutateKeymap(
      () => globalThis.pywebview.api.import_keymap(keymapJson.value),
      null,
      keymapJson,
    );
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
