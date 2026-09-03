let cursor = 0;

const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));

function metric(label, value, tone = "") {
  return `<article class="metric ${tone}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`;
}

let authenticated = false;

const isPhoneClient = () => /(iphone|android|mobile)/i.test(navigator.userAgent);

function formatTimestamp(value) {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "N/A" : timestamp.toLocaleString();
}

function controls(task) {
  if (!authenticated) return '<a class="login-link" href="/login">登录后控制</a>';
  const action = ["running", "queued", "verifying"].includes(task.state) ? "pause" : ["paused", "inbox", "ready", "needs_fix"].includes(task.state) ? "resume" : null;
  const lifecycle = action ? `<button class="small" data-task="${escapeHtml(task.task_id)}" data-action="${action}" data-revision="${escapeHtml(task.state_revision)}">${action === "pause" ? "暂停" : "继续"}</button>` : "";
  const priority = !["accepted", "cancelled"].includes(task.state)
    ? `<button class="small quiet" data-task="${escapeHtml(task.task_id)}" data-action="set_priority" data-priority="${Math.max(-10, task.priority - 1)}" data-revision="${escapeHtml(task.state_revision)}">优先级−</button><button class="small quiet" data-task="${escapeHtml(task.task_id)}" data-action="set_priority" data-priority="${Math.min(10, task.priority + 1)}" data-revision="${escapeHtml(task.state_revision)}">优先级＋</button>`
    : "";
  return lifecycle + priority;
}

function renderTask(task) {
  const activeNode = task.nodes.find((node) => ["running", "blocked", "indeterminate"].includes(node.state));
  const nextNode = task.nodes.find((node) => node.state === "pending");
  const phase = activeNode ? `${activeNode.node_id} · ${activeNode.state}` : task.state;
  const nextAction = task.blocker || (nextNode ? `下一节点：${nextNode.node_id}` : task.verdict || "等待状态推进");
  const steering = task.steering?.length ? task.steering.at(-1).instruction : "无追加指令";
  const nodes = task.nodes.map((node) => {
    const executor = node.effective_executor || node.executor;
    const model = node.result?.actual_model || node.effective_model || node.model;
    return `<span class="node ${escapeHtml(node.state)}">${escapeHtml(node.node_id)} · ${escapeHtml(executor)} / ${escapeHtml(model)} · ${escapeHtml(node.state)}</span>`;
  }).join("");
  const artifacts = task.nodes.flatMap((node) => Object.entries(node.result?.artifacts || {}).map(([name, ref]) => {
    const label = `${node.node_id} · ${name}`;
    return authenticated
      ? `<a class="artifact" href="/api/artifacts/${encodeURIComponent(ref)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>`
      : `<span class="artifact disabled">${escapeHtml(label)}</span>`;
  })).join("");
  const steer = authenticated && !["accepted", "cancelled"].includes(task.state)
    ? `<div class="steering"><input maxlength="500" data-steer-input="${escapeHtml(task.task_id)}" placeholder="给后续 attempt 补充一句指令（不扩大 scope）"><button class="small" data-steer-task="${escapeHtml(task.task_id)}" data-revision="${escapeHtml(task.state_revision)}">发送</button></div>`
    : "";
  const contract = task.contract || {};
  const contractDetails = `<details class="task-contract"><summary>查看任务契约、DAG 边界与验收条件</summary><dl><div><dt>仓库 / 基线</dt><dd>${escapeHtml(contract.repository || "N/A")} · ${escapeHtml(contract.base_sha || "N/A")}</dd></div><div><dt>允许范围</dt><dd>${escapeHtml((contract.allowed_scope || []).join(", ") || "N/A")}</dd></div><div><dt>禁止范围</dt><dd>${escapeHtml((contract.forbidden_scope || []).join(", ") || "无")}</dd></div><div><dt>依赖</dt><dd>${escapeHtml((contract.dependencies || []).join(", ") || "无")}</dd></div><div><dt>验收命令</dt><dd>${escapeHtml((contract.acceptance_commands || []).join(" · ") || "N/A")}</dd></div><div><dt>路由 / 权重</dt><dd>${escapeHtml(contract.task_type || "N/A")} · ${escapeHtml(contract.complexity || "N/A")} · ${escapeHtml(contract.task_points ?? 1)} 点</dd></div><div><dt>重试 / 超时</dt><dd>${escapeHtml(contract.retry_limit ?? "N/A")} 轮 · ${escapeHtml(contract.timeout_seconds ?? "N/A")} 秒</dd></div></dl></details>`;
  return `<article class="task" data-task-card="${escapeHtml(task.task_id)}"><div class="task-head"><div class="task-identity"><h3>${escapeHtml(task.task_id)}</h3><p class="task-objective" data-task-objective="${escapeHtml(task.task_id)}">${escapeHtml(task.contract.objective)}</p></div><div class="task-actions"><span class="pill ${escapeHtml(task.state)}" data-task-state="${escapeHtml(task.task_id)}">${escapeHtml(task.state)}</span>${controls(task)}</div></div><div class="task-brief"><span>阶段 <strong>${escapeHtml(phase)}</strong></span><span>下一步 <strong>${escapeHtml(nextAction)}</strong></span><span>优先级 / 更新 <strong data-task-updated="${escapeHtml(task.task_id)}" data-updated-at="${escapeHtml(task.updated_at)}">${escapeHtml(task.priority)} · ${escapeHtml(formatTimestamp(task.updated_at))}</strong></span><span>最新短指令 <strong>${escapeHtml(steering)}</strong></span></div><div class="nodes">${nodes}</div>${contractDetails}${artifacts ? `<div class="artifacts">${artifacts}</div>` : ""}${steer}</article>`;
}

