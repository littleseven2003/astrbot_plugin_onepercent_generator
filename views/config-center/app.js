const bridge = window.AstrBotPluginView;
const $ = (id) => document.getElementById(id);
const PAGE_TITLES = {
  status: ["当前状态", "用量与配置摘要"],
  model: ["模型配置", "统一配置中心"],
  search: ["搜索配置", "候选与回退"],
  rate: ["频率配置", "身份与额度"],
  session: ["会话配置", "管理员与会话范围"],
};
const SEARCH_MODES = [
  { key: "astrbot", label: "Astrbot与搜索插件", hint: "先 AstrBot 官方搜索，再已安装的搜索插件（含 Anysearch）" },
  { key: "api", label: "搜索API", hint: "调用已配置的 Tavily 搜索 API" },
  { key: "web", label: "网页搜索", hint: "解析 Bing 与百度搜索页" },
];
const SEARCH_MODE_LABELS = Object.fromEntries(SEARCH_MODES.map((item) => [item.key, item.label]));
const TIMEZONES = [
  ["", "服务器本地时区（默认）"],
  ["Asia/Shanghai", "Asia/Shanghai（中国标准时间）"],
  ["Asia/Hong_Kong", "Asia/Hong_Kong"],
  ["Asia/Taipei", "Asia/Taipei"],
  ["Asia/Tokyo", "Asia/Tokyo"],
  ["Asia/Seoul", "Asia/Seoul"],
  ["Asia/Singapore", "Asia/Singapore"],
  ["UTC", "UTC"],
  ["Europe/London", "Europe/London"],
  ["Europe/Paris", "Europe/Paris"],
  ["Europe/Berlin", "Europe/Berlin"],
  ["America/New_York", "America/New_York"],
  ["America/Chicago", "America/Chicago"],
  ["America/Los_Angeles", "America/Los_Angeles"],
  ["Australia/Sydney", "Australia/Sydney"],
];
const SECTION_LABELS = {
  model: "模型",
  search: "搜索",
  rate: "频率",
  session: "会话",
  admins: "管理员",
  advanced: "高级",
};
const SECTION_NAMES = Object.keys(SECTION_LABELS);
const state = {
  data: null,
  stats: null,
  page: "status",
  period: "7d",
  metric: "requests",
  selectedChats: null,
  chatDraft: new Set(),
  chatDraftAll: true,
  chatSearch: "",
  source: "manual",
  busy: false,
  drafts: {
    model: null,
    search: null,
    rate: null,
    session: null,
    admins: null,
    advanced: null,
  },
  // 发生保存冲突的分区：保留草稿但阻止继续自动写入，直到用户刷新。
  conflicts: {},
  // 任一分区收到冲突后整页表单都已过期：刷新前停止所有分区的自动写入，
  // 否则旧表单快照会把别的页面刚保存的其它分区值覆盖回去。
  staleBaseline: false,
  dirty: {
    model: false,
    search: false,
    rate: false,
    session: false,
    admins: false,
    advanced: false,
  },
  versions: {
    model: 0,
    search: 0,
    rate: 0,
    session: 0,
    admins: 0,
    advanced: 0,
  },
  dataEpoch: 0,
  // 服务端最近一次的配置修订号；保存时作为 base_revision 上送以检测跨页面覆盖。
  serverRevision: 0,
  // 已成功提交的写入代次。刷新结果若在读取期间被写入超越，必须整体丢弃。
  confirmedWriteEpoch: 0,
  // 每次排队写入时递增。刷新一开始读取就必须冻结其基线；读取期间只要有
  // 写入排队（无论成功或失败），这份读取结果都不能再重建草稿或重置版本。
  mutationEpoch: 0,
  mutationChain: Promise.resolve(),
  pendingMutations: 0,
  messageTokens: {},
  asyncTokens: {
    load: 0,
    models: 0,
    modelTest: 0,
    currentTest: 0,
    searchTest: 0,
    stats: 0,
  },
  identityConnector: "",
  searchModeOrder: null,
  searchModeEnabled: {},
  chartPoints: [],
  chartHover: -1,
};

