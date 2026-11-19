"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    elements.set(id, {
      listeners: {}, addEventListener(name, handler) { this.listeners[name] = handler; },
      classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
      dataset: {}, innerHTML: "", textContent: "", disabled: false,
    });
  }
  return elements.get(id);
}

const document = {
  getElementById: element,
  querySelectorAll() { return []; },
  addEventListener() {},
  body: element("body"),
  hidden: false,
};
const file = path.join(__dirname, "web", "app.js");
const original = fs.readFileSync(file, "utf8");
assert.match(original, /\nboot\(\);\s*$/);
const source = original.replace(/\nboot\(\);\s*$/, "\n");
const context = vm.createContext({document, setInterval() {}, setTimeout, structuredClone, assert});
vm.runInContext(source + `
  live = true;
  state = {quota: {}, accounts: [{
    id: "a", name: "A", plan: "Claude", enabled: true, stale: true,
    quota_status: "unavailable", quota_reason: "quota_backoff",
    retry_at: new Date(Date.now() + 60000).toISOString(),
    windows: {seven_day_opus: {remaining_percent: null, resets_at: null}},
    resets: {status: "unknown"}, routing: {},
  }]};
  renderAccounts();
  assert.equal(document.getElementById("quota-refresh").disabled, true);
  assert.match(document.getElementById("account-cards").innerHTML, /Opus 7 天额度未知/);
  assert.match(document.getElementById("account-cards").innerHTML, /额度查询等待重试/);
  state.accounts.push({id: "b", name: "B", plan: "Claude", enabled: true, stale: false,
    windows: {}, resets: {status: "unknown"}, routing: {}});
  renderAccounts();
  assert.equal(document.getElementById("quota-refresh").disabled, false);
  state.accounts[1].quota_reason = "upstream_http_429";
  state.accounts[1].retry_at = new Date(Date.now() + 30000).toISOString();
  updateQuotaRefreshButton();
  assert.equal(document.getElementById("quota-refresh").disabled, true);
  assert.match(document.getElementById("quota-refresh").textContent, /还需 (?:2[0-9]|30) 秒/);
  state.accounts.pop();
  state.accounts[0].quota_reason = "other";
  state.accounts[0].retry_at = null;
  state.accounts[0].stale = false;
  renderAccounts();
  assert.equal(document.getElementById("quota-refresh").disabled, false);
  assert.match(document.getElementById("account-cards").innerHTML, /部分额度未知/);
`, context, {filename: file, timeout: 5000});
vm.runInContext(`(async () => {
  state.clients = [];
  state.revision = 1;
  draft = [];
  state.accounts[0].quota_reason = 'quota_backoff';
  state.accounts[0].retry_at = new Date(Date.now() + 60000).toISOString();
  state.accounts.push({id:'b', name:'B', plan:'Claude', enabled:false, stale:true,
    windows:{}, resets:{status:'unknown'}, routing:{}});
  const updated = [{...state.accounts[0]}, {...state.accounts[1], enabled:true, stale:false},
    {id:'c', name:'C', plan:'Claude', enabled:true, stale:false, windows:{}, resets:{}, routing:{}}];
  request = async (path) => path === '/api/state'
    ? {accounts:structuredClone(updated), clients:[], revision:2, mode:'live', enforcement:'enforced', quota:{}, audit:[]}
    : {accounts:updated, query_status:'partial'};
  renderMetrics = renderMatrix = renderSelects = render = () => {};
  await refreshQuota(false);
  assert.equal(state.accounts.find((a) => a.id === 'b').enabled, true);
  assert.equal(state.accounts.some((a) => a.id === 'c'), true);
  assert.equal(state.revision, 2);
  assert.equal(document.getElementById('quota-refresh').disabled, false);
  state.accounts.push({id:'removed', name:'Removed', plan:'Claude', enabled:false, route_ready:false, windows:{}});
  request = async (path) => path === '/api/state'
    ? {accounts:structuredClone(updated), clients:[], revision:3, mode:'live', enforcement:'enforced', quota:{}, audit:[]}
    : {accounts:updated, query_status:'partial'};
  await refreshQuota(false);
  assert.equal(state.accounts.some((a) => a.id === 'removed'), false);
  assert.equal(state.revision, 3);
  state.clients = [{id:'k', enabled:true, account_ids:[]}];
  draft = [{id:'k', enabled:true, account_ids:['a']}];
  const another = [...updated, {id:'d', name:'D', plan:'Claude', enabled:true, windows:{}, resets:{}, routing:{}}];
  request = async () => ({accounts:another, query_status:'partial'});
  await refreshQuota(false);
  assert.equal(state.revision, 3);
  assert.equal(draft[0].account_ids[0], 'a');
  assert.equal(document.getElementById('save').disabled, true);
  let resolveState;
  let stateRequests = 0;
  request = async (path) => {
    if (path === '/api/state') {
      stateRequests++;
      return new Promise((resolve) => { resolveState = resolve; });
    }
    return {accounts:another, query_status:'partial'};
  };
  document.getElementById('discard').listeners.click();
  await Promise.resolve();
  assert.equal(document.getElementById('matrix').inert, true);
  const input = {dataset:{client:'k', account:'a'}, checked:true,
    matches:() => true, hasAttribute:() => false,
    closest:() => ({classList:{toggle() {}}, querySelector:() => ({})})};
  document.getElementById('matrix').listeners.change({target:input});
  assert.equal(draft[0].account_ids.length, 0);
  resolveState({accounts:structuredClone(another), clients:[{id:'k', enabled:true, account_ids:[]}],
    revision:4, mode:'live', enforcement:'enforced', quota:{}, audit:[]});
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(state.revision, 4);
  assert.equal(draft[0].account_ids.length, 0);
  assert.equal(document.getElementById('matrix').inert, false);
  state.accounts.push({id:'removed-again', name:'Removed', enabled:false, route_ready:false});
  let attempts = 0;
  request = async (path) => {
    if (path === '/api/state') {
      stateRequests++;
      if (++attempts === 1) throw new Error('temporary');
      return {accounts:another, clients:state.clients, revision:5, mode:'live', enforcement:'enforced', quota:{}, audit:[]};
    }
    return {accounts:another, query_status:'partial'};
  };
  await refreshQuota(false);
  assert.equal(state.revision, 4);
  await refreshQuota(false);
  assert.equal(state.revision, 5);
  assert.equal(attempts, 2);

  inventoryConflict = false;
  state.clients = [{id:'k', enabled:true, account_ids:[]}];
  draft = [{id:'k', enabled:true, account_ids:['a']}];
  let resolveSave;
  request = async (path) => {
    assert.equal(path, '/api/policies');
    return new Promise((resolve) => { resolveSave = resolve; });
  };
  const savingRequest = document.getElementById('save').listeners.click();
  await Promise.resolve();
  assert.equal(document.getElementById('matrix').inert, true);
  input.dataset.account = 'b';
  document.getElementById('matrix').listeners.change({target:input});
  assert.equal(draft[0].account_ids.includes('b'), false);
  resolveSave({...state, clients:structuredClone(draft), revision:6});
  await savingRequest;
  assert.equal(document.getElementById('matrix').inert, false);

  live = false;
  quotaRefreshAfter = 0;
  let previewRequest;
  request = async (path, data) => { previewRequest = {path, data}; return {accounts:state.accounts}; };
  state.accounts.forEach((a) => { a.retry_at = null; a.stale = false; });
  await refreshQuota(true);
  assert.equal(previewRequest.path, '/api/quota');
  assert.equal(previewRequest.data, undefined);
})()`, context, {filename: file, timeout: 5000}).then(() => {
  console.log("web quota status: ok");
}).catch((error) => { console.error(error); process.exitCode = 1; });