function afterNextVisualFrame() {
  if (typeof requestAnimationFrame !== "function") return Promise.resolve();
  return new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}

async function capturePhoneRender(data) {
  if (!data.authenticated || !isPhoneClient() || sessionStorage.getItem("workbench-phone-observed")) return null;
  if (!Array.isArray(data.tasks) || data.tasks.length === 0) return null;
  await afterNextVisualFrame();
  const section = document.querySelector("#task-ledger-section");
  const updated = document.querySelector("#updated");
  if (!section || !updated || updated.dataset.snapshotCursor !== String(data.health.cursor || 0)) return null;
  if (typeof getComputedStyle === "function" && getComputedStyle(section).display === "none") return null;
  const renderedTasks = [];
  for (const task of data.tasks) {
    const selector = CSS.escape(String(task.task_id));
    const card = document.querySelector(`[data-task-card="${selector}"]`);
    const objective = card?.querySelector(`[data-task-objective="${selector}"]`);
    const state = card?.querySelector(`[data-task-state="${selector}"]`);
    const taskUpdated = card?.querySelector(`[data-task-updated="${selector}"]`);
    if (!card || !objective?.textContent.trim() || state?.textContent.trim() !== String(task.state)) return null;
    if (!taskUpdated?.textContent.trim() || taskUpdated.dataset.updatedAt !== String(task.updated_at)) return null;
    renderedTasks.push({task_id: String(task.task_id), state: String(task.state), updated_at: String(task.updated_at)});
  }
  return {snapshot_cursor: data.health.cursor || 0, rendered_tasks: renderedTasks};
}

function renderAcceptance(check) {
  return `<article class="acceptance-check"><span class="pill ${escapeHtml(check.status)}">${escapeHtml(check.id)} · ${escapeHtml(check.status)}</span><div><strong>${escapeHtml(check.requirement)}</strong><p>${escapeHtml(check.evidence)}</p></div></article>`;
}