function esc(value) {
  return String(value == null ? "" : value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
// 后端 config_error 是机器码，展示时翻译成可操作的中文提示；未知码原样显示。
function configErrorText(code) {
  const map = {
    invalid_whitelist_umos: "UMO 白名单类型错误（应为字符串列表），请改为列表或清空",
    invalid_session_list: "会话列表类型错误（应为列表），请修正后重试",
    sessions_unset: "会话列表尚未设置（null）：请添加至少一个 UMO，或点击“清空并保存（不限制）”明确选择不限制",
    invalid_session_entries: "会话列表包含无效条目：保存时会移除这些条目，如需保留请先改成 platform:type:session 形式的完整 UMO",
    invalid_session_settings: "会话设置格式错误（应为对象）：请在原生配置页修正或重置该字段",
    no_valid_sessions: "会话列表非空但没有有效 UMO：请删除无效条目，或补充 platform:type:session 形式的完整 UMO",
  };
  return map[code] || code;
}
function msg(id, text, kind) {
  const el = $(id);
  if (!el) return;
  el.textContent = text || "";
  el.className = "message " + (kind || "");
}
function setGlobalNotice(text) {
  const el = $("globalNotice");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}
function setBusy(value, label) {
  state.busy = value;
  const el = $("loading");
  el.textContent = label || "加载中…";
  el.classList.toggle("hidden", !value);
}
function dirtySections() {
  return Object.keys(state.dirty).filter((section) => state.dirty[section]);
}
function updateDirtyBadge() {
  const sections = dirtySections();
  const badge = $("dirtyBadge");
  badge.classList.toggle("hidden", !sections.length);
  badge.textContent = sections.length
    ? "未保存：" + sections.map((section) => SECTION_LABELS[section]).join("、")
    : "未保存";
  badge.title = sections.length
    ? "以下分区有未保存草稿：" + sections.map((section) => SECTION_LABELS[section]).join("、")
    : "";
}
let toastTimer = null;
function showToast(text, kind) {
  const el = $("toast");
  if (!el) return;
  el.textContent = text;
  el.classList.remove("warn", "error");
  if (kind) el.classList.add(kind);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3000);
}
const saveTimers = {};
const saveJobs = {};
let autosavePaused = false;
const lastPayloads = {};
const lastSavedSignatures = {};
function scheduleSectionSave(section, delay) {
  if (autosavePaused || state.staleBaseline || state.conflicts[section]) return;
  clearTimeout(saveTimers[section]);
  saveTimers[section] = setTimeout(() => saveSectionNow(section), delay == null ? 600 : delay);
}
function payloadSignature(section) {
  const map = {
    model: () => modelPayload(),
    search: () => searchPayload(),
    rate: () => ratePayload(),
    advanced: () => advancedPayload(),
  };
  try { return JSON.stringify(map[section] ? map[section]() : null); }
  catch (error) { return null; }
}
function saveSectionNow(section) {
  clearTimeout(saveTimers[section]);
  if (!state.dirty[section]) return Promise.resolve();
  // 冲突后不再自动重试，避免旧草稿把别的页面刚写入的值覆盖回去；
  // staleBaseline 表示整页基线已过期，所有分区都停止写入直到刷新。
  if (state.staleBaseline || state.conflicts[section]) return Promise.resolve();
  const signature = payloadSignature(section);
  // 同一份内容已经在写或刚写过，不再重复发请求。
  if (signature && signature === lastSavedSignatures[section]) {
    markSectionClean(section);
    return Promise.resolve();
  }
  const job = saveJobs[section];
  // 每个分区同一时刻只发一个写请求；期间的编辑并入下一次。
  if (job) {
    job.again = true;
    return job.promise;
  }
  const current = { again: false, promise: null };
  saveJobs[section] = current;
  const run = section === "model" ? saveModel()
    : section === "search" ? saveSearch()
      : section === "rate" ? saveRate()
        : saveAdvanced();
  current.promise = Promise.resolve(run).finally(() => {
    delete saveJobs[section];
    if (current.again && state.dirty[section]) {
      scheduleSectionSave(section, 50);
    }
  });
  if (signature) lastPayloads[section] = signature;
  current.signature = signature;
  return current.promise;
}
// 测试与调试入口：强制提交一次当前分区（跳过同内容去重）。
if (typeof window !== "undefined") {
  window.__dshConfig = {
    saveSectionNow,
    setAutosavePaused(value) { autosavePaused = !!value; },
    resetSignature(section) {
      delete lastPayloads[section];
      delete lastSavedSignatures[section];
    },
  };
}
function renderSearchSaveState(text, kind) {
  // 自动保存后不再有独立的状态胶囊；保留函数以兼容内部调用。
  const el = $("searchSaveState");
  if (!el) return;
  el.textContent = text;
  el.className = "pill " + (kind || "");
}

function markSectionDirty(section, options) {
  if (!(section in state.dirty)) return;
  const touch = !options || options.touch !== false;
  if (touch) state.versions[section] += 1;
  state.dirty[section] = true;
  updateDirtyBadge();
  if (section === "search") {
    const hint = $("searchDraftHint");
    if (hint) {
      hint.textContent = state.staleBaseline
        ? "有未保存修改：自动保存已暂停，请刷新后再继续。"
        : "有未保存修改：正在自动保存…";
      hint.className = "hint warn";
    }
    renderSearchSaveState("草稿未保存", "warn");
  }
}
function markSectionClean(section) {
  if (!(section in state.dirty)) return;
  state.dirty[section] = false;
  updateDirtyBadge();
  if (section === "search") {
    const hint = $("searchDraftHint");
    if (hint) {
      hint.textContent = "没有未保存修改。修改后自动保存。";
      hint.className = "hint";
    }
    renderSearchSaveState("已保存", "ok");
  }
}
function hasUnsavedChanges() {
  return dirtySections().length > 0;
}
function pendingMutations() {
  return state.pendingMutations;
}
function queueMutation(task) {
  state.mutationEpoch += 1;
  state.pendingMutations += 1;
  const run = state.mutationChain.then(task);
  state.mutationChain = run.catch(() => {}).finally(() => {
    state.pendingMutations -= 1;
  });
  return run;
}
function nextMessageToken(id) {
  state.messageTokens[id] = (state.messageTokens[id] || 0) + 1;
  return state.messageTokens[id];
}
function setMessage(id, token, text, kind) {
  if ((state.messageTokens[id] || 0) !== token) return false;
  msg(id, text, kind);
  return true;
}
function invalidateAsyncOperations() {
  Object.keys(state.asyncTokens).forEach((key) => {
    state.asyncTokens[key] += 1;
  });
  Object.keys(state.messageTokens).forEach((key) => {
    state.messageTokens[key] += 1;
  });
}
function recomputeConnectors(fromServer) {
  if (!state.data || !state.data.session) return;
  const session = state.data.session;
  const connectors = new Set(Array.isArray(fromServer) ? fromServer : []);
  (session.sessions || []).forEach((item) => item.connector && connectors.add(item.connector));
  (session.admin_users || []).forEach((item) => item.connector && connectors.add(item.connector));
  (session.identity_links || []).forEach((item) => item.connector && connectors.add(item.connector));
  (session.known_identities || []).forEach((item) => item.connector && connectors.add(item.connector));
  session.connectors = Array.from(connectors).sort();
}
const SESSION_SNAPSHOT_FIELDS = [
  "allow_private",
  "allow_group",
  "sessions",
  "migration_required",
  "legacy_restrictions_pending",
  "legacy_umo_pending",
  "config_error",
  "invalid_sessions",
  "sessions_raw_type",
];
const ADMIN_SNAPSHOT_FIELDS = [
  "admin_users",
  "legacy_admin_pending",
  "legacy_admin_present",
];
const IDENTITY_SNAPSHOT_FIELDS = ["identity_links", "known_identities"];
// 前端内部会话分区名为 session，接口与 state.session 使用 sessions。
function apiSectionName(section) {
  return section === "session" ? "sessions" : section;
}
function applyServerSnapshot(section, data) {
  if (!data || !state.data) return;
  const target = apiSectionName(section);
  if (target === "model" && data.model) state.data.model = data.model;
  if (target === "search" && data.search) state.data.search = data.search;
  if (target === "rate" && data.rate) state.data.rate = data.rate;
  if (target === "advanced" && data.advanced) state.data.advanced = data.advanced;
  const source = data.session || {};
  if (target === "sessions") {
    SESSION_SNAPSHOT_FIELDS.forEach((key) => {
      if (key in source) state.data.session[key] = source[key];
    });
    recomputeConnectors(source.connectors);
  }
  if (target === "admins") {
    ADMIN_SNAPSHOT_FIELDS.forEach((key) => {
      if (key in source) state.data.session[key] = source[key];
    });
    recomputeConnectors(source.connectors);
  }
  if (target === "identity") {
    IDENTITY_SNAPSHOT_FIELDS.forEach((key) => {
      if (key in source) state.data.session[key] = source[key];
    });
    recomputeConnectors(source.connectors);
  }
}
function consumeSavedSection(section, submittedVersion, snapshot) {
  // 保存响应携带的是该次写入完成时的服务端状态；刷新读取结果由
  // confirmedWriteEpoch 单独拦截，不能在这里把已提交写入误判为过期。
  if (snapshot) applyServerSnapshot(section, snapshot);
  if (state.versions[section] !== submittedVersion) {
    // 只推进服务端基线，保存期间的新编辑继续作为未保存草稿。
    markSectionDirty(section, { touch: false });
    return false;
  }
  if (snapshot) {
    const fresh = draftFromState(state.data);
    state.drafts[section] = fresh[section];
    if (section === "model") state.source = fresh.model.source;
  }
  markSectionClean(section);
  if (lastPayloads[section]) lastSavedSignatures[section] = lastPayloads[section];
  return true;
}
function copyPayload(payload) {
  return JSON.parse(JSON.stringify(payload));
}
function notifyQueued(messageId) {
  if (pendingMutations() > 0) {
    setMessage(messageId, nextMessageToken(messageId), "已排队：当前保存或身份操作完成后执行。", "warn");
  }
}
function startSectionSave(section, payload, messageId, busyLabel, successText, failurePrefix) {
  // 冲突后整页基线已过期：会话/管理员这类显式保存也先停下，等刷新拿新基线。
  if (state.staleBaseline) {
    setMessage(
      messageId,
      nextMessageToken(messageId),
      "自动保存已暂停：配置已被其他页面更新，请先点击“刷新全部”取得新基线。",
      "warn",
    );
    return Promise.resolve();
  }
  const submittedVersion = state.versions[section];
  const body = copyPayload(payload);
  // 前端内部会话分区名为 session，服务端接口与测试契约使用 sessions。
  const requestSection = apiSectionName(section);
  notifyQueued(messageId);
  return queueMutation(async () => {
    const token = nextMessageToken(messageId);
    setBusy(true, busyLabel);
    setMessage(messageId, token, "正在保存…");
    if (section === "search") renderSearchSaveState("保存中…", "warn");
    try {
      // 排队期间别的分区可能已经收到冲突响应：真正发请求前再检查一次，
      // 不能把冲突前入队的旧快照在冲突后悄悄写进去。
      if (state.staleBaseline || state.conflicts[section]) {
        setMessage(
          messageId,
          token,
          "配置已被其他页面更新，已取消这次排队保存（草稿已保留），请刷新后再保存。",
          "warn",
        );
        return;
      }
      const data = ensureOk(await bridge.apiPost("config-center/save", {
        section: requestSection,
        data: body,
        base_revision: state.serverRevision,
      }));
      if (data && data.conflict) {
        // 另一页面已基于更新的状态写入；保留当前草稿、标记冲突并停止自动写入，
        // 绝不静默覆盖，直到用户刷新拿到新基线。
        if (typeof data.revision === "number") state.serverRevision = data.revision;
        state.conflicts[section] = true;
        state.staleBaseline = true;
        setGlobalNotice(
          "配置已被其他页面更新：自动保存已暂停，请点击“刷新全部”取得新基线后再继续（草稿已保留）。",
        );
        setMessage(
          messageId,
          token,
          data.message || "配置已被其他页面更新，已阻止本次保存；刷新前不会再自动保存（草稿已保留）。",
          "warn",
        );
        return;
      }
      if (!data.saved) throw new Error(data.message || "保存失败");
      state.confirmedWriteEpoch += 1;
      if (typeof data.revision === "number") state.serverRevision = data.revision;
      if (!data.state) {
        // 保存成功但状态回读失败：如实告知已写入、状态待确认，草稿按已提交清理，刷新后恢复真实状态。
        if (state.versions[section] === submittedVersion) markSectionClean(section);
        renderAll();
        setMessage(messageId, token, "配置已写入，但状态回读失败，页面显示可能滞后，请刷新确认。", "warn");
        return;
      }
      const clean = consumeSavedSection(section, submittedVersion, data.state);
      renderAll();
      if (clean) {
        setMessage(messageId, token, successText, "ok");
        showToast((SECTION_LABELS[section] || "配置") + "已自动保存");
      } else {
        setMessage(
          messageId,
          token,
          "已保存提交时的" + (SECTION_LABELS[section] || "该分区") +
            "配置；保存期间的新修改仍为未保存草稿，请再次保存。",
          "warn",
        );
      }
    } catch (error) {
      setMessage(
        messageId,
        token,
        (failurePrefix || "保存失败，草稿已保留：") + error.message,
        "error",
      );
      if (section === "search") {
        renderSearchSaveState("保存失败：" + error.message, "error");
      }
    } finally {
      setBusy(false);
    }
  });
}
function startIdentityMutation(body, busyLabel, successText, failurePrefix) {
  const request = copyPayload(body);
  notifyQueued("rateMessage");
  return queueMutation(async () => {
    const token = nextMessageToken("rateMessage");
    setBusy(true, busyLabel);
    try {
      const data = ensureOk(await bridge.apiPost("config-center/save", {
        section: "identity",
        data: request,
      }));
      if (!data.saved) throw new Error(data.message || "身份操作失败");
      state.confirmedWriteEpoch += 1;
      if (data.state) applyServerSnapshot("identity", data.state);
      renderAll();
      setMessage("rateMessage", token, successText, "ok");
    } catch (error) {
      setMessage("rateMessage", token, failurePrefix + error.message, "error");
    } finally {
      setBusy(false);
    }
  });
}
function setRangeNotice(text) {
  const el = $("rangeInfo");
  if (!el) return;
  el.textContent = text || "";
  el.className = text ? "hint warn range-info" : "hint range-info";
}
async function reloadConfiguration(reason, messageId) {
  if (pendingMutations()) {
    const text = "配置保存或身份操作进行中，请完成后再刷新。";
    if (messageId) msg(messageId, text, "warn");
    else setRangeNotice(text);
    return false;
  }
  if (!(await confirmDiscard(reason))) return false;
  try {
    const applied = await loadState();
    await loadStats();
    if (!applied) {
      if (messageId) {
        msg(messageId, "刷新未应用：读取期间有保存或身份操作，已保留当前配置和草稿。", "warn");
      }
      return false;
    }
    if (messageId) msg(messageId, "已刷新。", "ok");
    return true;
  } catch (error) {
    if (messageId) msg(messageId, "刷新失败：" + error.message, "error");
    return false;
  }
}
let pendingConfirm = null;
function resolveConfirm(ok) {
  if (!pendingConfirm) return;
  const pending = pendingConfirm;
  pendingConfirm = null;
  document.removeEventListener("keydown", pending.onKey);
  $("confirmLayer").classList.add("hidden");
  pending.resolve(ok);
  // 取消或确认后都要把焦点还给触发按钮，不能留在已隐藏的弹窗按钮上。
  const target = pending.returnFocus;
  if (target && typeof target.focus === "function" && document.contains(target)) {
    target.focus();
  }
}
function confirmDiscard(reason) {
  if (!hasUnsavedChanges()) return Promise.resolve(true);
  if (pendingConfirm) return Promise.resolve(false);
  const sections = dirtySections().map((section) => SECTION_LABELS[section]).join("、");
  $("confirmText").textContent =
    "当前有未保存的" + sections + "配置。" + reason + "会丢弃这些草稿，确定继续吗？";
  $("confirmLayer").classList.remove("hidden");
  return new Promise((resolve) => {
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        resolveConfirm(false);
      }
    };
    pendingConfirm = {
      resolve,
      onKey,
      returnFocus: document.activeElement,
    };
    document.addEventListener("keydown", onKey);
    $("confirmCancel").focus();
  });
}
function showError(error, target) {
  const text = "操作失败：" + (error && error.message ? error.message : String(error));
  if (target && $(target)) msg(target, text, "error");
  const section = document.querySelector(".section.active");
  if (section) {
    let box = section.querySelector(".error-box.load-error");
    if (!box) {
      box = document.createElement("div");
      box.className = "error-box load-error";
      box.setAttribute("role", "alert");
      section.prepend(box);
    }
    box.textContent = text;
  }
}
function clearError() {
  document.querySelectorAll(".load-error").forEach((el) => el.remove());
}
function ensureOk(data) {
  if (data && data.status === "error") {
    throw new Error(data.message || "操作失败");
  }
  return data;
}
function switchPage(page) {
  state.page = page;
  document.querySelectorAll(".nav button").forEach((button) => {
    button.setAttribute("aria-current", button.dataset.page === page ? "page" : "false");
    if (button.dataset.page === page) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelectorAll(".section").forEach((section) => {
    section.classList.toggle("active", section.id === "page-" + page);
  });
  const title = PAGE_TITLES[page] || PAGE_TITLES.status;
  $("pageTitle").textContent = title[0];
  $("pageKicker").textContent = title[1];
  if (page === "status") loadStats();
}
function applyTheme() {
  const context = bridge.getContext && bridge.getContext();
  document.documentElement.setAttribute("data-theme", context && context.isDark ? "dark" : "light");
}

function draftFromState(data) {
  const model = data.model || {};
  const manual = model.manual || {};
  const search = data.search || {};
  const rate = data.rate || {};
  const session = data.session || {};
  const advanced = data.advanced || {};
  return {
    model: {
      source: model.source === "astrbot" ? "astrbot" : "manual",
      providerId: model.astrbot_provider_id || "",
      preset: manual.provider_preset || "custom",
      baseUrl: manual.base_url || "",
      apiFormat: manual.api_format || "chat_completions",
      model: manual.model || "",
      sendTemperature: manual.send_temperature !== false,
      apiKey: "",
    },
    search: {
      enabled: !!search.enabled,
      mode: search.mode || "off",
      modeOrder: Array.isArray(search.mode_order) ? search.mode_order.slice() : null,
      modeEnabled: Array.isArray(search.mode_enabled) ? search.mode_enabled.slice() : null,
      resultCount: String(search.result_count || 3),
      timeout: String(search.timeout_ms || 8000),
      perTimeout: String(search.per_backend_timeout_ms || 0),
      summaryInput: String(search.summary_input_max_chars || 6000),
      summaryChars: String(search.summarize_max_chars || 150),
      materialMaxChars: String(search.material_max_chars || 3000),
      materialMaxItems: String(search.material_max_items || 6),
      materialSnippetChars: String(search.material_snippet_chars || 400),
      summarizeEnabled: !!search.summarize_enabled,
      apiProvider: search.api_provider || "tavily",
      apiBase: search.api_base_url || "",
      apiKey: "",
    },
    rate: {
      window: String(rate.window_minutes == null ? 10 : rate.window_minutes),
      maxRequests: String(rate.max_requests_per_window == null ? 3 : rate.max_requests_per_window),
      daily: String(rate.daily_max == null ? 20 : rate.daily_max),
      timezone: rate.reset_timezone || "",
    },
    session: {
      allowPrivate: !!session.allow_private,
      allowGroup: !!session.allow_group,
      sessions: (session.sessions || []).map((item) => ({ ...item })),
    },
    admins: (session.admin_users || []).map((item) => ({ ...item })),
    advanced: {
      games: (advanced.preset_games || []).join("\n"),
      showStats: !!advanced.show_generation_stats,
    },
  };
}
function resetDrafts(data) {
  const drafts = draftFromState(data);
  Object.keys(drafts).forEach((section) => {
    state.drafts[section] = drafts[section];
    state.dirty[section] = false;
    state.versions[section] = 0;
  });
  state.conflicts = {};
  state.staleBaseline = false;
  Object.keys(lastSavedSignatures).forEach((key) => delete lastSavedSignatures[key]);
  Object.keys(lastPayloads).forEach((key) => delete lastPayloads[key]);
  state.source = drafts.model.source;
  state.searchModeOrder = null;
  state.searchModeEnabled = {};
  state.chartHover = -1;
  updateDirtyBadge();
}
function captureModelDraft() {
  const draft = state.drafts.model;
  if (!draft) return;
  draft.source = state.source;
  draft.providerId = $("providerSelect").value || "";
  draft.preset = $("presetSelect").value || "custom";
  draft.baseUrl = $("baseUrl").value;
  draft.apiFormat = $("apiFormat").value;
  draft.model = $("modelInput").value;
  draft.sendTemperature = $("sendTemperature").checked;
  draft.apiKey = $("apiKey").value;
}
// 搜索页数字输入：临时清空不能落盘成 0，先保留上次的值；只有用户
// 主动输入 0 才写 0。blur 时把空白恢复成保留值，避免看起来像已被清空。
const SEARCH_NUMERIC_FIELDS = {
  searchResults: "resultCount",
  searchTimeout: "timeout",
  searchPerTimeout: "perTimeout",
  summaryInput: "summaryInput",
  summaryChars: "summaryChars",
  materialMaxChars: "materialMaxChars",
  materialMaxItems: "materialMaxItems",
  materialSnippetChars: "materialSnippetChars",
};
function captureSearchDraft() {
  const draft = state.drafts.search;
  if (!draft) return;
  draft.enabled = $("searchEnabled").checked;
  Object.keys(SEARCH_NUMERIC_FIELDS).forEach((id) => {
    const value = $(id).value.trim();
    if (value !== "") draft[SEARCH_NUMERIC_FIELDS[id]] = value;
  });
  draft.summarizeEnabled = $("summarizeEnabled").checked;
  draft.apiProvider = $("apiProvider").value;
  draft.apiBase = $("searchBase").value;
  draft.apiKey = $("searchKey").value;
}
function captureRateDraft() {
  const draft = state.drafts.rate;
  if (!draft) return;
  draft.window = $("windowNumber").value;
  draft.maxRequests = $("maxNumber").value;
  draft.daily = $("dailyNumber").value;
  draft.timezone = $("rateTimezone").value;
}
function captureSessionToggles() {
  const draft = state.drafts.session;
  if (!draft) return;
  draft.allowPrivate = $("allowPrivate").checked;
  draft.allowGroup = $("allowGroup").checked;
}
function captureAdvancedDraft() {
  const draft = state.drafts.advanced;
  if (!draft) return;
  draft.games = $("presetGames").value;
  draft.showStats = $("showStats").checked;
}
async function loadState() {
  invalidateAsyncOperations();
  const token = ++state.asyncTokens.load;
  state.dataEpoch += 1;
  const submittedVersions = { ...state.versions };
  const submittedWriteEpoch = state.confirmedWriteEpoch;
  const submittedMutationEpoch = state.mutationEpoch;
  setBusy(true, "读取配置…");
  clearError();
  setGlobalNotice("");
  try {
    const data = ensureOk(await bridge.apiGet("config-center/state"));
    if (token !== state.asyncTokens.load) return false;
    if (
      state.mutationEpoch !== submittedMutationEpoch ||
      pendingMutations() > 0 ||
      state.confirmedWriteEpoch !== submittedWriteEpoch
    ) {
      // 读取期间只要开始过写入，这份快照就早于写入完成时的草稿/版本。
      // 不能用它 resetDrafts（会复用版本号并覆盖在途提交的新草稿），也
      // 不能把已经成功落库的保存/身份状态回退。
      setGlobalNotice(
        "刷新结果已过期：读取期间有配置保存或身份操作，已保留当前配置和草稿，请重新刷新。",
      );
      return false;
    }
    const changed = SECTION_NAMES.filter(
      (section) => state.versions[section] !== submittedVersions[section],
    );
    if (!changed.length) {
      state.data = data;
      if (typeof data.revision === "number") state.serverRevision = data.revision;
      state.chatDraft = new Set(state.selectedChats || []);
      state.chatDraftAll = state.selectedChats === null;
      state.identityConnector = state.identityConnector || (data.session.connectors || [])[0] || "";
      resetDrafts(data);
    } else {
      // 刷新请求期间用户又编辑了某些分区：只采用未被编辑分区的服务端快照，
      // 已编辑分区的草稿必须原样保留，不能被刷新响应静默覆盖。服务端修订
      // 号已经拿到，后续保存用新基线，不会因为旧 base_revision 再次冲突。
      if (typeof data.revision === "number") state.serverRevision = data.revision;
      ["model", "search", "rate", "advanced"].forEach((section) => {
        if (!changed.includes(section)) applyServerSnapshot(section, data);
      });
      if (!changed.includes("session")) applyServerSnapshot("sessions", data);
      if (!changed.includes("admins")) applyServerSnapshot("admins", data);
      applyServerSnapshot("identity", data);
      const fresh = draftFromState(state.data);
      SECTION_NAMES.forEach((section) => {
        if (changed.includes(section)) {
          state.dirty[section] = true;
        } else {
          state.drafts[section] = fresh[section];
          state.dirty[section] = false;
          state.versions[section] = 0;
        }
      });
      if (!changed.includes("model")) state.source = fresh.model.source;
      if (!changed.includes("search")) {
        // 搜索分区采用服务端快照时，模式缓存也必须一起重建；否则旧缓存会
        // 在 renderSearchModes 中把旧模式写回新草稿，保存时覆盖服务端值。
        state.searchModeOrder = null;
        state.searchModeEnabled = {};
      }
      if (!changed.includes("session")) {
        state.chatDraft = new Set(state.selectedChats || []);
        state.chatDraftAll = state.selectedChats === null;
      }
      updateDirtyBadge();
      setGlobalNotice(
        state.staleBaseline
          ? "刷新只应用了未编辑的分区；冲突草稿仍在，自动保存继续暂停，请再刷新一次。"
          : "刷新完成；刷新期间的新修改已保留为未保存草稿，请确认后再保存。",
      );
    }
    renderAll();
    // 以渲染后的真实表单值为基线签名：未改动字段失焦时不会被当成新修改。
    Object.keys(state.drafts).forEach((section) => {
      try {
        const signature = payloadSignature(section);
        if (signature) lastSavedSignatures[section] = signature;
      } catch (error) { /* 草稿未就绪时忽略 */ }
    });
    return true;
  } catch (error) {
    if (token === state.asyncTokens.load) showError(error, "modelMessage");
    throw error;
  } finally {
    if (token === state.asyncTokens.load) setBusy(false);
  }
}

async function loadStats() {
  const token = ++state.asyncTokens.stats;
  state.chartHover = -1;
  hideChartTip();
  const params = { period: state.period };
  if (state.selectedChats === null) {
    // 全部聊天：不传 chats，服务端使用全部记录。
  } else if (state.selectedChats.length === 0) {
    params.chats = "__none__";
  } else {
    params.chats = state.selectedChats.join(",");
  }
  try {
    const data = ensureOk(await bridge.apiGet("config-center/stats", params));
    if (token !== state.asyncTokens.stats) return;
    state.stats = data;
    renderStats();
  } catch (error) {
    if (token !== state.asyncTokens.stats) return;
    renderStatsFailure(error);
  }
}

function renderStatsFailure(error) {
  // 统计请求失败时不能保留上一个时间范围/聊天筛选的卡片和曲线，否则
  // 页面会把这些旧数字显示在新范围标签下。整体切到失败态并清空图表。
  state.stats = null;
  state.chartPoints = [];
  state.chartHover = -1;
  hideChartTip();
  const message = error && error.message ? error.message : "未知错误";
  const cards = [
    ["请求数", "加载失败", "统计请求未成功"],
    ["实际搜索", "加载失败", "统计请求未成功"],
    ["Tokens", "加载失败", "统计请求未成功"],
  ];
  $("statCards").innerHTML = cards.map((item) =>
    '<div class="stat"><span>' + esc(item[0]) + '</span><strong>' + esc(item[1]) + '</strong><small>' + esc(item[2]) + '</small></div>'
  ).join("");
  $("tokenNote").textContent = "统计加载失败：" + message + "；未显示上一次范围的数据。";
  $("rangeInfo").textContent = "统计加载失败：" + message;
  const canvas = $("trendChart");
  if (canvas) canvas.classList.add("hidden");
  const empty = $("chartEmpty");
  if (empty) {
    empty.classList.remove("hidden");
    empty.textContent = "统计加载失败，未显示旧数据";
  }
  $("chartTable").innerHTML = '<table><thead><tr><th>时间</th><th>请求</th><th>搜索</th><th>Tokens</th></tr></thead><tbody><tr><td colspan="4">统计加载失败：未显示上一次范围的数据。</td></tr></tbody></table>';
  const list = $("chatList");
  if (list) list.innerHTML = '<div class="empty">统计加载失败，无法读取聊天列表。</div>';
  updateChatBulkUI([]);
}

// 保存回执会整块重渲染列表；先记住焦点，画完再还回去，避免键盘排序后
// 焦点掉到 BODY。优先按 id 找，找不到再用 data-focus 键（含模式行）。
function focusSnapshot() {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const snapshot = {
    id: el.id || "",
    focus: (el.dataset && el.dataset.focus) || "",
    start: -1,
    end: -1,
  };
  try {
    if (el.tagName === "INPUT" && el.type !== "checkbox" && typeof el.selectionStart === "number") {
      snapshot.start = el.selectionStart;
      snapshot.end = el.selectionEnd;
    }
  } catch (error) { /* 部分输入类型不支持 selection */ }
  return (snapshot.id || snapshot.focus) ? snapshot : null;
}
function restoreFocus(snapshot) {
  if (!snapshot) return;
  let el = snapshot.id ? document.getElementById(snapshot.id) : null;
  if (!el && snapshot.focus) {
    el = document.querySelector('[data-focus="' + snapshot.focus + '"]');
  }
  if (!el || el.closest("[inert]")) return;
  if (el.disabled) {
    // 排序后按钮可能变为不可用：交给同一行里仍可用的控件，别掉到 BODY。
    const row = el.closest(".mode-row, .candidate-row, .list-row");
    const alternative = row && row.querySelector("input:not([disabled]), button:not([disabled])");
    if (!alternative) return;
    el = alternative;
  }
  if (document.activeElement !== el) el.focus();
  if (snapshot.start >= 0 && typeof el.setSelectionRange === "function") {
    try { el.setSelectionRange(snapshot.start, snapshot.end); } catch (error) { /* ignore */ }
  }
}
function renderAll() {
  const focus = focusSnapshot();
  renderModel();
  renderSearch();
  renderRate();
  renderSession();
  renderAdvanced();
  renderSummary();
  renderStats();
  restoreFocus(focus);
}

function renderSummary() {
  if (!state.data) return;
  const model = state.data.model;
  const isA = model.source === "astrbot";
  const selectedProvider = (model.providers || []).find((p) => p.id === model.astrbot_provider_id);
  $("sumModel").textContent = isA
    ? (selectedProvider ? (selectedProvider.label || selectedProvider.id) : "未选择 AstrBot 模型")
    : (model.manual.model || "未选择模型");
  $("sumModelMeta").textContent = isA
    ? (model.astrbot_provider_id ? "Provider：" + model.astrbot_provider_id : "请选择已配置模型")
    : (model.manual.base_url || "未填写 Base URL");
  const search = state.data.search;
  const labels = { astrbot: "Astrbot与搜索插件", api: "搜索API", web: "网页搜索", auto: "旧版自动", off: "关闭" };
  $("sumSearch").textContent = (search.enabled ? labels[search.mode] || search.mode : "关闭搜索");
  const candidateText = (search.candidates || [])
    .filter((item) => item.in_mode && item.available)
    .map((item) => item.label).join("、") || "当前没有可用候选";
  // 只在有真实诊断时显示「最近实际使用」，不凭配置猜当前生效。
  const last = search.last_result || {};
  const backendLabels = { ...labels, anysearch: "Anysearch", official: "Astrbot 官方搜索" };
  const usedKey = last.status === "success"
    ? (last.effective_backend || last.mode)
    : "";
  const lastLabel = usedKey ? (backendLabels[usedKey] || usedKey) : "";
  $("sumSearchMeta").textContent = lastLabel
    ? candidateText + " · 最近实际使用：" + lastLabel
    : candidateText;
  const rate = state.data.rate;
  $("sumRate").textContent = rate.window_minutes === 0
    ? "窗口不限制"
    : rate.window_minutes + " 分钟 / " + rate.max_requests_per_window + " 次";
  $("sumRateMeta").textContent = "每日 " + (rate.daily_max === 0 ? "不限制" : rate.daily_max + " 次") + " · " + rate.timezone_label + " 重置";
  const session = state.data.session;
  const sessions = session.sessions || [];
  const enabledSessions = sessions.filter((item) => item.enabled);
  const typeAllows = (item) => item.type === "private"
    ? session.allow_private
    : item.type === "group" ? session.allow_group : true;
  const effectiveSessions = enabledSessions.filter(typeAllows);
  const typeMeta = (session.allow_private ? "私聊开" : "私聊关") + " · " + (session.allow_group ? "群聊开" : "群聊关");
  if (session.config_error) {
    $("sumSession").textContent = "配置无效，普通会话已拒绝";
    $("sumSessionMeta").textContent = "阻止原因：" + configErrorText(session.config_error) + " · " + typeMeta;
  } else if (session.migration_required) {
    $("sumSession").textContent = sessions.length
      ? effectiveSessions.length + " 个迁移期生效会话"
      : "迁移未完成，普通会话暂停";
    $("sumSessionMeta").textContent = sessions.length
      ? "旧限制尚未迁移；仅列表内启用的精确 UMO 放行 · " + typeMeta
      : "旧 QQ/群限制无法安全映射；需添加明确的 UMO 会话 · " + typeMeta;
  } else if (!sessions.length) {
    if (!session.allow_private && !session.allow_group) {
      $("sumSession").textContent = "全部会话类型关闭";
      $("sumSessionMeta").textContent = "空列表，但私聊和群聊总开关均关闭 · " + typeMeta;
    } else {
      $("sumSession").textContent = "不限制会话（空列表）";
      $("sumSessionMeta").textContent = "空列表按总开关放行 · " + typeMeta;
    }
  } else if (!enabledSessions.length) {
    $("sumSession").textContent = "0 个启用会话";
    $("sumSessionMeta").textContent = "列表非空但全部停用，不会按空列表放开 · " + typeMeta;
  } else if (!effectiveSessions.length) {
    $("sumSession").textContent = "0 个生效会话";
    $("sumSessionMeta").textContent = "启用项均被私聊/群聊总开关关闭 · " + typeMeta;
  } else {
    $("sumSession").textContent = effectiveSessions.length + " 个生效会话";
    $("sumSessionMeta").textContent = typeMeta + (effectiveSessions.length < enabledSessions.length ? " · 部分启用项被类型开关关闭" : "");
  }
  const started = state.data.stats_started_at;
  $("retentionInfo").textContent = state.data.stats_started_at
    ? "统计记录开始时间：" + new Date(started * 1000).toLocaleString() + "。全部范围只显示插件实际保留的记录，不包含启用前的历史。"
    : "暂无统计记录。";
}

function renderStats() {
  const stats = state.stats;
  if (!stats) return;
  const matched = Number.isFinite(stats.matched_events)
    ? stats.matched_events
    : Number(stats.retained_events || 0);
  const noData = matched === 0;
  const cards = noData ? [
    ["请求数", "暂无数据", "当前范围没有实际记录"],
    ["实际搜索", "暂无数据", "当前范围没有实际搜索尝试"],
    ["Tokens", "暂无数据", "当前范围没有模型调用"],
  ] : [
    ["请求数", stats.totals.requests, "实际进入生成流程"],
    ["实际搜索", stats.totals.searches, "真实发起过后端尝试"],
    ["Tokens", stats.totals.token_status === "unavailable" ? "不可得" : stats.totals.tokens, "按上游实际返回累计"],
  ];
  $("statCards").innerHTML = cards.map((item) =>
    '<div class="stat"><span>' + esc(item[0]) + '</span><strong>' + esc(item[1]) + '</strong><small>' + esc(item[2]) + '</small></div>'
  ).join("");
  const note = [];
  if (noData) {
    note.push("当前范围没有实际记录，未按 0 补计，也不展示未经验证的历史数据。");
  } else {
    if (stats.totals.token_status === "incomplete") note.push("部分模型调用未返回 Token，总量统计不完整。");
    if (stats.totals.token_status === "unavailable") note.push("当前范围内没有可用的 Token 用量，未按 0 处理。");
    note.push("生成 " + stats.totals.tokens_by_kind.generate + " · 搜索汇总 " + stats.totals.tokens_by_kind.summarize + " · 主动测试 " + stats.totals.tokens_by_kind.test);
    note.push("模型失败 " + stats.totals.failures + " 次 · 降级/回退后端尝试 " + stats.totals.fallback_attempts + " 次");
    const byMode = stats.totals.searches_by_mode || {};
    const modeLabels = { astrbot: "Astrbot与搜索插件", api: "搜索API", web: "网页搜索", auto: "旧版自动", off: "旧版关闭" };
    const modeParts = [];
    Object.keys(modeLabels).forEach((key) => {
      if (byMode[key]) modeParts.push(modeLabels[key] + " " + byMode[key]);
    });
    // 只把服务端明确归为 unknown 的旧记录标成“未记录模式”；astrbot/web
    // 是真实的新模式，不能靠减法倒推成旧记录。
    if (byMode.unknown) modeParts.push("未记录模式（旧记录） " + byMode.unknown);
    if (modeParts.length) note.push("搜索模式：" + modeParts.join(" · ") + "。");
    if (stats.totals.fallback_incomplete) {
      note.push("部分旧搜索记录缺少请求分组，回退次数不完整；未按猜测值补计。");
    }
  }
  $("tokenNote").textContent = note.join(" ");
  const start = new Date(stats.range_start * 1000).toLocaleString();
  const end = new Date(stats.range_end * 1000).toLocaleString();
  $("rangeInfo").textContent = "范围：" + start + " — " + end + "；当前范围记录 " + matched + " 条；插件共保留 " + stats.retained_events + " 条";
  drawChart(stats);
  const rows = stats.buckets || [];
  $("chartTable").innerHTML = noData
    ? '<table><thead><tr><th>时间</th><th>请求</th><th>搜索</th><th>Tokens</th></tr></thead><tbody><tr><td colspan="4">暂无数据：当前范围没有实际记录，未按 0 补计。</td></tr></tbody></table>'
    : '<table><thead><tr><th>时间</th><th>请求</th><th>搜索</th><th>Tokens</th></tr></thead><tbody>' + rows.map((row) =>
      '<tr><td>' + esc(row.label) + '</td><td>' + row.requests + '</td><td>' + row.searches + '</td><td>' + (row.tokens_missing ? "不完整" : row.tokens) + '</td></tr>'
    ).join("") + '</tbody></table>';
  renderChats(stats.chats || []);
}

function filteredChats(chats) {
  const query = state.chatSearch.trim().toLowerCase();
  return chats.filter((chat) =>
    !query || (chat.label + " " + chat.umo + " " + chat.connector).toLowerCase().includes(query)
  );
}

function updateChatBulkUI(chats) {
  const query = state.chatSearch.trim();
  const filtered = filteredChats(chats);
  $("chatAll").textContent = query ? "全选结果" : "全选";
  $("chatNone").textContent = query ? "全不选结果" : "全不选";
  const scope = $("chatBulkScope");
  if (scope) {
    scope.textContent = query
      ? "搜索命中 " + filtered.length + " 个聊天；全选/全不选只作用于当前筛选结果。"
      : "全选/全不选作用于全部聊天（" + chats.length + " 个）。";
  }
  const hint = $("chatSelectionHint");
  if (hint) {
    hint.textContent = state.chatDraftAll
      ? "草稿：全部聊天（含以后新增）"
      : "草稿：" + state.chatDraft.size + " 个聊天";
  }
}

function renderChats(chats) {
  const list = $("chatList");
  const filtered = filteredChats(chats);
  updateChatBulkUI(chats);
  if (!filtered.length) {
    list.innerHTML = '<div class="empty">没有匹配的聊天记录。切换范围或等待真实请求后再试。</div>';
    return;
  }
  list.innerHTML = filtered.map((chat) => {
    const key = esc(chat.key);
    const checked = state.chatDraft.has(chat.key) ? "checked" : "";
    return '<label><input type="checkbox" value="' + key + '" ' + checked + '/><span>' + esc(chat.label) + '<small>' + esc(chat.umo) + '</small></span></label>';
  }).join("");
  list.querySelectorAll("input[type=checkbox]").forEach((input) => {
    input.addEventListener("change", () => {
      // 勾选只修改草稿；点“应用”后才影响已应用筛选和统计数据。
      if (input.checked) state.chatDraft.add(input.value);
      else state.chatDraft.delete(input.value);
      // 单个勾选是明确选择；即使恰好勾满当前列表，应用时仍按当前集合，
      // 以免把“全选当前”误当成“以后新增聊天也自动包含”。
      state.chatDraftAll = false;
      updateChatBulkUI(chats);
    });
  });
}

function hideChartTip() {
  const tip = $("chartTip");
  if (!tip) return;
  tip.classList.add("hidden");
}

function showChartTip(index) {
  const tip = $("chartTip");
  const wrap = $("trendChart")?.parentElement;
  const point = state.chartPoints[index];
  const row = state.stats?.buckets?.[index];
  if (!tip || !wrap || !point || !row) return;
  const tokenValue = row.tokens_missing ? "不完整" : String(row.tokens ?? 0);
  tip.innerHTML = '<div class="chart-tip-head">' + esc(row.label) + '</div>' +
    '<div class="chart-tip-row' + (state.metric === "requests" ? " active" : "") + '"><span>请求</span><strong>' + esc(String(row.requests ?? 0)) + '</strong></div>' +
    '<div class="chart-tip-row' + (state.metric === "searches" ? " active" : "") + '"><span>实际搜索</span><strong>' + esc(String(row.searches ?? 0)) + '</strong></div>' +
    '<div class="chart-tip-row' + (state.metric === "tokens" ? " active" : "") + '"><span>Tokens</span><strong>' + esc(tokenValue) + '</strong></div>';
  tip.classList.remove("hidden");
  const tipW = tip.offsetWidth || 180;
  const tipH = tip.offsetHeight || 90;
  let left = point.x + 14;
  if (left + tipW > wrap.clientWidth - 4) left = point.x - tipW - 14;
  left = Math.max(4, left);
  const top = Math.max(4, Math.min(point.y - tipH / 2, wrap.clientHeight - tipH - 4));
  tip.style.left = left + "px";
  tip.style.top = top + "px";
}

function drawChart(stats) {
  const canvas = $("trendChart");
  const empty = $("chartEmpty");
  const rows = stats.buckets || [];
  const values = rows.map((row) => Number(row[state.metric] || 0));
  const hasData = values.some((value) => value > 0);
  empty.classList.toggle("hidden", hasData);
  canvas.classList.toggle("hidden", !hasData);
  state.chartPoints = [];
  if (!hasData) {
    // 有记录但所选指标全为 0 与“范围内没有任何记录”是两种状态，不能
    // 都显示“没有实际记录”，否则会把零值趋势误报成无数据。
    const matched = Number(stats.matched_events || 0);
    empty.textContent = matched > 0
      ? "当前范围有记录，但所选指标均为 0"
      : "当前范围内没有实际记录";
    state.chartHover = -1;
    hideChartTip();
    return;
  }
  const context = canvas.getContext("2d");
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 600;
  const height = 220;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const pad = { left: 42, right: 14, top: 16, bottom: 26 };
  const max = Math.max.apply(null, values.concat([1]));
  const plotW = Math.max(10, width - pad.left - pad.right);
  const plotH = height - pad.top - pad.bottom;
  const styles = getComputedStyle(document.documentElement);
  const token = (name, fallback) => styles.getPropertyValue(name).trim() || fallback;
  const line = token("--line", "#E2E8F0");
  const muted = token("--muted", "#55657D");
  const paper = token("--paper", "#FFFFFF");
  const accent = state.metric === "tokens" ? token("--teal", "#0F8F8E") : state.metric === "searches" ? token("--amber", "#B7791F") : token("--accent", "#2457F5");
  context.strokeStyle = line;
  context.lineWidth = 1;
  context.beginPath();
  for (let i = 0; i <= 4; i += 1) {
    const y = pad.top + plotH * i / 4;
    context.moveTo(pad.left, y);
    context.lineTo(width - pad.right, y);
    context.strokeStyle = line;
    context.stroke();
    context.fillStyle = muted;
    context.font = "11px -apple-system, sans-serif";
    context.fillText(String(Math.round(max * (4 - i) / 4)), 4, y + 4);
  }
  const points = values.map((value, index) => {
    const x = pad.left + (rows.length <= 1 ? plotW / 2 : plotW * index / (rows.length - 1));
    const y = pad.top + plotH * (1 - value / max);
    return { x, y, value, label: rows[index].label };
  });
  state.chartPoints = points;
  if (state.chartHover >= points.length) state.chartHover = -1;
  if (state.chartHover >= 0) {
    const active = points[state.chartHover];
    context.save();
    context.setLineDash([3, 4]);
    context.strokeStyle = line;
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(active.x, pad.top);
    context.lineTo(active.x, height - pad.bottom);
    context.stroke();
    context.restore();
  }
  context.beginPath();
  points.forEach((point, index) => {
    if (index === 0) context.moveTo(point.x, point.y);
    else context.lineTo(point.x, point.y);
  });
  context.strokeStyle = accent;
  context.lineWidth = 2;
  context.stroke();
  context.fillStyle = accent;
  points.forEach((point, index) => {
    context.beginPath();
    context.arc(point.x, point.y, state.chartHover === index ? 4.5 : 3, 0, Math.PI * 2);
    context.fill();
    if (state.chartHover === index) {
      context.strokeStyle = paper;
      context.lineWidth = 2;
      context.stroke();
    }
  });
  context.fillStyle = muted;
  context.font = "11px -apple-system, sans-serif";
  points.forEach((point, index) => {
    if (rows.length <= 7 || index % Math.ceil(rows.length / 7) === 0) {
      context.fillText(point.label, point.x - 12, height - 6);
    }
  });
}

function renderModel() {
  if (!state.data || !state.drafts.model) return;
  const model = state.data.model;
  const draft = state.drafts.model;
  state.source = draft.source;
  $("sourceAstrbot").classList.toggle("active", draft.source === "astrbot");
  $("sourceManual").classList.toggle("active", draft.source === "manual");
  $("sourceAstrbot").setAttribute("aria-pressed", String(draft.source === "astrbot"));
  $("sourceManual").setAttribute("aria-pressed", String(draft.source === "manual"));
  $("astrbotPanel").classList.toggle("hidden", draft.source !== "astrbot");
  $("manualPanel").classList.toggle("hidden", draft.source !== "manual");
  renderProviderOptions();
  $("providerHint").textContent = (model.providers || []).length
    ? "已发现 " + model.providers.length + " 个 AstrBot 聊天 Provider；可用上方筛选。"
    : "未发现已启用的 AstrBot 聊天 Provider，请先在 AstrBot 中配置。";
  const presets = model.presets || [];
  $("presetSelect").innerHTML = presets.map((preset) =>
    '<option value="' + esc(preset.id) + '">' + esc(preset.label) + '</option>'
  ).join("");
  $("presetSelect").value = draft.preset;
  $("baseUrl").value = draft.baseUrl;
  $("apiFormat").value = draft.apiFormat;
  $("modelInput").value = draft.model;
  $("sendTemperature").checked = draft.sendTemperature;
  $("apiKey").value = draft.apiKey;
  const manual = model.manual || {};
  $("keyState").textContent = manual.key_configured
    ? (manual.key_usable
      ? "已保存 Key " + (manual.key_masked || "••••••••") + "；留空保留，填入新值覆盖"
      : "地址已变更，旧地址的 Key 不会发送到新地址；请重新输入")
    : "未保存 Key";
  $("contextHint").textContent = "上下文长度：未知（未从服务可靠取得，不猜测）";
  $("modelHint").textContent = "可点击“获取模型列表”读取真实列表，也可手动填写模型名。";
}

function renderProviderSelectionState() {
  const el = $("providerSelectionState");
  if (!el || !state.data?.model) return;
  const current = (state.drafts.model && state.drafts.model.providerId) || "";
  const saved = state.data.model.astrbot_provider_id || "";
  const providers = state.data.model.providers || [];
  if (!current) {
    el.textContent = "未选择模型";
    el.className = "hint provider-selection";
    return;
  }
  const known = providers.find((provider) => provider.id === current);
  const label = known ? (known.label || known.id) : current + "（当前不可用）";
  const isSaved = current === saved;
  el.textContent = (isSaved ? "已保存：" : "待保存：") + label +
    (isSaved ? "" : "（尚未保存，点击保存后生效）");
  el.className = "hint provider-selection" + (isSaved ? "" : " pending");
}

function renderProviderOptions() {
  if (!state.data) return;
  const model = state.data.model;
  const select = $("providerSelect");
  const current = (state.drafts.model && state.drafts.model.providerId) || "";
  const filter = ($("providerFilter").value || "").toLowerCase();
  select.innerHTML = "";
  const providers = model.providers || [];
  if (!providers.length) {
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "请先在 AstrBot 中配置模型";
    empty.disabled = true;
    empty.selected = true;
    select.appendChild(empty);
    select.disabled = true;
    renderProviderSelectionState();
    return;
  }
  select.disabled = false;
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = "请选择已配置模型";
  placeholder.disabled = true;
  select.appendChild(placeholder);
  const filtered = providers.filter((provider) =>
    !filter || (provider.id + " " + (provider.label || "")).toLowerCase().includes(filter)
  );
  filtered.forEach((provider) => {
    const option = document.createElement("option");
    option.value = provider.id;
    option.textContent = provider.label || provider.id;
    select.appendChild(option);
  });
  if (current && !filtered.some((p) => p.id === current)) {
    const known = providers.find((p) => p.id === current);
    const missing = document.createElement("option");
    missing.value = current;
    missing.textContent = (known?.label || current) +
      (known ? "（不在筛选结果）" : "（当前不可用）");
    select.appendChild(missing);
  }
  if (!filtered.length && !current) {
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "没有匹配的模型";
    none.disabled = true;
    none.selected = true;
    select.appendChild(none);
  }
  select.value = current || "";
  renderProviderSelectionState();
}

function payload() {
  captureModelDraft();
  const draft = state.drafts.model;
  return {
    source: draft.source,
    astrbot_provider_id: draft.providerId,
    manual: {
      base_url: draft.baseUrl.trim(),
      model: draft.model.trim(),
      api_format: draft.apiFormat,
      send_temperature: draft.sendTemperature,
      provider_preset: draft.preset || "custom",
      api_key: draft.apiKey,
    },
    umo: "",
  };
}
function currentTestPayload() {
  return payload();
}

async function saveModel() {
  await startSectionSave(
    "model",
    payload(),
    "modelMessage",
    "保存模型配置…",
    "已保存。Key 不会回显完整内容。",
  );
}

async function fetchModels() {
  const submittedVersion = state.versions.model;
  const submittedEpoch = state.dataEpoch;
  const token = ++state.asyncTokens.models;
  const messageToken = nextMessageToken("modelMessage");
  setBusy(true, "请求模型列表…");
  setMessage("modelMessage", messageToken, "正在请求供应商真实模型列表…");
  try {
    const data = ensureOk(await bridge.apiPost("config-center/model/models", payload()));
    if (token !== state.asyncTokens.models || submittedEpoch !== state.dataEpoch) return;
    const models = data.models || [];
    $("modelList").innerHTML = models.map((model) =>
      '<option value="' + esc(model) + '"></option>'
    ).join("");
    let text = models.length
      ? "已获取 " + models.length + " 个模型，请从模型输入框建议中选择。"
      : "供应商未返回模型列表；可手动填写模型名。";
    if (submittedVersion !== state.versions.model) {
      text += " 列表基于点击时的表单；当前表单已修改，可重新获取。";
    }
    setMessage("modelMessage", messageToken, text, models.length ? "ok" : "");
  } catch (error) {
    if (token !== state.asyncTokens.models || submittedEpoch !== state.dataEpoch) return;
    $("modelList").innerHTML = "";
    setMessage("modelMessage", messageToken, "获取模型失败：" + error.message, "error");
  } finally {
    if (token === state.asyncTokens.models) setBusy(false);
  }
}

async function testModel() {
  const submittedVersion = state.versions.model;
  const submittedEpoch = state.dataEpoch;
  const token = ++state.asyncTokens.modelTest;
  const messageToken = nextMessageToken("modelMessage");
  setBusy(true, "测试模型…");
  setMessage("modelMessage", messageToken, "正在测试当前选择的模型…");
  try {
    const data = ensureOk(await bridge.apiPost("config-center/model/test", currentTestPayload()));
    if (token !== state.asyncTokens.modelTest || submittedEpoch !== state.dataEpoch) return;
    let text = "✅ " + (data.message || "模型测试成功");
    if (submittedVersion !== state.versions.model) {
      text += "（测试针对点击时的表单；当前有更新的未保存修改）";
    }
    setMessage("modelMessage", messageToken, text, "ok");
  } catch (error) {
    if (token !== state.asyncTokens.modelTest || submittedEpoch !== state.dataEpoch) return;
    setMessage("modelMessage", messageToken, "❌ " + error.message, "error");
  } finally {
    if (token === state.asyncTokens.modelTest) setBusy(false);
  }
}

function normalizeSearchModes(draft) {
  const known = SEARCH_MODES.map((item) => item.key);
  const first = !Array.isArray(state.searchModeOrder);
  let order = first
    ? (Array.isArray(draft.modeOrder) ? draft.modeOrder : []).filter((key) => known.includes(key))
    : state.searchModeOrder.filter((key) => known.includes(key));
  known.forEach((key) => { if (!order.includes(key)) order.push(key); });
  state.searchModeOrder = order;
  if (!state.searchModeEnabled || typeof state.searchModeEnabled !== "object") state.searchModeEnabled = {};
  Object.keys(state.searchModeEnabled).forEach((key) => {
    if (!known.includes(key)) delete state.searchModeEnabled[key];
  });
  if (first) {
    // 首次渲染：服务端保存的勾选是唯一依据；没有列表的旧配置才从
    // 可见 mode 推导（auto=三组、off=全关、单一模式=对应组）。
    const saved = Array.isArray(draft.modeEnabled)
      ? draft.modeEnabled.filter((key) => known.includes(key))
      : null;
    let enabledSet;
    if (saved) {
      enabledSet = new Set(saved);
    } else {
      const legacy = String(draft.mode || "auto").toLowerCase();
      let groups;
      if (legacy === "off") groups = [];
      else if (legacy === "auto" || !known.includes(legacy)) groups = known.slice();
      else groups = [legacy];
      enabledSet = new Set(groups);
    }
    known.forEach((key) => { state.searchModeEnabled[key] = enabledSet.has(key); });
  }
  draft.modeOrder = order.slice();
  draft.modeEnabled = known.filter((key) => state.searchModeEnabled[key]);
  draft.mode = effectiveSearchMode();
}

function effectiveSearchMode() {
  const order = state.searchModeOrder || [];
  const key = order.find((item) => state.searchModeEnabled[item]);
  return key || "off";
}

function modeMeta(key) {
  return SEARCH_MODES.find((item) => item.key === key) || SEARCH_MODES[0];
}

function renderSearchModes() {
  const focus = focusSnapshot();
  const draft = state.drafts.search;
  if (!draft) return;
  normalizeSearchModes(draft);
  const order = state.searchModeOrder;
  const masterOn = $("searchEnabled").checked;
  const effective = masterOn ? effectiveSearchMode() : "off";
  draft.mode = effective;
  const list = $("modeList");
  list.innerHTML = order.map((key, index) => {
    const meta = modeMeta(key);
    const enabled = !!state.searchModeEnabled[key];
    const status = enabled
      ? '<span class="status-badge ok">已勾选</span>'
      : '<span class="status-badge">未启用</span>';
    return '<div class="candidate-row mode-row" draggable="true" data-mode-row="' + esc(key) + '">' +
      '<span class="drag-handle" title="拖动调整优先级" aria-hidden="true">⋮⋮</span>' +
      '<label class="candidate-check"><input type="checkbox" data-mode-check="' + esc(key) + '" data-focus="mode-check:' + esc(key) + '" ' + (enabled ? "checked" : "") + ' aria-label="启用' + esc(meta.label) + '"/></label>' +
      '<div class="candidate-main"><strong>' + esc(meta.label) + '</strong><span>' + esc(meta.hint) + ' · 优先级 ' + (index + 1) + '</span></div>' +
      '<div class="candidate-actions">' + status +
      '<button type="button" class="order-btn" data-mode-move="' + esc(key) + '" data-direction="-1" data-focus="mode-move:' + esc(key) + ':-1" title="上移" ' + (index === 0 ? "disabled" : "") + '>↑</button>' +
      '<button type="button" class="order-btn" data-mode-move="' + esc(key) + '" data-direction="1" data-focus="mode-move:' + esc(key) + ':1" title="下移" ' + (index === order.length - 1 ? "disabled" : "") + '>↓</button>' +
      '</div></div>';
  }).join("");
  list.querySelectorAll("[data-mode-check]").forEach((input) => {
    input.addEventListener("change", () => {
      const key = input.dataset.modeCheck;
      state.searchModeEnabled[key] = input.checked;
      draft.mode = effectiveSearchMode();
      markSectionDirty("search");
      renderSearchModes();
      applySearchDisabled();
      // 排序后稍等再保存，避免保存响应重渲染打断键盘焦点恢复。
      scheduleSectionSave("search", 300);
    });
  });
  list.querySelectorAll("[data-mode-move]").forEach((button) => {
    button.addEventListener("click", () => moveSearchMode(button.dataset.modeMove, Number(button.dataset.direction)));
  });
  let draggedKey = "";
  restoreFocus(focus);
  list.querySelectorAll("[data-mode-row]").forEach((row) => {
    row.addEventListener("dragstart", (event) => {
      draggedKey = row.dataset.modeRow;
      row.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggedKey);
    });
    row.addEventListener("dragend", () => {
      row.classList.remove("dragging");
      draggedKey = "";
    });
    row.addEventListener("dragover", (event) => { event.preventDefault(); });
    row.addEventListener("drop", (event) => {
      event.preventDefault();
      const targetKey = row.dataset.modeRow;
      const from = state.searchModeOrder.indexOf(draggedKey);
      const to = state.searchModeOrder.indexOf(targetKey);
      if (from < 0 || to < 0 || from === to) return;
      state.searchModeOrder.splice(from, 1);
      state.searchModeOrder.splice(to, 0, draggedKey);
      draft.mode = effectiveSearchMode();
      markSectionDirty("search");
      renderSearchModes();
      applySearchDisabled();
      saveSectionNow("search");
    });
  });
}

function moveSearchMode(key, direction) {
  const order = state.searchModeOrder || [];
  const from = order.indexOf(key);
  const to = from + direction;
  if (from < 0 || to < 0 || to >= order.length) return;
  order.splice(from, 1);
  order.splice(to, 0, key);
  const draft = state.drafts.search;
  if (draft) draft.mode = effectiveSearchMode();
  markSectionDirty("search");
  renderSearchModes();
  applySearchDisabled();
  scheduleSectionSave("search", 300);
  // 键盘排序后重渲染会替换按钮，焦点必须回到同一行同一方向；
  // 已到边界时回到反向按钮，不能掉到 BODY。
  const selector = (dir) =>
    '.order-btn[data-mode-move="' + key + '"][data-direction="' + dir + '"]';
  let target = document.querySelector(selector(direction));
  if (!target || target.disabled) {
    target = document.querySelector(selector(-direction));
  }
  if (target && !target.disabled) target.focus();
}

function normalizedBaseUrl(value) {
  const text = String(value || "").trim();
  try {
    const url = new URL(text);
    const path = (url.pathname.replace(/\/+$/, "") || "/");
    return url.protocol + "//" + url.host.toLowerCase() + path + url.search;
  } catch (_) {
    return text.replace(/\/+$/, "");
  }
}

function searchBaseChanged() {
  const search = state.data?.search;
  const draft = state.drafts.search;
  if (!search || !draft || !search.api_key_configured) return false;
  return search.api_key_usable === false ||
    normalizedBaseUrl(draft.apiBase) !== normalizedBaseUrl(search.api_base_url);
}

function updateSearchKeyState() {
  const search = state.data?.search;
  const draft = state.drafts.search;
  if (!search || !draft) return;
  let text;
  if (searchBaseChanged()) {
    text = draft.apiKey.trim()
      ? "搜索地址已变更；保存后新 Key 将只绑定到当前地址。"
      : "搜索地址已变更：旧 Key 不会复用，必须输入新 Key 后才能保存。";
  } else if (search.api_key_configured) {
    text = draft.apiKey.trim()
      ? "保存后将用新 Key 替换已保存 Key，并绑定当前地址。"
      : "已保存搜索 Key " + (search.api_key_masked || "••••••••") + "；留空保留";
  } else {
    text = "未保存搜索 API Key";
  }
  $("searchMessage").textContent = text;
}

function renderSearch() {
  if (!state.data || !state.drafts.search) return;
  const search = state.data.search;
  const draft = state.drafts.search;
  $("searchEnabled").checked = draft.enabled;
  renderSearchModes();
  $("searchResults").value = draft.resultCount;
  $("searchTimeout").value = draft.timeout;
  $("searchPerTimeout").value = draft.perTimeout;
  $("summaryInput").value = draft.summaryInput;
  $("summaryChars").value = draft.summaryChars;
  $("materialMaxChars").value = draft.materialMaxChars;
  $("materialMaxItems").value = draft.materialMaxItems;
  $("materialSnippetChars").value = draft.materialSnippetChars;
  $("summarizeEnabled").checked = draft.summarizeEnabled;
  $("apiProvider").value = draft.apiProvider;
  $("searchBase").value = draft.apiBase;
  $("searchKey").value = draft.apiKey;
  updateSearchKeyState();
  const last = search.last_result || {};
  $("lastSearch").textContent = last && Object.keys(last).length
    ? JSON.stringify({
      status: last.status,
      mode: last.mode,
      effective_backend: last.effective_backend,
      attempted: last.attempted,
      skipped: last.skipped,
      degraded: last.degraded,
      result_count: (last.results || []).length,
      results: (last.results || []).slice(0, 5).map((item) => ({
        title: item.title,
        url: item.url,
      })),
      failure_reason: last.failure_reason,
    }, null, 2)
    : "还没有搜索记录。配置中心加载和切换筛选不会发起搜索。";
  const failure = last && last.failure_reason ? String(last.failure_reason) : "";
  const failureEl = $("searchFailure");
  if (failureEl) {
    failureEl.textContent = failure
      ? "最近一次失败原因：" + failure
      : "最近一次没有失败记录。";
    failureEl.className = failure ? "hint warn" : "hint";
  }
  applySearchDisabled();
}

function applySearchDisabled() {
  const enabled = $("searchEnabled").checked;
  ["searchResults", "searchTimeout", "searchPerTimeout", "summaryInput", "summaryChars", "materialMaxChars", "materialMaxItems", "materialSnippetChars", "summarizeEnabled", "apiProvider", "searchBase", "searchKey", "searchTest"].forEach((id) => {
    $(id).disabled = !enabled;
  });
  const modeList = $("modeList");
  if (modeList) {
    modeList.classList.toggle("disabled", !enabled);
    modeList.setAttribute("aria-disabled", String(!enabled));
    modeList.inert = !enabled;
  }
}
function searchPayload() {
  captureSearchDraft();
  const draft = state.drafts.search;
  normalizeSearchModes(draft);
  return {
    enabled: draft.enabled,
    mode: draft.mode,
    mode_order: (state.searchModeOrder || []).slice(),
    mode_enabled: SEARCH_MODES.map((item) => item.key).filter((key) => state.searchModeEnabled[key]),
    result_count: Number(draft.resultCount),
    timeout_ms: Number(draft.timeout),
    per_backend_timeout_ms: Number(draft.perTimeout),
    summary_input_max_chars: Number(draft.summaryInput),
    summarize_enabled: draft.summarizeEnabled,
    summarize_max_chars: Number(draft.summaryChars),
    material_max_chars: Number(draft.materialMaxChars),
    material_max_items: Number(draft.materialMaxItems),
    material_snippet_chars: Number(draft.materialSnippetChars),
    api_provider: draft.apiProvider,
    api_base_url: draft.apiBase.trim(),
    api_key: draft.apiKey,
    clear_api_key: false,
  };
}
async function saveSearch() {
  captureSearchDraft();
  if (searchBaseChanged() && !$("searchKey").value.trim()) {
    const token = nextMessageToken("searchSaveMessage");
    setMessage(
      "searchSaveMessage",
      token,
      "搜索服务地址已变更：请重新输入 API Key 后再保存。",
      "error",
    );
    $("searchKey").focus();
    return;
  }
  await startSectionSave(
    "search",
    searchPayload(),
    "searchSaveMessage",
    "保存搜索配置…",
    "搜索配置已保存。",
  );
}
async function testSearch() {
  const token = ++state.asyncTokens.searchTest;
  setBusy(true, "测试搜索…");
  $("searchTestResult").textContent = "正在发起一次真实搜索调用…";
  try {
    const data = ensureOk(await bridge.apiPost("config-center/search/test", { game: "原神" }));
    if (token !== state.asyncTokens.searchTest) return;
    const details = [];
    (data.attempted || []).forEach((entry) => {
      details.push("尝试 " + entry.backend + "：" + entry.status +
        (entry.reason ? "（" + entry.reason + "）" : "") +
        "，" + (entry.result_count || 0) + " 条");
    });
    (data.skipped || []).forEach((entry) => {
      details.push("跳过 " + entry.backend + "：" + (entry.reason || entry.status));
    });
    $("searchTestResult").textContent = (data.success ? "✅ " : "❌ ") + (data.message || "搜索完成") + "；搜索组：" + (data.mode || "未记录") + "，后端：" + (data.backend || "无") + "，结果 " + (data.result_count || 0) + " 条。" + (details.length ? "本次尝试：" + details.join("；") + "。" : "") + "测试使用已保存的搜索配置；不会保存、丢弃或上传当前未保存表单。";
  } catch (error) {
    if (token !== state.asyncTokens.searchTest) return;
    $("searchTestResult").textContent = "❌ " + error.message;
  } finally {
    if (token === state.asyncTokens.searchTest) setBusy(false);
  }
}

function syncRange(rangeId, numberId, hiddenRowId) {
  const savedValue = () => {
    const saved = (state.data && state.data.rate) || {};
    const map = {
      windowNumber: saved.window_minutes,
      maxNumber: saved.max_requests_per_window,
      dailyNumber: saved.daily_max,
    };
    const value = map[numberId];
    return value == null ? $(numberId).value : String(value);
  };
  const touch = (value, immediate) => {
    const text = String(value == null ? "" : value).trim();
    if (!text) {
      // 短暂清空输入时不写入 0（会被服务端解释为“不限制”）。
      markSectionDirty("rate");
      return;
    }
    $(numberId).value = text;
    $(rangeId).value = text;
    if (hiddenRowId) {
      $(hiddenRowId).classList.toggle("hidden", Number(text) === 0);
    }
    captureRateDraft();
    markSectionDirty("rate");
    if (immediate) saveSectionNow("rate");
    else scheduleSectionSave("rate", 400);
  };
  $(numberId).addEventListener("blur", () => {
    if (!String($(numberId).value).trim()) {
      const fallback = savedValue();
      $(numberId).value = fallback;
      $(rangeId).value = fallback;
      captureRateDraft();
      markSectionDirty("rate");
    }
  });
  $(rangeId).addEventListener("input", () => touch($(rangeId).value, false));
  $(rangeId).addEventListener("change", () => touch($(rangeId).value, true));
  $(numberId).addEventListener("input", () => touch($(numberId).value, false));
  $(numberId).addEventListener("change", () => touch($(numberId).value, true));
}
function renderTimezoneOptions(current) {
  const value = current || "";
  const options = TIMEZONES.slice();
  if (value && !options.some((item) => item[0] === value)) options.push([value, value + "（当前配置）"]);
  const select = $("rateTimezone");
  select.innerHTML = options.map((item) =>
    '<option value="' + esc(item[0]) + '">' + esc(item[1]) + '</option>'
  ).join("");
  select.value = value;
}
function renderRate() {
  if (!state.data || !state.drafts.rate) return;
  const rate = state.data.rate;
  const draft = state.drafts.rate;
  $("windowRange").value = draft.window;
  $("windowNumber").value = draft.window;
  $("maxRange").value = draft.maxRequests;
  $("maxNumber").value = draft.maxRequests;
  $("dailyRange").value = draft.daily;
  $("dailyNumber").value = draft.daily;
  renderTimezoneOptions(draft.timezone);
  const windowMinutes = Number(draft.window) || 0;
  const dailyMax = Number(draft.daily) || 0;
  $("rateRuntime").textContent = windowMinutes === 0
    ? "无窗口限制；每日" + (dailyMax === 0 ? "不限制" : " " + dailyMax + " 次") + "；管理员例外。"
    : windowMinutes + " 分钟最多 " + (Number(draft.maxRequests) || 0) + " 次；每日 " + (dailyMax === 0 ? "不限制" : dailyMax + " 次") + "；管理员例外。";
  $("maxRow").classList.toggle("hidden", windowMinutes === 0);
  const identities = state.data.session.known_identities || [];
  const connectors = state.data.session.connectors || [];
  $("identityGroupCount").textContent = String((state.data.session.identity_links || []).length);
  $("identityKnownCount").textContent = String(identities.length);
  if (!$("identityConnector").value) {
    $("identityConnector").value = state.identityConnector || connectors[0] || "";
  }
  $("identityList").innerHTML = identities.length ? identities.map((item) =>
    '<div class="list-row"><div class="meta"><strong>' + esc(item.connector) + ' / ' + esc(item.uid) + '</strong><span>' + (item.linked ? "已关联：" + esc(item.linked) : "未关联：独立计数") + '</span></div></div>'
  ).join("") : '<div class="empty">还没有可关联的真实身份记录。产生请求后才会出现。</div>';
  $("linkList").innerHTML = (state.data.session.identity_links || []).length
    ? state.data.session.identity_links.map((link) =>
      '<div class="list-row"><div class="meta"><strong>' + esc(link.connector) + '</strong><span>' + esc((link.uids || []).join("、")) + '</span></div><button class="mini danger" data-unlink="' + esc(link.id) + '">解除</button></div>'
    ).join("")
    : "";
  $("linkList").querySelectorAll("[data-unlink]").forEach((button) => {
    button.addEventListener("click", () => unlinkIdentity(button.dataset.unlink));
  });
}
function ratePayload() {
  captureRateDraft();
  const draft = state.drafts.rate;
  return {
    window_minutes: Number(draft.window),
    max_requests_per_window: Number(draft.maxRequests),
    daily_max: Number(draft.daily),
    reset_timezone: draft.timezone.trim(),
  };
}
async function saveRate() {
  const draft = state.drafts.rate;
  if (draft && [draft.window, draft.maxRequests, draft.daily]
    .some((value) => String(value == null ? "" : value).trim() === "")) {
    setMessage("rateMessage", nextMessageToken("rateMessage"), "请填写完整的频率数值后再保存。", "warn");
    return;
  }
  await startSectionSave(
    "rate",
    ratePayload(),
    "rateMessage",
    "保存频率配置…",
    "频率配置已保存。",
  );
}
async function linkIdentity() {
  const uids = $("identityUids").value.split(",").map((item) => item.trim()).filter(Boolean);
  await startIdentityMutation(
    { action: "link", connector: $("identityConnector").value, uids },
    "关联身份…",
    "身份已关联：共同历史只计一次；解除后各自新增的请求会相加，不会重置额度。",
    "关联失败，草稿未受影响：",
  );
}
async function unlinkIdentity(id) {
  await startIdentityMutation(
    { action: "unlink", id },
    "解除关联…",
    "已解除关联：共同历史仍只计一次；解除后各自新增的请求分别计数，不会重置额度。",
    "解除失败，草稿未受影响：",
  );
}

function renderSession() {
  if (!state.data || !state.drafts.session) return;
  const session = state.data.session;
  const draft = state.drafts.session;
  $("allowPrivate").checked = draft.allowPrivate;
  $("allowGroup").checked = draft.allowGroup;
  $("migrationBanner").classList.toggle("hidden", !session.migration_required);
  $("migrationBanner").textContent = session.migration_required
    ? "检测到旧版非空 QQ/群号限制，新 UMO 策略尚未明确。普通用户会被暂停；请添加明确的 UMO 会话后保存。"
    : "";
  const pendingUmos = session.legacy_umo_pending || [];
  $("umoPending").classList.toggle("hidden", !pendingUmos.length);
  if (pendingUmos.length) {
    const reasonText = {
      invalid_list: "列表格式无效",
      empty: "空条目",
      invalid_format: "缺少平台或会话段",
      unknown_type: "无法识别的会话类型",
    };
    const items = pendingUmos.map((item) =>
      (item.umo ? '"' + item.umo + '"' : "（空条目）") +
      "（" + (reasonText[item.reason] || "无法迁移") + "）"
    ).join("、");
    $("umoPending").textContent = "以下旧 UMO 无法安全迁移：" + items +
      "。这些条目不会授权任何会话，请添加明确的 UMO 会话后保存。";
  } else {
    $("umoPending").textContent = "";
  }
  const connectors = session.connectors || [];
  if (!$("adminConnector").value) {
    $("adminConnector").value = connectors[0] || "";
  }
  if (!$("identityConnector").value) {
    $("identityConnector").value = connectors[0] || "";
  }
  const admins = state.drafts.admins;
  $("adminList").innerHTML = admins.length ? admins.map((item, index) =>
    '<div class="list-row"><div class="meta"><strong>' + esc(item.uid) + '</strong><span>' + esc(item.connector || "未限定连接器（legacy）") + (item.legacy ? " · legacy" : "") + '</span></div><button class="mini danger" data-admin-remove="' + index + '">移除</button></div>'
  ).join("") : '<div class="empty">还没有管理员。空列表表示没有管理员例外，普通用户仍受会话和频率限制。</div>';
  const pendingAdmins = session.legacy_admin_pending || [];
  $("adminPending").classList.toggle("hidden", !pendingAdmins.length);
  $("adminPending").innerHTML = pendingAdmins.length
    ? "旧版管理员 " + pendingAdmins.map((item) => esc(item.uid)).join("、") + " 未绑定连接器，当前不会生效。需要保留权限的请按“连接器 + UID”重新添加；不需要的可以直接清除待迁移提示。 <button type=\"button\" id=\"clearAdminPending\" class=\"mini secondary\">清除待迁移提示</button>"
    : "";
  const clearPending = $("clearAdminPending");
  if (clearPending) {
    clearPending.addEventListener("click", async () => {
      await saveAdmins(true);
    });
  }
  $("adminList").querySelectorAll("[data-admin-remove]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.drafts.admins.splice(Number(button.dataset.adminRemove), 1);
      markSectionDirty("admins");
      renderSession();
      await saveAdmins();
    });
  });
  if (!$("adminConnector").value) {
    $("adminConnector").value = connectors[0] || "";
  }
  if (!$("identityConnector").value) {
    $("identityConnector").value = connectors[0] || "";
  }
  const filterConnector = $("sessionConnectorFilter").value;
  $("sessionConnectorFilter").innerHTML = '<option value="">全部连接器</option>' + connectors.map((item) =>
    '<option value="' + esc(item) + '">' + esc(item) + '</option>'
  ).join("");
  $("sessionConnectorFilter").value = filterConnector;
  const filterType = $("sessionTypeFilter").value;
  const rows = draft.sessions.filter((item) =>
    (!filterConnector || item.connector === filterConnector) && (!filterType || item.type === filterType)
  );
  $("sessionList").innerHTML = rows.length ? rows.map((item) => {
    const index = draft.sessions.indexOf(item);
    const enabled = !!item.enabled;
    return '<div class="list-row"><div class="meta"><strong>' + esc(item.connector) + ' · ' + esc(item.type) + '</strong><span>' + esc(item.umo) + '</span></div><div class="ops"><label class="row-check"><input type="checkbox" data-session-check="' + index + '" ' + (enabled ? "checked" : "") + '/><span class="state-text">' + (enabled ? "已启用" : "已停用") + '</span></label><button class="mini danger" data-session-remove="' + index + '">移除</button></div></div>';
  }).join("") : '<div class="empty">没有符合筛选的会话。空列表在总开关允许时表示不限制；添加后只允许列表中的 UMO。</div>';
  $("sessionList").querySelectorAll("[data-session-check]").forEach((input) => {
    input.addEventListener("change", async () => {
      const item = draft.sessions[Number(input.dataset.sessionCheck)];
      item.enabled = input.checked;
      markSectionDirty("session");
      renderSession();
      await saveSessions();
    });
  });
  $("sessionList").querySelectorAll("[data-session-remove]").forEach((button) => {
    button.addEventListener("click", async () => {
      draft.sessions.splice(Number(button.dataset.sessionRemove), 1);
      markSectionDirty("session");
      renderSession();
      await saveSessions();
    });
  });
  if (session.config_error) {
    $("sessionMessage").textContent = "配置无效：" + configErrorText(session.config_error) + "；普通用户已拒绝。";
  } else if ($("sessionMessage").textContent.startsWith("配置无效：")) {
    $("sessionMessage").textContent = "";
  }
  renderSessionRepair(session);
}

