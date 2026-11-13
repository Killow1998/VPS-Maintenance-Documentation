"use strict";

const $ = (id) => document.getElementById(id);
let state = null;
let draft = [];
let saving = false;
let live = false;
let quotaLoading = false;
let quotaError = "";
let resetSelection = null;
let resetBusy = false;
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

function grantName(g) {
  return ({full: "完整重置", fable: "Fable 用量返还", session: "5 小时重置"})[g.kind] || "官方 reset";
}

function grantUsable(g, status) {
  const usage = Object.values(g.percent_used || {});
  if (g.gives_back_weekly && usage.length && usage.every((value) => value === 0)) return false;
  return g.usable_now === true && !g.paused && g.resets_left > 0 && (!g.ends_at || Date.parse(g.ends_at) > Date.now()) && (!g.starts_at || Date.parse(g.starts_at) <= Date.now()) && (g.use_requires_limit === false || status?.at_limit === true);
}

function grantUnavailableReason(g, status) {
  if (grantUsable(g, status)) return "";
  if (!(g.resets_left > 0)) return "次数已用完";
  if (g.ends_at && Date.parse(g.ends_at) <= Date.now()) return "已到期";
  if (g.starts_at && Date.parse(g.starts_at) > Date.now()) return "尚未开始";
  if (g.paused) return "官方已暂停此 reset";
  const usage = Object.values(g.percent_used || {});
  if (g.gives_back_weekly && usage.length && usage.every((value) => value === 0)) return "暂无可返还的 Fable 用量";
  if (g.use_requires_limit !== false && status?.at_limit !== true) return "需达到官方额度限制后才能使用";
  return "官方当前未允许使用";
}

function resetUnavailableReason(a) {
  if (!live) return "演示模式不能兑换 reset";
  if (!a.enabled) return "账号已停用";
  if (a.stale) return "当前为旧快照，请刷新额度后再试";
  if (a.reset_pending) return "上次重置结果未确认，请核对官方状态，勿重复兑换";
  if (a.resets?.status !== "ok") return "官方 reset 信息未取得，请刷新额度";
  if (!a.resets.eligible) return "官方未确认该账号符合 reset 使用条件";
  const grants = a.resets.grants || [];
  if (!grants.length) return "暂无官方 reset";
  if (grants.some((g) => grantUsable(g, a.resets))) return "";
  return grants.map((g) => `${grantName(g)}：${grantUnavailableReason(g, a.resets)}`).join("；");
}

function resetScope(g) {
  return g.kind === "fable" ? "退还 Fable 周用量并补满 5 小时；其他模型周用量不变。" : g.kind === "full" ? "恢复 5 小时和 7 天额度。" : "恢复 5 小时额度，7 天额度不变。";
}

function openReset(accountId) {
  const account = state.accounts.find((a) => a.id === accountId);
  if (!account || resetBusy || !live) return;
  resetSelection = {accountId, operationId: crypto.randomUUID(), grantId: null};
  $("reset-account").textContent = account.name;
  $("reset-message").textContent = "请选择本次使用的 reset。";
  $("reset-options").hidden = false;
  $("reset-cancel").textContent = "取消";
  $("reset-confirm").disabled = true;
  $("reset-options").innerHTML = (account.resets?.grants || []).map((g) => {
    const reason = grantUnavailableReason(g, account.resets);
    return `<label class="grant-choice"><input type="radio" name="reset-grant" value="${esc(g.id)}" ${grantUsable(g, account.resets) ? "" : "disabled"}><span>${esc(grantName(g))} · 剩余 ${g.resets_left} 次<small>${esc(resetScope(g))}</small><small>${g.ends_at ? `到期 ${esc(timestamp(g.ends_at))}（${esc(duration(g.ends_at))}）` : "无到期时间"}</small>${reason ? `<small>不可用：${esc(reason)}</small>` : ""}</span></label>`;
  }).join("");
  $("reset-dialog").showModal();
}