function renderCapabilityModel(model) {
  const available = model?.status === "available";
  const routable = available && model?.routable === true;
  const tone = routable ? "ok" : model?.status === "observed" ? "pending" : "deferred";
  const state = routable ? "available · routable" : model?.status === "observed" ? "observed only" : (model?.status || "N/A");
  const roles = Array.isArray(model?.roles) && model.roles.length ? model.roles.join(" · ") : "N/A";
  const tasks = Array.isArray(model?.task_types) && model.task_types.length ? model.task_types.join(" · ") : "N/A";
  const quality = model?.quality?.floor || "unknown";
  const cost = model?.cost?.relative || "unknown";
  const latency = model?.latency?.class || "unknown";
  const effort = model?.reasoning?.preferred_effort || "N/A";
  return `<article class="capability-model"><div class="capability-model-head"><span class="pill ${tone}">${escapeHtml(state)}</span><strong>${escapeHtml(model?.provider || "N/A")} / ${escapeHtml(model?.model_id || "N/A")}</strong></div><p>family ${escapeHtml(model?.model_family || "N/A")} · CLI ${escapeHtml(model?.agent_cli_version || "N/A")}</p><p>角色：${escapeHtml(roles)}</p><p>任务：${escapeHtml(tasks)}</p><p>策略：quality ${escapeHtml(quality)} · cost ${escapeHtml(cost)} · latency ${escapeHtml(latency)} · effort ${escapeHtml(effort)}</p><p>来源：${escapeHtml(model?.policy_origin || "observed-only")}</p></article>`;
}

function renderCapabilities(registry) {
  const active = registry?.active;
  if (!active) {
    const detail = registry?.error || "尚未激活能力目录";
    return `<p class="muted">${escapeHtml(detail)}；请运行 capabilities refresh。</p>`;
  }
  const agents = Object.entries(active.agents || {}).map(([provider, agent]) => `<span class="capability-agent"><strong>${escapeHtml(provider)}</strong> · ${escapeHtml(agent?.status || "N/A")} · CLI ${escapeHtml(agent?.cli_version || "N/A")}</span>`).join("");
  const models = Array.isArray(active.models) ? active.models : [];
  const routable = models.filter((model) => model?.status === "available" && model?.routable === true).length;
  return `<div class="capability-meta"><span>catalog ${escapeHtml(active.catalog_id || registry.active_generation_id || "N/A")}</span><span>routable ${routable}/${models.length}</span></div><div class="capability-agents">${agents || '<span class="muted">Agent 版本 N/A</span>'}</div><div class="capability-models">${models.length ? models.map(renderCapabilityModel).join("") : '<p class="muted">没有模型观测</p>'}</div>`;
}

function renderApproval(approval) {
  const request = approval.request || {};
  const buttons = authenticated
    ? `<div class="approval-actions"><button class="small" data-approval="${escapeHtml(approval.approval_id)}" data-decision="retry" data-revision="${escapeHtml(approval.task_revision)}">重试</button><button class="small warning" data-approval="${escapeHtml(approval.approval_id)}" data-decision="fail" data-revision="${escapeHtml(approval.task_revision)}">标记失败</button><button class="small danger" data-approval="${escapeHtml(approval.approval_id)}" data-decision="cancel" data-revision="${escapeHtml(approval.task_revision)}">取消任务</button></div>`
    : '<a class="login-link" href="/login">登录后审批</a>';
  return `<article class="approval"><div><span class="pill needs_approval">${escapeHtml(approval.kind)}</span><h3>${escapeHtml(approval.task_id)} · ${escapeHtml(request.node_id)}</h3><p>${escapeHtml(request.reason)}</p><span class="muted">attempt ${escapeHtml(request.attempt)} · revision ${escapeHtml(approval.task_revision)}</span></div>${buttons}</article>`;
}

