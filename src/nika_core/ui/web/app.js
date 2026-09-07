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
  const modelInputs = Object.freeze({
    route_kind: document.getElementById("model-route-kind"),
    provider_id: document.getElementById("model-provider"),
    model: document.getElementById("model-name"),
    base_url: document.getElementById("model-base-url"),
    credential_ref: document.getElementById("model-credential-ref"),
    private_data_allowed: document.getElementById("model-private-data"),
    timeout_seconds: document.getElementById("model-timeout"),
  });
  const modelStatus = document.getElementById("model-settings-status");
  const modelSave = document.getElementById("model-save");
  let modelRevision = 0;
  let modelDirty = false;
  let modelPending = false;
  let modelGeneration = 0;
  const autostartInput = document.getElementById("autostart-enabled");
  const autostartSave = document.getElementById("autostart-save");
  const autostartStatus = document.getElementById("autostart-status");
  let autostartDirty = false;
  let autostartPending = false;
  let autostartGeneration = 0;
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
    renderModelSettings(null);
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

  function setModelControlsDisabled(disabled) {
    for (const input of Object.values(modelInputs)) {
      if (input) input.disabled = disabled;
    }
    if (modelSave) modelSave.disabled = disabled;
  }

  function applyModelRouteControls(disabled = false) {
    const route = modelInputs.route_kind?.value;
    const local = route === "ollama";
    if (modelInputs.route_kind) modelInputs.route_kind.disabled = disabled;
    if (modelInputs.model) modelInputs.model.disabled = disabled;
    if (modelInputs.base_url) modelInputs.base_url.disabled = disabled;
    if (modelInputs.timeout_seconds) modelInputs.timeout_seconds.disabled = disabled;
    if (modelInputs.provider_id) modelInputs.provider_id.disabled = disabled || local;
    if (modelInputs.credential_ref) modelInputs.credential_ref.disabled = disabled || local;
    if (modelInputs.private_data_allowed) {
      modelInputs.private_data_allowed.disabled = disabled || local;
    }
    if (modelSave) modelSave.disabled = disabled;
    if (local && !disabled) {
      if (modelInputs.provider_id) modelInputs.provider_id.value = "ollama";
      if (modelInputs.credential_ref) modelInputs.credential_ref.value = "";
      if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = true;
    }
  }

  function validModelSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return false;
    if (snapshot.status === "invalid") return true;
    if (snapshot.status === "missing") {
      return snapshot.revision === 0;
    }
    if (snapshot.status !== "ready") return false;
    if (!Number.isSafeInteger(snapshot.revision) || snapshot.revision < 1) return false;
    if (!["ollama", "openai_compatible"].includes(snapshot.route_kind)) return false;
    if (typeof snapshot.provider_id !== "string" || !snapshot.provider_id.trim()) return false;
    if (typeof snapshot.model !== "string" || !snapshot.model.trim()) return false;
    if (typeof snapshot.base_url !== "string" || !snapshot.base_url.trim()) return false;
    if (
      typeof snapshot.timeout_seconds !== "number"
      || !Number.isFinite(snapshot.timeout_seconds)
      || snapshot.timeout_seconds <= 0
      || snapshot.timeout_seconds > 600
    ) return false;
    if (typeof snapshot.private_data_allowed !== "boolean") return false;
    if (typeof snapshot.credential_configured !== "boolean") return false;
    if (snapshot.route_kind === "ollama") {
      return snapshot.provider_id === "ollama"
        && snapshot.credential_configured === false
        && snapshot.private_data_allowed === true;
    }
    return snapshot.provider_id !== "ollama" && snapshot.credential_configured === true;
  }

  function defaultModelDraft() {
    if (modelInputs.route_kind) modelInputs.route_kind.value = "ollama";
    if (modelInputs.provider_id) modelInputs.provider_id.value = "ollama";
    if (modelInputs.model) modelInputs.model.value = "";
    if (modelInputs.base_url) modelInputs.base_url.value = "http://localhost:11434";
    if (modelInputs.credential_ref) modelInputs.credential_ref.value = "";
    if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = true;
    if (modelInputs.timeout_seconds) modelInputs.timeout_seconds.value = "60";
  }

  function renderModelSettings(snapshot) {
    if (!modelStatus || modelPending) return;
    const valid = validModelSnapshot(snapshot);
    if (!valid || snapshot?.status === "invalid") {
      if (!modelDirty) modelRevision = 0;
      setModelControlsDisabled(true);
      modelStatus.textContent = snapshot?.status === "invalid"
        ? "Збережені налаштування моделі пошкоджені або несумісні. Нові завдання з моделлю заблоковано."
        : "Не вдалося прочитати налаштування моделі. Перечитайте стан перед зміною.";
      return;
    }

    if (snapshot.status === "missing") {
      if (!modelDirty) {
        modelRevision = 0;
        defaultModelDraft();
      }
      applyModelRouteControls(false);
      modelStatus.textContent = modelDirty
        ? "Модель змінено, але ще не збережено."
        : "Модель для нових завдань ще не вибрано. Вкажіть назву моделі й збережіть.";
      return;
    }

    if (modelDirty && snapshot.revision !== modelRevision) {
      setModelControlsDisabled(false);
      applyModelRouteControls(false);
      if (modelSave) modelSave.disabled = true;
      modelStatus.textContent = "Збережені налаштування змінилися в іншому вікні. Натисніть «Перечитати модель» перед збереженням.";
      return;
    }

    if (!modelDirty) {
      modelRevision = snapshot.revision;
      modelInputs.route_kind.value = snapshot.route_kind;
      modelInputs.provider_id.value = snapshot.provider_id;
      modelInputs.model.value = snapshot.model;
      modelInputs.base_url.value = snapshot.base_url;
      modelInputs.credential_ref.value = "";
      modelInputs.private_data_allowed.checked = snapshot.private_data_allowed;
      modelInputs.timeout_seconds.value = String(snapshot.timeout_seconds);
    }
    applyModelRouteControls(false);
    const credentialNote = snapshot.route_kind === "openai_compatible"
      ? " Посилання на змінну середовища налаштовано, але навмисно не показується; для зміни API-маршруту введіть env:НАЗВА знову."
      : "";
    modelStatus.textContent = modelDirty
      ? "Модель змінено, але ще не збережено."
      : `Модель збережено для нових завдань: ${snapshot.provider_id}, ${snapshot.model}.${credentialNote}`;
  }

  function updateModelRouteDraft() {
    const route = modelInputs.route_kind?.value;
    if (route === "ollama") {
      if (modelInputs.provider_id) modelInputs.provider_id.value = "ollama";
      if (modelInputs.credential_ref) modelInputs.credential_ref.value = "";
      if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = true;
      if (modelInputs.base_url && !modelInputs.base_url.value.trim()) {
        modelInputs.base_url.value = "http://localhost:11434";
      }
    } else if (route === "openai_compatible") {
      if (modelInputs.provider_id?.value === "ollama") modelInputs.provider_id.value = "";
      if (modelInputs.base_url?.value === "http://localhost:11434") modelInputs.base_url.value = "";
      if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = false;
    }
    applyModelRouteControls(false);
  }

  function markModelDirty() {
    if (modelPending) return;
    modelDirty = true;
    updateModelRouteDraft();
    if (modelStatus) {
      modelStatus.textContent = "Модель змінено, але ще не збережено.";
    }
  }

  for (const input of Object.values(modelInputs)) {
    input?.addEventListener(input?.type === "checkbox" || input?.tagName === "SELECT" ? "change" : "input", markModelDirty);
  }

  function modelPayload() {
    const route = modelInputs.route_kind?.value;
    const model = modelInputs.model?.value.trim() || "";
    const baseUrl = modelInputs.base_url?.value.trim() || "";
    const timeout = Number(modelInputs.timeout_seconds?.value);
    if (!["ollama", "openai_compatible"].includes(route)) {
      throw new Error("Виберіть тип маршруту моделі.");
    }
    if (!model) throw new Error("Введіть назву моделі.");
    if (!baseUrl) throw new Error("Введіть базову адресу постачальника.");
    if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 600) {
      throw new Error("Тайм-аут моделі має бути числом від 1 до 600 секунд.");
    }
    if (route === "ollama") {
      return {
        revision: modelRevision,
        route_kind: "ollama",
        provider_id: "ollama",
        model,
        base_url: baseUrl,
        credential_ref: null,
        private_data_allowed: true,
        timeout_seconds: timeout,
      };
    }
    const provider = modelInputs.provider_id?.value.trim() || "";
    const credentialRef = modelInputs.credential_ref?.value.trim() || "";
    if (!provider || provider === "ollama") {
      throw new Error("Введіть окремий ідентифікатор постачальника API.");
    }
    if (!/^env:[A-Za-z_][A-Za-z0-9_]*$/.test(credentialRef)) {
      throw new Error("Введіть лише посилання на змінну середовища у форматі env:НАЗВА.");
    }
    return {
      revision: modelRevision,
      route_kind: "openai_compatible",
      provider_id: provider,
      model,
      base_url: baseUrl,
      credential_ref: credentialRef,
      private_data_allowed: Boolean(modelInputs.private_data_allowed?.checked),
      timeout_seconds: timeout,
    };
  }

  async function dispatchModel(actionId, trigger) {
    if (modelPending) return;
    const save = actionId === "settings.model.configure";
    let payload = {};
    if (save) {
      try {
        payload = modelPayload();
      } catch (error) {
        announce(error instanceof Error ? error.message : "Перевірте налаштування моделі.", true);
        modelStatus.textContent = error instanceof Error ? error.message : "Перевірте налаштування моделі.";
        if (modelInputs.route_kind?.value === "openai_compatible"
            && !modelInputs.credential_ref?.value.trim()) {
          modelInputs.credential_ref?.focus();
        } else if (!modelInputs.model?.value.trim()) {
          modelInputs.model?.focus();
        } else {
          modelInputs.route_kind?.focus();
        }
        return;
      }
    }

    modelPending = true;
    modelGeneration += 1;
    setModelControlsDisabled(true);
    let result = null;
    try {
      result = await globalThis.pywebview.api.dispatch({
        request_id: requestId(),
        action_id: actionId,
        payload,
      });
      if (!["completed", "failed", "rejected"].includes(result?.status)) {
        throw new Error("Invalid model settings acknowledgement");
      }
      const failed = result.status !== "completed";
      if (!failed) modelDirty = false;
      announce(result.message, failed);
      appendLog(result.message);
    } catch {
      announce("Немає підтвердження зміни моделі. Перечитайте збережені налаштування перед повтором.", true);
      appendLog("Немає підтвердження зміни моделі; автоматичний повтор не виконується.");
    } finally {
      modelPending = false;
      modelGeneration += 1;
      if (!await refreshState({ announceTeamTransitions: false })) renderModelSettings(null);
      const focusId = result?.focus_id
        || (result?.status === "failed" || result?.status === "rejected"
          ? trigger?.dataset?.errorFocusTarget
          : null);
      if (focusId) focusElementById(focusId);
      else if (modelInputs.route_kind && !modelInputs.route_kind.disabled) modelInputs.route_kind.focus();
      else trigger?.focus?.();
    }
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
  document.getElementById("model-reload")?.addEventListener("click", () => {
    modelDirty = false;
  });

  document.getElementById("source-reload")?.addEventListener("click", async () => {
    sourceDirty = false;
    if (await refreshState()) announce("Збережені налаштування перечитано.");
  });

  async function refreshState({ announceTeamTransitions = true } = {}) {
    const autostartReadGeneration = autostartGeneration;
    const modelReadGeneration = modelGeneration;
    if (!globalThis.pywebview?.api?.get_state) {
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      if (modelReadGeneration === modelGeneration) renderModelSettings(null);
      reportStateUnavailable();
      return false;
    }
    let response;
    try {
      response = await globalThis.pywebview.api.get_state();
    } catch {
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      if (modelReadGeneration === modelGeneration) renderModelSettings(null);
      reportStateUnavailable();
      return false;
    }
    if (!response?.ok) {
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      if (modelReadGeneration === modelGeneration) renderModelSettings(null);
      reportStateUnavailable();
      return false;
    }
    const state = response.state || {};
    if (autostartReadGeneration === autostartGeneration) renderAutostart(state.autostart ?? null);
    if (modelReadGeneration === modelGeneration) renderModelSettings(state.v01_model_settings ?? null);
    renderSourceSetup(state.v01_sources ?? null);
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
    if (["settings.autostart.configure", "settings.autostart.refresh"].includes(actionId)) {
      await dispatchAutostart(actionId, trigger);
      return;
    }
    if (["settings.model.configure", "settings.model.refresh"].includes(actionId)) {
      await dispatchModel(actionId, trigger);
      return;
    }
    const payload = {};
    if (actionId === "task.create") payload.command = commandInput.value.trim();
    if (actionId === "team.sources.configure") {
      payload.revision = sourceRevision;
      for (const [key, input] of Object.entries(sourceInputs)) payload[key] = input?.value ?? "";
    }
    const result = await globalThis.pywebview.api.dispatch({ request_id: requestId(), action_id: actionId, payload });
    const failed = result.status === "failed" || result.status === "rejected";
    if (actionId === "team.sources.configure" && result.status === "completed") sourceDirty = false;
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
