"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let draft = [];
let saving = false;
let live = false;
let quotaLoading = false;
let quotaError = "";
const esc = (value) => String(value).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const compact = (number) => new Intl.NumberFormat("en-US", {notation: "compact", maximumFractionDigits: 1}).format(number);
const normalized = (rows) => rows.map(({id, enabled, account_ids}) => ({id, enabled, account_ids: [...account_ids].sort()}));
const dirty = () => state && JSON.stringify(normalized(draft)) !== JSON.stringify(normalized(state.clients));
const changedRows = () => draft.filter((row, i) => JSON.stringify(normalized([row])) !== JSON.stringify(normalized([state.clients[i]]))).length;

async function request(path, body) {
  const response = await fetch(new URL(path.replace(/^\//, ""), window.location.href), {
    method: body === undefined ? "GET" : "POST",
    cache: "no-store",
    headers: body === undefined ? {} : {"Content-Type": "application/json"},
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (response.status === 401 && live) showLogin();
  if (!response.ok) throw new Error(data.error?.message || data.error || "读取失败，请重试。");
  return data;
}

function error(message = "") {
  $("error").textContent = message;
  $("error").classList.toggle("hidden", !message);
}

function renderMetrics() {
  const cells = [
    ["API Key", state.clients.length],
    ["可用账号", `${state.accounts.filter((a) => a.enabled).length} / ${state.accounts.length}`],
    ["已授权关系", draft.reduce((n, c) => n + c.account_ids.length, 0)],
    ["待保存 Key", changedRows()],
  ];
  $("metrics").innerHTML = cells.map(([label, value]) => `<div class="metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
}

function renderStatus() {
  const changed = dirty();
  $("save").disabled = saving || !changed;
  $("discard").disabled = saving || !changed;
  $("save").textContent = saving ? "正在保存…" : live ? "保存权限" : "保存本地策略";
  $("save-status").classList.toggle("dirty-label", changed);
  $("save-status").textContent = changed
    ? `${changedRows()} 个 Key 有未保存修改 · 尚未生效`
    : live ? `策略 v${state.revision} · ${state.enforcement === "enforced" ? "新请求按此版本执行" : "尚未切换线上流量"}` : `本地策略 v${state.revision} 已保存 · 尚未应用到 VPS`;
}

function switchHTML(checked, client, account) {
  const label = account ? `${client.name} 调用${account.name}` : `启用 ${client.name}`;
  return `<input class="switch" type="checkbox" role="switch" aria-label="${esc(label)}" data-client="${esc(client.id)}" ${account ? `data-account="${esc(account.id)}"` : "data-enabled"} ${checked ? "checked" : ""}>`;
}

function renderMatrix() {
  const query = $("search").value.toLowerCase().trim();
  $("matrix").querySelector("thead").innerHTML = `<tr><th>API Key</th><th>Key 启用</th>${state.accounts.map((a) => `<th>${esc(a.name)}<small>${esc(a.plan)}${a.enabled ? "" : " · 当前不可用"}</small></th>`).join("")}<th>获准账号</th></tr>`;
  const rows = draft.filter((c) => c.name.toLowerCase().includes(query));
  $("matrix").querySelector("tbody").innerHTML = rows.length ? rows.map((c) => `<tr class="${c.enabled ? "" : "disabled-row"}" data-client-id="${esc(c.id)}"><td><div class="key-name">${esc(c.name)}</div><div class="key-meta">${c.usage?.requests == null ? "1h 用量暂不可用" : `1h · ${esc(c.usage.requests)} 条记录 · ${c.usage.tokens == null ? "未知" : esc(compact(c.usage.tokens))} tokens`}</div></td><td>${switchHTML(c.enabled, c)}</td>${state.accounts.map((a) => `<td>${switchHTML(c.account_ids.includes(a.id), c, a)}</td>`).join("")}<td class="count">${c.account_ids.length} / ${state.accounts.length}</td></tr>`).join("") : `<tr><td colspan="${state.accounts.length + 3}" class="empty">没有匹配的 Key。</td></tr>`;
}

function duration(end) {
  let minutes = Math.max(0, Math.ceil((Date.parse(end) - Date.now()) / 60000));
  const days = Math.floor(minutes / 1440);
  minutes %= 1440;
  const hours = Math.floor(minutes / 60);
  minutes %= 60;
  if (days) return `${days}d${hours}h left`;
  if (hours) return `${hours}h${minutes}m left`;
  return `${minutes}m left`;
}

function timestamp(value) {
  if (!value || !Number.isFinite(Date.parse(value))) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false}).format(new Date(value));
}

function renderAccounts() {
  const meta = state.quota || {};
  $("quota-caption").textContent = live
    ? `北京时间 UTC+8 · ${meta.checked_at ? `采集于 ${timestamp(meta.checked_at)}` : "尚无采集时间"}`
    : "模拟快照 · 北京时间 UTC+8";
  $("quota-status").textContent = quotaError || (live
    ? `页面每 30 秒刷新 · 额度每 ${Math.round((meta.poll_seconds || 300) / 60)} 分钟采集 · 百分比为服务商窗口剩余额度`
    : "演示数据");
  $("quota-status").classList.toggle("quota-warning", !!quotaError || meta.query_status === "unavailable");
  $("account-cards").innerHTML = state.accounts.map((a) => {
    const windows = Object.entries(a.windows || {});
    const quota = windows.length ? windows.map(([key, value]) => {
      const percent = value.remaining_percent;
      if (percent === null || !Number.isFinite(percent)) return `<div class="account-unknown">额度未知</div>`;
      const kind = a.stale ? "stale" : percent <= 15 ? "critical" : percent <= 40 ? "low" : "";
      const windowName = ({five_hour: "5 小时", seven_day: "7 天", seven_day_sonnet: "Sonnet 7 天", seven_day_opus: "Opus 7 天"})[key] || key;
      return `<div class="quota-row"><div class="quota-line"><span>${esc(windowName)}剩余额度${a.stale ? " · 旧值" : ""}</span><strong class="${kind}">${esc(percent)}%</strong></div><progress class="${kind}" max="100" value="${esc(percent)}" aria-label="${esc(a.name + windowName)}剩余额度 ${esc(percent)}%"></progress><div class="reset">${value.resets_at ? `重置 ${esc(timestamp(value.resets_at))}（${esc(duration(value.resets_at))}）` : "重置时间未知"}</div></div>`;
    }).join("") : `<div class="account-unknown">${esc(!a.enabled ? "账号已停用" : a.quota_reason === "observer_unavailable" ? "额度查询服务暂不可用" : "尚无额度数据")}</div>`;
    const reasons = {observer_unavailable: "额度查询服务暂不可用", missing_scope: "额度授权缺少 profile 权限", upstream_http_429: "额度查询被限流", upstream_http_401: "额度授权已失效", upstream_http_403: "额度查询被拒绝", quota_lookup_failed: "额度采集失败", credential_refresh_failed: "额度凭据刷新失败", quota_identity_mismatch: "额度授权账号不匹配", quota_source_missing: "缺少额度授权"};
    const notice = a.stale && windows.length ? "旧快照 · 等待更新" : a.quota_status === "unavailable" ? reasons[a.quota_reason] || "额度采集失败" : "";
    const updated = a.as_of ? `更新 ${timestamp(a.as_of)}` : "";
    const retry = a.retry_at ? ` · 重试 ${timestamp(a.retry_at)}（${duration(a.retry_at)}）` : "";
    return `<article class="account-card"><div class="account-title"><div><div class="account-name">${esc(a.name)}</div><div class="account-plan">${esc(a.plan)}${a.route_ready === false ? " · 路由待配置" : ""}</div></div><span class="status ${a.enabled ? "" : "offline"}"><i></i>${a.enabled ? "启用" : "停用"}</span></div>${quota}<div class="account-updated ${notice ? "quota-warning" : ""}">${esc([notice, updated].filter(Boolean).join(" · ") + retry)}</div></article>`;
  }).join("");
}

async function refreshQuota() {
  if (!state || quotaLoading || document.body.classList.contains("needs-login")) return;
  quotaLoading = true;
  $("quota-refresh").disabled = true;
  $("quota-refresh").textContent = "正在刷新…";
  try {
    const snapshot = await request("/api/quota");
    const byId = new Map(snapshot.accounts.map((a) => [a.id, a]));
    for (const account of state.accounts) {
      const current = byId.get(account.id);
      if (!current) { account.stale = true; continue; }
      for (const field of ["windows", "stale", "as_of", "quota_status", "quota_reason", "retry_at"]) account[field] = current[field];
    }
    const {accounts, ...meta} = snapshot;
    state.quota = meta;
    quotaError = meta.query_status === "unavailable" ? "额度查询服务暂不可用；账号启用状态不受此查询结果影响。" : "";
  } catch (_) {
    quotaError = "刷新失败，保留上次额度快照。";
    state.accounts.forEach((a) => { a.stale = true; });
  } finally {
    quotaLoading = false;
    $("quota-refresh").disabled = false;
    $("quota-refresh").textContent = "刷新额度";
    renderAccounts();
  }
}

function renderAudit() {
  $("audit-list").innerHTML = state.audit.length ? state.audit.map((a) => `<div class="audit-row"><span class="audit-time">${esc(timestamp(a.timestamp))}</span><span>${esc(a.summary)}</span><span class="audit-revision">v${a.revision}</span></div>`).join("") : `<div class="empty">暂无修改记录。保存策略后，这里会记录管理员的修改。</div>`;
}

function renderSelects() {
  const previousClient = $("check-client").value;
  const previousAccount = $("check-account").value;
  $("check-client").innerHTML = state.clients.map((c) => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join("");
  $("check-account").innerHTML = state.accounts.map((a) => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join("");
  if (state.clients.some((c) => c.id === previousClient)) $("check-client").value = previousClient;
  if (state.accounts.some((a) => a.id === previousAccount)) $("check-account").value = previousAccount;
}

function render() {
  renderMetrics(); renderMatrix(); renderStatus(); renderAccounts(); renderAudit(); renderSelects();
}

async function load(preserve = false) {
  error();
  $("refresh").disabled = true;
  try {
    const data = await request("/api/state");
    const keepDraft = preserve && dirty();
    if (keepDraft && data.revision !== state.revision) throw new Error("策略已在另一窗口更新。先撤销本页修改，再刷新快照。");
    state = data;
    live = state.mode === "live";
    document.body.classList.remove("needs-login");
    $("login-panel").classList.add("hidden");
    $("logout").classList.toggle("hidden", !live);
    $("mode-badge").textContent = live ? "管理员 · VPS" : "本地原型 · 未连接 VPS";
    $("mode-notice").textContent = live ? "网页与 TUI 共用权限配置。保存后约束新请求；已发出的请求和历史用量保留。新 Key 默认没有账号权限。" : "当前展示模拟账号、用量和额度。保存只更新本机策略，尚未改变线上 CPA 的调用权限。";
    quotaError = state.quota?.query_status === "unavailable" ? "额度查询服务暂不可用；账号启用状态不受此查询结果影响。" : "";
    if (!keepDraft) draft = structuredClone(state.clients);
    render();
    document.body.dataset.ready = "true";
  } catch (e) { error(e.message); }
  finally { $("refresh").disabled = false; }
}

$("matrix").addEventListener("change", (event) => {
  const input = event.target;
  if (!input.matches("input[data-client]")) return;
  const row = draft.find((c) => c.id === input.dataset.client);
  if (input.hasAttribute("data-enabled")) row.enabled = input.checked;
  else {
    const selected = new Set(row.account_ids);
    input.checked ? selected.add(input.dataset.account) : selected.delete(input.dataset.account);
    row.account_ids = [...selected].sort();
  }
  const tr = input.closest("tr");
  tr.classList.toggle("disabled-row", !row.enabled);
  tr.querySelector(".count").textContent = `${row.account_ids.length} / ${state.accounts.length}`;
  error(); renderMetrics(); renderStatus();
});

$("search").addEventListener("input", renderMatrix);
$("refresh").addEventListener("click", () => load(true));
$("quota-refresh").addEventListener("click", refreshQuota);
$("discard").addEventListener("click", () => {
  draft = structuredClone(state.clients);
  error(); renderMatrix(); renderMetrics(); renderStatus();
});
$("save").addEventListener("click", async () => {
  if (saving || !dirty()) return;
  saving = true; error(); renderStatus();
  try {
    state = await request("/api/policies", {expected_revision: state.revision, clients: normalized(draft)});
    draft = structuredClone(state.clients);
    $("check-result").textContent = live ? "策略已更新，请重新检查。" : "策略已更新，请重新检查。线上 CPA 尚未连接。";
    $("check-result").className = "check-result";
    render();
  } catch (e) { error(e.message); }
  finally { saving = false; renderStatus(); }
});
$("check").addEventListener("click", async () => {
  $("check").disabled = true; error();
  try {
    const result = await request("/api/check", {client_id: $("check-client").value, account_id: $("check-account").value});
    $("check-result").textContent = `${result.allowed ? "允许" : "拒绝"} · ${result.reason}（v${result.revision}${dirty() ? "，不含未保存修改" : ""}）`;
    $("check-result").className = `check-result ${result.allowed ? "allowed" : "denied"}`;
  } catch (e) { error(e.message); }
  finally { $("check").disabled = false; }
});
for (const select of [$("check-client"), $("check-account")]) select.addEventListener("change", () => {
  $("check-result").textContent = "选择已更改，请重新检查。";
  $("check-result").className = "check-result";
});
document.querySelectorAll(".nav-link").forEach((link) => link.addEventListener("click", () => {
  document.querySelectorAll(".nav-link").forEach((a) => a.classList.toggle("active", a === link));
}));
setInterval(() => { if (!document.hidden) refreshQuota(); }, 30000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshQuota(); });
function showLogin() {
  document.body.classList.add("needs-login");
  $("login-panel").classList.remove("hidden");
}
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-submit").disabled = true;
  try {
    await request("/api/login", {username: $("username").value, password: $("password").value});
    $("password").value = "";
    $("login-error").textContent = "";
    await load();
  } catch (e) { $("login-error").textContent = e.message; }
  finally { $("login-submit").disabled = false; }
});
$("logout").addEventListener("click", async () => {
  try { await request("/api/logout", {}); showLogin(); }
  catch (e) { error(e.message); }
});
async function boot() {
  try { live = (await request("/api/mode")).mode === "live"; } catch (_) { /* Local preview has no login. */ }
  const ticket = new URLSearchParams(window.location.hash.slice(1)).get("ticket");
  if (ticket && live) {
    history.replaceState(null, "", window.location.pathname);
    try { await request("/api/login", {ticket}); }
    catch (e) { $("login-error").textContent = e.message; }
  }
  await load();
}
boot();