async function confirmReset() {
  if (resetBusy || !resetSelection?.grantId) return;
  resetBusy = true;
  $("reset-confirm").disabled = true;
  $("reset-cancel").disabled = true;
  $("reset-options").querySelectorAll("input").forEach((input) => { input.disabled = true; });
  $("reset-message").textContent = "正在提交官方重置，请勿重复操作…";
  try {
    const result = await request("/api/quota", {action: "redeem", account_id: resetSelection.accountId,
      grant_id: resetSelection.grantId, operation_id: resetSelection.operationId});
    const messages = {reset: "官方重置成功，额度已重新采集。", already_used: "该 reset 已使用，已更新额度。", unknown: "结果未确认。请刷新查看官方状态，勿重复兑换。", rate_limited: "官方请求限流，请稍后查看额度。", auth_error: "官方授权不可用，未完成重置。", not_limited: "官方返回当前未达到该 reset 的使用条件。", cooldown: "官方暂不允许再次重置。", ineligible: "该账号不符合使用条件。", unavailable: "该 reset 当前不可用。"};
    $("reset-message").textContent = messages[result.result] || "操作处理中，请刷新查看额度。";
    const account = result.quota?.accounts?.find((a) => a.id === resetSelection.accountId);
    if (result.result === "reset" && (account?.stale || account?.routing?.blocked)) {
      $("reset-message").textContent += "账号仍有其他额度或调用限制，请查看账号卡片。";
    }
    $("reset-options").hidden = true;
    await refreshQuota();
  } catch (_) {
    $("reset-message").textContent = "操作未确认，请刷新额度并核对 reset 次数，勿重复兑换。";
  } finally {
    resetBusy = false;
    resetSelection = null;
    $("reset-cancel").disabled = false;
    $("reset-cancel").textContent = "关闭";
  }
}

