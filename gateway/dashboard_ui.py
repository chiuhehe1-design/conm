#!/usr/bin/env python3
"""
EXECUTIVE REAL-TIME WEB DASHBOARD (HTML5 / CSS3 / Vanilla JS)
Provides an ultra-responsive, zero-dependency operations console for PrimeNode ANTI.
Works offline and behind air-gapped WireGuard tunnels without external CDNs.
"""

from typing import Optional


def get_dashboard_html(initial_token: Optional[str] = None) -> str:
    injected_token = initial_token or ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ANTI // Autonomous Company OS Dashboard</title>
  <style>
    :root {{
      --bg: #0b0f19;
      --card-bg: #111827;
      --card-border: #1f293d;
      --text-main: #f9fafb;
      --text-muted: #9ca3af;
      --accent-cyan: #06b6d4;
      --accent-green: #10b981;
      --accent-amber: #f59e0b;
      --accent-red: #ef4444;
      --accent-indigo: #6366f1;
      --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text-main);
      font-family: var(--font-family);
      line-height: 1.5;
      padding: 24px;
      min-height: 100vh;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 24px;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand-logo {{
      width: 36px;
      height: 36px;
      border-radius: 8px;
      background: linear-gradient(135deg, var(--accent-indigo), var(--accent-cyan));
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      color: #fff;
      font-size: 18px;
    }}
    .brand h1 {{
      font-size: 20px;
      font-weight: 700;
      letter-spacing: 0.5px;
    }}
    .brand span {{
      font-size: 12px;
      color: var(--accent-cyan);
      text-transform: uppercase;
      letter-spacing: 1.5px;
      font-weight: 600;
      display: block;
    }}
    .header-controls {{
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
    }}
    .status-badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.15);
      color: var(--accent-green);
      border: 1px solid rgba(16, 185, 129, 0.3);
    }}
    .status-badge.paused {{
      background: rgba(239, 68, 68, 0.15);
      color: var(--accent-red);
      border-color: rgba(239, 68, 68, 0.3);
    }}
    .pulse-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
    }}
    .btn {{
      padding: 8px 16px;
      border-radius: 6px;
      border: 1px solid var(--card-border);
      background: #1f293d;
      color: var(--text-main);
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .btn:hover {{ background: #2b3952; }}
    .btn-danger {{ background: rgba(239, 68, 68, 0.2); color: var(--accent-red); border-color: rgba(239, 68, 68, 0.4); }}
    .btn-danger:hover {{ background: rgba(239, 68, 68, 0.35); }}
    .btn-primary {{ background: var(--accent-indigo); color: #fff; border-color: transparent; }}
    .btn-primary:hover {{ background: #4f46e5; }}

    /* KPI Metrics Grid */
    .kpi-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .kpi-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 18px;
      position: relative;
      overflow: hidden;
    }}
    .kpi-title {{
      font-size: 12px;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      font-weight: 600;
      margin-bottom: 8px;
    }}
    .kpi-val {{
      font-size: 26px;
      font-weight: 700;
      color: var(--text-main);
    }}
    .kpi-sub {{
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 4px;
    }}
    .kpi-green {{ color: var(--accent-green); }}
    .kpi-cyan {{ color: var(--accent-cyan); }}
    .kpi-amber {{ color: var(--accent-amber); }}
    .kpi-indigo {{ color: var(--accent-indigo); }}

    /* Layout Sections */
    .main-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 20px;
      margin-bottom: 24px;
    }}
    @media (max-width: 1024px) {{
      .main-grid {{ grid-template-columns: 1fr; }}
    }}
    .panel {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 10px;
      padding: 20px;
      display: flex;
      flex-direction: column;
    }}
    .panel-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--card-border);
    }}
    .panel-title {{
      font-size: 15px;
      font-weight: 600;
      letter-spacing: 0.3px;
    }}
    .panel-count {{
      font-size: 12px;
      background: #1f293d;
      padding: 2px 8px;
      border-radius: 12px;
      color: var(--text-muted);
    }}

    /* Table Styles */
    .table-container {{
      overflow-x: auto;
      max-height: 380px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      text-align: left;
    }}
    th {{
      padding: 10px 12px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--card-border);
      font-weight: 600;
      text-transform: uppercase;
      font-size: 11px;
    }}
    td {{
      padding: 10px 12px;
      border-bottom: 1px solid rgba(31, 41, 61, 0.5);
    }}
    tr:hover td {{
      background: rgba(255, 255, 255, 0.02);
    }}
    .badge {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
    }}
    .badge-opire {{ background: #4338ca; color: #e0e7ff; }}
    .badge-superteam {{ background: #7c2d12; color: #ffedd5; }}
    .badge-algora {{ background: #065f46; color: #d1fae5; }}
    .badge-github {{ background: #374151; color: #f3f4f6; }}
    .stage-badge {{
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 600;
    }}
    .stage-CONFIRMED {{ background: rgba(16, 185, 129, 0.2); color: var(--accent-green); }}
    .stage-TRACKING {{ background: rgba(6, 182, 212, 0.2); color: var(--accent-cyan); }}
    .stage-SUBMIT {{ background: rgba(99, 102, 241, 0.2); color: var(--accent-indigo); }}
    .stage-EXECUTE {{ background: rgba(245, 158, 11, 0.2); color: var(--accent-amber); }}
    .stage-ELIGIBILITY {{ background: rgba(156, 163, 175, 0.2); color: #d1d5db; }}

    /* Progress bar */
    .bar-bg {{
      background: #1f293d;
      border-radius: 4px;
      height: 6px;
      width: 100%;
      overflow: hidden;
    }}
    .bar-fill {{
      height: 100%;
      background: var(--accent-cyan);
      border-radius: 4px;
    }}

    /* Auth Modal */
    .auth-banner {{
      background: rgba(99, 102, 241, 0.1);
      border: 1px solid rgba(99, 102, 241, 0.3);
      border-radius: 8px;
      padding: 12px 16px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 20px;
    }}
    .auth-banner input {{
      background: #1f293d;
      border: 1px solid var(--card-border);
      color: #fff;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 13px;
      width: 320px;
    }}
  </style>
</head>
<body>

  <!-- Top Header -->
  <div class="header">
    <div class="brand">
      <div class="brand-logo">A</div>
      <div>
        <h1>PRIME NODE // ANTI</h1>
        <span>Autonomous Company OS • 24/7 Operations</span>
      </div>
    </div>
    <div class="header-controls">
      <div id="statusBadge" class="status-badge">
        <div class="pulse-dot"></div>
        <span id="statusText">SYSTEM ACTIVE</span>
      </div>
      <button class="btn btn-primary" onclick="triggerCycle()">⚡ Run Cycle</button>
      <button class="btn" style="border-color: var(--accent-indigo); color: var(--accent-cyan);" onclick="triggerRebalance()">⚖️ Rebalance</button>
      <button class="btn" style="border-color: var(--accent-cyan); color: var(--accent-cyan);" onclick="triggerBackup()">💾 Backup</button>
      <button class="btn" style="border-color: var(--accent-green); color: var(--accent-green);" onclick="triggerDRDrill()">🛡️ DR Drill</button>
      <button class="btn" style="border-color: var(--accent-amber); color: var(--accent-amber);" onclick="triggerHourlyEval()">🔬 Hourly Upgrade</button>
      <button class="btn btn-danger" onclick="togglePause()">🚨 Break-Glass</button>
      <button class="btn" onclick="refreshAll()">🔄 Refresh</button>
    </div>
  </div>


  <!-- Token Bar -->
  <div class="auth-banner" id="authBanner">
    <div>
      <strong>Bearer Authorization:</strong>
      <span style="color: var(--text-muted); font-size: 13px; margin-left: 8px;">Enter ANTI / Owner Token to view telemetry</span>
    </div>
    <div style="display: flex; gap: 8px;">
      <input type="password" id="tokenInput" placeholder="Paste Bearer Token...">
      <button class="btn" onclick="saveToken()">Save Key</button>
    </div>
  </div>

  <!-- Executive KPI Metrics -->
  <div class="kpi-grid">
    <div class="kpi-card">
      <div class="kpi-title">Confirmed Cash Flow</div>
      <div class="kpi-val kpi-green" id="kpiConfirmed">$0.00 USDC</div>
      <div class="kpi-sub">On-Chain Base EVM Verified</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Pipeline Opportunities</div>
      <div class="kpi-val kpi-cyan" id="kpiPipeline">$0.00 USDC</div>
      <div class="kpi-sub" id="kpiOppCount">0 active bounties</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Compute Token Burn</div>
      <div class="kpi-val kpi-amber" id="kpiCompute">$0.0000</div>
      <div class="kpi-sub">ModelRouter & LLM Inference</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Net Capital ROI</div>
      <div class="kpi-val kpi-indigo" id="kpiRoi">0.0x</div>
      <div class="kpi-sub">Audited Yield Multiplier</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Net Profit & Margin</div>
      <div class="kpi-val kpi-green" id="kpiNetProfit">$0.00</div>
      <div class="kpi-sub" id="kpiProfitMargin">0.0% margin (after gas & compute)</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-title">Autonomous Cycle</div>
      <div class="kpi-val" id="kpiCycle">0.00s</div>
      <div class="kpi-sub" id="kpiCycleStatus">Ready</div>
    </div>
  </div>

  <!-- Main Content Grid -->
  <div class="main-grid">
    <!-- Opportunity Pipeline Panel -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Active Revenue Opportunities</span>
        <span class="panel-count" id="oppCount">0 Items</span>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>Platform</th>
              <th>Repo / Task</th>
              <th>Reward</th>
              <th>Stage</th>
            </tr>
          </thead>
          <tbody id="oppTableBody">
            <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Loading opportunities...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- AI Workforce Fleet Panel -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Autonomous Workforce Fleet</span>
        <span class="panel-count" id="workerCount">0 Agents</span>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>Agent</th>
              <th>Domain</th>
              <th>Status</th>
              <th>Load</th>
              <th>Quality</th>
            </tr>
          </thead>
          <tbody id="workerTableBody">
            <tr><td colspan="5" style="text-align: center; color: var(--text-muted);">Loading workforce...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Secondary Content Grid -->
  <div class="main-grid">
    <!-- Self-Improvement RFCs Panel -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Self-Improvement Governance (RFCs)</span>
        <span class="panel-count" id="rfcCount">0 Promoted</span>
      </div>
      <div class="table-container">
        <table>
          <thead>
            <tr>
              <th>RFC ID</th>
              <th>Title</th>
              <th>Subsystem</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="rfcTableBody">
            <tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Loading RFCs...</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- System Telemetry & Logs Panel -->
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">Autonomous Engine Telemetry</span>
        <span class="panel-count" id="sysVersion">v2.0-canary</span>
      </div>
      <div style="font-family: monospace; font-size: 12px; color: var(--text-muted); line-height: 1.8;" id="telemetryBox">
        Connecting to PrimeNode Control Gateway...
      </div>
    </div>
  </div>

  <script>
    let TOKEN = "{injected_token}" || sessionStorage.getItem("anti_bearer_token") || "";
    if (TOKEN) {{
      document.getElementById("tokenInput").value = TOKEN;
    }}

    function saveToken() {{
      const val = document.getElementById("tokenInput").value.trim();
      if (val) {{
        TOKEN = val;
        sessionStorage.setItem("anti_bearer_token", TOKEN);
        refreshAll();
      }}
    }}

    function getHeaders() {{
      const h = {{ "Content-Type": "application/json" }};
      if (TOKEN) {{
        h["Authorization"] = "Bearer " + TOKEN;
      }}
      return h;
    }}

    async function apiGet(endpoint) {{
      try {{
        const res = await fetch(endpoint, {{ headers: getHeaders() }});
        if (res.status === 401) {{
          document.getElementById("authBanner").style.borderColor = "var(--accent-red)";
          return null;
        }}
        return await res.json();
      }} catch (err) {{
        console.error("API error (" + endpoint + "):", err);
        return null;
      }}
    }}

    async function refreshAll() {{
      // 1. Health & Status
      const health = await apiGet("/api/v1/health");
      if (health) {{
        const isPaused = health.paused;
        const badge = document.getElementById("statusBadge");
        const text = document.getElementById("statusText");
        if (isPaused) {{
          badge.className = "status-badge paused";
          text.textContent = "SYSTEM PAUSED";
        }} else {{
          badge.className = "status-badge";
          text.textContent = "SYSTEM ACTIVE (" + (health.caller_role || "ANONYMOUS") + ")";
        }}
      }}

      // 2. Financial Summary
      const summary = await apiGet("/api/v1/revenue/summary");
      if (summary) {{
        document.getElementById("kpiConfirmed").textContent = "$" + (summary.confirmed_revenue_usd || 0).toFixed(2) + " USDC";
        document.getElementById("kpiPipeline").textContent = "$" + (summary.pipeline_potential_usd || 0).toFixed(2) + " USDC";
        document.getElementById("kpiCompute").textContent = "$" + (summary.total_compute_cost_usd || 0).toFixed(4);
        document.getElementById("kpiRoi").textContent = (summary.avg_settled_roi_ratio || 0).toFixed(1) + "x";
        document.getElementById("kpiOppCount").textContent = (summary.total_opportunities || 0) + " tracked bounties";
      }}

      // 2b. Financial P&L Ledger
      const finData = await apiGet("/api/v1/financial/pnl");
      if (finData && finData.summary) {{
        const pnl = finData.summary;
        document.getElementById("kpiNetProfit").textContent = "$" + pnl.net_profit_usd.toFixed(2);
        document.getElementById("kpiProfitMargin").textContent = pnl.net_profit_margin_pct.toFixed(1) + "% margin (gas: $" + pnl.total_gas_fees_usd.toFixed(4) + ")";
      }}

      // 3. Autonomous Status
      const autoStatus = await apiGet("/api/v1/autonomous/status");
      if (autoStatus && autoStatus.latest_cycle) {{
        const c = autoStatus.latest_cycle;
        document.getElementById("kpiCycle").textContent = c.duration_sec.toFixed(2) + "s";
        document.getElementById("kpiCycleStatus").textContent = "Scanned: " + c.scanned_count + " | Exec: " + c.executed_count + " | Settled: " + (c.settled_count || 0);

        document.getElementById("telemetryBox").innerHTML = `
          <div><strong>Last Cycle ID:</strong> ${{c.cycle_id}}</div>
          <div><strong>Execution Duration:</strong> ${{c.duration_sec.toFixed(4)}}s</div>
          <div><strong>Scanned Opportunities:</strong> ${{c.scanned_count}}</div>
          <div><strong>Eligible Bounties:</strong> ${{c.eligible_count}}</div>
          <div><strong>Kahn DAGs Executed:</strong> ${{c.executed_count}}</div>
          <div><strong>On-Chain Settled:</strong> ${{c.settled_count || 0}} ($${{c.confirmed_usd || 0}} USDC)</div>
          <div><strong>Self-Improvement RFCs:</strong> ${{c.rfcs_generated}} gen / ${{c.rfcs_promoted}} promoted</div>
          <div><strong>Cycle Status:</strong> <span style="color:var(--accent-green);">${{c.status}}</span></div>
        `;
      }}

      // 4. Opportunities List
      const oppsData = await apiGet("/api/v1/revenue/opportunities");
      if (oppsData && oppsData.opportunities) {{
        document.getElementById("oppCount").textContent = oppsData.count + " Items";
        const tbody = document.getElementById("oppTableBody");
        if (oppsData.opportunities.length === 0) {{
          tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">No active opportunities</td></tr>';
        }} else {{
          tbody.innerHTML = oppsData.opportunities.map(o => `
            <tr>
              <td><span class="badge badge-${{o.platform.toLowerCase()}}">${{o.platform}}</span></td>
              <td>
                <div style="font-weight: 600;">${{o.title}}</div>
                <div style="font-size: 11px; color: var(--text-muted);">${{o.target_repo}}</div>
              </td>
              <td style="font-weight: 700; color: var(--accent-cyan);">$${{o.raw_reward_usd.toFixed(2)}}</td>
              <td><span class="stage-badge stage-${{o.stage.split('_')[0]}}">${{o.stage}}</span></td>
            </tr>
          `).join('');
        }}
      }}

      // 5. Workforce Status
      const wfData = await apiGet("/api/v1/workforce/status");
      if (wfData && wfData.workers) {{
        document.getElementById("workerCount").textContent = wfData.count + " Agents";
        const tbody = document.getElementById("workerTableBody");
        tbody.innerHTML = wfData.workers.map(w => `
          <tr>
            <td>
              <div style="font-weight: 600;">${{w.worker_name}}</div>
              <div style="font-size: 11px; color: var(--text-muted);">${{w.worker_id}}</div>
            </td>
            <td><span style="font-size: 12px; color: var(--accent-indigo);">${{w.domain}}</span></td>
            <td><span class="badge" style="background:#1e293b; color: var(--accent-cyan);">${{w.state}}</span></td>
            <td style="width: 80px;">
              <div class="bar-bg">
                <div class="bar-fill" style="width: ${{Math.min(100, (w.current_load / w.max_load) * 100)}}%;"></div>
              </div>
            </td>
            <td style="color: var(--accent-green);">${{(w.quality_rate * 100).toFixed(0)}}%</td>
          </tr>
        `).join('');
      }}

      // 6. RFCs Status
      const rfcData = await apiGet("/api/v1/rfcs");
      if (rfcData && rfcData.rfcs) {{
        document.getElementById("rfcCount").textContent = rfcData.count + " Promoted";
        const tbody = document.getElementById("rfcTableBody");
        tbody.innerHTML = rfcData.rfcs.map(r => `
          <tr>
            <td><code>${{r.rfc_id}}</code></td>
            <td style="font-weight: 600;">${{r.title}}</td>
            <td><span style="color: var(--accent-cyan);">${{r.target_subsystem}}</span></td>
            <td><span class="stage-badge stage-CONFIRMED">${{r.stage}}</span></td>
          </tr>
        `).join('');
      }}
    }}

    async function triggerCycle() {{
      if (!confirm("Trigger an immediate Autonomous Cycle on PrimeNode?")) return;
      try {{
        await fetch("/api/v1/autonomous/cycle", {{ method: "POST", headers: getHeaders() }});
        setTimeout(refreshAll, 500);
      }} catch (err) {{
        alert("Cycle trigger failed: " + err);
      }}
    }}

    async function triggerDRDrill() {{
      if (!confirm("Execute full Disaster Recovery & WAL Integrity Drill across all databases?")) return;
      try {{
        const res = await fetch("/api/v1/dr/drill", {{ method: "POST", headers: getHeaders() }});
        const d = await res.json();
        alert("DR Drill Status: " + d.status + " in " + d.duration_ms + "ms\\nCertificate: " + (d.certificate_sha256 || "").slice(0, 16));
        refreshAll();
      }} catch (err) {{
        alert("DR Drill execution failed: " + err);
      }}
    }}

    async function triggerRebalance() {{
      if (!confirm("Rebalance autonomous workforce across all domains based on WorkerScore?")) return;
      try {{
        const res = await fetch("/api/v1/workforce/rebalance", {{ method: "POST", headers: getHeaders() }});
        const d = await res.json();
        alert("Workforce Rebalance: " + (d.actions_taken ? d.actions_taken.length : 0) + " actions taken.\\nAvg Score: " + (d.avg_composite_score || 0));
        refreshAll();
      }} catch (err) {{
        alert("Rebalance failed: " + err);
      }}
    }}

    async function triggerBackup() {{
      if (!confirm("Create atomic SQLite state backup archive across all databases?")) return;
      try {{
        const res = await fetch("/api/v1/backup/snapshot", {{ method: "POST", headers: getHeaders() }});
        const d = await res.json();
        alert("Backup Created: " + d.snapshot_id + "\\nSHA-256: " + (d.archive_sha256 || "").slice(0, 16) + "\\nSize: " + d.size_bytes + " bytes");
        refreshAll();
      }} catch (err) {{
        alert("Backup failed: " + err);
      }}
    }}

    async function triggerHourlyEval() {{
      if (!confirm("Run complete regression test gate and continuous self-improvement upgrade cycle now?")) return;
      try {{
        const res = await fetch("/api/v1/system/hourly-eval", {{ method: "POST", headers: getHeaders() }});
        const d = await res.json();
        alert("Hourly Eval & Upgrade: " + d.status + "\\nTests: " + (d.tests_passed ? "PASS" : "FAIL") + "\\nRFCs Promoted: " + d.rfcs_promoted + "\\nDuration: " + d.duration_sec + "s\\nCert: " + (d.certificate_sha256 || "").slice(0, 16));
        refreshAll();
      }} catch (err) {{
        alert("Hourly eval execution failed: " + err);
      }}
    }}

    async function togglePause() {{
      const pass = prompt("Enter Break-Glass Action ('PAUSE' or 'RESUME'):");
      if (pass === "PAUSE") {{
        await fetch("/api/v1/owner/killswitch", {{ method: "POST", headers: getHeaders() }});
      }} else if (pass === "RESUME") {{
        await fetch("/api/v1/owner/recover", {{ method: "POST", headers: getHeaders() }});
      }}
      setTimeout(refreshAll, 400);
    }}

    // Real-Time Server-Sent Events (SSE) Stream
    let eventSource = null;
    function connectEventStream() {{
      if (!window.EventSource) return;
      if (eventSource) eventSource.close();

      const url = "/api/v1/events" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "");
      try {{
        eventSource = new EventSource(url);
        eventSource.addEventListener("connected", () => console.log("SSE connected"));
        eventSource.addEventListener("CYCLE_COMPLETED", () => refreshAll());
        eventSource.addEventListener("PAYMENT_CONFIRMED", () => refreshAll());
        eventSource.addEventListener("DR_DRILL_COMPLETED", () => refreshAll());
        eventSource.addEventListener("WORKFORCE_REBALANCED", () => refreshAll());
        eventSource.addEventListener("SNAPSHOT_CREATED", () => refreshAll());
        eventSource.addEventListener("FINANCIAL_LEDGER_UPDATED", () => refreshAll());
        eventSource.addEventListener("HOURLY_CYCLE_COMPLETED", () => refreshAll());
        eventSource.onerror = () => {{
          eventSource.close();
          setTimeout(connectEventStream, 15000);
        }};
      }} catch (e) {{
        console.warn("EventSource setup skipped:", e);
      }}
    }}

    // Auto-refresh every 5 seconds & connect SSE stream
    setInterval(refreshAll, 5000);
    refreshAll();
    connectEventStream();
  </script>
</body>
</html>"""