// 配置错误时给出可点击的修复入口：管理员不必猜“空列表 = 不限制”，
// 也不用先切到原生配置页。读路径与写路径使用同一套条目诊断。
function renderSessionRepair(session) {
  const target = $("sessionRepair");
  if (!target) return;
  const invalid = Array.isArray(session.invalid_sessions) ? session.invalid_sessions : [];
  const reasonText = {
    entry_not_object: "不是对象",
    umo_missing: "缺少 UMO",
    umo_malformed: "UMO 格式不完整（需要 platform:MessageType:session）",
    umo_unknown_type: "会话类型未知",
  };
  const listError = ["sessions_unset", "invalid_session_list", "invalid_session_settings"]
    .includes(session.config_error);
  let html = "";
  if (invalid.length) {
    const items = invalid.slice(0, 5).map((item) =>
      "第 " + (Number(item.index) + 1) + " 条" +
      (item.umo ? "（" + esc(item.umo) + "）" : "") +
      "：" + esc(reasonText[item.reason] || item.reason)
    ).join("；");
    html += '<p class="notice">会话列表有 ' + invalid.length + " 条无法识别的条目：" +
      items + (invalid.length > 5 ? "；…" : "") +
      "。它们当前不会授权任何会话；保存时会被移除。</p>";
  }
  if (listError) {
    const raw = session.sessions_raw_type === "NoneType"
      ? "null（尚未配置）"
      : (session.sessions_raw_type || "未知类型");
    html += '<p class="notice">会话列表的实际值是 ' + esc(raw) +
      "，不是数组，因此普通会话被拒绝。</p>";
  }
  if (invalid.length || listError) {
    html += '<div class="action-row">' +
      '<button type="button" id="clearSessionsUnrestricted" class="secondary btn-compact">清空并保存（不限制）</button>' +
      "</div>";
  }
  target.innerHTML = html;
  const clear = $("clearSessionsUnrestricted");
  if (clear) {
    clear.addEventListener("click", async () => {
      state.drafts.session.sessions = [];
      markSectionDirty("session");
      renderSession();
      await saveSessions();
    });
  }
}