function renderAccounts() {
  const meta = state.quota || {};
  $("quota-caption").textContent = live
    ? `北京时间 UTC+8 · ${meta.checked_at ? `采集于 ${timestamp(meta.checked_at)}` : "尚无采集时间"}`
    : "模拟快照 · 北京时间 UTC+8";
  $("quota-status").textContent = quotaError || (live
    ? `页面每 30 秒更新 · 后台约每 ${Math.round((meta.poll_seconds || 60) / 60)} 分钟采集 · 手动刷新至少间隔 30 秒`
    : "演示数据");
  $("quota-status").classList.toggle("quota-warning", !!quotaError || meta.query_status === "unavailable");
  $("account-cards").innerHTML = state.accounts.map((a) => {
    const windows = Object.entries(a.windows || {});
    const quota = windows.length ? windows.map(([key, value]) => {
      const percent = value.remaining_percent;
      if (percent === null || !Number.isFinite(percent)) return `<div class="account-unknown">额度未知</div>`;
      const kind = a.stale ? "stale" : percent <= 15 ? "critical" : percent <= 40 ? "low" : "";
      const windowName = ({five_hour: "5 小时", seven_day: "7 天", seven_day_sonnet: "Sonnet 7 天", seven_day_opus: "Opus 7 天", seven_day_fable: "Fable 7 天"})[key] || key;
      return `<div class="quota-row"><div class="quota-line"><span>${esc(windowName)}剩余额度${a.stale ? " · 旧值" : ""}</span><strong class="${kind}">${esc(percent)}%</strong></div><progress class="${kind}" max="100" value="${esc(percent)}" aria-label="${esc(a.name + windowName)}剩余额度 ${esc(percent)}%"></progress><div class="reset">${value.resets_at ? `重置 ${esc(timestamp(value.resets_at))}（${esc(duration(value.resets_at))}）` : "重置时间未知"}</div></div>`;
    }).join("") : `<div class="account-unknown">${esc(!a.enabled ? "账号已停用" : a.quota_reason === "observer_unavailable" ? "额度查询服务暂不可用" : "尚无额度数据")}</div>`;
    const reasons = {observer_unavailable: "额度查询服务暂不可用", missing_scope: "额度授权缺少 profile 权限", upstream_http_429: "额度查询被限流", upstream_http_401: "额度授权已失效", upstream_http_403: "额度查询被拒绝", quota_lookup_failed: "额度采集失败", credential_refresh_failed: "额度凭据刷新失败", quota_identity_mismatch: "额度授权账号不匹配", quota_source_missing: "缺少额度授权"};
    const notice = a.stale && windows.length ? "旧快照 · 等待更新" : a.quota_status === "unavailable" ? reasons[a.quota_reason] || "额度采集失败" : "";
    const updated = a.as_of ? `更新 ${timestamp(a.as_of)}` : "";
    const retry = a.retry_at ? ` · 重试 ${timestamp(a.retry_at)}（${duration(a.retry_at)}）` : "";
    const routing = a.routing || {};
    const availability = !a.enabled ? "账号已停用" : a.stale ? "等待额度更新" : Object.values(a.windows || {}).some((w) => w.remaining_percent === 0) ? "部分额度已用完" : routing.blocked ? "调用暂不可用" : "可调用";
    const resetRows = (a.resets?.grants || []).map((g) => `<div class="grant-row"><span>${esc(grantName(g))} · ${g.resets_left}/${g.resets_total} 次</span><small>${g.ends_at ? `到期 ${esc(timestamp(g.ends_at))}（${esc(duration(g.ends_at))}）` : "无到期时间"}</small></div>`).join("");
    const resetReason = resetUnavailableReason(a);
    const resets = `<div class="reset-grants">${a.resets?.status === "ok" ? resetRows || "暂无官方 reset" : "官方 reset 信息未取得"}<button class="button subtle" data-reset-account="${esc(a.id)}" ${resetReason ? `disabled aria-describedby="reset-status-${esc(a.id)}"` : ""}>重置 Claude 额度</button>${resetReason ? `<p class="reset" id="reset-status-${esc(a.id)}">${esc(resetReason)}</p>` : ""}</div>`;
    return `<article class="account-card"><div class="account-title"><div><div class="account-name">${esc(a.name)}</div><div class="account-plan">${esc(a.plan)}${a.route_ready === false ? " · 路由待配置" : ""} · ${esc(availability)}</div></div><span class="status ${a.enabled ? "" : "offline"}"><i></i>${a.enabled ? "启用" : "停用"}</span></div>${quota}${resets}<div class="account-updated ${notice ? "quota-warning" : ""}">${esc([notice, updated].filter(Boolean).join(" · ") + retry)}</div></article>`;
  }).join("");
}

async function refreshQuota(manual = false) {
  if (!state || quotaLoading || document.body.classList.contains("needs-login")) return;
  quotaLoading = true;
  $("quota-refresh").disabled = true;
  $("quota-refresh").textContent = "正在刷新…";
  try {
    const answer = await request("/api/quota", manual ? {action: "refresh"} : undefined);
    const snapshot = manual ? answer.quota : answer;
    const byId = new Map(snapshot.accounts.map((a) => [a.id, a]));
    for (const account of state.accounts) {
      const current = byId.get(account.id);
      if (!current) { account.stale = true; continue; }
      for (const field of ["windows", "stale", "as_of", "quota_status", "quota_reason", "retry_at", "resets", "routing", "reset_pending"]) account[field] = current[field];
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
$("quota-refresh").addEventListener("click", () => refreshQuota(true));
$("account-cards").addEventListener("click", (e) => {
  const button = e.target.closest("[data-reset-account]");
  if (button && !button.disabled) openReset(button.dataset.resetAccount);
});
$("reset-options").addEventListener("change", (e) => {
  if (!resetBusy && resetSelection) { resetSelection.grantId = e.target.value; $("reset-confirm").disabled = false; }
});
$("reset-confirm").addEventListener("click", confirmReset);
$("reset-cancel").addEventListener("click", () => $("reset-dialog").close());
$("reset-dialog").addEventListener("cancel", (e) => { if (resetBusy) e.preventDefault(); });
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
