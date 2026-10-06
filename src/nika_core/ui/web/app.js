(() => {
  "use strict";

  const statusNode = document.getElementById("app-status");
  const activityLog = document.getElementById("activity-log");
  const keymapBody = document.getElementById("keymap-body");
  const keymapJson = document.getElementById("keymap-json");
  const commandInput = document.getElementById("command-input");
  const speechText = document.getElementById("speech-text");
  const speechStatus = document.getElementById("speech-status");
  const speechStart = document.getElementById("speech-start");
  const speechCancel = document.getElementById("speech-cancel");
  const allowedSpeechStatuses = new Set([
    "unavailable",
    "idle",
    "running",
    "draining",
    "cancelling",
    "completed",
    "cancelled",
    "failed",
  ]);
  let speechTerminalSignature = null;
  const voiceStatus = document.getElementById("voice-status");
  const voiceTranscript = document.getElementById("voice-transcript");
  const voiceStart = document.getElementById("voice-start");
  const voiceCancel = document.getElementById("voice-cancel");
  const voiceUseCommand = document.getElementById("voice-use-command");
  const voiceModelSource = document.getElementById("voice-model-source");
  const voiceModelStatus = document.getElementById("voice-model-status");
  const voiceModelImport = document.getElementById("voice-model-import");
  const voiceModelCancel = document.getElementById("voice-model-cancel");
  const allowedVoiceModelSetupStatuses = new Set([
    "missing",
    "installed",
    "partial",
    "importing",
    "cancelling",
    "restart_required",
    "failed",
    "cancelled",
  ]);
  let voiceModelTerminalSignature = null;
  const allowedVoiceStatuses = new Set([
    "idle",
    "running",
    "cancelling",
    "completed",
    "failed",
    "cancelled",
  ]);
  let voiceTranscriptValue = "";
  let voiceTerminalSignature = null;
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
  const recoveryStatus = document.getElementById("recovery-status");
  const recoverySummary = document.getElementById("recovery-summary");
  const recoveryFields = Object.freeze({
    auto_resume_count: document.getElementById("recovery-auto-count"),
    manual_resume_count: document.getElementById("recovery-manual-count"),
    approval_count: document.getElementById("recovery-approval-count"),
    uncertain_count: document.getElementById("recovery-uncertain-count"),
    blocked_count: document.getElementById("recovery-blocked-count"),
    resume_failed_count: document.getElementById("recovery-failed-count"),
  });
  const allowedRecoveryStatuses = new Set([
    "not_started",
    "inventory",
    "ready",
    "recovering",
    "manual",
    "attention",
    "failed",
  ]);
  let recoverySignature = null;
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
  const productProjectStatuses = document.getElementById("product-project-statuses");
  const productProjectStatusesList = document.getElementById("product-project-statuses-list");
  const productProjectStatusesEmpty = document.getElementById("product-project-statuses-empty");
  const productProjectStatusesTruncated = document.getElementById(
    "product-project-statuses-truncated",
  );
  const productProjectDecision = document.getElementById("product-project-decision");
  const productProjectDecisionFields = Object.freeze({
    decision_id: document.getElementById("product-project-decision-id"),
    title: document.getElementById("product-project-decision-title"),
    question: document.getElementById("product-project-decision-question"),
    risk_level: document.getElementById("product-project-decision-risk"),
    state: document.getElementById("product-project-decision-state"),
  });
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
    model_text: document.getElementById("team-final-model-text"),
    model_provider: document.getElementById("team-final-model-provider"),
    model_name: document.getElementById("team-final-model-name"),
  });
  const productStatusKindLabels = Object.freeze({
    requirement: "Вимога",
    milestone: "Етап",
    architecture_decision: "Архітектурне рішення",
    team_role: "Роль команди",
    repository: "Репозиторій",
    component: "Компонент",
    qa: "QA",
    build: "Збірка",
    release: "Реліз",
    credential: "Облікові дані",
    deployment: "Розгортання",
    incident: "Інцидент",
    blocker: "Блокер",
  });
  const productProjectUnavailableMessage = "Стан поточного ProductProject недоступний.";
  const teamTaskUnavailableMessage = "Стан командного завдання недоступний.";
  const unavailableStateLabel = "Стан недоступний";
  const teamRoleLabels = Object.freeze({
    supervisor: "Координатор",
    worker: "Виконавець",
    checker: "Перевіряльник",
  });
  const taskStateLabels = Object.freeze({
    CREATED: "Створено",
    READY: "Готове до запуску",
    RUNNING: "Виконується",
    WAITING_TOOL: "Очікує інструмент",
    WAITING_APPROVAL: "Очікує підтвердження",
    PAUSED: "Призупинено",
    RETRYING: "Очікує повторної спроби",
    BLOCKED: "Заблоковано",
    COMPLETED: "Завершено",
    FAILED: "Завершено з помилкою",
    CANCELLED: "Скасовано",
    ARCHIVED: "Архівовано",
    not_in_task_queue: "Поза чергою завдань",
  });
  const memberStateLabels = Object.freeze({
    spawned: "Створено",
    running: "Виконується",
    waiting_approval: "Очікує підтвердження",
    paused: "Призупинено",
    completed: "Завершено",
    failed: "Завершено з помилкою",
    cancelled: "Скасовано",
  });
  const teamStateLabels = Object.freeze({
    active: "Активне",
    completed: "Завершено",
    failed: "Завершено з помилкою",
    cancelled: "Скасовано",
  });
  const finalStatusLabels = Object.freeze({
    completed: "Завершено",
    failed: "Завершено з помилкою",
    cancelled: "Скасовано",
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
  let stateUnavailableReported = false;
  const maxActivityItems = 200;
  // Foreground reconciliation has priority over background state polling.
  let foregroundStateRefreshPending = 0;

  function validDispatchResponse(response, expectedRequestId) {
    return Boolean(
      response
      && typeof response === "object"
      && !Array.isArray(response)
      && response.request_id === expectedRequestId
      && ["accepted", "completed", "failed", "rejected"].includes(response.status)
      && typeof response.message === "string"
      && (response.focus_id == null || typeof response.focus_id === "string"),
    );
  }

  function validKeymapResponse(response, requireData = false) {
    return Boolean(
      response
      && typeof response === "object"
      && !Array.isArray(response)
      && typeof response.ok === "boolean"
      && typeof response.message === "string"
      && (!requireData || !response.ok || typeof response.data === "string"),
    );
  }

  let bridgeInitializationStarted = false;
  let statePollHandle = null;
  let statePollPending = false;
  let stateRefreshGeneration = 0;
  let lastStateReady = false;
  let teamStateSignature = null;
  let teamModelResultAvailable = null;

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

  const shortcutModifierAliases = Object.freeze({
    alt: "alt",
    ctrl: "ctrl",
    control: "ctrl",
    shift: "shift",
    win: "win",
    windows: "win",
    meta: "win",
    super: "win",
  });
  const shortcutModifierOrder = Object.freeze(["ctrl", "alt", "shift", "win"]);

  function canonicalEventPrimaryKey(key) {
    if (key === " ") return "space";
    return String(key || "").toLowerCase();
  }

  function eventBinding(event) {
    const parts = [];
    if (event.ctrlKey) parts.push("ctrl");
    if (event.altKey) parts.push("alt");
    if (event.shiftKey) parts.push("shift");
    if (event.metaKey) parts.push("win");
    const key = canonicalEventPrimaryKey(event.key);
    if (["control", "alt", "shift", "meta"].includes(key)) return null;
    if (!key) return null;
    parts.push(key);
    return parts.join("+");
  }

  function normalizedBinding(binding) {
    const rawParts = String(binding || "")
      .split("+")
      .map((part) => part.trim().toLowerCase())
      .filter(Boolean);
    const modifiers = new Set();
    const primaryKeys = [];
    for (const part of rawParts) {
      const modifier = shortcutModifierAliases[part];
      if (modifier) {
        if (modifiers.has(modifier)) return "";
        modifiers.add(modifier);
      } else {
        primaryKeys.push(part);
      }
    }
    if (primaryKeys.length !== 1) return "";
    return [
      ...shortcutModifierOrder.filter((modifier) => modifiers.has(modifier)),
      primaryKeys[0],
    ].join("+");
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

  function presentState(labels, value) {
    if (typeof value !== "string") return unavailableStateLabel;
    return Object.prototype.hasOwnProperty.call(labels, value)
      ? labels[value]
      : unavailableStateLabel;
  }

  function validProductStatusItem(item) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return false;
    const required = ["kind", "item_id", "label", "state"];
    if (required.some((field) => typeof item[field] !== "string" || !item[field].trim())) {
      return false;
    }
    if (!Object.prototype.hasOwnProperty.call(productStatusKindLabels, item.kind)) return false;
    return typeof item.detail === "string";
  }

  function validProductDecision(decision) {
    if (decision === null) return true;
    if (!decision || typeof decision !== "object" || Array.isArray(decision)) return false;
    const stringFields = ["decision_id", "title", "question", "state"];
    if (stringFields.some((field) => typeof decision[field] !== "string" || !decision[field].trim())) {
      return false;
    }
    if (decision.state !== "pending") return false;
    return Number.isInteger(decision.risk_level)
      && decision.risk_level >= 0
      && decision.risk_level <= 4;
  }

  function validProductProject(project) {
    if (!project || typeof project !== "object" || Array.isArray(project)) return false;
    const stringFields = ["title", "project_id", "goal", "state"];
    if (stringFields.some((field) => typeof project[field] !== "string" || !project[field].trim())) {
      return false;
    }
    if (!Number.isInteger(project.spec_version) || project.spec_version < 1) return false;
    const countFields = ["blocker_count", "status_count", "decision_count"];
    if (!countFields.every((field) => Number.isInteger(project[field]) && project[field] >= 0)) {
      return false;
    }
    if (!Array.isArray(project.status_items) || project.status_items.length > 24) return false;
    if (!project.status_items.every(validProductStatusItem)) return false;
    if (project.status_items.length > project.status_count) return false;
    if (typeof project.status_items_truncated !== "boolean") return false;
    if (project.status_items_truncated !== (project.status_items.length < project.status_count)) {
      return false;
    }
    return Object.prototype.hasOwnProperty.call(project, "current_decision")
      && validProductDecision(project.current_decision);
  }

  function clearProductProjectFields() {
    for (const node of Object.values(productProjectFields)) node.textContent = "";
    for (const node of Object.values(productProjectDecisionFields)) node.textContent = "";
    productProjectStatusesList.replaceChildren();
    productProjectStatusesEmpty.hidden = false;
    productProjectStatusesTruncated.textContent = "";
    productProjectStatusesTruncated.hidden = true;
    productProjectStatuses.hidden = true;
    productProjectDecision.hidden = true;
  }

  function renderProductProjectUnavailable(message) {
    productProjectEmpty.textContent = message || productProjectUnavailableMessage;
    productProjectEmpty.hidden = false;
    productProjectSummary.hidden = true;
    clearProductProjectFields();
  }

  function reportStateUnavailable() {
    renderStartupRecovery(null);
    renderModelSettings(null);
    renderProductProjectUnavailable(productProjectUnavailableMessage);
    renderTeamTaskUnavailable();
    if (stateUnavailableReported) return;
    stateUnavailableReported = true;
    announce(productProjectUnavailableMessage, true);
    appendLog(productProjectUnavailableMessage);
  }

  function renderProductProjectStatuses(project) {
    productProjectStatusesList.replaceChildren();
    for (const item of project.status_items) {
      const row = document.createElement("li");
      const kind = productStatusKindLabels[item.kind];
      const detail = item.detail ? ` ${item.detail}` : "";
      row.textContent = `${kind}: ${item.label}; стан: ${item.state}.${detail}`;
      productProjectStatusesList.appendChild(row);
    }
    productProjectStatusesEmpty.hidden = project.status_items.length > 0;
    if (project.status_items_truncated) {
      productProjectStatusesTruncated.textContent = (
        `Показано ${project.status_items.length} з ${project.status_count} записів; `
        + "блокери мають пріоритет."
      );
      productProjectStatusesTruncated.hidden = false;
    } else {
      productProjectStatusesTruncated.textContent = "";
      productProjectStatusesTruncated.hidden = true;
    }
    productProjectStatuses.hidden = false;
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
    renderProductProjectStatuses(project);
    const decision = project.current_decision;
    if (decision === null) {
      productProjectDecision.hidden = true;
    } else {
      productProjectDecisionFields.decision_id.textContent = decision.decision_id;
      productProjectDecisionFields.title.textContent = decision.title;
      productProjectDecisionFields.question.textContent = decision.question;
      productProjectDecisionFields.risk_level.textContent = `R${decision.risk_level}`;
      productProjectDecisionFields.state.textContent = "Очікує рішення";
      productProjectDecision.hidden = false;
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
    teamModelResultAvailable = null;
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

  function validBoundedModelIdentity(value, maxLength, { rejectDelete = false } = {}) {
    return Boolean(
      typeof value === "string"
      && value.length > 0
      && value.length <= maxLength
      && value === value.trim()
      && ![...value].some((char) => (
        char.charCodeAt(0) < 32 || (rejectDelete && char.charCodeAt(0) === 127)
      ))
    );
  }

  function validModelResult(result) {
    if (!result || typeof result !== "object" || Array.isArray(result)) return false;
    const keys = Object.keys(result).sort();
    const expectedKeys = [
      "model",
      "provenance_validated",
      "provider_id",
      "provider_kind",
      "text",
    ];
    if (keys.length !== expectedKeys.length
        || keys.some((key, index) => key !== expectedKeys[index])) return false;
    return Boolean(
      typeof result.text === "string"
      && result.text.length > 0
      && result.text.length <= 2000
      && result.text === result.text.trim()
      && !result.text.includes("\0")
      && validBoundedModelIdentity(result.provider_id, 128, { rejectDelete: true })
      && ["local", "cloud"].includes(result.provider_kind)
      && validBoundedModelIdentity(result.model, 512)
      && result.provenance_validated === true
    );
  }

  function validComparison(comparison) {
    if (comparison == null) return true;
    if (typeof comparison !== "object" || Array.isArray(comparison)) return false;
    const keys = Object.keys(comparison).sort();
    const requiredKeys = [
      "agreement_count",
      "difference_count",
      "source_states",
      "status",
      "validated",
    ];
    const allowedKeys = comparison.model_result == null
      ? requiredKeys
      : [...requiredKeys, "model_result"].sort();
    if (keys.length !== allowedKeys.length
        || keys.some((key, index) => key !== allowedKeys[index])) return false;
    const validComparisonStatuses = ["agree", "disagree", "partial"];
    const allowedComparisonStatuses = [
      ...validComparisonStatuses,
      "missing",
      "worker_error",
      "evidence_invalid",
    ];
    if (!allowedComparisonStatuses.includes(comparison.status)
        || typeof comparison.validated !== "boolean") return false;
    if (!Array.isArray(comparison.source_states)
        || ![0, 2].includes(comparison.source_states.length)
        || comparison.source_states.some((state) => (
          !["valid", "missing", "worker_error", "evidence_invalid"].includes(state)
        ))) return false;
    let expectedNoncomparisonStatus = null;
    if (comparison.source_states.length === 0) {
      expectedNoncomparisonStatus = "evidence_invalid";
    } else if (comparison.source_states.includes("evidence_invalid")) {
      expectedNoncomparisonStatus = "evidence_invalid";
    } else if (comparison.source_states.includes("worker_error")) {
      expectedNoncomparisonStatus = "worker_error";
    } else if (comparison.source_states.includes("missing")) {
      expectedNoncomparisonStatus = "missing";
    }
    if (expectedNoncomparisonStatus === null) {
      if (!validComparisonStatuses.includes(comparison.status)) return false;
    } else if (comparison.status !== expectedNoncomparisonStatus) {
      return false;
    }
    if (!Number.isSafeInteger(comparison.agreement_count)
        || comparison.agreement_count < 0
        || comparison.agreement_count > 100
        || !Number.isSafeInteger(comparison.difference_count)
        || comparison.difference_count < 0
        || comparison.difference_count > 100) {
      return false;
    }
    const countsCoherent = (
      (comparison.status === "agree"
        && comparison.agreement_count === 1
        && comparison.difference_count === 0)
      || (comparison.status === "disagree"
        && comparison.agreement_count === 0
        && comparison.difference_count === 1)
      || (comparison.status === "partial"
        && comparison.agreement_count >= 1
        && comparison.difference_count === 1)
      || (!validComparisonStatuses.includes(comparison.status)
        && comparison.agreement_count === 0
        && comparison.difference_count === 0)
    );
    if (!countsCoherent) return false;
    const evidenceValid = validComparisonStatuses.includes(comparison.status)
      && comparison.source_states.every((state) => state === "valid");
    if (comparison.validated !== evidenceValid) return false;
    if (comparison.model_result == null) return true;
    return comparison.validated === true && validModelResult(comparison.model_result);
  }

  function validFinalResult(result, taskId, teamId, teamState, terminalMemberCount) {
    if (result == null) return true;
    if (
      !result
      || typeof result !== "object"
      || Array.isArray(result)
      || !Object.prototype.hasOwnProperty.call(finalMessages, result.status)
      || result.status !== teamState
      || result.task_id !== taskId
      || result.team_id !== teamId
      || !Number.isInteger(result.terminal_member_count)
      || result.terminal_member_count !== terminalMemberCount
      || !Number.isInteger(result.result_record_count)
      || result.result_record_count < 0
    ) {
      return false;
    }
    if (result.comparison != null && result.status !== "completed") return false;
    return validComparison(result.comparison);
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
    const terminalTeamState = team.state !== "active";
    if (terminalTeamState !== (finalResult != null)) return false;
    if (!Array.isArray(events) || !events.every(validTeamEvent)) return false;
    const terminalMemberCount = members.filter((member) => (
      ["completed", "failed", "cancelled"].includes(member.state)
    )).length;
    return validFinalResult(
      finalResult,
      task.task_id,
      team.team_id,
      team.state,
      terminalMemberCount,
    );
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
    appendDefinitionItem(details, "Стан", presentState(memberStateLabels, member.state));
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
      task_state: projection.task.state,
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
      final_model_result: projection.final_result?.comparison?.model_result
        ? [
          projection.final_result.comparison.model_result.text,
          projection.final_result.comparison.model_result.provider_id,
          projection.final_result.comparison.model_result.model,
        ]
        : null,
    });
  }

  function renderTeamTask(projection) {
    if (projection == null) {
      const nextSignature = "none";
      const changed = teamStateSignature !== null && teamStateSignature !== nextSignature;
      teamStateSignature = nextSignature;
      teamModelResultAvailable = null;
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
    const modelResultAvailable = Boolean(projection.final_result?.comparison?.model_result);
    const modelResultBecameAvailable = teamModelResultAvailable === false && modelResultAvailable;
    teamStateSignature = nextSignature;
    teamModelResultAvailable = modelResultAvailable;
    const { task, team, members, events, final_result: finalResult } = projection;
    teamTaskFields.task_id.textContent = task.task_id;
    teamTaskFields.command.textContent = task.command || "Команда не збережена у bounded projection.";
    teamTaskFields.task_state.textContent = presentState(taskStateLabels, task.state);
    teamTaskFields.team_id.textContent = team.team_id;
    teamTaskFields.team_state.textContent = presentState(teamStateLabels, team.state);
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
      teamFinalFields.status.textContent = presentState(finalStatusLabels, finalResult.status);
      teamFinalFields.text.textContent = finalMessages[finalResult.status];
      teamFinalFields.task_id.textContent = finalResult.task_id;
      teamFinalFields.team_id.textContent = finalResult.team_id;
      const modelResult = finalResult.comparison?.model_result || null;
      teamFinalFields.model_text.textContent = modelResult?.text
        || "Немає перевіреної відповіді моделі для цього результату.";
      teamFinalFields.model_provider.textContent = modelResult?.provider_id || "Не застосовується";
      teamFinalFields.model_name.textContent = modelResult?.model || "Не застосовується";
      teamFinalEmpty.hidden = true;
      teamFinalSummary.hidden = false;
    }

    teamTaskEmpty.hidden = true;
    teamTaskSummary.hidden = false;
    return { ok: true, changed, modelResultBecameAvailable };
  }

  function validStartupRecovery(snapshot) {
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return false;
    if (snapshot.schema_version !== 1 || !allowedRecoveryStatuses.has(snapshot.status)) return false;
    return Object.keys(recoveryFields).every((field) => (
      Number.isSafeInteger(snapshot[field]) && snapshot[field] >= 0
    ));
  }

  function renderStartupRecovery(snapshot) {
    if (!recoveryStatus || !recoverySummary) {
      return { ok: false, changed: false, message: "Стан відновлення недоступний." };
    }
    if (!validStartupRecovery(snapshot)) {
      const changed = recoverySignature !== "invalid";
      recoverySignature = "invalid";
      recoveryStatus.textContent = "Стан відновлення недоступний або несумісний.";
      recoverySummary.hidden = true;
      for (const node of Object.values(recoveryFields)) {
        if (node) node.textContent = "—";
      }
      return {
        ok: false,
        changed,
        message: "Стан відновлення після перезапуску недоступний або несумісний.",
        assertive: true,
      };
    }

    for (const [field, node] of Object.entries(recoveryFields)) {
      if (node) node.textContent = String(snapshot[field]);
    }
    recoverySummary.hidden = false;

    const messages = {
      not_started: "Перевірка незавершеної роботи ще не почалася.",
      inventory: "Nika перевіряє незавершену роботу після перезапуску.",
      ready: "Перевірку відновлення завершено. Немає роботи, яку треба автоматично або вручну продовжити.",
      recovering: "Nika безпечно продовжує лише crash-left роботу з перевіреним checkpoint.",
      manual: "Є робота, що очікує ручного продовження або підтвердження. Автоматичний запуск не виконується.",
      attention: "Невизначена або заблокована робота. Автоматичний повтор не виконується; потрібна перевірка стану.",
      failed: "Не вдалося безпечно перевірити незавершену роботу. Автоматичне продовження заблоковано.",
    };
    const nextSignature = JSON.stringify([
      snapshot.status,
      ...Object.keys(recoveryFields).map((field) => snapshot[field]),
    ]);
    const changed = recoverySignature !== null && recoverySignature !== nextSignature;
    recoverySignature = nextSignature;
    recoveryStatus.textContent = messages[snapshot.status];
    return {
      ok: true,
      changed,
      message: messages[snapshot.status],
      assertive: ["attention", "failed"].includes(snapshot.status),
    };
  }

  function setModelControlsDisabled(disabled) {
    for (const input of Object.values(modelInputs)) {
      if (input) input.disabled = disabled;
    }
    if (modelSave) modelSave.disabled = disabled;
  }

  function applyModelRouteControls(disabled = false) {
    const route = modelInputs.route_kind?.value;
    const deterministic = route === "deterministic";
    const foundry = route === "foundry_local";
    const ollama = route === "ollama";
    const api = route === "openai_compatible";
    if (modelInputs.route_kind) modelInputs.route_kind.disabled = disabled;
    if (modelInputs.model) modelInputs.model.disabled = disabled || deterministic;
    if (modelInputs.base_url) {
      modelInputs.base_url.disabled = disabled || deterministic || foundry;
    }
    if (modelInputs.timeout_seconds) modelInputs.timeout_seconds.disabled = disabled;
    if (modelInputs.provider_id) {
      modelInputs.provider_id.disabled = disabled || deterministic || foundry || ollama;
    }
    if (modelInputs.credential_ref) {
      modelInputs.credential_ref.disabled = disabled || !api;
    }
    if (modelInputs.private_data_allowed) {
      modelInputs.private_data_allowed.disabled = disabled || !api;
    }
    if (modelSave) modelSave.disabled = disabled;
    if (disabled) return;
    if (deterministic) {
      if (modelInputs.provider_id) modelInputs.provider_id.value = "";
      if (modelInputs.model) modelInputs.model.value = "";
      if (modelInputs.base_url) modelInputs.base_url.value = "";
      if (modelInputs.credential_ref) modelInputs.credential_ref.value = "";
      if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = true;
    } else if (foundry) {
      if (modelInputs.provider_id) modelInputs.provider_id.value = "foundry-local";
      if (modelInputs.base_url) modelInputs.base_url.value = "";
      if (modelInputs.credential_ref) modelInputs.credential_ref.value = "";
      if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = true;
    } else if (ollama) {
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
    if (!["deterministic", "foundry_local", "ollama", "openai_compatible"].includes(snapshot.route_kind)) {
      return false;
    }
    if (
      typeof snapshot.timeout_seconds !== "number"
      || !Number.isFinite(snapshot.timeout_seconds)
      || snapshot.timeout_seconds <= 0
      || snapshot.timeout_seconds > 600
    ) return false;
    if (typeof snapshot.private_data_allowed !== "boolean") return false;
    if (typeof snapshot.credential_configured !== "boolean") return false;
    const text = (value) => typeof value === "string" && Boolean(value.trim());
    if (snapshot.route_kind === "deterministic") {
      return snapshot.provider_id === null
        && snapshot.provider_kind === null
        && snapshot.model === null
        && snapshot.base_url === null
        && snapshot.credential_configured === false
        && snapshot.private_data_allowed === true;
    }
    if (snapshot.route_kind === "foundry_local") {
      return snapshot.provider_id === "foundry-local"
        && snapshot.provider_kind === "local"
        && text(snapshot.model)
        && snapshot.base_url === null
        && snapshot.credential_configured === false
        && snapshot.private_data_allowed === true;
    }
    if (snapshot.route_kind === "ollama") {
      return snapshot.provider_id === "ollama"
        && snapshot.provider_kind === "local"
        && text(snapshot.model)
        && text(snapshot.base_url)
        && snapshot.credential_configured === false
        && snapshot.private_data_allowed === true;
    }
    return text(snapshot.provider_id)
      && snapshot.provider_id !== "ollama"
      && snapshot.provider_id !== "foundry-local"
      && snapshot.provider_kind === "cloud"
      && text(snapshot.model)
      && text(snapshot.base_url)
      && snapshot.credential_configured === true;
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
      modelInputs.provider_id.value = snapshot.provider_id ?? "";
      modelInputs.model.value = snapshot.model ?? "";
      modelInputs.base_url.value = snapshot.base_url ?? "";
      modelInputs.credential_ref.value = "";
      modelInputs.private_data_allowed.checked = snapshot.private_data_allowed;
      modelInputs.timeout_seconds.value = String(snapshot.timeout_seconds);
    }
    applyModelRouteControls(false);
    const credentialNote = snapshot.route_kind === "openai_compatible"
      ? " Посилання на змінну середовища налаштовано, але навмисно не показується; для зміни API-маршруту введіть env:НАЗВА знову."
      : "";
    let savedDescription;
    if (snapshot.route_kind === "deterministic") {
      savedDescription = "Детермінований режим без LLM.";
    } else if (snapshot.route_kind === "foundry_local") {
      savedDescription = `Foundry Local, ${snapshot.model}.`;
    } else {
      savedDescription = `${snapshot.provider_id}, ${snapshot.model}.`;
    }
    modelStatus.textContent = modelDirty
      ? "Модель змінено, але ще не збережено."
      : `Модель збережено для нових завдань: ${savedDescription}${credentialNote}`;
  }

  function updateModelRouteDraft() {
    const route = modelInputs.route_kind?.value;
    if (route === "deterministic") {
      if (modelInputs.provider_id) modelInputs.provider_id.value = "";
      if (modelInputs.model) modelInputs.model.value = "";
      if (modelInputs.base_url) modelInputs.base_url.value = "";
      if (modelInputs.credential_ref) modelInputs.credential_ref.value = "";
      if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = true;
    } else if (route === "foundry_local") {
      if (modelInputs.provider_id) modelInputs.provider_id.value = "foundry-local";
      if (modelInputs.base_url) modelInputs.base_url.value = "";
      if (modelInputs.credential_ref) modelInputs.credential_ref.value = "";
      if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = true;
    } else if (route === "ollama") {
      if (modelInputs.provider_id) modelInputs.provider_id.value = "ollama";
      if (modelInputs.credential_ref) modelInputs.credential_ref.value = "";
      if (modelInputs.private_data_allowed) modelInputs.private_data_allowed.checked = true;
      if (modelInputs.base_url && !modelInputs.base_url.value.trim()) {
        modelInputs.base_url.value = "http://localhost:11434";
      }
    } else if (route === "openai_compatible") {
      if (["ollama", "foundry-local"].includes(modelInputs.provider_id?.value)) {
        modelInputs.provider_id.value = "";
      }
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
    if (!["deterministic", "foundry_local", "ollama", "openai_compatible"].includes(route)) {
      throw new Error("Виберіть тип маршруту моделі.");
    }
    if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 600) {
      throw new Error("Тайм-аут моделі має бути числом від 1 до 600 секунд.");
    }
    if (route === "deterministic") {
      return {
        revision: modelRevision,
        route_kind: "deterministic",
        provider_id: null,
        model: null,
        base_url: null,
        credential_ref: null,
        private_data_allowed: true,
        timeout_seconds: timeout,
      };
    }
    if (!model) throw new Error("Введіть назву моделі.");
    if (route === "foundry_local") {
      return {
        revision: modelRevision,
        route_kind: "foundry_local",
        provider_id: "foundry-local",
        model,
        base_url: null,
        credential_ref: null,
        private_data_allowed: true,
        timeout_seconds: timeout,
      };
    }
    if (!baseUrl) throw new Error("Введіть базову адресу постачальника.");
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
    if (!provider || ["ollama", "foundry-local"].includes(provider)) {
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

    const enabledModelInputsBeforeDispatch = new Set(
      Object.values(modelInputs).filter((input) => input && !input.disabled),
    );
    modelPending = true;
    modelGeneration += 1;
    setModelControlsDisabled(true);
    let result = null;
    try {
      const dispatchRequestId = requestId();
      result = await globalThis.pywebview.api.dispatch({
        request_id: dispatchRequestId,
        action_id: actionId,
        payload,
      });
      if (!validDispatchResponse(result, dispatchRequestId) || result.status === "accepted") {
        throw new Error("Invalid model settings acknowledgement");
      }
      const failed = result.status !== "completed";
      if (!failed) modelDirty = false;
      announce(result.message, failed);
      appendLog(result.message);
    } catch {
      result = null;
      document.documentElement.dataset.nikaReady = "false";
      announce("Немає підтвердження зміни моделі. Перечитайте збережені налаштування перед повтором.", true);
      appendLog("Немає підтвердження зміни моделі; автоматичний повтор не виконується.");
    } finally {
      modelPending = false;
      modelGeneration += 1;
      const focusId = result?.focus_id
        || (result?.status === "failed" || result?.status === "rejected"
          ? trigger?.dataset?.errorFocusTarget
          : null);
      const focusTarget = focusId ? document.getElementById(focusId) : null;
      if (
        focusTarget instanceof HTMLElement
        && focusTarget.disabled
        && enabledModelInputsBeforeDispatch.has(focusTarget)
      ) {
        focusTarget.disabled = false;
      }
      const focusApplied = focusId ? focusElementById(focusId) : false;
      let stateReady = false;
      foregroundStateRefreshPending += 1;
      try {
        stateReady = await refreshState({
          announceTeamTransitions: false,
          requireCurrentGeneration: result === null,
        });
      } finally {
        foregroundStateRefreshPending -= 1;
      }
      document.documentElement.dataset.nikaReady = stateReady ? "true" : "false";
      if (!stateReady) {
        renderModelSettings(null);
      }
      if (!focusApplied) {
        const refreshedFocusApplied = focusId ? focusElementById(focusId) : false;
        if (!refreshedFocusApplied) {
          if (modelInputs.route_kind && !modelInputs.route_kind.disabled) modelInputs.route_kind.focus();
          else trigger?.focus?.();
        }
      }
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
    let uncertain = false;
    try {
      const dispatchRequestId = requestId();
      const result = await globalThis.pywebview.api.dispatch({
        request_id: dispatchRequestId, action_id: actionId, payload,
      });
      if (!validDispatchResponse(result, dispatchRequestId) || result.status === "accepted") {
        throw new Error("Invalid acknowledgement");
      }
      const failed = result.status !== "completed";
      if (!failed || !save) autostartDirty = false;
      announce(result.message, failed);
      appendLog(result.message);
    } catch {
      // The OS write may have completed before the bridge disconnected. No blind retry.
      uncertain = true;
      document.documentElement.dataset.nikaReady = "false";
      announce("Немає підтвердження зміни автозапуску. Перечитайте стан перед повтором.", true);
    } finally {
      autostartPending = false;
      autostartGeneration += 1;
      let stateReady = false;
      foregroundStateRefreshPending += 1;
      try {
        stateReady = await refreshState({
          announceTeamTransitions: false,
          requireCurrentGeneration: uncertain,
        });
      } finally {
        foregroundStateRefreshPending -= 1;
      }
      document.documentElement.dataset.nikaReady = stateReady ? "true" : "false";
      if (!stateReady) {
        renderAutostart(null);
      }
      if (!autostartInput.disabled) autostartInput.focus();
      else if (trigger && !trigger.disabled) trigger.focus();
      else focusElementById("autostart-heading");
    }
  }

  function renderSpeech(snapshot) {
    const failClosed = (message = "Стан озвучення недоступний або несумісний.") => {
      speechTerminalSignature = null;
      if (speechStatus) speechStatus.textContent = message;
      if (speechStart) speechStart.disabled = true;
      if (speechCancel) speechCancel.disabled = true;
      return false;
    };
    if (
      !snapshot
      || snapshot.schema !== "nika.packaged-speech-state:v1"
      || typeof snapshot.available !== "boolean"
      || !allowedSpeechStatuses.has(snapshot.status)
      || !Number.isSafeInteger(snapshot.generation)
      || snapshot.generation < 0
      || typeof snapshot.active !== "boolean"
      || typeof snapshot.message !== "string"
      || snapshot.message.length === 0
    ) {
      return failClosed();
    }
    const counters = [
      snapshot.accepted_characters,
      snapshot.spoken_characters,
      snapshot.chunk_count,
      snapshot.pending_characters,
    ];
    if (counters.some((value) => !Number.isSafeInteger(value) || value < 0)) {
      return failClosed();
    }
    if (!snapshot.available) {
      if (snapshot.status !== "unavailable" || snapshot.active) return failClosed();
      if (speechStatus) speechStatus.textContent = snapshot.message;
      if (speechStart) speechStart.disabled = true;
      if (speechCancel) speechCancel.disabled = true;
      speechTerminalSignature = null;
      return true;
    }

    const activeStatus = ["running", "draining", "cancelling"].includes(snapshot.status);
    if (snapshot.status === "unavailable" || snapshot.active !== activeStatus) {
      return failClosed();
    }
    if (snapshot.spoken_characters > snapshot.accepted_characters
        || snapshot.pending_characters > snapshot.accepted_characters) {
      return failClosed();
    }
    if (speechStatus) speechStatus.textContent = snapshot.message;
    if (speechStart) speechStart.disabled = snapshot.active;
    if (speechCancel) {
      speechCancel.disabled = !snapshot.active || snapshot.status === "cancelling";
    }

    const terminal = ["completed", "cancelled", "failed"].includes(snapshot.status);
    const signature = terminal
      ? JSON.stringify([snapshot.generation, snapshot.status, snapshot.message])
      : null;
    if (signature !== null && signature !== speechTerminalSignature) {
      speechTerminalSignature = signature;
      announce(snapshot.message, snapshot.status === "failed");
    } else if (!terminal) {
      speechTerminalSignature = null;
    }
    return true;
  }


  function renderVoiceModelSetup(snapshot) {
    const failClosed = (message = "Стан локальної голосової моделі недоступний або несумісний.") => {
      voiceModelTerminalSignature = null;
      if (voiceModelStatus) voiceModelStatus.textContent = message;
      if (voiceModelSource) voiceModelSource.disabled = true;
      if (voiceModelImport) voiceModelImport.disabled = true;
      if (voiceModelCancel) voiceModelCancel.disabled = true;
      return false;
    };
    if (
      !snapshot
      || snapshot.schema !== "nika.packaged-voice-model-setup:v1"
      || !allowedVoiceModelSetupStatuses.has(snapshot.status)
      || !Number.isSafeInteger(snapshot.generation)
      || snapshot.generation < 0
      || typeof snapshot.active !== "boolean"
      || typeof snapshot.installed !== "boolean"
      || typeof snapshot.can_import !== "boolean"
      || typeof snapshot.restart_required !== "boolean"
      || typeof snapshot.message !== "string"
      || snapshot.message.length === 0
    ) {
      return failClosed();
    }

    const activeStatus = snapshot.status === "importing" || snapshot.status === "cancelling";
    if (snapshot.active !== activeStatus) return failClosed();
    const retryableTerminal = snapshot.status === "failed" || snapshot.status === "cancelled";
    const validState = (
      (snapshot.status === "missing"
        && !snapshot.active
        && !snapshot.installed
        && !snapshot.restart_required)
      || (snapshot.status === "installed"
        && !snapshot.active
        && snapshot.installed
        && !snapshot.can_import
        && !snapshot.restart_required)
      || (snapshot.status === "partial"
        && !snapshot.active
        && !snapshot.installed
        && !snapshot.can_import
        && !snapshot.restart_required)
      || (snapshot.status === "importing"
        && snapshot.active
        && !snapshot.installed
        && !snapshot.can_import
        && !snapshot.restart_required)
      || (snapshot.status === "cancelling"
        && snapshot.active
        && !snapshot.installed
        && !snapshot.can_import
        && !snapshot.restart_required)
      || (snapshot.status === "restart_required"
        && !snapshot.active
        && snapshot.installed
        && !snapshot.can_import
        && snapshot.restart_required)
      || (retryableTerminal
        && !snapshot.active
        && !snapshot.installed
        && !snapshot.restart_required)
    );
    if (!validState) return failClosed();

    if (voiceModelStatus) voiceModelStatus.textContent = snapshot.message;
    if (voiceModelSource) voiceModelSource.disabled = !snapshot.can_import;
    if (voiceModelImport) voiceModelImport.disabled = !snapshot.can_import;
    if (voiceModelCancel) {
      voiceModelCancel.disabled = !snapshot.active || snapshot.status === "cancelling";
    }
    if (snapshot.status === "restart_required" && voiceModelSource) {
      voiceModelSource.value = "";
    }

    const terminal = ["restart_required", "failed", "cancelled"].includes(snapshot.status);
    const signature = terminal && snapshot.generation > 0
      ? JSON.stringify([snapshot.generation, snapshot.status, snapshot.message])
      : null;
    if (signature !== null && signature !== voiceModelTerminalSignature) {
      voiceModelTerminalSignature = signature;
      announce(snapshot.message, snapshot.status === "failed");
    } else if (!terminal) {
      voiceModelTerminalSignature = null;
    }
    return true;
  }

  function renderVoice(snapshot) {
    const failClosed = (message = "Стан голосового вводу недоступний або несумісний.") => {
      voiceTranscriptValue = "";
      voiceTerminalSignature = null;
      if (voiceStatus) voiceStatus.textContent = message;
      if (voiceTranscript) voiceTranscript.textContent = "Недоступно.";
      if (voiceStart) voiceStart.disabled = true;
      if (voiceCancel) voiceCancel.disabled = true;
      if (voiceUseCommand) voiceUseCommand.disabled = true;
      return false;
    };
    if (
      !snapshot
      || snapshot.schema !== "nika.packaged-voice-state:v1"
      || typeof snapshot.available !== "boolean"
      || typeof snapshot.message !== "string"
      || snapshot.message.length === 0
    ) {
      return failClosed();
    }
    if (!snapshot.available) {
      if (snapshot.turn !== null) return failClosed();
      voiceTranscriptValue = "";
      voiceTerminalSignature = null;
      if (voiceStatus) voiceStatus.textContent = snapshot.message;
      if (voiceTranscript) voiceTranscript.textContent = "Голосовий ввід недоступний.";
      if (voiceStart) voiceStart.disabled = true;
      if (voiceCancel) voiceCancel.disabled = true;
      if (voiceUseCommand) voiceUseCommand.disabled = true;
      return true;
    }

    const turn = snapshot.turn;
    if (
      !turn
      || turn.schema !== "nika.desktop-voice-state:v1"
      || !allowedVoiceStatuses.has(turn.status)
      || typeof turn.message !== "string"
      || turn.message.length === 0
      || typeof turn.active !== "boolean"
      || ![null, true, false].includes(turn.activated)
      || !(turn.transcript === null || typeof turn.transcript === "string")
    ) {
      return failClosed();
    }
    const activeStatus = turn.status === "running" || turn.status === "cancelling";
    if (turn.active !== activeStatus) return failClosed();

    if (voiceStatus) voiceStatus.textContent = turn.message;
    if (voiceStart) voiceStart.disabled = turn.active;
    if (voiceCancel) voiceCancel.disabled = !turn.active || turn.status === "cancelling";

    const canUseTranscript = turn.status === "completed"
      && turn.activated === true
      && typeof turn.transcript === "string"
      && turn.transcript.length > 0;
    voiceTranscriptValue = canUseTranscript ? turn.transcript : "";
    if (voiceTranscript) {
      voiceTranscript.textContent = typeof turn.transcript === "string" && turn.transcript.length > 0
        ? turn.transcript
        : "Ще немає.";
    }
    if (voiceUseCommand) voiceUseCommand.disabled = !canUseTranscript;

    const terminal = ["completed", "failed", "cancelled"].includes(turn.status);
    const signature = terminal
      ? JSON.stringify([turn.request_id, turn.status, turn.activated, turn.message])
      : null;
    if (signature !== null && signature !== voiceTerminalSignature) {
      voiceTerminalSignature = signature;
      announce(turn.message, turn.status === "failed");
    } else if (!terminal) {
      voiceTerminalSignature = null;
    }
    return true;
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

  voiceUseCommand?.addEventListener("click", () => {
    if (!voiceTranscriptValue) return;
    commandInput.value = voiceTranscriptValue;
    commandInput.focus();
    announce(
      "Розпізнаний текст перенесено в поле команди. Перевірте його перед створенням завдання.",
    );
  });

  async function refreshState({ announceTeamTransitions = true, requireCurrentGeneration = false } = {}) {
    const stateReadGeneration = ++stateRefreshGeneration;
    const isCurrentStateRead = () => stateReadGeneration === stateRefreshGeneration;
    const autostartReadGeneration = autostartGeneration;
    const modelReadGeneration = modelGeneration;
    if (!globalThis.pywebview?.api?.get_state) {
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      if (modelReadGeneration === modelGeneration) renderModelSettings(null);
      renderVoiceModelSetup(null);
      renderVoice(null);
      lastStateReady = false;
      reportStateUnavailable();
      return false;
    }
    let response;
    try {
      response = await globalThis.pywebview.api.get_state();
    } catch {
      if (!isCurrentStateRead()) return requireCurrentGeneration ? false : lastStateReady;
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      if (modelReadGeneration === modelGeneration) renderModelSettings(null);
      renderVoiceModelSetup(null);
      renderVoice(null);
      lastStateReady = false;
      reportStateUnavailable();
      return false;
    }
    if (!isCurrentStateRead()) return requireCurrentGeneration ? false : lastStateReady;
    if (!response?.ok) {
      if (autostartReadGeneration === autostartGeneration) renderAutostart(null);
      if (modelReadGeneration === modelGeneration) renderModelSettings(null);
      renderVoiceModelSetup(null);
      renderVoice(null);
      lastStateReady = false;
      reportStateUnavailable();
      return false;
    }
    const state = response.state || {};
    const recoveryRender = renderStartupRecovery(state.startup_recovery ?? null);
    if (autostartReadGeneration === autostartGeneration) renderAutostart(state.autostart ?? null);
    if (modelReadGeneration === modelGeneration) renderModelSettings(state.v01_model_settings ?? null);
    renderSourceSetup(state.v01_sources ?? null);
    renderVoiceModelSetup(state.voice_model_setup ?? null);
    renderSpeech(state.speech ?? null);
    renderVoice(state.voice ?? null);
    renderItems(
      tasksList,
      tasksEmpty,
      state.tasks || [],
      (item) => (
        `ID: ${item.task_id} — ${presentState(taskStateLabels, item.state)} — `
        + (item.command || "Без назви")
      ),
    );
    renderItems(agentsList, agentsEmpty, state.agents || [], (item) => `${item.name} — ${item.goal}`);
    renderItems(workspacesList, workspacesEmpty, state.workspaces || [], (item) => `${item.name} — ${item.description || "Без опису"}`);
    const productReady = renderProductProject(state.product_project ?? null);
    const teamRender = renderTeamTask(state.v01_team_task ?? null);
    if (!recoveryRender.ok) {
      lastStateReady = false;
      announce(recoveryRender.message, true);
      return false;
    }
    if (!teamRender.ok) {
      lastStateReady = false;
      announce(teamTaskUnavailableMessage, true);
      return false;
    }
    if (!productReady) {
      lastStateReady = false;
      return false;
    }
    stateUnavailableReported = false;
    if (announceTeamTransitions && recoveryRender.changed) {
      announce(
        teamRender.modelResultBecameAvailable
          ? `${recoveryRender.message} Перевірена відповідь моделі доступна в підсумку командного завдання.`
          : recoveryRender.message,
        recoveryRender.assertive,
      );
    } else if (announceTeamTransitions && teamRender.changed) {
      announce(
        teamRender.modelResultBecameAvailable
          ? "Перевірена відповідь моделі доступна в підсумку командного завдання."
          : "Стан командного завдання оновлено.",
      );
    }
    lastStateReady = true;
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
      document.documentElement.dataset.nikaReady = "false";
      announce(message, true);
      appendLog(message);
      let stateReady = false;
      try {
        stateReady = await refreshState({ requireCurrentGeneration: true });
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
    foregroundStateRefreshPending += 1;
    try {
      const payload = {};
      if (actionId === "task.create") payload.command = commandInput.value.trim();
      if (actionId === "voice.model.import") payload.source_root = voiceModelSource?.value ?? "";
      if (actionId === "speech.start") payload.text = speechText?.value ?? "";
      if (actionId === "team.sources.configure") {
        payload.revision = sourceRevision;
        for (const [key, input] of Object.entries(sourceInputs)) payload[key] = input?.value ?? "";
      }
      const dispatchRequestId = requestId();
      let result;
      try {
        result = await globalThis.pywebview.api.dispatch({
          request_id: dispatchRequestId, action_id: actionId, payload,
        });
      } catch {
        // The durable effect may have committed before the bridge disconnected. Never retry blindly.
        await reconcileUncertain(
          "Немає підтвердження виконання дії. Стан буде перечитано перед можливим повтором.",
        );
        return;
      }
      if (!validDispatchResponse(result, dispatchRequestId)) {
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
      foregroundStateRefreshPending -= 1;
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
      if (!validKeymapResponse(response)) {
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
      if (document.hidden || statePollPending || foregroundStateRefreshPending > 0) return;
      statePollPending = true;
      try {
        const ready = await refreshState();
        document.documentElement.dataset.nikaReady = ready ? "true" : "false";
      } finally {
        statePollPending = false;
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
      if (!validKeymapResponse(response, true)) {
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