function sessionPayload(preservePolicy = false) {
  captureSessionToggles();
  const draft = state.drafts.session;
  const payload = {
    allow_private: draft.allowPrivate,
    allow_group: draft.allowGroup,
  };
  // 总开关只表达放行策略，不表达会话列表：草稿里的 sessions 可能只是
  // 读路径给出的安全占位（例如 null -> []）。把它一并提交会把“尚未配置”
  // 洗成“明确不限制”，所以开关保存必须省略 sessions 键。
  if (!preservePolicy) payload.sessions = draft.sessions;
  return payload;
}
async function saveSessions(preservePolicy = false) {
  await startSectionSave(
    "session",
    sessionPayload(preservePolicy),
    "sessionMessage",
    "保存会话配置…",
    "会话配置已保存。",
  );
}
async function addSession() {
  const umo = $("newUmo").value.trim();
  if (!umo) return;
  // 与宿主 core/umo_alias.parse_umo 和后端 session_entry_error 同一口径：
  // 按 ":" 切成三段，第三段起都是完整 session_id（可再含冒号，
  // qq:FriendMessage::b 的 ":b"、qq:FriendMessage:a:b 的 "a:b"）。
  // 注意 JS 的 split(":", 2) 只保留 2 个元素，不能当 Python 的 maxsplit 用。
  const parts = umo.split(":");
  const rawType = (parts[1] || "").toLowerCase();
  const type = rawType.includes("group") ? "group" : rawType.includes("friend") || rawType.includes("private") ? "private" : "unknown";
  const sessionId = parts.length >= 3 ? parts.slice(2).join(":") : "";
  if (
    parts.length < 3 ||
    !(parts[0] || "").trim() ||
    !(parts[1] || "").trim() ||
    sessionId === "" ||
    type === "unknown"
  ) {
    msg("sessionMessage", "UMO 必须是 platform:MessageType:session（完整会话 ID 不能为空），且类型可识别。", "error");
    return;
  }
  state.drafts.session.sessions.push({ umo, connector: parts[0], type, enabled: true });
  $("newUmo").value = "";
  markSectionDirty("session");
  renderSession();
  await saveSessions();
}
async function saveAdmins(clearLegacyPending = false) {
  const payload = { admin_users: state.drafts.admins };
  if (clearLegacyPending) payload.clear_legacy_admin_pending = true;
  await startSectionSave(
    "admins",
    payload,
    "adminMessage",
    "保存管理员…",
    "管理员已保存。",
    "保存管理员失败，草稿已保留：",
  );
}
async function addAdmin() {
  const connector = $("adminConnector").value;
  const uid = $("adminUid").value.trim();
  if (!connector || !uid) {
    msg("adminMessage", "请选择连接器并填写发送者 UID。", "error");
    return;
  }
  state.drafts.admins.push({ connector, uid, legacy: false });
  $("adminUid").value = "";
  markSectionDirty("admins");
  renderSession();
  await saveAdmins();
}