function alertText(alert) {
  if (alert.event_type === "approval.requested") return "需要你的审批";
  if (alert.event_type === "node.indeterminate") return "执行结果不确定";
  if (alert.event_type === "node.blocked") return "节点已阻塞";
  if (alert.event_type === "node.routed") return `执行器已切换：${alert.payload?.reason || "路由策略"}`;
  if (alert.event_type === "coordinator.started") return "Mac mini 协调器已启动";
  if (alert.event_type === "coordinator.stopped") return "Mac mini 协调器已停止";
  if (alert.event_type === "coordinator.failed") return `协调器执行失败：${alert.payload?.error || "未知错误"}`;
  if (alert.event_type === "quota.refresh_unavailable") return "Claude 配额快照不可用；Claude 调度已关闭";
  if (alert.event_type === "quota.refresh_failed") return `Claude 配额刷新失败：${alert.payload?.error || "未知错误"}`;
  if (alert.event_type === "task.state_changed") return `任务进入 ${alert.payload?.to}`;
  return alert.event_type;
}

function renderAlert(alert) {
  const tone = ["approval.requested", "node.indeterminate", "node.blocked", "coordinator.stopped", "coordinator.failed", "quota.refresh_failed", "quota.refresh_unavailable"].includes(alert.event_type) ? "error" : alert.payload?.to === "accepted" ? "ok" : "pending";
  return `<article class="alert"><span class="pill ${tone}">#${escapeHtml(alert.cursor)}</span><div><strong>${escapeHtml(alertText(alert))}</strong><p>${escapeHtml(alert.task_id || "system")} ${escapeHtml(alert.node_id || "")} · ${escapeHtml(new Date(alert.created_at).toLocaleString())}</p></div></article>`;
}

