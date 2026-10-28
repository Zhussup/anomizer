"use strict";

/*
 * Local-only UI for Anomizer.
 *
 * The access token is read from the URL fragment once, sent to /api/auth, and
 * removed from the address bar.  It is kept only in memory; the HttpOnly
 * session cookie is the normal credential.  No data ever leaves this origin.
 */
(() => {
  const REQUEST_HEADER = "X-Anomizer-Request";
  const TOKEN_HEADER = "X-Anomizer-Token";
  const JOB_STORAGE_KEY = "anomizer.job";
  const MAX_UPLOAD_BYTES = 64 * 1024 * 1024 * 1024;
  const SVG_NS = "http://www.w3.org/2000/svg";
  const EPSILON = 0.001;

  const ui = {};
  let toastTimer = 0;

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = "ApiError";
      this.status = status;
    }
  }

  const state = {
    token: "",
    authenticated: false,
    authBusy: false,
    loaded: false,
    info: null,
    settings: null,
    detector: null,
    version: 0,
    currentTime: 0,
    playing: false,
    playStartedAt: 0,
    playStartTime: 0,
    playTimer: 0,
    playbackToken: 0,
    frameAbort: null,
    frameUrl: "",
    frameSeq: 0,
    frameTimer: 0,
    scrubbing: false,
    scrubResume: false,
    lastStillPosition: 0,
    lastStillRequestAt: 0,
    openPanelOpen: false,
    opening: false,
    uploading: false,
    uploadXhr: null,
    dirty: false,
    saving: false,
    editSeq: 0,
    saveTimer: 0,
    savePromise: Promise.resolve(),
    saveBadgeTimer: 0,
    job: null,
    jobId: "",
    jobTimer: 0,
    jobRequest: 0,
    exportBusy: false,
    previewDirty: false,
  };

  const CONTROL_IDS = [
    "blurEnabled",
    "blurMethod",
    "blurStrength",
    "blurMargin",
    "showBoxes",
    "voiceSemitones",
    "volume",
    "preserveFormants",
    "mute",
    "crf",
    "preset",
    "trimStart",
    "trimEnd",
  ];

  const BUTTON_IDS = [
    "addRegionBtn",
    "resetRegionsBtn",
    "trimStartHereBtn",
    "trimEndHereBtn",
    "trimFullBtn",
  ];

  const ALL_IDS = [
    "serverState",
    "saveState",
    "sourceTitle",
    "exportBadge",
    "openAnotherBtn",
    "logoutBtn",
    "authView",
    "authForm",
    "tokenInput",
    "authBtn",
    "authError",
    "appView",
    "openPanel",
    "closeOpenPanelBtn",
    "pathForm",
    "pathInput",
    "pathOpenBtn",
    "openPathError",
    "dropzone",
    "fileInput",
    "chooseFileBtn",
    "uploadProgressWrap",
    "uploadProgress",
    "uploadProgressText",
    "cancelUploadBtn",
    "openUploadError",
    "workView",
    "previewStage",
    "previewPlaceholder",
    "stillImage",
    "liveImage",
    "previewLoading",
    "timeBadge",
    "rangeBadge",
    "previewRes",
    "previewDecoder",
    "playBtn",
    "playIcon",
    "stillBtn",
    "transportTime",
    "playHint",
    "timeline",
    "tlTrim",
    "tlBlurGroup",
    "tlVoiceGroup",
    "tlTrimStart",
    "tlTrimEnd",
    "tlPlayhead",
    "timelineScale",
    "trimStart",
    "trimEnd",
    "trimStartNote",
    "trimEndNote",
    "trimStartHereBtn",
    "trimEndHereBtn",
    "trimFullBtn",
    "trimLength",
    "sourceName",
    "sourceMeta",
    "sourceKind",
    "sourcePrivacy",
    "changeSourceBtn",
    "closeSourceBtn",
    "blurEnabled",
    "blurMethod",
    "blurStrength",
    "blurStrengthOut",
    "blurMargin",
    "blurMarginOut",
    "showBoxes",
    "detectorWarning",
    "voiceSemitones",
    "voiceOut",
    "volume",
    "volumeOut",
    "preserveFormants",
    "mute",
    "audioBadge",
    "voiceHint",
    "regionsBody",
    "regionsEmpty",
    "regionCount",
    "addRegionBtn",
    "resetRegionsBtn",
    "crf",
    "crfOut",
    "preset",
    "exportBtn",
    "jobBox",
    "jobStatus",
    "jobPercent",
    "jobProgress",
    "jobMessage",
    "jobError",
    "downloadLink",
    "cancelJobBtn",
    "deleteJobBtn",
    "exportHint",
    "previewAudio",
    "toast",
  ];

  // ------------------------------------------------------------------ helpers
  function cacheElements() {
    for (const id of ALL_IDS) {
      ui[id] = document.getElementById(id);
    }
  }

  function clamp(value, minimum, maximum) {
    return Math.min(Math.max(value, minimum), maximum);
  }

  function numberOr(value, fallback) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function roundTime(value) {
    const duration = state.info ? numberOr(state.info.duration, 0) : 0;
    return clamp(Math.round(value * 1000) / 1000, 0, duration || value);
  }

  function pad(value, width = 2) {
    return String(value).padStart(width, "0");
  }

  function formatClock(seconds) {
    const totalMs = Math.max(0, Math.round(numberOr(seconds, 0) * 1000));
    const hours = Math.floor(totalMs / 3600000);
    const minutes = Math.floor((totalMs % 3600000) / 60000);
    const secs = Math.floor((totalMs % 60000) / 1000);
    const millis = totalMs % 1000;
    const base = hours > 0
      ? `${pad(hours)}:${pad(minutes)}:${pad(secs)}`
      : `${pad(minutes)}:${pad(secs)}`;
    return `${base}.${pad(millis, 3)}`;
  }

  function formatShortClock(seconds) {
    const total = Math.max(0, Math.floor(numberOr(seconds, 0)));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    if (hours > 0) {
      return `${pad(hours)}:${pad(minutes)}:${pad(secs)}`;
    }
    return `${pad(minutes)}:${pad(secs)}`;
  }

  function formatBytes(bytes) {
    const size = Math.max(0, numberOr(bytes, 0));
    if (size >= 1024 ** 3) {
      return `${(size / 1024 ** 3).toFixed(2)} ГБ`;
    }
    if (size >= 1024 ** 2) {
      return `${(size / 1024 ** 2).toFixed(1)} МБ`;
    }
    if (size >= 1024) {
      return `${Math.round(size / 1024)} КБ`;
    }
    return `${Math.round(size)} Б`;
  }

  function semitoneLabel(value) {
    const number = numberOr(value, 0);
    const absolute = Math.abs(number);
    const last = absolute % 10;
    const lastTwo = absolute % 100;
    let word = "полутонов";
    if (last === 1 && lastTwo !== 11) {
      word = "полутон";
    } else if (last >= 2 && last <= 4 && (lastTwo < 12 || lastTwo > 14)) {
      word = "полутона";
    }
    return `${number > 0 ? "+" : ""}${number.toFixed(1)} ${word}`;
  }

  function setText(id, value) {
    const node = ui[id];
    if (node) {
      node.textContent = value;
    }
  }

  function setHidden(id, hidden) {
    const node = ui[id];
    if (node) {
      node.hidden = Boolean(hidden);
    }
  }

  function setInlineError(id, message) {
    const node = ui[id];
    if (!node) {
      return;
    }
    node.textContent = message || "";
    node.hidden = !message;
  }

  function setDisabled(ids, disabled) {
    for (const id of ids) {
      const node = ui[id];
      if (node) {
        node.disabled = Boolean(disabled);
      }
    }
  }

  function showToast(message, { error = false, timeout = 4200 } = {}) {
    window.clearTimeout(toastTimer);
    ui.toast.textContent = message;
    ui.toast.hidden = false;
    ui.toast.classList.toggle("is-error", Boolean(error));
    // Force a style flush so the transition runs when a toast is reused.
    void ui.toast.offsetWidth;
    ui.toast.classList.add("is-visible");
    toastTimer = window.setTimeout(() => {
      ui.toast.classList.remove("is-visible");
      window.setTimeout(() => {
        ui.toast.hidden = true;
      }, 200);
    }, timeout);
  }

  function networkMessage() {
    return "Локальный сервер не отвечает. Проверьте, что программа запущена.";
  }

  // -------------------------------------------------------------------- API
  function apiHeaders(mutating) {
    const headers = {};
    if (mutating) {
      headers[REQUEST_HEADER] = "1";
    }
    if (state.token) {
      headers[TOKEN_HEADER] = state.token;
    }
    return headers;
  }

  async function readJson(response, { skipUnauthorized = false } = {}) {
    if (!response.ok) {
      let message = "Ошибка запроса.";
      if (response.status === 401) {
        message = "Нужен доступ к веб-интерфейсу.";
      } else if (response.status === 403) {
        message = "Запрос отклонён локальной защитой.";
      } else if (response.status === 413) {
        message = "Файл слишком большой.";
      }
      try {
        const payload = await response.json();
        if (payload && typeof payload.detail === "string" && payload.detail) {
          message = payload.detail;
        }
      } catch (error) {
        // A JSON error body is not guaranteed for every failure.
      }
      if (response.status === 401 && !skipUnauthorized) {
        handleUnauthorized();
      }
      throw new ApiError(message, response.status);
    }
    if (response.status === 204) {
      return null;
    }
    return response.json();
  }

  async function api(path, {
    method = "GET",
    body = undefined,
    signal = undefined,
    skipUnauthorized = false,
  } = {}) {
    const mutating = !["GET", "HEAD", "OPTIONS"].includes(method);
    const headers = apiHeaders(mutating);
    if (body !== undefined) {
      headers["Content-Type"] = "application/json";
    }
    let response;
    try {
      response = await fetch(path, {
        method,
        credentials: "same-origin",
        cache: "no-store",
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
      });
    } catch (error) {
      if (error && error.name === "AbortError") {
        throw error;
      }
      throw new ApiError(networkMessage(), 0);
    }
    return readJson(response, { skipUnauthorized });
  }

  function handleUnauthorized() {
    state.authenticated = false;
    state.token = "";
    stopPlayback({ loadStill: false });
    stopJobPolling();
    showAuth("Сессия закрыта. Введите код доступа из терминала.");
  }

  async function authenticate(token, { silent = false } = {}) {
    if (state.authBusy) {
      return false;
    }
    state.authBusy = true;
    setAuthBusy(true);
    try {
      await api("/api/auth", {
        method: "POST",
        body: { token },
        skipUnauthorized: true,
      });
      state.token = token;
      state.authenticated = true;
      state.authBusy = false;
      setAuthBusy(false);
      setInlineError("authError", "");
      showApp();
      await refreshState({ initial: true });
      return true;
    } catch (error) {
      state.authenticated = false;
      state.token = "";
      if (silent) {
        showAuth(
          error.status === 429
            ? error.message
            : "Код доступа не подошёл. Введите код из терминала."
        );
      } else {
        setAuthBusy(false);
        setInlineError(
          "authError",
          error.status === 0
            ? error.message
            : error.status === 429
              ? error.message
              : "Неверный код доступа."
        );
        if (ui.authView.hidden) {
          showAuth();
        }
        ui.tokenInput.focus();
        ui.tokenInput.select();
      }
      return false;
    } finally {
      state.authBusy = false;
      setAuthBusy(false);
    }
  }

  function setAuthBusy(busy) {
    state.authBusy = busy;
    if (!ui.authBtn) {
      return;
    }
    ui.authBtn.disabled = busy;
    ui.tokenInput.disabled = busy;
    ui.authBtn.textContent = busy ? "Проверяем…" : "Открыть интерфейс";
  }

  function showAuth(message = "") {
    state.authenticated = false;
    ui.authView.hidden = false;
    ui.appView.hidden = true;
    ui.logoutBtn.hidden = true;
    ui.openAnotherBtn.hidden = true;
    setText("serverState", "Нет доступа");
    if (message) {
      setInlineError("authError", message);
    }
    window.setTimeout(() => {
      if (!ui.authView.hidden) {
        ui.tokenInput.focus();
      }
    }, 30);
  }

  function showApp() {
    state.authenticated = true;
    ui.authView.hidden = true;
    ui.appView.hidden = false;
    ui.logoutBtn.hidden = false;
    setText("serverState", "Локальный сервер");
    renderOpenPanel();
  }

  async function logout() {
    try {
      await api("/api/auth/logout", { method: "POST", skipUnauthorized: true });
    } catch (error) {
      // Clearing the local UI must not depend on a server round trip.
    }
    stopPlayback({ loadStill: false });
    stopJobPolling();
    state.token = "";
    state.authenticated = false;
    state.loaded = false;
    state.info = null;
    state.settings = null;
    state.job = null;
    state.jobId = "";
    clearStoredJob();
    clearPreview();
    ui.tokenInput.value = "";
    setInlineError("authError", "");
    showAuth();
  }

  // ==== STATE ====
  function normalizeSettings(raw, info) {
    const duration = Math.max(0, numberOr(info && info.duration, 0));
    const source = raw && typeof raw === "object" ? raw : {};
    let start = clamp(numberOr(source.start, 0), 0, duration);
    let end = source.end == null
      ? duration
      : clamp(numberOr(source.end, duration), 0, duration);
    if (end - start < 0.02) {
      end = clamp(start + 0.02, 0, duration);
    }
    if (end - start < 0.02) {
      start = clamp(end - 0.02, 0, duration);
    }
    const regions = Array.isArray(source.regions)
      ? source.regions
          .map((region) => {
            const first = clamp(numberOr(region && region.start, 0), 0, duration);
            const last = clamp(numberOr(region && region.end, 0), 0, duration);
            return {
              start: first,
              end: last,
              blur: Boolean(region && region.blur),
              voice: Boolean(region && region.voice),
            };
          })
          .filter((region) => region.end - region.start >= EPSILON)
      : [];
    return {
      start,
      end,
      blur_enabled: source.blur_enabled !== false,
      blur_method: source.blur_method === "pixelate" ? "pixelate" : "gaussian",
      blur_strength: clamp(numberOr(source.blur_strength, 3), 1, 5),
      blur_margin: clamp(numberOr(source.blur_margin, 0.22), 0, 0.45),
      show_boxes: source.show_boxes === true,
      voice_semitones: clamp(numberOr(source.voice_semitones, -3), -12, 12),
      preserve_formants: source.preserve_formants === true,
      volume: clamp(numberOr(source.volume, 1), 0, 2),
      mute: source.mute === true,
      regions,
      crf: Math.round(clamp(numberOr(source.crf, 20), 0, 51)),
      preset: [
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
      ].includes(source.preset)
        ? source.preset
        : "veryfast",
    };
  }

  function applyServerState(payload, {
    preserveLocal = false,
    reloadFrame = false,
    discardEdits = false,
  } = {}) {
    const wasLoaded = state.loaded;
    const previousName = state.info ? state.info.name : "";
    const previousDuration = state.info
      ? numberOr(state.info.duration, 0)
      : 0;

    state.version = Math.max(0, Math.round(numberOr(payload && payload.version, 0)));
    state.loaded = Boolean(payload && payload.loaded === true);

    if (!state.loaded) {
      stopPlayback({ loadStill: false });
      state.info = null;
      state.settings = null;
      state.detector = null;
      state.currentTime = 0;
      state.openPanelOpen = true;
      state.dirty = false;
      state.editSeq += 1;
      state.saving = false;
      state.job = null;
      state.jobId = "";
      state.exportBusy = false;
      clearStoredJob();
      stopJobPolling();
      ui.saveState.hidden = true;
      clearPreview();
      renderAll({ reloadFrame: false });
      return;
    }

    state.info = payload.info || {};
    state.detector = payload.detector || { backend: "", warning: null };
    const initialLoad = !wasLoaded;
    const sourceChanged = wasLoaded && (
      previousName !== state.info.name
      || previousDuration !== numberOr(state.info.duration, 0)
    );
    const sourceReset = initialLoad || sourceChanged;

    if (state.playing && (sourceReset || !preserveLocal)) {
      stopPlayback({ loadStill: false });
    }

    if (!preserveLocal || !state.settings || discardEdits) {
      state.settings = normalizeSettings(payload.settings, state.info);
      state.dirty = false;
      state.editSeq += 1;
      window.clearTimeout(state.saveTimer);
      state.saveTimer = 0;
      renderSaveBadge("Настройки сохранены", true);
    }

    if (sourceReset) {
      state.openPanelOpen = false;
      state.currentTime = state.settings.start;
    }
    if (sourceChanged) {
      state.job = null;
      state.jobId = "";
      state.exportBusy = false;
      clearStoredJob();
      stopJobPolling();
      renderJob();
    }

    const duration = numberOr(state.info.duration, 0);
    state.currentTime = clamp(state.currentTime, 0, duration);
    renderAll({ reloadFrame: reloadFrame || sourceReset });
  }

  async function refreshState({
    initial = false,
    preserveLocal = false,
    reloadFrame = false,
    discardEdits = false,
  } = {}) {
    try {
      const payload = await api("/api/state");
      applyServerState(payload, {
        preserveLocal,
        reloadFrame: reloadFrame || initial,
        discardEdits,
      });
      if (payload && payload.loaded) {
        await restoreJob();
      }
      return payload;
    } catch (error) {
      if (error && error.status === 401) {
        return null;
      }
      if (error && error.name === "AbortError") {
        return null;
      }
      if (initial) {
        showAuth(
          error && error.status === 0
            ? error.message
            : "Не удалось загрузить состояние интерфейса."
        );
      } else {
        showToast(
          error && error.message ? error.message : networkMessage(),
          { error: true }
        );
      }
      return null;
    }
  }

  function renderAll({ reloadFrame = false } = {}) {
    renderOpenPanel();
    renderSource();
    renderControls();
    renderRegions();
    renderTimeline();
    renderTransport();
    renderAvailability();
    renderJob();
    updateExportHint();
    if (state.loaded) {
      setAspect();
      if (reloadFrame && !state.playing) {
        loadStill(state.currentTime, { force: true });
      }
    } else {
      clearPreview();
    }
  }

  function renderOpenPanel() {
    if (!state.authenticated) {
      ui.openPanel.hidden = true;
      ui.workView.hidden = true;
      return;
    }
    const open = !state.loaded || state.openPanelOpen;
    ui.openPanel.hidden = !open;
    ui.workView.hidden = !(state.loaded && !open);
    ui.closeOpenPanelBtn.hidden = !(state.loaded && open);
    ui.openAnotherBtn.hidden = !state.authenticated || open;
    if (state.loaded) {
      setText("sourceTitle", "Источник");
    }
  }

  function appendMeta(label, value) {
    const term = document.createElement("dt");
    term.textContent = label;
    const definition = document.createElement("dd");
    definition.textContent = value;
    ui.sourceMeta.append(term, definition);
  }

  function renderSource() {
    const info = state.info;
    if (!state.loaded || !info) {
      setText("sourceName", "—");
      setText("sourceKind", "—");
      ui.sourceKind.className = "badge";
      setText("sourcePrivacy", "Оригинал не изменяется. Результат экспортируется в отдельный файл.");
      ui.sourceMeta.replaceChildren();
      setText("previewRes", "—");
      setHidden("previewDecoder", true);
      setText("audioBadge", "—");
      ui.audioBadge.className = "badge";
      setHidden("detectorWarning", true);
      return;
    }

    setText("sourceName", info.name || "Видео");
    setText("sourceKind", info.uploaded ? "Загрузка" : "С диска");
    ui.sourceKind.className = info.uploaded ? "badge badge-ok" : "badge";
    setText(
      "sourcePrivacy",
      info.uploaded
        ? "Временная копия удалится при закрытии видео или остановке сервера. Оригинал на диске не изменяется."
        : "Файл читается с диска только для чтения. Оригинал не изменяется, результат — отдельный MP4."
    );

    ui.sourceMeta.replaceChildren();
    appendMeta(
      "Разрешение",
      `${numberOr(info.width, 0)}×${numberOr(info.height, 0)}`
    );
    appendMeta(
      "Частота кадров",
      `${numberOr(info.fps, 0).toFixed(2)} кадр/с`
    );
    appendMeta("Длительность", formatClock(info.duration));
    appendMeta("Кадров", String(Math.max(0, Math.round(numberOr(info.frame_count, 0)))));
    appendMeta(
      "Звук",
      info.has_audio
        ? `${Math.round(numberOr(info.sample_rate, 0))} Гц`
        : "нет"
    );

    setText(
      "previewRes",
      `${numberOr(info.width, 0)}×${numberOr(info.height, 0)}`
    );
    setText("previewDecoder", info.decode_with_ffmpeg ? "FFmpeg" : "OpenCV");
    setHidden("previewDecoder", false);

    const warning = state.detector && state.detector.warning;
    if (warning) {
      setText("detectorWarning", warning);
      setHidden("detectorWarning", false);
    } else {
      setHidden("detectorWarning", true);
    }
  }

  function resetControlValues() {
    ui.blurEnabled.checked = true;
    ui.blurMethod.value = "gaussian";
    ui.blurStrength.value = "3";
    ui.blurMargin.value = "0.22";
    ui.showBoxes.checked = false;
    ui.voiceSemitones.value = "-3";
    ui.volume.value = "1";
    ui.preserveFormants.checked = false;
    ui.mute.checked = false;
    ui.crf.value = "20";
    ui.preset.value = "veryfast";
    ui.trimStart.value = "";
    ui.trimEnd.value = "";
    setText("trimStartNote", "—");
    setText("trimEndNote", "—");
    setText("trimLength", "Длительность: —");
    setText("rangeBadge", "");
    setText("blurStrengthOut", "3.0");
    setText("blurMarginOut", "22%");
    setText("voiceOut", semitoneLabel(-3));
    setText("volumeOut", "100%");
    setText("crfOut", "20");
  }

  function renderControls() {
    if (!state.loaded || !state.settings) {
      resetControlValues();
      return;
    }
    const settings = state.settings;
    ui.blurEnabled.checked = settings.blur_enabled;
    ui.blurMethod.value = settings.blur_method;
    ui.blurStrength.value = String(settings.blur_strength);
    ui.blurMargin.value = String(settings.blur_margin);
    ui.showBoxes.checked = settings.show_boxes;
    ui.voiceSemitones.value = String(settings.voice_semitones);
    ui.volume.value = String(settings.volume);
    ui.preserveFormants.checked = settings.preserve_formants;
    ui.mute.checked = settings.mute;
    ui.crf.value = String(settings.crf);
    ui.preset.value = settings.preset;

    ui.trimStart.value = settings.start.toFixed(2);
    ui.trimEnd.value = settings.end.toFixed(2);
    renderControlLabels();
  }

  function renderControlLabels() {
    const settings = state.settings;
    if (!state.loaded || !settings) {
      return;
    }
    setText("blurStrengthOut", numberOr(settings.blur_strength, 3).toFixed(1));
    setText(
      "blurMarginOut",
      `${Math.round(clamp(numberOr(settings.blur_margin, 0.22), 0, 0.45) * 100)}%`
    );
    setText("voiceOut", semitoneLabel(settings.voice_semitones));
    setText(
      "volumeOut",
      `${Math.round(clamp(numberOr(settings.volume, 1), 0, 2) * 100)}%`
    );
    setText("crfOut", String(Math.round(numberOr(settings.crf, 20))));
    setText("trimStartNote", formatClock(settings.start));
    setText("trimEndNote", formatClock(settings.end));
    setText(
      "trimLength",
      `Длительность: ${formatClock(settings.end - settings.start)}`
    );
    setText(
      "rangeBadge",
      `${formatClock(settings.start)} — ${formatClock(settings.end)}`
    );

    const info = state.info || {};
    if (!info.has_audio) {
      setText("audioBadge", "Без звука");
      ui.audioBadge.className = "badge";
      setText("voiceHint", "В этом файле нет звуковой дорожки.");
    } else if (settings.mute) {
      setText("audioBadge", "Звук выключен");
      ui.audioBadge.className = "badge";
      setText("voiceHint", "Звук будет удалён из результата.");
    } else {
      setText("audioBadge", "Звук есть");
      ui.audioBadge.className = "badge badge-ok";
      setText("voiceHint", "Голос меняется только внутри выбранных диапазонов.");
    }
  }

  function validateSettings() {
    const settings = state.settings;
    if (!state.loaded || !settings) {
      return "Видео не открыто.";
    }
    const duration = numberOr(state.info && state.info.duration, 0);
    if (!Number.isFinite(settings.start) || !Number.isFinite(settings.end)) {
      return "Проверьте начало и конец фрагмента.";
    }
    if (settings.start < 0 || settings.end > duration + 0.0005) {
      return "Фрагмент выходит за границы видео.";
    }
    if (settings.end - settings.start < 0.02) {
      return "Фрагмент должен быть длиннее 0,02 секунды.";
    }
    for (let index = 0; index < settings.regions.length; index += 1) {
      const region = settings.regions[index];
      if (!Number.isFinite(region.start) || !Number.isFinite(region.end)) {
        return `Диапазон ${index + 1}: проверьте начало и конец.`;
      }
      if (
        region.start < 0
        || region.end > duration + 0.0005
        || region.end - region.start < EPSILON
      ) {
        return `Диапазон ${index + 1}: конец должен быть больше начала.`;
      }
    }
    return "";
  }

  function buildPayload() {
    const settings = state.settings;
    const duration = numberOr(state.info && state.info.duration, 0);
    return {
      start: roundTime(settings.start),
      end: clamp(roundTime(settings.end), 0, duration),
      blur_enabled: Boolean(settings.blur_enabled),
      blur_method: settings.blur_method,
      blur_strength: Number(settings.blur_strength),
      blur_margin: Number(settings.blur_margin),
      show_boxes: Boolean(settings.show_boxes),
      voice_semitones: Number(settings.voice_semitones),
      preserve_formants: Boolean(settings.preserve_formants),
      volume: Number(settings.volume),
      mute: Boolean(settings.mute),
      regions: settings.regions.map((region) => ({
        start: roundTime(region.start),
        end: clamp(roundTime(region.end), 0, duration),
        blur: Boolean(region.blur),
        voice: Boolean(region.voice),
      })),
      crf: Math.round(Number(settings.crf)),
      preset: settings.preset,
    };
  }

  function updateExportHint() {
    const settings = state.settings;
    if (!state.loaded || !settings) {
      setText("exportHint", "Сначала откройте видео.");
      return;
    }
    const problem = validateSettings();
    if (problem) {
      setText("exportHint", problem);
      return;
    }
    const fps = Math.max(1, numberOr(state.info && state.info.fps, 1));
    const frames = Math.max(1, Math.round((settings.end - settings.start) * fps));
    setText(
      "exportHint",
      `Фрагмент ${formatClock(settings.start)} — ${formatClock(settings.end)} · ~${frames} кадров. Оригинал не изменяется.`
    );
  }

  function renderAvailability() {
    const loaded = state.loaded;
    const running = Boolean(state.job && state.job.status === "running");
    const sourceBusy = state.opening || state.uploading || running;

    setDisabled(CONTROL_IDS, !loaded || state.opening || state.uploading);
    setDisabled(BUTTON_IDS, !loaded || state.opening || state.uploading);
    ui.addRegionBtn.disabled = !loaded
      || state.opening
      || state.uploading
      || (state.settings && state.settings.regions.length >= 200);
    ui.resetRegionsBtn.disabled = !loaded || state.opening || state.uploading;

    ui.playBtn.disabled = !loaded || state.opening;
    ui.stillBtn.disabled = !loaded || state.opening;

    ui.pathInput.disabled = state.opening || state.uploading;
    ui.pathOpenBtn.disabled = state.opening || state.uploading;
    ui.fileInput.disabled = state.opening || state.uploading;
    ui.cancelUploadBtn.hidden = !state.uploading;
    ui.uploadProgressWrap.hidden = !state.uploading;

    ui.changeSourceBtn.disabled = !loaded || sourceBusy;
    ui.closeSourceBtn.disabled = !loaded || sourceBusy;
    const invalid = !loaded || Boolean(validateSettings());
    ui.exportBtn.disabled = invalid
      || running
      || state.exportBusy
      || state.opening
      || state.uploading;
    ui.openAnotherBtn.disabled = state.opening || state.uploading;
  }

  function setAspect() {
    if (!state.loaded || !state.info) {
      return;
    }
    const width = Math.max(1, numberOr(state.info.width, 1));
    const height = Math.max(1, numberOr(state.info.height, 1));
    ui.previewStage.style.setProperty("--preview-aspect", `${width} / ${height}`);
  }

  function clearPreview() {
    state.frameSeq += 1;
    if (state.frameAbort) {
      state.frameAbort.abort();
      state.frameAbort = null;
    }
    window.clearTimeout(state.frameTimer);
    state.frameTimer = 0;
    if (state.frameUrl) {
      URL.revokeObjectURL(state.frameUrl);
      state.frameUrl = "";
    }
    ui.stillImage.removeAttribute("src");
    ui.stillImage.hidden = true;
    ui.liveImage.removeAttribute("src");
    ui.liveImage.hidden = true;
    ui.previewPlaceholder.hidden = false;
    ui.previewLoading.hidden = true;
    setText("timeBadge", "00:00.000");
    setText("rangeBadge", "");
    setText("previewRes", "—");
    setHidden("previewDecoder", true);
    ui.previewStage.style.removeProperty("--preview-aspect");
    stopAudioElement();
  }

  // ==== OPEN ====
  function finishOpen(payload) {
    stopPlayback({ loadStill: false });
    state.openPanelOpen = false;
    state.dirty = false;
    state.editSeq += 1;
    window.clearTimeout(state.saveTimer);
    state.saveTimer = 0;
    state.job = null;
    state.jobId = "";
    clearStoredJob();
    stopJobPolling();
    applyServerState(payload, { discardEdits: true, reloadFrame: false });
    if (state.loaded && state.settings) {
      state.currentTime = state.settings.start;
      renderTransport();
      renderTimeline();
      loadStill(state.currentTime, { force: true });
      renderSaveBadge("Настройки сохранены", true);
      showToast(`Открыто: ${state.info.name}`);
    }
    renderAvailability();
    renderJob();
    updateExportHint();
  }

  async function openByPath(event) {
    event.preventDefault();
    if (state.opening || state.uploading) {
      return;
    }
    const path = ui.pathInput.value.trim();
    if (!path) {
      setInlineError("openPathError", "Укажите полный путь к видео.");
      return;
    }
    setInlineError("openPathError", "");
    setInlineError("openUploadError", "");
    state.opening = true;
    renderAvailability();
    try {
      const payload = await api("/api/open", {
        method: "POST",
        body: { path },
      });
      finishOpen(payload);
    } catch (error) {
      if (error && error.name !== "AbortError") {
        setInlineError(
          "openPathError",
          error.message || "Не удалось открыть видео."
        );
      }
    } finally {
      state.opening = false;
      renderAvailability();
    }
  }

  function xhrError(xhr) {
    let message = "Не удалось загрузить файл.";
    if (xhr.status === 401) {
      message = "Нужен доступ к веб-интерфейсу.";
    } else if (xhr.status === 413) {
      message = "Файл слишком большой для веб-загрузки.";
    } else if (xhr.status === 409) {
      message = "Дождитесь завершения текущего экспорта.";
    }
    try {
      const payload = JSON.parse(xhr.responseText || "{}");
      if (payload && typeof payload.detail === "string" && payload.detail) {
        message = payload.detail;
      }
    } catch (error) {
      // Keep the status-based message.
    }
    return message;
  }

  function uploadFile(file) {
    if (!file || state.uploading || state.opening) {
      return;
    }
    setInlineError("openUploadError", "");
    setInlineError("openPathError", "");
    if (file.size <= 0) {
      setInlineError("openUploadError", "Файл пустой.");
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setInlineError(
        "openUploadError",
        "Файл больше 64 ГБ. Откройте его по пути с диска."
      );
      return;
    }

    const xhr = new XMLHttpRequest();
    state.uploadXhr = xhr;
    state.uploading = true;
    ui.uploadProgress.value = 0;
    setText("uploadProgressText", "0%");
    renderAvailability();

    xhr.open(
      "POST",
      `/api/upload?name=${encodeURIComponent(file.name)}`,
      true
    );
    xhr.setRequestHeader(REQUEST_HEADER, "1");
    if (state.token) {
      xhr.setRequestHeader(TOKEN_HEADER, state.token);
    }
    xhr.setRequestHeader("Content-Type", "application/octet-stream");

    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) {
        setText("uploadProgressText", "Загрузка…");
        return;
      }
      const fraction = event.total > 0 ? event.loaded / event.total : 0;
      ui.uploadProgress.value = clamp(fraction, 0, 1);
      setText("uploadProgressText", `${Math.round(fraction * 100)}%`);
    });

    xhr.addEventListener("load", () => {
      if (state.uploadXhr !== xhr) {
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        let payload = null;
        try {
          payload = JSON.parse(xhr.responseText || "{}");
        } catch (error) {
          payload = null;
        }
        cleanupUpload();
        if (payload && payload.loaded) {
          ui.uploadProgress.value = 1;
          setText("uploadProgressText", "100%");
          finishOpen(payload);
        } else {
          setInlineError(
            "openUploadError",
            "Сервер не подтвердил открытие файла."
          );
        }
      } else {
        const message = xhrError(xhr);
        cleanupUpload();
        if (xhr.status === 401) {
          handleUnauthorized();
        } else {
          setInlineError("openUploadError", message);
        }
      }
    });

    xhr.addEventListener("error", () => {
      if (state.uploadXhr !== xhr) {
        return;
      }
      cleanupUpload();
      setInlineError("openUploadError", networkMessage());
    });

    xhr.addEventListener("abort", () => {
      if (state.uploadXhr !== xhr) {
        return;
      }
      cleanupUpload();
      setInlineError("openUploadError", "Загрузка отменена.");
    });

    try {
      xhr.send(file);
    } catch (error) {
      cleanupUpload();
      setInlineError("openUploadError", "Не удалось начать загрузку.");
    }
  }

  function cleanupUpload() {
    if (state.uploadXhr) {
      const xhr = state.uploadXhr;
      state.uploadXhr = null;
      try {
        xhr.abort();
      } catch (error) {
        // Already finished.
      }
    }
    state.uploading = false;
    ui.uploadProgressWrap.hidden = true;
    ui.uploadProgress.value = 0;
    setText("uploadProgressText", "0%");
    renderAvailability();
  }

  function cancelUpload() {
    if (!state.uploadXhr) {
      return;
    }
    const xhr = state.uploadXhr;
    try {
      xhr.abort();
    } catch (error) {
      cleanupUpload();
    }
  }

  async function closeSource() {
    if (!state.loaded) {
      return;
    }
    if (state.job && state.job.status === "running") {
      showToast("Сначала дождитесь экспорта или отмените его.", { error: true });
      return;
    }
    stopPlayback({ loadStill: false });
    try {
      const payload = await api("/api/close", { method: "POST" });
      state.job = null;
      state.jobId = "";
      clearStoredJob();
      stopJobPolling();
      state.openPanelOpen = true;
      applyServerState(payload, { discardEdits: true });
      renderJob();
      showToast("Видео закрыто. Временные файлы удалены.");
    } catch (error) {
      if (error && error.name !== "AbortError") {
        showToast(error.message || "Не удалось закрыть видео.", { error: true });
      }
    }
  }

  function showOpenPanel() {
    if (!state.authenticated) {
      return;
    }
    setInlineError("openPathError", "");
    setInlineError("openUploadError", "");
    state.openPanelOpen = true;
    renderOpenPanel();
    renderAvailability();
    window.setTimeout(() => {
      if (!ui.openPanel.hidden) {
        ui.pathInput.focus();
      }
    }, 30);
  }

  function hideOpenPanel() {
    if (!state.loaded) {
      return;
    }
    state.openPanelOpen = false;
    renderOpenPanel();
    renderAvailability();
  }

  // ==== SETTINGS ====
  function renderSaveBadge(message, autoHide = false) {
    window.clearTimeout(state.saveBadgeTimer);
    if (!state.loaded) {
      ui.saveState.hidden = true;
      return;
    }
    ui.saveState.hidden = false;
    ui.saveState.textContent = message;
    if (autoHide) {
      state.saveBadgeTimer = window.setTimeout(() => {
        ui.saveState.hidden = true;
      }, 2200);
    }
  }

  function markEdited({ timeline = false, preview = false, delay = 400 } = {}) {
    if (!state.loaded) {
      return;
    }
    state.dirty = true;
    state.editSeq += 1;
    if (preview) {
      state.previewDirty = true;
    }
    if (state.playing) {
      stopPlayback({ loadStill: false });
    }
    renderControlLabels();
    if (timeline) {
      renderTimeline();
    }
    updateExportHint();
    renderAvailability();
    renderSaveBadge("Не сохранено");
    scheduleSave(delay);
  }

  function scheduleSave(delay = 400) {
    if (!state.loaded) {
      return;
    }
    window.clearTimeout(state.saveTimer);
    state.saveTimer = window.setTimeout(() => {
      state.saveTimer = 0;
      startSave();
    }, delay);
  }

  function startSave() {
    state.savePromise = state.savePromise.then(
      () => saveLoop(),
      () => saveLoop()
    );
    return state.savePromise;
  }

  async function saveLoop() {
    if (!state.loaded || !state.dirty) {
      return;
    }
    const problem = validateSettings();
    if (problem) {
      renderSaveBadge("Проверьте значения");
      updateExportHint();
      renderAvailability();
      return;
    }
    if (state.playing) {
      stopPlayback({ loadStill: false });
    }

    const seq = state.editSeq;
    const payload = buildPayload();
    state.saving = true;
    renderSaveBadge("Сохранение…");
    try {
      const data = await api(
        `/api/settings?v=${state.version}`,
        { method: "PUT", body: payload }
      );
      applySaveResult(data, seq);
    } catch (error) {
      state.saving = false;
      if (error && error.name === "AbortError") {
        return;
      }
      if (error && error.status === 409) {
        renderSaveBadge("Не сохранено");
        showToast(
          "Настройки изменились в другой вкладке. Состояние обновлено.",
          { error: true }
        );
        await refreshState({ reloadFrame: true, discardEdits: true });
      } else if (error && error.status !== 401) {
        renderSaveBadge("Не сохранено");
        showToast(error.message || networkMessage(), { error: true });
      }
      return;
    }
    state.saving = false;

    if (state.editSeq !== seq) {
      scheduleSave(60);
      return;
    }
    state.dirty = false;
    state.previewDirty = false;
    renderSaveBadge("Настройки сохранены", true);
    if (!state.playing) {
      loadStill(state.currentTime, { force: true });
    }
  }

  function adoptServerSettings(raw) {
    const normalized = normalizeSettings(raw, state.info);
    if (!state.settings) {
      state.settings = normalized;
      return true;
    }
    const current = state.settings;
    for (const key of Object.keys(normalized)) {
      if (key !== "regions") {
        current[key] = normalized[key];
      }
    }
    if (current.regions.length !== normalized.regions.length) {
      current.regions = normalized.regions;
      return true;
    }
    // Mutate the existing region objects so table event handlers, which close
    // over them, keep editing the live values.
    for (let index = 0; index < normalized.regions.length; index += 1) {
      const target = current.regions[index];
      const source = normalized.regions[index];
      target.start = source.start;
      target.end = source.end;
      target.blur = source.blur;
      target.voice = source.voice;
    }
    return false;
  }

  function applySaveResult(data, seq) {
    state.version = Math.max(0, Math.round(numberOr(data.version, state.version)));
    state.info = data.info || state.info;
    state.detector = data.detector || state.detector;
    let rerenderRegions = false;
    if (seq === state.editSeq) {
      rerenderRegions = adoptServerSettings(data.settings);
      state.dirty = false;
    }
    renderSource();
    if (rerenderRegions) {
      renderRegions();
    }
    renderControlLabels();
    renderTimeline();
    renderAvailability();
    updateExportHint();
  }

  async function flushSave() {
    if (!state.loaded) {
      return;
    }
    window.clearTimeout(state.saveTimer);
    state.saveTimer = 0;
    const problem = validateSettings();
    if (problem) {
      throw new Error(problem);
    }
    let attempts = 0;
    while (state.dirty && attempts < 2) {
      attempts += 1;
      await startSave();
    }
    if (state.dirty) {
      throw new Error("Не удалось сохранить настройки.");
    }
  }

  function trimValue(input) {
    const raw = String(input.value).trim().replace(",", ".");
    if (!raw) {
      return null;
    }
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }

  function trimEdited(field) {
    const settings = state.settings;
    if (!state.loaded || !settings) {
      return;
    }
    const input = field === "start" ? ui.trimStart : ui.trimEnd;
    const value = trimValue(input);
    if (value === null) {
      input.classList.add("is-invalid");
      return;
    }
    input.classList.remove("is-invalid");
    const duration = numberOr(state.info && state.info.duration, 0);
    settings[field] = clamp(value, 0, duration);
    markEdited({ timeline: true, preview: true });
  }

  function trimCommitted(field) {
    const settings = state.settings;
    if (!state.loaded || !settings) {
      return;
    }
    const input = field === "start" ? ui.trimStart : ui.trimEnd;
    const duration = numberOr(state.info && state.info.duration, 0);
    let value = trimValue(input);
    if (value === null) {
      value = settings[field];
    }
    value = clamp(value, 0, duration);
    if (field === "start") {
      if (settings.end - value < 0.02) {
        value = Math.max(0, settings.end - 0.02);
      }
      settings.start = value;
    } else {
      if (value - settings.start < 0.02) {
        value = Math.min(duration, settings.start + 0.02);
      }
      settings.end = value;
    }
    input.classList.remove("is-invalid");
    input.value = settings[field].toFixed(2);
    markEdited({ timeline: true, preview: true, delay: 120 });
  }

  function setTrimStartHere() {
    if (!state.loaded || !state.settings) {
      return;
    }
    const value = roundTime(state.currentTime);
    if (state.settings.end - value < 0.02) {
      showToast("Кадр слишком близко к концу фрагмента.", { error: true });
      return;
    }
    state.settings.start = value;
    ui.trimStart.value = value.toFixed(2);
    markEdited({ timeline: true, preview: true, delay: 120 });
  }

  function setTrimEndHere() {
    if (!state.loaded || !state.settings) {
      return;
    }
    const value = roundTime(state.currentTime);
    if (value - state.settings.start < 0.02) {
      showToast("Кадр слишком близко к началу фрагмента.", { error: true });
      return;
    }
    state.settings.end = value;
    ui.trimEnd.value = value.toFixed(2);
    markEdited({ timeline: true, preview: true, delay: 120 });
  }

  function setTrimFull() {
    if (!state.loaded || !state.settings) {
      return;
    }
    const duration = Math.max(0, numberOr(state.info && state.info.duration, 0));
    state.settings.start = 0;
    state.settings.end = duration;
    ui.trimStart.value = "0.00";
    ui.trimEnd.value = duration.toFixed(2);
    markEdited({ timeline: true, preview: true, delay: 120 });
  }

  function renderRegions() {
    ui.regionsBody.replaceChildren();
    if (!state.loaded || !state.settings) {
      setText("regionCount", "0");
      ui.regionsEmpty.hidden = false;
      return;
    }
    const regions = state.settings.regions;
    const duration = Math.max(0, numberOr(state.info && state.info.duration, 0));
    setText("regionCount", String(regions.length));
    ui.regionsEmpty.hidden = regions.length > 0;

    regions.forEach((region, index) => {
      const row = document.createElement("tr");
      const startCell = document.createElement("td");
      const endCell = document.createElement("td");
      const blurCell = document.createElement("td");
      const voiceCell = document.createElement("td");
      const actionsCell = document.createElement("td");

      const startInput = document.createElement("input");
      startInput.type = "number";
      startInput.min = "0";
      startInput.max = duration.toFixed(3);
      startInput.step = "0.01";
      startInput.value = Number(region.start).toFixed(2);
      startInput.setAttribute("aria-label", `Начало диапазона ${index + 1}`);
      startInput.addEventListener("input", () => {
        const value = trimValue(startInput);
        if (value === null) {
          startInput.classList.add("is-invalid");
          return;
        }
        region.start = clamp(value, 0, duration);
        updateRegionValidity(startInput, region);
        regionEdited();
      });
      startInput.addEventListener("change", () => {
        normalizeRegionBoundary(region, "start", duration, startInput);
      });

      const endInput = document.createElement("input");
      endInput.type = "number";
      endInput.min = "0";
      endInput.max = duration.toFixed(3);
      endInput.step = "0.01";
      endInput.value = Number(region.end).toFixed(2);
      endInput.setAttribute("aria-label", `Конец диапазона ${index + 1}`);
      endInput.addEventListener("input", () => {
        const value = trimValue(endInput);
        if (value === null) {
          endInput.classList.add("is-invalid");
          return;
        }
        region.end = clamp(value, 0, duration);
        updateRegionValidity(endInput, region);
        regionEdited();
      });
      endInput.addEventListener("change", () => {
        normalizeRegionBoundary(region, "end", duration, endInput);
      });

      const blurBox = document.createElement("input");
      blurBox.type = "checkbox";
      blurBox.checked = Boolean(region.blur);
      blurBox.setAttribute("aria-label", `Блюр в диапазоне ${index + 1}`);
      blurBox.addEventListener("change", () => {
        region.blur = blurBox.checked;
        regionEdited();
      });

      const voiceBox = document.createElement("input");
      voiceBox.type = "checkbox";
      voiceBox.checked = Boolean(region.voice);
      voiceBox.setAttribute("aria-label", `Голос в диапазоне ${index + 1}`);
      voiceBox.addEventListener("change", () => {
        region.voice = voiceBox.checked;
        regionEdited();
      });

      const splitButton = document.createElement("button");
      splitButton.type = "button";
      splitButton.className = "icon-action";
      splitButton.textContent = "✂";
      splitButton.title = "Разделить по текущему кадру";
      splitButton.setAttribute("aria-label", `Разделить диапазон ${index + 1}`);
      splitButton.addEventListener("click", () => splitRegion(index));

      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "icon-action is-danger";
      deleteButton.textContent = "✕";
      deleteButton.title = "Удалить диапазон";
      deleteButton.setAttribute("aria-label", `Удалить диапазон ${index + 1}`);
      deleteButton.addEventListener("click", () => deleteRegion(index));

      const actions = document.createElement("div");
      actions.className = "row-actions";
      actions.append(splitButton, deleteButton);

      startCell.append(startInput);
      endCell.append(endInput);
      blurCell.append(blurBox);
      voiceCell.append(voiceBox);
      actionsCell.append(actions);
      row.append(startCell, endCell, blurCell, voiceCell, actionsCell);
      ui.regionsBody.append(row);
      updateRegionValidity(startInput, region);
      updateRegionValidity(endInput, region);
    });
  }

  function updateRegionValidity(input, region) {
    const invalid = region.end - region.start < EPSILON;
    input.classList.toggle("is-invalid", invalid);
  }

  function normalizeRegionBoundary(region, field, duration, input) {
    let value = trimValue(input);
    if (value === null) {
      value = region[field];
    }
    value = clamp(value, 0, duration);
    if (field === "start") {
      if (region.end - value < EPSILON) {
        value = Math.max(0, region.end - 0.01);
      }
      region.start = value;
    } else {
      if (value - region.start < EPSILON) {
        value = Math.min(duration, region.start + 0.01);
      }
      region.end = value;
    }
    input.value = region[field].toFixed(2);
    updateRegionValidity(input, region);
    regionEdited();
  }

  function regionEdited() {
    if (!state.loaded || !state.settings) {
      return;
    }
    markEdited({ timeline: true, preview: true, delay: 500 });
  }

  function addRegion() {
    if (!state.loaded || !state.settings) {
      return;
    }
    const duration = Math.max(0, numberOr(state.info && state.info.duration, 0));
    let start = clamp(state.currentTime, 0, duration);
    let end = Math.min(duration, start + 5);
    if (end - start < 0.05) {
      start = Math.max(0, duration - 1);
      end = duration;
    }
    if (end - start < EPSILON) {
      showToast("Не удалось создать диапазон в этом месте.", { error: true });
      return;
    }
    state.settings.regions.push({
      start,
      end,
      blur: true,
      voice: true,
    });
    state.settings.regions.sort((left, right) => left.start - right.start);
    renderRegions();
    regionEdited();
  }

  function splitRegion(index) {
    if (!state.loaded || !state.settings) {
      return;
    }
    const region = state.settings.regions[index];
    if (!region) {
      return;
    }
    const at = state.currentTime;
    if (at <= region.start + EPSILON || at >= region.end - EPSILON) {
      showToast("Поставьте курсор внутрь диапазона, чтобы разделить его.", {
        error: true,
      });
      return;
    }
    state.settings.regions.splice(
      index,
      1,
      { start: region.start, end: at, blur: region.blur, voice: region.voice },
      { start: at, end: region.end, blur: region.blur, voice: region.voice }
    );
    renderRegions();
    regionEdited();
  }

  function deleteRegion(index) {
    if (!state.loaded || !state.settings) {
      return;
    }
    state.settings.regions.splice(index, 1);
    renderRegions();
    regionEdited();
  }

  function resetRegions() {
    if (!state.loaded || !state.settings) {
      return;
    }
    const duration = Math.max(0, numberOr(state.info && state.info.duration, 0));
    if (duration < EPSILON) {
      return;
    }
    state.settings.regions = [
      { start: 0, end: duration, blur: true, voice: true },
    ];
    renderRegions();
    regionEdited();
  }

  // ==== PREVIEW ====
  function duration() {
    return Math.max(0, numberOr(state.info && state.info.duration, 0));
  }

  function trimStart() {
    return state.settings ? numberOr(state.settings.start, 0) : 0;
  }

  function trimEnd() {
    return state.settings
      ? numberOr(state.settings.end, duration())
      : duration();
  }

  function stopAudioElement() {
    const audio = ui.previewAudio;
    if (!audio) {
      return;
    }
    try {
      audio.pause();
    } catch (error) {
      // A paused or unloaded element is fine.
    }
    audio.removeAttribute("src");
    try {
      audio.load();
    } catch (error) {
      // Ignore unsupported media resets.
    }
  }

  function frameErrorMessage(response) {
    if (response.status === 404) {
      return "Кадр недоступен.";
    }
    if (response.status === 409) {
      return "Настройки изменились.";
    }
    return "Не удалось показать кадр.";
  }

  async function loadStill(seconds, { force = false } = {}) {
    if (!state.loaded || state.playing) {
      return;
    }
    const total = duration();
    const requested = clamp(numberOr(seconds, 0), 0, total);
    if (
      !force
      && state.lastStillPosition
      && Math.abs(state.lastStillPosition - requested) < 0.0005
    ) {
      return;
    }
    state.frameSeq += 1;
    const seq = state.frameSeq;
    if (state.frameAbort) {
      state.frameAbort.abort();
    }
    const controller = new AbortController();
    state.frameAbort = controller;
    window.clearTimeout(state.frameTimer);
    state.frameTimer = window.setTimeout(() => {
      if (seq === state.frameSeq && !state.playing) {
        ui.previewLoading.hidden = false;
      }
    }, 180);

    try {
      const response = await fetch(
        `/api/frame?t=${requested.toFixed(3)}&v=${state.version}`,
        {
          credentials: "same-origin",
          cache: "no-store",
          headers: apiHeaders(false),
          signal: controller.signal,
        }
      );
      if (!response.ok) {
        if (response.status === 401) {
          handleUnauthorized();
        }
        throw new ApiError(frameErrorMessage(response), response.status);
      }
      const blob = await response.blob();
      if (seq !== state.frameSeq || state.playing || !blob.size) {
        return;
      }
      const frameTime = Number(response.headers.get("X-Frame-Time"));
      const url = URL.createObjectURL(blob);
      if (state.frameUrl) {
        URL.revokeObjectURL(state.frameUrl);
      }
      state.frameUrl = url;
      state.lastStillPosition = requested;
      ui.stillImage.src = url;
      ui.stillImage.hidden = false;
      ui.previewPlaceholder.hidden = true;
      ui.liveImage.hidden = true;
      ui.previewLoading.hidden = true;
      if (Number.isFinite(frameTime)) {
        setText("timeBadge", formatClock(frameTime));
      } else {
        setText("timeBadge", formatClock(requested));
      }
    } catch (error) {
      if (!error || error.name === "AbortError" || seq !== state.frameSeq) {
        return;
      }
      ui.previewLoading.hidden = true;
      if (error.status === 409) {
        refreshState({ reloadFrame: true });
        return;
      }
      if (error.status === 401) {
        return;
      }
      if (error.status !== 0) {
        showToast(error.message || "Не удалось показать кадр.", { error: true });
      }
    } finally {
      if (seq === state.frameSeq) {
        window.clearTimeout(state.frameTimer);
        state.frameTimer = 0;
        ui.previewLoading.hidden = true;
        state.frameAbort = null;
      }
    }
  }

  function renderTransport() {
    const total = duration();
    const current = state.loaded
      ? clamp(state.currentTime, 0, total)
      : 0;
    setText("timeBadge", formatClock(current));
    setText(
      "transportTime",
      `${formatClock(current)} / ${formatClock(total)}`
    );
    setText("playIcon", state.playing ? "❚❚" : "▶");
    ui.playBtn.setAttribute(
      "aria-label",
      state.playing ? "Пауза" : "Воспроизвести"
    );
    ui.playBtn.setAttribute("aria-pressed", state.playing ? "true" : "false");
    if (!state.loaded) {
      setText("playHint", "Пробел — играть/пауза");
    } else if (!state.info || !state.info.has_audio) {
      setText("playHint", "Видео без звука");
    } else if (state.settings && state.settings.mute) {
      setText("playHint", "Звук отключён в настройках");
    } else {
      setText("playHint", "Пробел — играть/пауза");
    }
    ui.timeline.setAttribute("aria-valuemin", "0");
    ui.timeline.setAttribute("aria-valuemax", total.toFixed(3));
    ui.timeline.setAttribute("aria-valuenow", current.toFixed(3));
    ui.timeline.setAttribute("aria-valuetext", formatClock(current));
    renderTimelinePlayhead();
  }

  function startPlayback() {
    if (!state.loaded || !state.settings || state.playing) {
      return;
    }
    const start = trimStart();
    const end = trimEnd();
    if (end - start < 0.02) {
      showToast("Фрагмент пустой. Проверьте начало и конец.", { error: true });
      return;
    }
    if (state.currentTime < start || state.currentTime >= end - 0.02) {
      state.currentTime = start;
    }
    state.playing = true;
    state.playbackToken += 1;
    const token = state.playbackToken;
    state.playStartedAt = performance.now();
    state.playStartTime = state.currentTime;

    if (state.frameAbort) {
      state.frameAbort.abort();
      state.frameAbort = null;
    }
    window.clearTimeout(state.frameTimer);
    state.frameTimer = 0;
    ui.previewPlaceholder.hidden = true;
    ui.previewLoading.hidden = true;
    ui.stillImage.hidden = true;
    ui.liveImage.hidden = false;
    ui.liveImage.onerror = () => {
      if (!state.playing || state.playbackToken !== token) {
        return;
      }
      pausePlayback({ loadStill: true });
      showToast(
        "Не удалось запустить видеопоток. Возможно, браузер блокирует cookie.",
        { error: true, timeout: 6000 }
      );
    };

    const query = `from=${state.currentTime.toFixed(3)}&v=${state.version}`;
    ui.liveImage.src = `/api/stream?${query}`;

    if (state.info && state.info.has_audio && !state.settings.mute) {
      ui.previewAudio.src = `/api/audio?${query}`;
      const promise = ui.previewAudio.play();
      if (promise && typeof promise.catch === "function") {
        promise.catch(() => {
          if (state.playing) {
            setText("playHint", "Нажмите ▶ ещё раз, чтобы включить звук");
          }
        });
      }
    } else {
      stopAudioElement();
    }

    window.clearInterval(state.playTimer);
    state.playTimer = window.setInterval(tickPlayback, 120);
    tickPlayback();
    renderTransport();
  }

  function tickPlayback() {
    if (!state.playing || !state.settings) {
      return;
    }
    const end = trimEnd();
    const elapsed = (performance.now() - state.playStartedAt) / 1000;
    const current = state.playStartTime + elapsed;
    if (current >= end) {
      state.currentTime = end;
      renderTransport();
      pausePlayback({ loadStill: true });
      return;
    }
    state.currentTime = current;
    renderTransport();
  }

  function pausePlayback({ loadStill: refresh = true } = {}) {
    const wasPlaying = state.playing;
    if (wasPlaying && state.settings) {
      const elapsed = (performance.now() - state.playStartedAt) / 1000;
      state.currentTime = clamp(
        state.playStartTime + elapsed,
        trimStart(),
        trimEnd()
      );
    }
    state.playing = false;
    state.playbackToken += 1;
    window.clearInterval(state.playTimer);
    state.playTimer = 0;
    ui.liveImage.onerror = null;
    ui.liveImage.removeAttribute("src");
    ui.liveImage.hidden = true;
    ui.stillImage.hidden = false;
    stopAudioElement();
    renderTransport();
    if (refresh && state.loaded) {
      loadStill(state.currentTime, { force: true });
    }
  }

  function stopPlayback({ loadStill: refresh = true } = {}) {
    if (state.playing || !ui.liveImage.hidden || ui.previewAudio.src) {
      pausePlayback({ loadStill: refresh });
      return;
    }
    stopAudioElement();
  }

  function seekTo(seconds, { resume = false } = {}) {
    if (!state.loaded) {
      return;
    }
    const wasPlaying = state.playing;
    if (wasPlaying) {
      pausePlayback({ loadStill: false });
    }
    state.currentTime = clamp(numberOr(seconds, 0), 0, duration());
    renderTransport();
    loadStill(state.currentTime, { force: true });
    if (resume && wasPlaying) {
      startPlayback();
    }
  }

  function timelineFraction(event) {
    const rect = ui.timeline.getBoundingClientRect();
    if (!rect.width) {
      return 0;
    }
    return clamp((event.clientX - rect.left) / rect.width, 0, 1);
  }

  function scrubTo(event) {
    const total = duration();
    state.currentTime = timelineFraction(event) * total;
    renderTransport();
    const now = performance.now();
    if (now - state.lastStillRequestAt > 90) {
      state.lastStillRequestAt = now;
      loadStill(state.currentTime, { force: true });
    }
  }

  function beginScrub(event) {
    if (!state.loaded) {
      return;
    }
    event.preventDefault();
    state.scrubbing = true;
    state.scrubResume = state.playing;
    if (state.playing) {
      pausePlayback({ loadStill: false });
    }
    try {
      ui.timeline.setPointerCapture(event.pointerId);
    } catch (error) {
      // Pointer capture is best-effort.
    }
    scrubTo(event);
  }

  function moveScrub(event) {
    if (!state.scrubbing) {
      return;
    }
    event.preventDefault();
    scrubTo(event);
  }

  function endScrub(event) {
    if (!state.scrubbing) {
      return;
    }
    state.scrubbing = false;
    try {
      ui.timeline.releasePointerCapture(event.pointerId);
    } catch (error) {
      // Already released.
    }
    loadStill(state.currentTime, { force: true });
    if (state.scrubResume) {
      state.scrubResume = false;
      startPlayback();
    }
  }

  // ==== TIMELINE ====
  function mergeIntervals(intervals) {
    if (!intervals.length) {
      return [];
    }
    const cleaned = intervals
      .map(([start, end]) => [
        Math.max(0, Number(start)),
        Math.max(0, Number(end)),
      ])
      .filter(([start, end]) => end - start >= EPSILON)
      .sort((left, right) => left[0] - right[0]);
    if (!cleaned.length) {
      return [];
    }
    const merged = [cleaned[0]];
    for (let index = 1; index < cleaned.length; index += 1) {
      const [start, end] = cleaned[index];
      const last = merged[merged.length - 1];
      if (start <= last[1] + EPSILON) {
        last[1] = Math.max(last[1], end);
      } else {
        merged.push([start, end]);
      }
    }
    return merged;
  }

  function computeIntervals(regions, effect, total) {
    if (!Array.isArray(regions)) {
      return [];
    }
    return mergeIntervals(
      regions
        .filter((region) => Boolean(region[effect]))
        .map((region) => [
          clamp(numberOr(region.start, 0), 0, total),
          clamp(numberOr(region.end, 0), 0, total),
        ])
    );
  }

  function svgRect(x, width, className, y, height, titleText) {
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", x.toFixed(2));
    rect.setAttribute("y", String(y));
    rect.setAttribute("width", Math.max(0.6, width).toFixed(2));
    rect.setAttribute("height", String(height));
    rect.setAttribute("rx", "2");
    rect.setAttribute("class", className);
    if (titleText) {
      const title = document.createElementNS(SVG_NS, "title");
      title.textContent = titleText;
      rect.append(title);
    }
    return rect;
  }

  function renderTimeline() {
    const total = duration();
    ui.tlBlurGroup.replaceChildren();
    ui.tlVoiceGroup.replaceChildren();
    if (!state.loaded || !state.settings || total < EPSILON) {
      ui.tlTrim.setAttribute("x", "0");
      ui.tlTrim.setAttribute("width", "1000");
      ui.tlTrimStart.setAttribute("x1", "0");
      ui.tlTrimStart.setAttribute("x2", "0");
      ui.tlTrimEnd.setAttribute("x1", "1000");
      ui.tlTrimEnd.setAttribute("x2", "1000");
      const first = document.createElement("span");
      first.textContent = "00:00";
      const last = document.createElement("span");
      last.textContent = "00:00";
      ui.timelineScale.replaceChildren(first, last);
      renderTimelinePlayhead();
      return;
    }

    const scale = 1000 / total;
    const startX = clamp(state.settings.start, 0, total) * scale;
    const endX = clamp(state.settings.end, 0, total) * scale;
    ui.tlTrim.setAttribute("x", startX.toFixed(2));
    ui.tlTrim.setAttribute("width", Math.max(0.5, endX - startX).toFixed(2));
    ui.tlTrimStart.setAttribute("x1", startX.toFixed(2));
    ui.tlTrimStart.setAttribute("x2", startX.toFixed(2));
    ui.tlTrimEnd.setAttribute("x1", endX.toFixed(2));
    ui.tlTrimEnd.setAttribute("x2", endX.toFixed(2));

    const blur = computeIntervals(state.settings.regions, "blur", total);
    const voice = computeIntervals(state.settings.regions, "voice", total);
    for (const [intervalStart, intervalEnd] of blur) {
      ui.tlBlurGroup.append(
        svgRect(
          intervalStart * scale,
          (intervalEnd - intervalStart) * scale,
          "tl-blur",
          8,
          20,
          `Блюр ${formatClock(intervalStart)} — ${formatClock(intervalEnd)}`
        )
      );
    }
    for (const [intervalStart, intervalEnd] of voice) {
      ui.tlVoiceGroup.append(
        svgRect(
          intervalStart * scale,
          (intervalEnd - intervalStart) * scale,
          "tl-voice",
          40,
          20,
          `Голос ${formatClock(intervalStart)} — ${formatClock(intervalEnd)}`
        )
      );
    }

    const first = document.createElement("span");
    first.textContent = "00:00";
    const last = document.createElement("span");
    last.textContent = formatShortClock(total);
    ui.timelineScale.replaceChildren(first, last);
    renderTimelinePlayhead();
  }

  function renderTimelinePlayhead() {
    const total = duration();
    const position = total > 0
      ? clamp(state.currentTime / total, 0, 1) * 1000
      : 0;
    ui.tlPlayhead.setAttribute("x1", position.toFixed(2));
    ui.tlPlayhead.setAttribute("x2", position.toFixed(2));
  }

  // ==== JOB ====
  function jobStatusText(status) {
    if (status === "running") {
      return "Экспорт…";
    }
    if (status === "done") {
      return "Готово";
    }
    if (status === "cancelled") {
      return "Отменено";
    }
    if (status === "error") {
      return "Ошибка";
    }
    return "Подготовка…";
  }

  function renderJob() {
    const job = state.job;
    if (!job) {
      ui.jobBox.hidden = true;
      ui.jobStatus.textContent = "Подготовка…";
      ui.jobPercent.textContent = "0%";
      ui.jobProgress.value = 0;
      ui.jobMessage.textContent = "";
      ui.jobError.textContent = "";
      ui.jobError.hidden = true;
      ui.downloadLink.hidden = true;
      ui.downloadLink.removeAttribute("href");
      ui.cancelJobBtn.hidden = true;
      ui.deleteJobBtn.hidden = true;
      setText("exportBadge", "MP4");
      renderAvailability();
      return;
    }

    const status = job.status || "running";
    const progress = clamp(numberOr(job.progress, 0), 0, 1);
    ui.jobBox.hidden = false;
    ui.jobStatus.textContent = jobStatusText(status);
    ui.jobPercent.textContent = `${Math.round(progress * 100)}%`;
    ui.jobProgress.value = progress;

    let message = job.message || "";
    if (status === "done" && job.size_bytes) {
      message = `${message || "Готово"} · ${formatBytes(job.size_bytes)}`;
    }
    ui.jobMessage.textContent = message;
    if (job.error) {
      ui.jobError.textContent = job.error;
      ui.jobError.hidden = false;
    } else {
      ui.jobError.textContent = "";
      ui.jobError.hidden = true;
    }

    const ready = status === "done" && Boolean(job.download_url);
    ui.downloadLink.hidden = !ready;
    if (ready) {
      ui.downloadLink.href = job.download_url;
      ui.downloadLink.setAttribute(
        "download",
        job.download_name || "video_anonymized.mp4"
      );
      setText("exportBadge", "Готово");
    } else {
      ui.downloadLink.removeAttribute("href");
      setText("exportBadge", status === "running" ? "Экспорт" : "MP4");
    }

    ui.cancelJobBtn.hidden = status !== "running";
    ui.deleteJobBtn.hidden = status === "running";
    renderAvailability();
  }

  function storeJob(job) {
    try {
      window.sessionStorage.setItem(
        JOB_STORAGE_KEY,
        JSON.stringify({
          id: job.id,
          sourceName: state.info ? state.info.name : "",
        })
      );
    } catch (error) {
      // sessionStorage can be blocked; the job still works in this tab.
    }
  }

  function readStoredJob() {
    try {
      const raw = window.sessionStorage.getItem(JOB_STORAGE_KEY);
      if (!raw) {
        return null;
      }
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.id === "string" && parsed.id) {
        return parsed;
      }
    } catch (error) {
      // Malformed or unavailable storage is treated as empty.
    }
    return null;
  }

  function clearStoredJob() {
    try {
      window.sessionStorage.removeItem(JOB_STORAGE_KEY);
    } catch (error) {
      // Ignore storage restrictions.
    }
  }

  function stopJobPolling() {
    window.clearInterval(state.jobTimer);
    state.jobTimer = 0;
    state.jobRequest += 1;
  }

  function pollJob() {
    if (!state.jobId) {
      return;
    }
    stopJobPolling();
    state.jobTimer = window.setInterval(fetchJob, 800);
    fetchJob();
  }

  async function fetchJob() {
    const jobId = state.jobId;
    if (!jobId) {
      return;
    }
    const request = state.jobRequest;
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (state.jobId !== jobId || request !== state.jobRequest) {
        return;
      }
      state.job = job;
      renderJob();
      if (job.status !== "running") {
        stopJobPolling();
      }
    } catch (error) {
      if (state.jobId !== jobId || request !== state.jobRequest) {
        return;
      }
      if (error && error.status === 404) {
        state.job = null;
        state.jobId = "";
        clearStoredJob();
        stopJobPolling();
        renderJob();
        return;
      }
      if (error && error.status === 401) {
        return;
      }
      // Keep the last known state and retry; a transient local hiccup should
      // not throw away a running export.
    }
  }

  async function restoreJob() {
    const stored = readStoredJob();
    if (!stored) {
      return;
    }
    if (!state.loaded || !state.info || stored.sourceName !== state.info.name) {
      clearStoredJob();
      return;
    }
    if (state.job && state.job.id === stored.id) {
      renderJob();
      if (state.job.status === "running") {
        pollJob();
      }
      return;
    }
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(stored.id)}`);
      if (!state.loaded || stored.sourceName !== state.info.name) {
        clearStoredJob();
        return;
      }
      state.job = job;
      state.jobId = job.id;
      renderJob();
      if (job.status === "running") {
        pollJob();
      }
    } catch (error) {
      if (error && error.status === 404) {
        clearStoredJob();
      }
    }
  }

  async function startExport() {
    if (!state.loaded || state.exportBusy) {
      return;
    }
    if (state.job && state.job.status === "running") {
      showToast("Экспорт уже выполняется.", { error: true });
      return;
    }
    const problem = validateSettings();
    if (problem) {
      showToast(problem, { error: true });
      return;
    }
    state.exportBusy = true;
    renderAvailability();
    try {
      await flushSave();
      if (state.playing) {
        pausePlayback({ loadStill: false });
      }
      const job = await api(
        `/api/export?v=${state.version}`,
        { method: "POST" }
      );
      state.job = job;
      state.jobId = job.id;
      storeJob(job);
      renderJob();
      pollJob();
      showToast("Экспорт запущен. Оригинал не изменяется.");
    } catch (error) {
      if (error && error.name !== "AbortError" && error.status !== 401) {
        showToast(error.message || networkMessage(), { error: true });
      }
    } finally {
      state.exportBusy = false;
      renderAvailability();
    }
  }

  async function cancelJob() {
    if (!state.jobId) {
      return;
    }
    try {
      const job = await api(
        `/api/jobs/${encodeURIComponent(state.jobId)}/cancel`,
        { method: "POST" }
      );
      state.job = job;
      renderJob();
      pollJob();
    } catch (error) {
      if (error && error.status !== 401) {
        showToast(error.message || "Не удалось отменить экспорт.", {
          error: true,
        });
      }
    }
  }

  async function deleteJob() {
    if (!state.jobId) {
      return;
    }
    if (state.job && state.job.status === "running") {
      showToast("Сначала отмените экспорт.", { error: true });
      return;
    }
    try {
      await api(`/api/jobs/${encodeURIComponent(state.jobId)}`, {
        method: "DELETE",
      });
      state.job = null;
      state.jobId = "";
      clearStoredJob();
      stopJobPolling();
      renderJob();
      showToast("Результат удалён.");
    } catch (error) {
      if (error && error.status !== 401) {
        showToast(error.message || "Не удалось удалить результат.", {
          error: true,
        });
      }
    }
  }

  // ==== EVENTS ====
  function togglePlayback() {
    if (!state.loaded) {
      return;
    }
    if (state.playing) {
      pausePlayback({ loadStill: true });
    } else {
      startPlayback();
    }
  }

  function nudgePlayhead(delta) {
    if (!state.loaded) {
      return;
    }
    const wasPlaying = state.playing;
    if (wasPlaying) {
      pausePlayback({ loadStill: false });
    }
    state.currentTime = clamp(state.currentTime + delta, 0, duration());
    renderTransport();
    loadStill(state.currentTime, { force: true });
    if (wasPlaying) {
      startPlayback();
    }
  }

  function normalizeTokenInput(value) {
    let token = String(value || "").trim();
    if (token.includes("#")) {
      token = token.slice(token.lastIndexOf("#") + 1);
    }
    try {
      token = decodeURIComponent(token);
    } catch (error) {
      // Keep the raw value if it is not percent-encoded.
    }
    return token;
  }

  function bindEvents() {
    ui.authForm.addEventListener("submit", (event) => {
      event.preventDefault();
      const token = normalizeTokenInput(ui.tokenInput.value);
      if (!token) {
        setInlineError("authError", "Введите код доступа.");
        return;
      }
      setInlineError("authError", "");
      authenticate(token);
    });

    ui.tokenInput.addEventListener("input", () => {
      setInlineError("authError", "");
    });

    ui.logoutBtn.addEventListener("click", logout);

    ui.pathForm.addEventListener("submit", openByPath);
    ui.pathInput.addEventListener("input", () => {
      setInlineError("openPathError", "");
    });
    ui.changeSourceBtn.addEventListener("click", showOpenPanel);
    ui.openAnotherBtn.addEventListener("click", showOpenPanel);
    ui.closeOpenPanelBtn.addEventListener("click", hideOpenPanel);
    ui.closeSourceBtn.addEventListener("click", closeSource);
    ui.cancelUploadBtn.addEventListener("click", cancelUpload);

    ui.fileInput.addEventListener("change", () => {
      const file = ui.fileInput.files && ui.fileInput.files[0];
      ui.fileInput.value = "";
      if (file) {
        uploadFile(file);
      }
    });

    ui.dropzone.addEventListener("dragenter", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (!state.opening && !state.uploading) {
        ui.dropzone.classList.add("is-dragover");
      }
    });
    ui.dropzone.addEventListener("dragover", (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (event.dataTransfer) {
        event.dataTransfer.dropEffect = "copy";
      }
    });
    ui.dropzone.addEventListener("dragleave", (event) => {
      event.preventDefault();
      event.stopPropagation();
      ui.dropzone.classList.remove("is-dragover");
    });
    ui.dropzone.addEventListener("drop", (event) => {
      event.preventDefault();
      event.stopPropagation();
      ui.dropzone.classList.remove("is-dragover");
      const file = event.dataTransfer
        && event.dataTransfer.files
        && event.dataTransfer.files[0];
      if (file) {
        uploadFile(file);
      }
    });
    document.addEventListener("dragover", (event) => {
      event.preventDefault();
    });
    document.addEventListener("drop", (event) => {
      event.preventDefault();
    });

    ui.playBtn.addEventListener("click", togglePlayback);
    ui.stillBtn.addEventListener("click", () => {
      loadStill(state.currentTime, { force: true });
    });

    ui.timeline.addEventListener("pointerdown", beginScrub);
    ui.timeline.addEventListener("pointermove", moveScrub);
    ui.timeline.addEventListener("pointerup", endScrub);
    ui.timeline.addEventListener("pointercancel", endScrub);
    ui.timeline.addEventListener("lostpointercapture", () => {
      if (!state.scrubbing) {
        return;
      }
      state.scrubbing = false;
      loadStill(state.currentTime, { force: true });
      if (state.scrubResume) {
        state.scrubResume = false;
        startPlayback();
      }
    });
    ui.timeline.addEventListener("keydown", (event) => {
      if (!state.loaded) {
        return;
      }
      if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const step = event.shiftKey ? 5 : 1;
        nudgePlayhead(event.key === "ArrowLeft" ? -step : step);
      } else if (event.key === "Home") {
        event.preventDefault();
        seekTo(0);
      } else if (event.key === "End") {
        event.preventDefault();
        seekTo(duration());
      } else if (event.code === "Space" || event.key === " ") {
        event.preventDefault();
        togglePlayback();
      }
    });

    ui.blurEnabled.addEventListener("change", () => {
      if (!state.settings) {
        return;
      }
      state.settings.blur_enabled = ui.blurEnabled.checked;
      markEdited({ preview: true });
    });
    ui.blurMethod.addEventListener("change", () => {
      if (!state.settings) {
        return;
      }
      state.settings.blur_method = ui.blurMethod.value === "pixelate"
        ? "pixelate"
        : "gaussian";
      markEdited({ preview: true });
    });
    ui.blurStrength.addEventListener("input", () => {
      if (!state.settings) {
        return;
      }
      state.settings.blur_strength = clamp(
        numberOr(ui.blurStrength.value, 3),
        1,
        5
      );
      markEdited({ preview: true, delay: 300 });
    });
    ui.blurMargin.addEventListener("input", () => {
      if (!state.settings) {
        return;
      }
      state.settings.blur_margin = clamp(
        numberOr(ui.blurMargin.value, 0.22),
        0,
        0.45
      );
      markEdited({ preview: true, delay: 300 });
    });
    ui.showBoxes.addEventListener("change", () => {
      if (!state.settings) {
        return;
      }
      state.settings.show_boxes = ui.showBoxes.checked;
      markEdited({ preview: true });
    });

    ui.voiceSemitones.addEventListener("input", () => {
      if (!state.settings) {
        return;
      }
      state.settings.voice_semitones = clamp(
        numberOr(ui.voiceSemitones.value, -3),
        -12,
        12
      );
      markEdited({ delay: 300 });
    });
    ui.volume.addEventListener("input", () => {
      if (!state.settings) {
        return;
      }
      state.settings.volume = clamp(numberOr(ui.volume.value, 1), 0, 2);
      markEdited({ delay: 300 });
    });
    ui.preserveFormants.addEventListener("change", () => {
      if (!state.settings) {
        return;
      }
      state.settings.preserve_formants = ui.preserveFormants.checked;
      markEdited();
    });
    ui.mute.addEventListener("change", () => {
      if (!state.settings) {
        return;
      }
      state.settings.mute = ui.mute.checked;
      markEdited();
    });

    ui.crf.addEventListener("input", () => {
      if (!state.settings) {
        return;
      }
      state.settings.crf = Math.round(clamp(numberOr(ui.crf.value, 20), 0, 51));
      markEdited({ delay: 300 });
    });
    ui.preset.addEventListener("change", () => {
      if (!state.settings) {
        return;
      }
      state.settings.preset = ui.preset.value;
      markEdited();
    });

    ui.trimStart.addEventListener("input", () => trimEdited("start"));
    ui.trimStart.addEventListener("change", () => trimCommitted("start"));
    ui.trimEnd.addEventListener("input", () => trimEdited("end"));
    ui.trimEnd.addEventListener("change", () => trimCommitted("end"));
    ui.trimStartHereBtn.addEventListener("click", setTrimStartHere);
    ui.trimEndHereBtn.addEventListener("click", setTrimEndHere);
    ui.trimFullBtn.addEventListener("click", setTrimFull);

    ui.addRegionBtn.addEventListener("click", addRegion);
    ui.resetRegionsBtn.addEventListener("click", resetRegions);

    ui.exportBtn.addEventListener("click", startExport);
    ui.cancelJobBtn.addEventListener("click", cancelJob);
    ui.deleteJobBtn.addEventListener("click", deleteJob);

    document.addEventListener("keydown", (event) => {
      if (!state.authenticated || !state.loaded || ui.authView.hidden === false) {
        return;
      }
      if (event.defaultPrevented) {
        return;
      }
      const target = event.target;
      if (target) {
        const tag = target.tagName;
        if (
          target.isContentEditable
          || tag === "INPUT"
          || tag === "SELECT"
          || tag === "TEXTAREA"
          || tag === "BUTTON"
        ) {
          return;
        }
      }
      if (event.code === "Space" || event.key === " ") {
        event.preventDefault();
        togglePlayback();
      } else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
        event.preventDefault();
        const step = event.shiftKey ? 5 : 1;
        nudgePlayhead(event.key === "ArrowLeft" ? -step : step);
      } else if (event.key === "Home") {
        event.preventDefault();
        seekTo(0);
      } else if (event.key === "End") {
        event.preventDefault();
        seekTo(duration());
      }
    });
  }

  // ==== BOOT ====
  async function boot() {
    setText("serverState", "Проверка доступа…");
    const fragment = window.location.hash
      ? window.location.hash.slice(1)
      : "";
    if (fragment) {
      state.token = normalizeTokenInput(fragment);
      try {
        window.history.replaceState(
          null,
          "",
          `${window.location.pathname}${window.location.search}`
        );
      } catch (error) {
        // Older browsers may refuse replaceState; the token is still used.
      }
    }

    if (state.token) {
      await authenticate(state.token, { silent: true });
      return;
    }

    try {
      const payload = await api("/api/state", { skipUnauthorized: true });
      state.authenticated = true;
      showApp();
      applyServerState(payload, { discardEdits: true, reloadFrame: true });
      if (payload && payload.loaded) {
        await restoreJob();
      }
    } catch (error) {
      if (error && error.status === 401) {
        showAuth();
      } else if (error && error.status === 0) {
        showAuth(error.message);
      } else {
        showAuth("Введите код доступа из терминала.");
      }
    }
  }

  function start() {
    cacheElements();
    bindEvents();
    boot();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