function renderAdvanced() {
  if (!state.data || !state.drafts.advanced) return;
  const draft = state.drafts.advanced;
  $("presetGames").value = draft.games;
  $("showStats").checked = draft.showStats;
}
function advancedPayload() {
  captureAdvancedDraft();
  const draft = state.drafts.advanced;
  return {
    preset_games: draft.games.split("\n").map((item) => item.trim()).filter(Boolean),
    show_generation_stats: draft.showStats,
  };
}
async function saveAdvanced() {
  await startSectionSave(
    "advanced",
    advancedPayload(),
    "advancedMessage",
    "保存高级配置…",
    "高级配置已保存。",
  );
}

async function testCurrentModel() {
  captureModelDraft();
  const submittedVersion = state.versions.model;
  const submittedEpoch = state.dataEpoch;
  const token = ++state.asyncTokens.currentTest;
  const draft = state.drafts.model;
  const target = draft.source === "astrbot"
    ? ("AstrBot Provider " + (draft.providerId || "未选择 AstrBot 模型"))
    : ("手动模型 " + (draft.model || "未填写") + " @ " + (draft.baseUrl || "未填写"));
  const unsaved = dirtySections().includes("model");
  $("sumModelTest").textContent = "目标：" + target + (unsaved ? "（使用当前未保存表单，不会保存配置）" : "") + "；正在测试…";
  try {
    const data = ensureOk(await bridge.apiPost("config-center/model/test", currentTestPayload()));
    if (token !== state.asyncTokens.currentTest || submittedEpoch !== state.dataEpoch) return;
    let text = "✅ " + (data.message || "模型测试成功");
    if (submittedVersion !== state.versions.model) {
      text += "（测试针对点击时的表单；当前有更新的未保存修改）";
    }
    $("sumModelTest").textContent = text;
  } catch (error) {
    if (token !== state.asyncTokens.currentTest || submittedEpoch !== state.dataEpoch) return;
    $("sumModelTest").textContent = "❌ " + error.message;
  }
}

