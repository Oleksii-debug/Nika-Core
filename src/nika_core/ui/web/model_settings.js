(() => {
  "use strict";

  const fields = Object.freeze({
    route_kind: document.getElementById("model-route-kind"),
    provider_id: document.getElementById("model-provider"),
    model: document.getElementById("model-name"),
    base_url: document.getElementById("model-base-url"),
    credential_ref: document.getElementById("model-credential-ref"),
    private_data_allowed: document.getElementById("model-private-data"),
    timeout_seconds: document.getElementById("model-timeout"),
  });
  const save = document.getElementById("model-save");
  const reload = document.getElementById("model-reload");
  const status = document.getElementById("model-settings-status");
  const appStatus = document.getElementById("app-status");
  const activityLog = document.getElementById("activity-log");
  let revision = 0;
  let dirty = false;
  let pending = false;
  let pollHandle = null;

  function announce(message, assertive = false) {
    if (!appStatus) return;
    appStatus.setAttribute("aria-live", assertive ? "assertive" : "polite");
    appStatus.textContent = message || "Готово.";
  }

  function log(message) {
    if (!activityLog || !message) return;
    const item = document.createElement("li");
    item.textContent = message;
    activityLog.appendChild(item);
  }

  function requestId() {
    if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
    return `model-ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function setDisabled(disabled) {
    for (const field of Object.values(fields)) if (field) field.disabled = disabled;
    if (save) save.disabled = disabled;
    if (reload) reload.disabled = disabled;
  }

  function applyRouteRules() {
    const ollama = fields.route_kind?.value === "ollama";
    if (fields.credential_ref) {
      fields.credential_ref.disabled = pending || ollama;
      if (ollama && dirty) fields.credential_ref.value = "";
    }
    if (fields.private_data_allowed) fields.private_data_allowed.disabled = pending || ollama;
  }

  function defaults() {
    revision = 0;
    if (fields.route_kind) fields.route_kind.value = "ollama";
    if (fields.provider_id) fields.provider_id.value = "ollama";
    if (fields.model) fields.model.value = "qwen3:8b";
    if (fields.base_url) fields.base_url.value = "http://localhost:11434";
    if (fields.credential_ref) fields.credential_ref.value = "";
    if (fields.private_data_allowed) fields.private_data_allowed.checked = false;
    if (fields.timeout_seconds) fields.timeout_seconds.value = "60";
    applyRouteRules();
  }

  function validSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object" || Array.isArray(snapshot)) return false;
    if (snapshot.status === "missing") return snapshot.revision === 0;
    if (snapshot.status !== "ready") return false;
    return Number.isSafeInteger(snapshot.revision)
      && snapshot.revision > 0
      && ["ollama", "openai_compatible"].includes(snapshot.route_kind)
      && typeof snapshot.provider_id === "string"
      && typeof snapshot.model === "string"
      && typeof snapshot.base_url === "string"
      && typeof snapshot.timeout_seconds === "number"
      && typeof snapshot.private_data_allowed === "boolean"
      && typeof snapshot.credential_configured === "boolean";
  }

  function render(snapshot, force = false) {
    if (!validSnapshot(snapshot)) {
      status.textContent = "Налаштування моделі недоступні або пошкоджені. Нове завдання не створюйте, доки стан не буде відновлено.";
      setDisabled(true);
      return false;
    }
    setDisabled(false);
    if (snapshot.status === "missing") {
      if (!dirty || force) defaults();
      status.textContent = "Модель ще не збережена. Запропоновано локальний Ollama qwen3:8b; перевірте поля й натисніть «Зберегти модель».";
      applyRouteRules();
      return true;
    }
    if (!dirty || force) {
      revision = snapshot.revision;
      fields.route_kind.value = snapshot.route_kind;
      fields.provider_id.value = snapshot.provider_id;
      fields.model.value = snapshot.model;
      fields.base_url.value = snapshot.base_url;
      fields.credential_ref.value = "";
      fields.private_data_allowed.checked = snapshot.route_kind === "ollama"
        ? false : snapshot.private_data_allowed;
      fields.timeout_seconds.value = String(snapshot.timeout_seconds);
      dirty = false;
    }
    const credentialText = snapshot.route_kind === "openai_compatible"
      ? (snapshot.credential_configured
        ? " Посилання на облікові дані збережено, але навмисно не показується."
        : " Посилання на облікові дані не налаштовано.")
      : "";
    status.textContent = `Збережено: ${snapshot.provider_id}, модель ${snapshot.model}.${credentialText}`
      + (dirty ? " Є незбережені зміни." : "");
    applyRouteRules();
    return true;
  }

  async function refresh(force = false) {
    if (pending || !globalThis.pywebview?.api?.get_state) return false;
    try {
      const response = await globalThis.pywebview.api.get_state();
      return response?.ok === true && render(response.state?.v01_model_settings, force);
    } catch {
      status.textContent = "Не вдалося перечитати модель. Збережені значення не змінено.";
      return false;
    }
  }

  function payload() {
    const routeKind = fields.route_kind.value;
    const timeout = Number(fields.timeout_seconds.value);
    return {
      revision,
      schema_version: 1,
      route_kind: routeKind,
      provider_id: fields.provider_id.value.trim(),
      model: fields.model.value.trim(),
      base_url: fields.base_url.value.trim(),
      credential_ref: routeKind === "ollama" ? null : fields.credential_ref.value.trim(),
      private_data_allowed: routeKind === "ollama" ? false : fields.private_data_allowed.checked,
      timeout_seconds: timeout,
    };
  }

  async function dispatchModel(actionId) {
    if (pending || !globalThis.pywebview?.api?.dispatch) {
      announce("Міст Nika для налаштувань моделі ще не готовий.", true);
      return;
    }
    pending = true;
    setDisabled(true);
    try {
      const response = await globalThis.pywebview.api.dispatch({
        request_id: requestId(),
        action_id: actionId,
        payload: actionId === "settings.model.configure" ? payload() : {},
      });
      const failed = !["completed", "accepted"].includes(response?.status);
      announce(response?.message || "Налаштування моделі не підтверджено.", failed);
      log(response?.message);
      if (!failed) dirty = false;
    } catch {
      announce("Немає підтвердження зміни моделі. Перечитайте збережені значення перед повтором.", true);
    } finally {
      pending = false;
      await refresh(true);
      const target = actionId === "settings.model.configure" ? fields.route_kind : document.getElementById("model-settings-heading");
      target?.focus?.({ preventScroll: false });
    }
  }

  for (const field of Object.values(fields)) {
    field?.addEventListener("input", () => {
      dirty = true;
      status.textContent = "Є незбережені зміни моделі.";
    });
    field?.addEventListener("change", () => {
      dirty = true;
      applyRouteRules();
      status.textContent = "Є незбережені зміни моделі.";
    });
  }

  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("#model-save, #model-reload");
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    void dispatchModel(button.id === "model-save" ? "settings.model.configure" : "settings.model.refresh");
  }, true);

  async function initialize() {
    if (await refresh(true)) {
      if (pollHandle === null && typeof window.setInterval === "function") {
        pollHandle = window.setInterval(() => { if (!document.hidden && !dirty) void refresh(false); }, 2000);
      }
    }
  }

  window.addEventListener("pywebviewready", () => { void initialize(); });
  window.addEventListener("beforeunload", () => {
    if (pollHandle !== null && typeof window.clearInterval === "function") window.clearInterval(pollHandle);
  });
  if (globalThis.pywebview?.api) void initialize();
})();