async function refreshSnapshot() {
  const response = await fetch("/api/snapshot", {cache: "no-store"});
  const data = await response.json();
  document.querySelector("#health").className = "pill ok";
  const build = data.build?.commit ? ` · ${data.build.commit.slice(0, 10)}` : "";
  document.querySelector("#health").textContent = `v${data.version}${build} · online`;
  const counts = data.health.task_counts || {};
  const active = (counts.running || 0) + (counts.queued || 0) + (counts.verifying || 0);
  const quota = data.quota;
  const quotaPolicy = data.quota_policy;
  const quotaProductivity = data.quota_productivity;
  const capabilityRegistry = data.capability_registry || {};
  const latestFiveHourProductivity = [...(quotaProductivity?.windows || [])]
    .reverse()
    .find((window) => window.kind === "five-hour" && window.status === "ok");
  const activeModels = data.health.active_models || {};
  const activeSonnet = Object.entries(activeModels).filter(([model]) => model.toLowerCase().includes("sonnet")).reduce((total, [, count]) => total + count, 0);
  const activeHigh = Object.entries(activeModels).filter(([model]) => !model.toLowerCase().includes("sonnet") && (model.toLowerCase().includes("opus") || model.toLowerCase().includes("fable"))).reduce((total, [, count]) => total + count, 0);
  const sonnetCap = quotaPolicy?.models?.sonnet?.max_concurrency ?? 0;
  const highCap = Math.max(quotaPolicy?.models?.opus?.max_concurrency ?? 0, quotaPolicy?.models?.fable?.max_concurrency ?? 0);
  const quotaZones = quotaPolicy?.zones ? `O ${quotaPolicy.zones.opus} · S ${quotaPolicy.zones.sonnet} · F ${quotaPolicy.zones.fable}` : "unknown";
  const acceptance = data.acceptance;
  const approvals = data.approvals || [];
  const alerts = data.alerts || [];
  authenticated = data.authenticated;
  document.querySelector("#metrics").innerHTML = [
    metric("任务总数", data.tasks.length),
    metric("运行/排队", active, active ? "running" : ""),
    metric("已验收", counts.accepted || 0, "ok"),
    metric("状态陈旧", data.diagnostics.stale_tasks.length, data.diagnostics.stale_tasks.length ? "error" : "ok"),
    metric("Claude 五小时剩余", quota?.five_hour_remaining == null ? "未知/禁用" : `${quota.five_hour_remaining}%`, quota?.five_hour_remaining > 25 ? "ok" : "error"),
    metric("Claude 调度区", quotaZones, quotaPolicy?.zone === "green" ? "ok" : quotaPolicy?.zone === "yellow" || quotaPolicy?.zone === "mixed" ? "pending" : "error"),
    metric("Claude 当前并发", `高阶 ${activeHigh}/${highCap} · S ${activeSonnet}/${sonnetCap}`, activeHigh || activeSonnet ? "running" : ""),
    metric(
      "每 10% Claude 配额产出",
      latestFiveHourProductivity?.accepted_points_per_10_percent == null
        ? "证据不足"
        : `${latestFiveHourProductivity.accepted_points_per_10_percent} 点`,
      latestFiveHourProductivity ? "ok" : "pending",
    ),
    metric("验收通过", `${acceptance.counts.ok}/${acceptance.checks.length}`, acceptance.complete ? "ok" : "pending"),
    metric("待处理审批", approvals.length, approvals.length ? "error" : "ok"),
    metric("事件游标", data.health.cursor),
  ].join("");
  const capabilityActive = capabilityRegistry.active;
  const capabilityModels = Array.isArray(capabilityActive?.models) ? capabilityActive.models : [];
  const capabilityRoutable = capabilityModels.filter((model) => model?.status === "available" && model?.routable === true).length;
  document.querySelector("#capability-summary").textContent = capabilityActive
    ? `${capabilityRoutable}/${capabilityModels.length} routable · ${capabilityActive.catalog_id || "N/A"}`
    : (capabilityRegistry.error || "未激活");
  document.querySelector("#capabilities").innerHTML = renderCapabilities(capabilityRegistry);
  document.querySelector("#approval-summary").textContent = approvals.length ? `${approvals.length} pending` : "0 pending";
  document.querySelector("#approvals").innerHTML = approvals.length ? approvals.map(renderApproval).join("") : '<p class="muted">当前没有待处理审批</p>';
  document.querySelector("#alerts").innerHTML = alerts.length ? alerts.slice(-8).reverse().map(renderAlert).join("") : '<p class="muted">当前没有重要提醒</p>';
  const backlog = acceptance.backlog || [];
  document.querySelector("#acceptance-summary").textContent = `${acceptance.counts.ok} ok · ${acceptance.counts.pending} pending · ${acceptance.counts.error} error · ${backlog.length} backlog`;
  document.querySelector("#acceptance").innerHTML = [...acceptance.checks, ...backlog].map(renderAcceptance).join("");
  document.querySelector("#tasks").innerHTML = data.tasks.length ? data.tasks.map(renderTask).join("") : '<p class="muted">暂无任务</p>';
  document.querySelectorAll("button[data-task]").forEach((button) => button.addEventListener("click", controlTask));
  document.querySelectorAll("button[data-steer-task]").forEach((button) => button.addEventListener("click", steerTask));
  document.querySelectorAll("button[data-approval]").forEach((button) => button.addEventListener("click", decideApproval));
  const updated = document.querySelector("#updated");
  updated.textContent = `刷新 ${new Date().toLocaleTimeString()}`;
  updated.dataset.snapshotCursor = String(data.health.cursor || 0);
  cursor = Math.max(cursor, data.health.cursor || 0);
  const renderedReceipt = await capturePhoneRender(data);
  await recordPhoneObservation(data, renderedReceipt);
  notifyNewAlerts(alerts);
}

