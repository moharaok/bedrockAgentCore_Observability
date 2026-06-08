let agents = [];
let selectedAgent = null;

// --- Init ---
document.addEventListener("DOMContentLoaded", () => {
    loadAgents();
});

function autoSelectAgentFromUrl() {
    const params = new URLSearchParams(window.location.search);
    const agentName = params.get("agent");
    if (agentName && agents.length > 0) {
        const match = agents.find(a => a.agentRuntimeName === agentName);
        if (match) {
            selectAgent(match.agentRuntimeId);
        }
    }
}

// --- API calls ---
async function loadAgents() {
    const listEl = document.getElementById("agentList");
    listEl.innerHTML = '<div class="loading"><div class="spinner"></div><span>Loading agents...</span></div>';

    try {
        const res = await fetch("/api/agents");
        const data = await res.json();
        agents = data.agents || [];
        document.getElementById("agentCount").textContent = agents.length;
        renderAgentList();
        autoSelectAgentFromUrl();
    } catch (err) {
        listEl.innerHTML = `<div class="empty-state"><p>Error: ${err.message}</p></div>`;
    }
}

async function loadAgentDetail(agent) {
    const panel = document.getElementById("detailPanel");
    panel.innerHTML = '<div class="loading"><div class="spinner"></div><span>Loading details...</span></div>';

    try {
        const res = await fetch(`/api/agents/${agent.agentRuntimeId}/detail?region=${agent.region}`);
        const data = await res.json();
        renderAgentDetail(data.agent, agent);
        loadSessions(agent);
    } catch (err) {
        panel.innerHTML = `<div class="empty-state"><p>Error: ${err.message}</p></div>`;
    }
}