function bind() {
  document.querySelectorAll(".nav button").forEach((button) => {
    button.addEventListener("click", () => switchPage(button.dataset.page));
  });
  $("periodSeg").addEventListener("click", (event) => {
    const button = event.target.closest("[data-period]");
    if (!button) return;
    state.period = button.dataset.period;
    $("periodSeg").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    loadStats();
  });
  $("metricSeg").addEventListener("click", (event) => {
    const button = event.target.closest("[data-metric]");
    if (!button) return;
    state.metric = button.dataset.metric;
    state.chartHover = -1;
    hideChartTip();
    $("metricSeg").querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button));
    if (state.stats) drawChart(state.stats);
  });
  const trend = $("trendChart");
  const chartIndexAt = (event) => {
    const points = state.chartPoints || [];
    if (!points.length) return -1;
    const rect = trend.getBoundingClientRect();
    const x = event.clientX - rect.left;
    let nearest = 0;
    points.forEach((point, index) => {
      if (Math.abs(point.x - x) < Math.abs(points[nearest].x - x)) nearest = index;
    });
    return Math.abs(points[nearest].x - x) <= 28 ? nearest : -1;
  };
  trend.addEventListener("pointermove", (event) => {
    const index = chartIndexAt(event);
    if (index === state.chartHover) {
      if (index >= 0) showChartTip(index);
      return;
    }
    state.chartHover = index;
    if (state.stats) drawChart(state.stats);
    if (index >= 0) showChartTip(index);
    else hideChartTip();
  });
  trend.addEventListener("pointerleave", () => {
    if (state.chartHover === -1) return;
    state.chartHover = -1;
    hideChartTip();
    if (state.stats) drawChart(state.stats);
  });
  const resetChatDraft = () => {
    state.chatDraftAll = state.selectedChats === null;
    if (state.chatDraftAll) {
      state.chatDraft = new Set((state.stats?.chats || []).map((chat) => chat.key));
    } else {
      state.chatDraft = new Set(state.selectedChats);
    }
  };
  const setChatOpen = (open) => {
    if (!open) resetChatDraft();
    $("chatPanel").classList.toggle("hidden", !open);
    $("chatToggle").setAttribute("aria-expanded", String(open));
  };
  $("chatToggle").addEventListener("click", () => {
    const open = $("chatPanel").classList.contains("hidden");
    setChatOpen(open);
    if (open) {
      resetChatDraft();
      state.chatSearch = "";
      $("chatSearch").value = "";
      if (state.stats) renderChats(state.stats.chats || []);
      $("chatSearch").focus();
    }
  });
  document.addEventListener("click", (event) => {
    if (!$("chatPanel").classList.contains("hidden") && !event.target.closest(".chat-filter")) setChatOpen(false);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("chatPanel").classList.contains("hidden")) {
      setChatOpen(false);
      $("chatToggle").focus();
    }
  });
  $("chatSearch").addEventListener("input", () => {
    state.chatSearch = $("chatSearch").value;
    if (state.stats) renderChats(state.stats.chats || []);
  });
  $("chatAll").addEventListener("click", () => {
    // 只填草稿；“全部聊天”要等应用后才成为 null（含以后新增的聊天）。
    // 搜索激活时只作用于当前筛选结果，无搜索时作用于全部聊天。
    // 没有聊天时草稿同样是空，必须用独立标记区分“全部”和“未选择”。
    const chats = state.stats?.chats || [];
    const filtered = filteredChats(chats);
    // 搜索无命中时，全选/全不选没有可作用的对象；保持草稿不变，避免
    // 应用后把“全部聊天（含以后新增）”悄悄变成空/显式集合。
    if (state.chatSearch.trim() && !filtered.length) return;
    if (!state.chatSearch.trim()) {
      state.chatDraftAll = true;
      state.chatDraft = new Set(chats.map((chat) => chat.key));
    } else {
      state.chatDraftAll = false;
      filtered.forEach((chat) => state.chatDraft.add(chat.key));
    }
    if (state.stats) renderChats(chats);
  });
  $("chatNone").addEventListener("click", () => {
    const chats = state.stats?.chats || [];
    const filtered = filteredChats(chats);
    if (state.chatSearch.trim() && !filtered.length) return;
    state.chatDraftAll = false;
    if (!state.chatSearch.trim()) {
      state.chatDraft = new Set();
    } else {
      filtered.forEach((chat) => state.chatDraft.delete(chat.key));
    }
    if (state.stats) renderChats(chats);
  });
  $("chatApply").addEventListener("click", () => {
    const draft = Array.from(state.chatDraft);
    if (state.chatDraftAll) {
      state.selectedChats = null;
    } else if (!draft.length) {
      state.selectedChats = [];
    } else {
      state.selectedChats = draft;
    }
    const label = state.selectedChats === null
      ? "全部聊天"
      : state.selectedChats.length ? state.selectedChats.length + " 个聊天" : "未选择聊天";
    $("chatToggleLabel").textContent = label;
    const hint = $("chatSelectionHint");
    if (hint) hint.textContent = label;
    setChatOpen(false);
    loadStats();
  });
  $("refreshAll").addEventListener("click", async () => {
    await reloadConfiguration("刷新全部配置");
  });
  $("confirmOk").addEventListener("click", () => resolveConfirm(true));
  $("confirmCancel").addEventListener("click", () => resolveConfirm(false));
  $("confirmLayer").addEventListener("click", (event) => {
    if (event.target === $("confirmLayer")) resolveConfirm(false);
  });
  document.querySelectorAll("[data-status-go]").forEach((button) => {
    button.addEventListener("click", () => switchPage(button.dataset.statusGo));
  });
  $("sourceAstrbot").addEventListener("click", () => {
    captureModelDraft();
    state.drafts.model.source = "astrbot";
    state.source = "astrbot";
    markSectionDirty("model");
    renderModel();
    // 来源切换与随后的表单编辑合并成一次写请求。
    scheduleSectionSave("model", 300);
  });
  $("sourceManual").addEventListener("click", () => {
    captureModelDraft();
    state.drafts.model.source = "manual";
    state.source = "manual";
    markSectionDirty("model");
    renderModel();
    // 来源切换与随后的表单编辑合并成一次写请求。
    scheduleSectionSave("model", 300);
  });
  $("refreshProviders").addEventListener("click", async () => {
    await reloadConfiguration("刷新 Provider 列表", "modelMessage");
  });
  $("providerFilter").addEventListener("input", renderProviderOptions);
  $("testModel").addEventListener("click", testModel);
  $("testCurrentModel").addEventListener("click", testCurrentModel);
  $("fetchModels").addEventListener("click", fetchModels);
  $("providerSelect").addEventListener("change", () => {
    captureModelDraft();
    markSectionDirty("model");
    renderProviderSelectionState();
    saveSectionNow("model");
  });
  $("presetSelect").addEventListener("change", () => {
    captureModelDraft();
    const draft = state.drafts.model;
    const preset = (state.data?.model?.presets || []).find((item) => item.id === $("presetSelect").value);
    if (!preset) return;
    draft.preset = $("presetSelect").value;
    draft.baseUrl = preset.base_url || "";
    draft.apiFormat = preset.api_format || "chat_completions";
    $("baseUrl").value = draft.baseUrl;
    $("apiFormat").value = draft.apiFormat;
    $("keyState").textContent = "供应商/地址已切换；旧地址 Key 不会自动复用，请重新输入。";
    markSectionDirty("model");
    saveSectionNow("model");
  });
  $("baseUrl").addEventListener("input", () => {
    captureModelDraft();
    const manual = state.data?.model?.manual || {};
    if (manual.key_configured && $("baseUrl").value.trim().replace(/\/$/, "") !== String(manual.base_url || "").replace(/\/$/, "")) {
      $("keyState").textContent = "地址已变更；旧地址 Key 不会发送到新地址，请重新输入。";
    }
    markSectionDirty("model");
    scheduleSectionSave("model", 800);
  });
  // 全局自动保存：普通输入防抖，失焦/下拉/勾选立即提交；Key 整值在失焦或回车提交。
  const modelChanged = (immediate) => {
    captureModelDraft();
    markSectionDirty("model");
    if (immediate) saveSectionNow("model"); else scheduleSectionSave("model");
  };
  ["apiFormat", "modelInput"].forEach((id) => {
    $(id).addEventListener("input", () => modelChanged(false));
    $(id).addEventListener("change", () => modelChanged(true));
    $(id).addEventListener("blur", () => modelChanged(true));
  });
  // Key 输入只标记草稿，不逐字提交；失焦或回车时整值保存。
  $("apiKey").addEventListener("input", () => {
    captureModelDraft();
    markSectionDirty("model");
  });
  $("apiKey").addEventListener("blur", () => modelChanged(true));
  $("apiKey").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); modelChanged(true); }
  });
  $("sendTemperature").addEventListener("change", () => modelChanged(true));
  $("searchEnabled").addEventListener("change", () => {
    captureSearchDraft();
    // 先标记草稿再渲染：渲染时读取 state.dirty.search 决定显示
    // 「已保存策略」还是「草稿预览」，顺序反了会把草稿当成当前生效。
    markSectionDirty("search");
    renderSearchModes();
    applySearchDisabled();
    saveSectionNow("search");
  });
  const searchChanged = (immediate) => {
    captureSearchDraft();
    updateSearchKeyState();
    markSectionDirty("search");
    if (immediate) saveSectionNow("search"); else scheduleSectionSave("search");
  };
  Object.keys(SEARCH_NUMERIC_FIELDS).forEach((id) => {
    $(id).addEventListener("blur", () => {
      if ($(id).value.trim() !== "") return;
      const draft = state.drafts.search;
      if (draft) $(id).value = draft[SEARCH_NUMERIC_FIELDS[id]];
    });
  });
  ["searchResults", "searchTimeout", "searchPerTimeout", "summaryInput", "summaryChars", "materialMaxChars", "materialMaxItems", "materialSnippetChars", "apiProvider", "searchBase"].forEach((id) => {
    $(id).addEventListener("input", () => searchChanged(false));
    $(id).addEventListener("change", () => searchChanged(true));
    $(id).addEventListener("blur", () => searchChanged(true));
  });
  // Key 只在失焦或回车时整值提交，避免每次按键都把半截 Key 发出去。
  $("searchKey").addEventListener("input", () => {
    captureSearchDraft();
    updateSearchKeyState();
    markSectionDirty("search");
  });
  $("searchKey").addEventListener("blur", () => searchChanged(true));
  $("searchKey").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); searchChanged(true); }
  });
  $("summarizeEnabled").addEventListener("change", () => searchChanged(true));
  $("searchTest").addEventListener("click", testSearch);
  $("linkIdentity").addEventListener("click", linkIdentity);
  $("addAdmin").addEventListener("click", addAdmin);
  $("addSession").addEventListener("click", addSession);
  $("sessionConnectorFilter").addEventListener("change", renderSession);
  $("sessionTypeFilter").addEventListener("change", renderSession);
  const rateChanged = (immediate) => {
    captureRateDraft();
    markSectionDirty("rate");
    if (immediate) saveSectionNow("rate"); else scheduleSectionSave("rate");
  };
  ["windowNumber", "maxNumber", "dailyNumber"].forEach((id) => {
    $(id).addEventListener("input", () => rateChanged(false));
    $(id).addEventListener("change", () => rateChanged(true));
    $(id).addEventListener("blur", () => rateChanged(true));
  });
  $("rateTimezone").addEventListener("change", () => rateChanged(true));
  ["allowPrivate", "allowGroup"].forEach((id) => {
    $(id).addEventListener("change", async () => {
      captureSessionToggles();
      markSectionDirty("session");
      await saveSessions(true);
    });
  });
  const advancedChanged = (immediate) => {
    captureAdvancedDraft();
    markSectionDirty("advanced");
    if (immediate) saveSectionNow("advanced"); else scheduleSectionSave("advanced");
  };
  $("presetGames").addEventListener("input", () => advancedChanged(false));
  $("presetGames").addEventListener("blur", () => advancedChanged(true));
  $("showStats").addEventListener("change", () => advancedChanged(true));
  // 桌面端左右两栏各自滚动。短视口或窗口刚缩放时，浏览器合成层可能
  // 收到滚轮事件却不滚动目标栏；这里只在「这次滚轮没有让目标栏移动」时
  // 按同一增量补滚，浏览器已经滚过就完全不插手，避免叠加或重复滚动。
  // 滚轮落在栏内的输入框、文本域这类内部滚动区时交给它自己处理。
  const nestedScrollerConsumesWheel = (target, container) => {
    let node = target;
    while (node && node !== container) {
      if (node.nodeType === 1) {
        const style = window.getComputedStyle(node);
        if (
          /(auto|scroll)/.test(style.overflowY) &&
          node.scrollHeight > node.clientHeight + 1
        ) {
          return true;
        }
      }
      node = node.parentElement;
    }
    return false;
  };
  const assistWheelScroll = (element) => {
    if (!element) return;
    // 上一次结束时的滚动位置：原生滚动发生在 wheel 派发之前，位置变了就
    // 说明浏览器已经处理过这次滚轮。
    let lastSeen = element.scrollTop;
    element.addEventListener("scroll", () => {
      lastSeen = element.scrollTop;
    }, { passive: true });
    element.addEventListener("wheel", (event) => {
      if (Math.abs(event.deltaY) < 1) return;
      if (nestedScrollerConsumesWheel(event.target, element)) return;
      const maxScroll = element.scrollHeight - element.clientHeight;
      if (maxScroll <= 1) return;
      const current = element.scrollTop;
      const nativeMoved = current !== lastSeen;
      lastSeen = current;
      if (nativeMoved) return;
      const next = Math.min(maxScroll, Math.max(0, current + event.deltaY));
      if (next === current) return;
      element.scrollTop = next;
      lastSeen = next;
    }, { passive: true });
  };
  assistWheelScroll(document.querySelector(".sidebar"));
  assistWheelScroll(document.querySelector(".workspace"));
  syncRange("windowRange", "windowNumber", "maxRow");
  syncRange("maxRange", "maxNumber");
  syncRange("dailyRange", "dailyNumber");
  window.addEventListener("beforeunload", (event) => {
    if (!hasUnsavedChanges() && !pendingMutations()) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function init() {
  await bridge.ready();
  applyTheme();
  bridge.onContext(applyTheme);
  bind();
  try {
    await loadState();
    await loadStats();
  } catch (error) {
    console.error(error);
  }
}
init();