function notifyNewAlerts(alerts) {
  if (!("Notification" in window) || Notification.permission !== "granted" || !alerts.length) return;
  const latest = Math.max(...alerts.map((alert) => Number(alert.cursor)));
  const stored = localStorage.getItem("workbench-alert-cursor");
  if (stored == null) {
    localStorage.setItem("workbench-alert-cursor", String(latest));
    return;
  }
  alerts.filter((alert) => Number(alert.cursor) > Number(stored)).forEach((alert) => {
    try { new Notification("Codex Workbench", {body: `${alertText(alert)} · ${alert.task_id || "system"}`}); }
    catch (error) { console.warn("notification unavailable", error); }
  });
  localStorage.setItem("workbench-alert-cursor", String(latest));
}

async function decideApproval(event) {
  const button = event.currentTarget;
  document.querySelectorAll(`button[data-approval="${CSS.escape(button.dataset.approval)}"]`).forEach((item) => { item.disabled = true; });
  const response = await fetch(`/api/approvals/${encodeURIComponent(button.dataset.approval)}/decide`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({decision: button.dataset.decision, expected_revision: Number(button.dataset.revision)}),
  });
  if (!response.ok) alert((await response.json()).error || "审批失败");
  await refreshSnapshot();
}

async function recordPhoneObservation(data, renderedReceipt) {
  if (!renderedReceipt) return;
  let clientId = localStorage.getItem("workbench-phone-client-id");
  if (!clientId) {
    clientId = `phone-${crypto.randomUUID()}`;
    localStorage.setItem("workbench-phone-client-id", clientId);
  }
  const response = await fetch("/api/clients/observe", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({client_id: clientId, ...renderedReceipt}),
  });
  if (response.ok) sessionStorage.setItem("workbench-phone-observed", "true");
}

async function controlTask(event) {
  const button = event.currentTarget;
  button.disabled = true;
  const body = {action: button.dataset.action, expected_revision: Number(button.dataset.revision)};
  if (button.dataset.action === "set_priority") body.priority = Number(button.dataset.priority);
  const response = await fetch(`/api/tasks/${encodeURIComponent(button.dataset.task)}/control`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!response.ok) alert((await response.json()).error || "控制失败");
  await refreshSnapshot();
}

async function steerTask(event) {
  const button = event.currentTarget;
  const input = document.querySelector(`input[data-steer-input="${CSS.escape(button.dataset.steerTask)}"]`);
  const instruction = input.value.trim();
  if (!instruction) return;
  button.disabled = true;
  const response = await fetch(`/api/tasks/${encodeURIComponent(button.dataset.steerTask)}/steer`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({instruction, expected_revision: Number(button.dataset.revision)}),
  });
  if (!response.ok) alert((await response.json()).error || "补充指令失败");
  await refreshSnapshot();
}

async function refreshEvents() {
  const response = await fetch(`/api/events?after=${Math.max(0, cursor - 30)}`, {cache: "no-store"});
  const data = await response.json();
  const events = data.events || [];
  if (events.length) cursor = Math.max(cursor, ...events.map((event) => event.cursor));
  document.querySelector("#cursor").textContent = `cursor ${cursor}`;
  document.querySelector("#events").innerHTML = events.slice(-30).reverse().map((event) => `<div class="event"><span>#${event.cursor}</span><strong>${escapeHtml(event.event_type)}</strong><span>${escapeHtml(event.task_id || "system")} ${escapeHtml(event.node_id || "")}</span></div>`).join("") || '<p class="muted">暂无事件</p>';
}

async function tick() {
  try { await Promise.all([refreshSnapshot(), refreshEvents()]); }
  catch (error) {
    document.querySelector("#health").className = "pill error";
    document.querySelector("#health").textContent = "stale / disconnected";
  }
}

tick();
setInterval(tick, 5000);

document.querySelector("#enable-notifications").addEventListener("click", async (event) => {
  if (!("Notification" in window)) {
    event.currentTarget.textContent = "当前浏览器不支持";
    return;
  }
  const permission = await Notification.requestPermission();
  event.currentTarget.textContent = permission === "granted" ? "前台通知已启用" : "通知未授权";
});