async function loadSessions(agent) {
    const container = document.getElementById("sessionContainer");
    if (!container) return;
    container.innerHTML = '<div class="loading"><div class="spinner"></div><span>Querying CloudWatch Logs...</span></div>';

    const seconds = document.getElementById("timeRange").value;
    const endTime = Math.floor(Date.now() / 1000);
    const startTime = endTime - parseInt(seconds);
    const agentName = `${agent.agentRuntimeName}.DEFAULT`;

    try {
        const res = await fetch(`/api/sessions?startTime=${startTime}&endTime=${endTime}&agentName=${agentName}&region=${agent.region}`);
        const data = await res.json();
        renderSessions(data.sessions || [], agent.agentRuntimeName);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${err.message}</p></div>`;
    }
}

// --- Rendering ---
function renderAgentList() {
    const listEl = document.getElementById("agentList");
    if (agents.length === 0) {
        listEl.innerHTML = '<div class="empty-state"><p>No agents found.</p></div>';
        return;
    }

    listEl.innerHTML = agents.map(agent => {
        const protocol = getProtocol(agent);
        const protocolClass = protocol === "MCP" ? "protocol-mcp" : "protocol-http";
        const selected = selectedAgent && selectedAgent.agentRuntimeId === agent.agentRuntimeId ? " selected" : "";
        return `
            <div class="agent-item${selected}" onclick="selectAgent('${agent.agentRuntimeId}')">
                <div class="agent-info">
                    <div class="agent-name">${agent.agentRuntimeName}</div>
                    <div class="agent-meta">
                        <span class="region-badge">${agent.region}</span>
                        <span class="protocol-badge ${protocolClass}">${protocol}</span>
                        <span class="status-dot status-${agent.status.toLowerCase()}"></span>
                    </div>
                </div>
                <div class="agent-version">v${agent.agentRuntimeVersion}</div>
            </div>
        `;
    }).join("");
}

function renderAgentDetail(detail, agent) {
    const panel = document.getElementById("detailPanel");

    const envVars = detail.environmentVariables
        ? Object.entries(detail.environmentVariables).map(([k, v]) =>
            `<div class="detail-row"><span class="label">${k}</span><span class="value mono small">${v}</span></div>`
        ).join("")
        : "";

    const authSection = detail.authorizerConfiguration?.customJWTAuthorizer
        ? `<div class="detail-section">
            <h4>Authorization</h4>
            <div class="detail-row"><span class="label">Discovery URL</span><span class="value mono small">${detail.authorizerConfiguration.customJWTAuthorizer.discoveryUrl}</span></div>
            <div class="detail-row"><span class="label">Allowed Clients</span><span class="value mono small">${(detail.authorizerConfiguration.customJWTAuthorizer.allowedClients || []).join(", ")}</span></div>
           </div>`
        : "";

    const envSection = envVars
        ? `<div class="detail-section"><h4>Environment Variables</h4>${envVars}</div>`
        : "";

    panel.innerHTML = `
        <div class="agent-detail">
            <div class="detail-header">
                <h2>${agent.agentRuntimeName}</h2>
                <button class="close-btn" onclick="clearSelection()">✕</button>
            </div>
            <div class="detail-grid">
                <div class="detail-section">
                    <h4>General</h4>
                    <div class="detail-row"><span class="label">ID</span><span class="value mono">${detail.agentRuntimeId}</span></div>
                    <div class="detail-row"><span class="label">Version</span><span class="value">${detail.agentRuntimeVersion}</span></div>
                    <div class="detail-row"><span class="label">Status</span><span class="value badge-ready">${detail.status}</span></div>
                    <div class="detail-row"><span class="label">Protocol</span><span class="value">${detail.protocolConfiguration?.serverProtocol || "N/A"}</span></div>
                    <div class="detail-row"><span class="label">Region</span><span class="value">${agent.region}</span></div>
                    <div class="detail-row"><span class="label">Network</span><span class="value">${detail.networkConfiguration?.networkMode || "N/A"}</span></div>
                </div>
                <div class="detail-section">
                    <h4>Lifecycle</h4>
                    <div class="detail-row"><span class="label">Created</span><span class="value">${formatDate(detail.createdAt)}</span></div>
                    <div class="detail-row"><span class="label">Last Updated</span><span class="value">${formatDate(detail.lastUpdatedAt)}</span></div>
                    <div class="detail-row"><span class="label">Idle Timeout</span><span class="value">${detail.lifecycleConfiguration?.idleRuntimeSessionTimeout || "N/A"}s</span></div>
                    <div class="detail-row"><span class="label">Max Lifetime</span><span class="value">${detail.lifecycleConfiguration?.maxLifetime || "N/A"}s</span></div>
                </div>
                <div class="detail-section">
                    <h4>Infrastructure</h4>
                    <div class="detail-row"><span class="label">Role ARN</span><span class="value mono small">${detail.roleArn || "N/A"}</span></div>
                    <div class="detail-row"><span class="label">Container</span><span class="value mono small">${detail.agentRuntimeArtifact?.containerConfiguration?.containerUri || "N/A"}</span></div>
                </div>
                ${authSection}
                ${envSection}
            </div>
        </div>
        <div id="sessionContainer" class="session-list">
            <div class="loading"><div class="spinner"></div><span>Querying CloudWatch Logs...</span></div>
        </div>
    `;
}

function renderSessions(sessions, agentName) {
    const container = document.getElementById("sessionContainer");
    if (!container) return;

    if (sessions.length === 0) {
        container.innerHTML = `
            <div class="session-list">
                <div class="session-header"><h3>Sessions for ${agentName}</h3><span class="session-count">0 sessions</span></div>
                <div class="empty-state"><p>No sessions found for the selected time range.</p></div>
            </div>`;
        return;
    }

    const rows = sessions.map(s => {
        const sessionLink = s.sessionId
            ? `<a href="#" class="session-link" onclick="openSessionDetail('${s.sessionId}', '${s.service}'); return false;">${s.sessionId}</a>`
            : "—";
        return `
            <tr>
                <td class="mono">${sessionLink}</td>
                <td>${s.service || ""}</td>
                <td class="center">${s.spanCount || ""}</td>
                <td>${s.firstSeen || ""}</td>
                <td>${s.lastSeen || ""}</td>
            </tr>
        `;
    }).join("");

    container.innerHTML = `
        <div class="session-header">
            <h3>Sessions for ${agentName}</h3>
            <span class="session-count">${sessions.length} sessions</span>
        </div>
        <div class="table-container">
            <table>
                <thead><tr><th>Session ID</th><th>Service</th><th>Spans</th><th>First Seen</th><th>Last Seen</th></tr></thead>
                <tbody>${rows}</tbody>
            </table>
        </div>
    `;
}

// --- Actions ---
function selectAgent(agentId) {
    selectedAgent = agents.find(a => a.agentRuntimeId === agentId);
    renderAgentList();
    if (selectedAgent) {
        loadAgentDetail(selectedAgent);
    }
}

function clearSelection() {
    selectedAgent = null;
    renderAgentList();
    document.getElementById("detailPanel").innerHTML = `
        <div class="placeholder">
            <div class="placeholder-icon">📊</div>
            <h2>Select an agent to view details</h2>
            <p>Choose an agent from the list to see its metadata and session activity.</p>
        </div>`;
}

function onTimeRangeChange() {
    if (selectedAgent) {
        loadSessions(selectedAgent);
    }
}

// --- Helpers ---
function getProtocol(agent) {
    const name = agent.agentRuntimeName.toLowerCase();
    if (name.includes("mcp") || name.includes("server")) return "MCP";
    return "HTTP";
}

function formatDate(dateStr) {
    if (!dateStr) return "N/A";
    try {
        return new Date(dateStr).toLocaleString();
    } catch {
        return dateStr;
    }
}

// --- Session Detail Panel ---
async function openSessionDetail(sessionId, service) {
    const panel = document.getElementById("sessionDetailPanel");
    const content = document.getElementById("sessionDetailContent");

    panel.classList.add("open");
    addOverlay();

    content.innerHTML = `
        <div class="session-tabs">
            <button class="tab-btn active" onclick="switchTab('conversation')">Conversation</button>
            <button class="tab-btn" onclick="switchTab('metrics')">Metrics</button>
            <button class="tab-btn" onclick="switchTab('spans')">Spans</button>
        </div>
        <div id="tabContent">
            <div class="loading"><div class="spinner"></div><span>Loading conversation...</span></div>
        </div>
    `;

    // Store context for tab switching
    window._sessionContext = { sessionId, service };

    loadConversationTab(sessionId, service);
}

function switchTab(tab) {
    const buttons = document.querySelectorAll(".tab-btn");
    buttons.forEach(b => b.classList.remove("active"));
    event.target.classList.add("active");

    const ctx = window._sessionContext;
    if (tab === "spans") {
        loadSpansTab(ctx.sessionId, ctx.service);
    } else if (tab === "conversation") {
        loadConversationTab(ctx.sessionId, ctx.service);
    } else if (tab === "metrics") {
        loadMetricsTab(ctx.sessionId, ctx.service);
    }
}

async function loadMetricsTab(sessionId, service) {
    const container = document.getElementById("tabContent");
    container.innerHTML = '<div class="loading"><div class="spinner"></div><span>Computing metrics...</span></div>';

    const seconds = document.getElementById("timeRange").value;
    const endTime = Math.floor(Date.now() / 1000);
    const startTime = endTime - parseInt(seconds);
    const region = selectedAgent ? selectedAgent.region : "us-east-1";

    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/metrics?region=${region}&startTime=${startTime}&endTime=${endTime}`);
        const data = await res.json();
        renderMetrics(data.metrics, sessionId);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${err.message}</p></div>`;
    }
}

function renderMetrics(metrics, sessionId) {
    const container = document.getElementById("tabContent");
    const o = metrics.overview;
    const l = metrics.latency;
    const llm = metrics.llm;
    const t = metrics.tokens;
    const tools = metrics.tools;
    const cost = metrics.cost_estimate;

    container.innerHTML = `
        <div class="metrics-dashboard">
            <div class="metrics-section">
                <h4>📊 Overview</h4>
                <div class="metrics-grid">
                    <div class="metric-card">
                        <div class="metric-value">${o.total_invocations}</div>
                        <div class="metric-label">User Turns</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${o.total_llm_calls}</div>
                        <div class="metric-label">LLM Calls</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${o.total_tool_calls}</div>
                        <div class="metric-label">Tool Calls</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${o.total_gateway_calls}</div>
                        <div class="metric-label">Gateway Calls</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${o.event_loop_cycles}</div>
                        <div class="metric-label">Event Loops</div>
                    </div>
                    <div class="metric-card ${o.tool_errors > 0 ? 'metric-error' : ''}">
                        <div class="metric-value">${o.tool_errors + o.http_errors}</div>
                        <div class="metric-label">Errors</div>
                    </div>
                </div>
            </div>

            <div class="metrics-section">
                <h4>⏱️ Latency</h4>
                <div class="metrics-grid">
                    <div class="metric-card metric-wide">
                        <div class="metric-value">${l.total_session_ms.toFixed(0)}ms</div>
                        <div class="metric-label">Total Session Time</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${l.avg_per_invocation_ms.toFixed(0)}ms</div>
                        <div class="metric-label">Avg per Turn</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${l.total_llm_time_ms.toFixed(0)}ms</div>
                        <div class="metric-label">LLM Time</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${l.total_tool_time_ms.toFixed(0)}ms</div>
                        <div class="metric-label">Tool Time</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${l.total_gateway_time_ms.toFixed(0)}ms</div>
                        <div class="metric-label">Gateway Time</div>
                    </div>
                </div>
                ${l.invocation_latencies_ms.length > 0 ? `
                    <div class="metric-detail">
                        <strong>Per-turn latency:</strong> ${l.invocation_latencies_ms.map((v,i) => `Turn ${i+1}: ${v}ms`).join(' | ')}
                    </div>
                ` : ''}
            </div>

            <div class="metrics-section">
                <h4>🧠 LLM Performance</h4>
                <div class="metrics-grid">
                    <div class="metric-card">
                        <div class="metric-value">${llm.models_used.join(', ') || 'N/A'}</div>
                        <div class="metric-label">Model</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${llm.avg_ttft_ms}ms</div>
                        <div class="metric-label">Avg TTFT</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${llm.p95_ttft_ms}ms</div>
                        <div class="metric-label">P95 TTFT</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${l.avg_llm_call_ms.toFixed(0)}ms</div>
                        <div class="metric-label">Avg LLM Duration</div>
                    </div>
                </div>
            </div>

            <div class="metrics-section">
                <h4>🔤 Token Usage</h4>
                <div class="metrics-grid">
                    <div class="metric-card">
                        <div class="metric-value">${t.total_input.toLocaleString()}</div>
                        <div class="metric-label">Input Tokens</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${t.total_output.toLocaleString()}</div>
                        <div class="metric-label">Output Tokens</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${t.total.toLocaleString()}</div>
                        <div class="metric-label">Total Tokens</div>
                    </div>
                    <div class="metric-card">
                        <div class="metric-value">${t.cache_read.toLocaleString()}</div>
                        <div class="metric-label">Cache Read</div>
                    </div>
                </div>
                <div class="metric-detail">
                    <strong>Avg per turn:</strong> ${t.avg_input_per_invocation.toLocaleString()} in / ${t.avg_output_per_invocation.toLocaleString()} out
                </div>
            </div>

            <div class="metrics-section">
                <h4>🔧 Tools</h4>
                <div class="tools-table">
                    <table>
                        <thead><tr><th>Tool</th><th>Success</th><th>Errors</th><th>Total Time</th></tr></thead>
                        <tbody>
                            ${Object.entries(tools.tool_details).map(([name, d]) => `
                                <tr>
                                    <td><strong>${name}</strong></td>
                                    <td class="center">${d.success}</td>
                                    <td class="center ${d.error > 0 ? 'text-error' : ''}">${d.error}</td>
                                    <td class="center">${d.total_ms.toFixed(0)}ms</td>
                                </tr>
                            `).join('')}
                        </tbody>
                    </table>
                </div>
            </div>

            <div class="metrics-section">
                <h4>💰 Cost Estimate</h4>
                <div class="metrics-grid">
                    <div class="metric-card metric-highlight">
                        <div class="metric-value">$${cost.total_usd.toFixed(6)}</div>
                        <div class="metric-label">Total Session Cost</div>
                    </div>
                </div>
                ${cost.breakdown && cost.breakdown.length > 0 ? `
                    <div class="tools-table">
                        <table>
                            <thead><tr><th>Model</th><th>Input Tokens</th><th>Output Tokens</th><th>$/1K In</th><th>$/1K Out</th><th>Cost</th></tr></thead>
                            <tbody>
                                ${cost.breakdown.map(b => `
                                    <tr>
                                        <td><strong>${b.model}</strong></td>
                                        <td class="center">${b.input_tokens.toLocaleString()}</td>
                                        <td class="center">${b.output_tokens.toLocaleString()}</td>
                                        <td class="center">$${b.input_price_per_1k}</td>
                                        <td class="center">$${b.output_price_per_1k}</td>
                                        <td class="center"><strong>$${b.total_usd.toFixed(6)}</strong></td>
                                    </tr>
                                `).join('')}
                            </tbody>
                        </table>
                    </div>
                ` : ''}
                <div class="metric-detail">
                    ${cost.note}<br>
                    <a href="${cost.source}" target="_blank" style="color:#4299e1">${cost.source}</a>
                </div>
            </div>
        </div>
    `;
}

async function loadSpansTab(sessionId, service) {
    const container = document.getElementById("tabContent");
    container.innerHTML = '<div class="loading"><div class="spinner"></div><span>Loading spans...</span></div>';

    const seconds = document.getElementById("timeRange").value;
    const endTime = Math.floor(Date.now() / 1000);
    const startTime = endTime - parseInt(seconds);
    const region = selectedAgent ? selectedAgent.region : "us-east-1";

    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/spans?region=${region}&startTime=${startTime}&endTime=${endTime}`);
        const data = await res.json();
        renderSessionDetail(data.spans || [], sessionId, service);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${err.message}</p></div>`;
    }
}

async function loadConversationTab(sessionId, service) {
    const container = document.getElementById("tabContent");
    container.innerHTML = '<div class="loading"><div class="spinner"></div><span>Loading conversation...</span></div>';

    const seconds = document.getElementById("timeRange").value;
    const endTime = Math.floor(Date.now() / 1000);
    const startTime = endTime - parseInt(seconds);
    const region = selectedAgent ? selectedAgent.region : "us-east-1";
    const agentId = selectedAgent ? selectedAgent.agentRuntimeId : "";

    try {
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/conversation?region=${region}&startTime=${startTime}&endTime=${endTime}&agentId=${agentId}`);
        const data = await res.json();
        renderConversation(data.conversation || [], sessionId);
    } catch (err) {
        container.innerHTML = `<div class="empty-state"><p>Error: ${err.message}</p></div>`;
    }
}

function renderConversation(conversation, sessionId) {
    const container = document.getElementById("tabContent");

    if (conversation.length === 0) {
        container.innerHTML = `<div class="empty-state"><p>No conversation data found for this session.</p></div>`;
        return;
    }

    const messagesHtml = conversation.map(msg => {
        const style = msg.style || msg.role || "system";
        const icon = msg.icon || "•";
        const title = msg.title || msg.type || "";
        const content = msg.content || "";

        let styleClass;
        switch (style) {
            case "user": styleClass = "msg-user"; break;
            case "assistant": styleClass = "msg-assistant"; break;
            case "tool_call": styleClass = "msg-tool-call"; break;
            case "tool_result": styleClass = "msg-tool-result"; break;
            case "error": styleClass = "msg-error"; break;
            case "summary": styleClass = "msg-summary"; break;
            case "llm": styleClass = "msg-llm"; break;
            case "gateway": styleClass = "msg-gateway"; break;
            case "aws_api": styleClass = "msg-aws_api"; break;
            case "system_prompt": styleClass = "msg-system_prompt"; break;
            case "llm_messages": styleClass = "msg-llm_messages"; break;
            case "llm_response": styleClass = "msg-llm_response"; break;
            default: styleClass = "msg-system"; break;
        }

        const contentHtml = formatContent(content, style);

        return `
            <div class="chat-message ${styleClass}">
                <div class="msg-header">
                    <span class="msg-icon">${icon}</span>
                    <span class="msg-title">${title}</span>
                </div>
                <div class="msg-content">${contentHtml}</div>
            </div>
        `;
    }).join("");

    container.innerHTML = `
        <div class="conversation-container">
            <div class="conversation-header">
                <span>Session Replay</span>
                <span class="session-count">${conversation.length} steps</span>
            </div>
            <div class="chat-messages">${messagesHtml}</div>
        </div>
    `;
}

function formatContent(content, role) {
    if (!content) return "";

    // Escape HTML first
    let html = escapeHtml(content);

    // Format code blocks (```json ... ```)
    html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre class="code-block"><code>$2</code></pre>');
    html = html.replace(/```([\s\S]*?)```/g, '<pre class="code-block"><code>$1</code></pre>');

    // Format bold (**text**)
    html = html.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');

    // Format inline code (`text`)
    html = html.replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>');

    // Newlines to <br>
    html = html.replace(/\n/g, "<br>");

    return html;
}

function escapeHtml(text) {
    const div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML.replace(/\n/g, "<br>");
}

function closeSessionDetail() {
    document.getElementById("sessionDetailPanel").classList.remove("open");
    removeOverlay();
}

function addOverlay() {
    let overlay = document.getElementById("panelOverlay");
    if (!overlay) {
        overlay = document.createElement("div");
        overlay.id = "panelOverlay";
        overlay.className = "overlay open";
        overlay.onclick = closeSessionDetail;
        document.body.appendChild(overlay);
    } else {
        overlay.classList.add("open");
    }
}

function removeOverlay() {
    const overlay = document.getElementById("panelOverlay");
    if (overlay) overlay.classList.remove("open");
}

function renderSessionDetail(spans, sessionId, service) {
    const content = document.getElementById("tabContent");

    if (spans.length === 0) {
        content.innerHTML = `
            <div class="session-summary">
                <h4>Session: ${sessionId}</h4>
                <div class="summary-row"><span class="label">Service</span><span class="value">${service}</span></div>
            </div>
            <div class="empty-state"><p>No spans found for this session.</p></div>`;
        return;
    }

    // Summary
    const totalDuration = spans.reduce((sum, s) => sum + s.durationMs, 0);
    const errors = spans.filter(s => s.status === "ERROR").length;
    const firstSpan = spans[0];
    const lastSpan = spans[spans.length - 1];

    let summaryHtml = `
        <div class="session-summary">
            <h4>📋 Session: ${truncate(sessionId, 40)}</h4>
            <div class="summary-row"><span class="label">Service</span><span class="value">${service}</span></div>
            <div class="summary-row"><span class="label">Total Spans</span><span class="value">${spans.length}</span></div>
            <div class="summary-row"><span class="label">Total Duration</span><span class="value">${totalDuration.toFixed(1)} ms</span></div>
            <div class="summary-row"><span class="label">Errors</span><span class="value" style="color:${errors > 0 ? '#c53030' : '#276749'}">${errors}</span></div>
            <div class="summary-row"><span class="label">Trace ID</span><span class="value" style="font-size:0.7rem;font-family:monospace">${firstSpan.traceId}</span></div>
        </div>
    `;

    // Span cards
    const spanCards = spans.map((span, idx) => {
        const kindClass = `kind-${span.kind.toLowerCase()}`;
        const statusClass = span.status === "ERROR" ? "status-error" : span.status === "OK" ? "status-ok" : "";

        const attrEntries = Object.entries(span.attributes || {})
            .filter(([k]) => !k.startsWith("aws.local") && k !== "PlatformType" && k !== "telemetry.extended")
            .map(([k, v]) => `${k}: ${v}`)
            .join("\n");

        const eventsHtml = (span.events || []).map(e => {
            const attrs = e.attributes || {};
            if (attrs["exception.type"]) {
                return `<div style="margin-top:4px;padding:4px 8px;background:#fed7d7;border-radius:4px;font-size:0.7rem;color:#c53030">
                    <strong>${attrs["exception.type"]}</strong>: ${truncate(attrs["exception.message"] || "", 100)}
                </div>`;
            }
            return "";
        }).join("");

        return `
            <div class="span-card">
                <div class="span-card-header">
                    <span class="span-name">${span.name}</span>
                    <span class="span-duration">${span.durationMs} ms</span>
                </div>
                <div class="span-meta">
                    <span class="span-tag ${kindClass}">${span.kind}</span>
                    ${statusClass ? `<span class="span-tag ${statusClass}">${span.status}</span>` : ""}
                    <span class="span-tag">span: ${span.spanId.substring(0, 8)}</span>
                    ${span.parentSpanId ? `<span class="span-tag">parent: ${span.parentSpanId.substring(0, 8)}</span>` : ""}
                </div>
                ${eventsHtml}
                ${attrEntries ? `
                    <div class="span-attrs">
                        <span class="span-attrs-toggle" onclick="toggleAttrs(${idx})">▶ Attributes (${Object.keys(span.attributes).length})</span>
                        <div class="span-attrs-content" id="attrs-${idx}">${attrEntries}</div>
                    </div>
                ` : ""}
            </div>
        `;
    }).join("");

    content.innerHTML = summaryHtml + spanCards;
}

function toggleAttrs(idx) {
    const el = document.getElementById(`attrs-${idx}`);
    if (el) el.classList.toggle("open");
}

function truncate(str, max) {
    if (!str) return "";
    return str.length > max ? str.substring(0, max) + "..." : str;
}
