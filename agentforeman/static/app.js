"use strict";

const $ = (s) => document.querySelector(s);
const MIN = 60 * 1000;
const STUCK_MS = 10 * MIN;
const PARKED_MS = 6 * 60 * MIN;
const STALL_MS = 30 * MIN;
const TRACE_WINDOW_MS = 2 * 60 * MIN;

let S = null;
let offset = 0;
let lastUpdate = 0;
let prevNeeds = null;
let route = "overview";
let drawer = null; // { sid, aid } where aid is set for a subagent drawer
let drawerTab = "timeline";
let selected = null;
let confirmStop = null; // { aid, until } while a Stop button asks to confirm; aid is session:<id> for a session
let confirmSimilar = null; // { rid, until } while "Approve all" asks to confirm
let allowHint = null; // { rid, rule } after Always allow added a rule while rules are off
let ruleDraft = ""; // the rule typed on the Rules page, kept across live updates
const drafts = {}; // reply text per session, kept across live updates
const steerDrafts = {}; // steering note text per session or subagent
let TOKEN = document.querySelector('meta[name="foreman-token"]')?.content || "";
const filt = { q: "", project: "", status: "all" };
const sort = { key: "priority", dir: 1 };

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const now = () => Date.now() + offset;
const compact = () => innerWidth < 1400;
// The Sessions table keeps Model and Host for wide screens, so titles and activity get the room.
const wide = () => innerWidth >= 1600;

function fmt(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) return "-";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return m % 60 ? `${h}h ${m % 60}m` : `${h}h`;
  const d = Math.floor(h / 24);
  return h % 24 ? `${d}d ${h % 24}h` : `${d}d`;
}
const since = (ts) => (ts == null ? "-" : fmt(now() - ts));
const span = (a, b) => (a == null ? "-" : fmt((b ?? now()) - a));
const clock = (ts) => (ts == null ? "" : new Date(ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }));
const stamp = (ts) => (ts == null ? "No timestamp" : new Date(ts).toLocaleString());
const ktok = (n) => (n == null ? "-" : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${Math.round(n / 1e3)}k` : `${n}`);
const host = (a) => (a.entrypoint === "claude-vscode" ? "VS Code" : a.entrypoint === "cli" ? "Terminal" : a.entrypoint || "-");
const sum = (arr) => (arr || []).reduce((x, y) => x + y, 0);

function modelName(m) {
  if (!m) return "-";
  // Only a Claude model ID gets the short form. Any other name shows as it is.
  if (!/^claude-[a-z]/.test(m)) return m;
  const p = m.replace(/^claude-/, "").replace(/\[.*\]$/, "").split("-");
  const v = p.slice(1).filter((x) => /^\d{1,2}$/.test(x)).slice(0, 2).join(".");
  return p[0][0].toUpperCase() + p[0].slice(1) + (v ? ` ${v}` : "");
}

// Session triage state. The rank is the triage order.
const STATES = {
  input: { label: "Needs input", cls: "b-input", rank: 0, tip: "Turn ended. Waiting for your reply." },
  approval: { label: "Awaiting approval", cls: "b-approval", rank: 1, tip: "A tool call has no result yet. Likely a permission prompt." },
  stalled: { label: "Stalled", cls: "b-stalled", rank: 2, tip: "Busy, but no new event from the session or its subagents in 10+ min." },
  running: { label: "Running", cls: "b-running", rank: 3, tip: "Busy per Claude Code." },
  idle: { label: "Idle", cls: "b-idle", rank: 4, tip: "Nothing pending." },
  parked: { label: "Parked", cls: "b-parked", rank: 5, tip: "Turn ended over 6h ago. Kept out of the attention queue." },
};
const lastActive = (a) => a.last_activity_ms ?? a.last_entry_ms ?? a.since_ms;
function stateOf(a) {
  if (a.approval) return "approval";
  if (a.status === "waiting") return a.status_guess ? "approval" : "input";
  if (a.status === "your_turn") return a.since_ms != null && now() - a.since_ms > PARKED_MS ? "parked" : "input";
  if (a.status === "working") return lastActive(a) != null && now() - lastActive(a) > STUCK_MS ? "stalled" : "running";
  return "idle";
}
const needsYou = (a) => ["input", "approval"].includes(stateOf(a));
const badge = (k, extra = "") => `<span class="badge ${STATES[k].cls}" data-tip="${esc(STATES[k].tip)}">${STATES[k].label}${extra}</span>`;

// Subagent state, including teammates and failures.
const SUB = {
  running: ["Running", "b-running", "Working now."],
  stalled: ["Stalled", "b-stalled", "Marked running, but no file change in 30+ min."],
  done: ["Done", "b-done", "Finished."],
  failed: ["Failed", "b-error", "Finished with an error."],
  killed: ["Killed", "b-error", "Stopped before it finished."],
  ended: ["Ended", "b-done", "Quiet for 15+ min with no open tool call. Probably finished."],
  "tm-working": ["Teammate · working", "b-running", "An Agent Teams teammate that works now."],
  "tm-idle": ["Teammate · idle", "b-idle", "An Agent Teams teammate that waits for messages."],
  "tm-ended": ["Teammate · ended", "b-done", "The teammate's process ended."],
  stopping: ["Stopping", "b-stalled", "You stopped it. It ends at its next tool call."],
};
function subState(s) {
  if (s.stop_requested) return "stopping";
  if (s.state === "done") return s.kind === "teammate" ? "tm-ended" : SUB[s.outcome] ? s.outcome : "done";
  if (s.kind === "teammate") return s.state === "running" ? "tm-working" : "tm-idle";
  return now() - s.updated_ms > STALL_MS ? "stalled" : "running";
}
const subBadge = (s) => {
  const [l, c, t] = SUB[subState(s)];
  return `<span class="badge ${c}" data-tip="${esc(t)}">${l}</span>`;
};
const isWorkingSub = (s) => s.kind !== "teammate" && subState(s) === "running";
// The Running tile counts working teammates too.
const isWorking = (s) => isWorkingSub(s) || subState(s) === "tm-working";
const subTitle = (s) => s.description || s.name;

function repeatChip(r) {
  if (!r) return "";
  const what = `${r.tool} ${r.target}`;
  const tip = `${what} ran ${r.count} times in the last 8 tool calls, so it can be stuck in a loop. A steer note can change its course.`;
  return `<span class="flag-repeat" data-tip="${esc(tip)}">Repeating: ${esc(what)} x${esc(r.count)}</span>`;
}

function sharedChip(a) {
  const files = a.shared_files || [];
  if (!files.length) return "";
  const names = [...new Set(files.flatMap((f) => f.with.map((w) => w.name)))].join(", ");
  const tip = `${files.map((f) => f.path).join(", ")}: also edited by ${names} in the last 30 minutes.`;
  return `<span class="flag-repeat" data-tip="${esc(tip)}">Shared files: ${esc(files.length)}, with ${esc(names)}</span>`;
}

function actHtml(x, cls = "act") {
  if (!x) return `<span class="muted">-</span>`;
  const main = x.desc || x.summary || "";
  const tip = `${[x.tool, x.summary].filter(Boolean).join(": ")}${x.ts_ms ? ` · ${stamp(x.ts_ms)}` : ""}`;
  const err = x.error ? ' <span class="badge b-error" data-tip="The tool returned an error.">Error</span>' : "";
  const tool = x.tool ? `<span class="tool">${esc(x.tool)}</span>` : "";
  return `<div class="${cls}" data-tip="${esc(tip)}">${tool}<span class="${x.desc ? "txt" : "sum"}">${esc(main)}</span>${err}</div>`;
}

// The ask usually closes a reply, so rows show the end of its last paragraph.
function lastPara(msg, n = 160) {
  const paras = String(msg || "").split(/\n\s*\n/).map((p) => p.replace(/\s+/g, " ").trim()).filter(Boolean);
  const p = paras.length ? paras[paras.length - 1] : "";
  if (p.length <= n) return p;
  const cut = p.slice(-n);
  const sp = cut.indexOf(" ");
  return `...${sp > 0 && sp < 30 ? cut.slice(sp + 1) : cut}`;
}
const clip = (t, n = 600) => (t && t.length > n ? `${t.slice(0, n)}...` : t || "");
// The cell cuts from the left, so the closing question stays visible at any column width.
const msgCell = (m) => `<div class="act muted" data-tip="${esc(clip(m))}"><span class="txt tail"><bdi>${esc(lastPara(m, 400) || "-")}</bdi></span></div>`;

function spark(act, w = 120, h = 20) {
  const list = act && act.length ? act : new Array(30).fill(0);
  const max = Math.max(1, ...list);
  const bw = w / list.length;
  const bars = list
    .map((v, i) => {
      const bh = v ? Math.max(2, Math.round((v / max) * (h - 1))) : 1;
      return `<rect x="${(i * bw).toFixed(1)}" y="${h - bh}" width="${Math.max(1, bw - 1).toFixed(1)}" height="${bh}"${v ? "" : ' class="z"'}/>`;
    })
    .join("");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">${bars}</svg>`;
}

function matches(a) {
  if (filt.project && a.cwd !== filt.project) return false;
  if (!filt.q) return true;
  const hay = [a.title, a.name, a.project, a.cwd, a.last_message, a.last_prompt, a.model,
    ...(a.timeline || []).map((x) => `${x.tool} ${x.summary} ${x.desc}`),
    ...(a.subagents || []).flatMap((s) => [s.name, s.description, ...(s.recent_actions || []).map((x) => `${x.tool} ${x.summary} ${x.desc}`)])].join(" ").toLowerCase();
  return hay.includes(filt.q);
}
const agents = () => (S ? S.agents.filter(matches) : []);

function subtitle(a) {
  const repeat = a.name.toLowerCase().includes(a.project.toLowerCase());
  return repeat && a.last_prompt ? a.last_prompt : a.project;
}
// The session title is what the Claude Code tab shows. The short handle stays below it.
const nameOf = (a) => a.title || a.name;
const handle = (a) => (a.name.toLowerCase().includes(a.project.toLowerCase()) ? a.name : `${a.name} · ${a.project}`);
const sessionCell = (a) =>
  `<div class="cell-main" data-tip="${esc(`${nameOf(a)} · ${a.cwd}`)}">${esc(nameOf(a))}</div><div class="cell-sub" data-tip="${esc(a.last_prompt ? `Latest request: ${a.last_prompt}` : a.project)}">${esc(a.title ? handle(a) : subtitle(a))}</div>`;

function currentCell(a) {
  if (a.current_action) return actHtml(a.current_action);
  if (needsYou(a) && a.last_message) return msgCell(a.last_message);
  const run = (a.subagents || []).filter(isWorkingSub);
  if (run.length) {
    const s = run[0];
    return `<div class="act" data-tip="${esc(`${subTitle(s)}: ${s.last_action ? s.last_action.summary : "starting"}`)}"><span class="tool">Subagent</span><span class="txt">${esc(subTitle(s))}</span></div>`;
  }
  const last = (a.recent_actions || [])[0];
  return last ? `<div class="muted">${actHtml(last)}</div>` : `<span class="muted">-</span>`;
}

function subsCell(a) {
  const subs = a.subagents || [];
  if (!subs.length) return `<span data-tip="No subagents in the last 6h.">-</span>`;
  const run = subs.filter(isWorking).length;
  const tip = `${run} working, ${subs.length} shown, ${a.subagents_total ?? subs.length} changed in 6h.`;
  return `<span data-tip="${esc(tip)}">${run} / ${subs.length}</span>`;
}

function sorted(list) {
  const key = {
    // Inside a state, the session that entered it first comes first. That key changes only
    // when the state changes, so rows do not jump under the pointer on every tool call.
    priority: (a) => [STATES[stateOf(a)].rank, a.since_ms ?? Infinity, a.started_ms ?? Infinity, a.session_id],
    name: (a) => [nameOf(a).toLowerCase()],
    state: (a) => [STATES[stateOf(a)].rank],
    last: (a) => [-(lastActive(a) || 0)],
    since: (a) => [a.since_ms ?? Infinity],
    ctx: (a) => [-(a.context_tokens || 0)],
    calls: (a) => [-sum(a.team_activity)],
    subs: (a) => [-(a.subagents || []).filter(isWorkingSub).length],
  }[sort.key];
  return [...list].sort((x, y) => {
    const kx = key(x);
    const ky = key(y);
    for (let i = 0; i < kx.length; i++) if (kx[i] !== ky[i]) return (kx[i] < ky[i] ? -1 : 1) * sort.dir;
    return 0;
  });
}

function th(label, key, cls = "", tip = "") {
  const arrow = sort.key === key ? `<span class="arr">${sort.dir > 0 ? "↑" : "↓"}</span>` : "";
  return `<th class="sort ${cls}" data-sort="${key}"${tip ? ` data-tip="${esc(tip)}"` : ""}>${label}${arrow}</th>`;
}

const hth = (label, tip, cls = "") => `<th${cls ? ` class="${cls}"` : ""} data-tip="${esc(tip)}">${label}</th>`;

const FIRST_RUN = "No Claude Code sessions found. Start one, or run agentforeman demo to see sample sessions.";

function sessionsTable(list, mode) {
  // With no session at all and no filter, nothing failed to match, so say how to start.
  if (!list.length && !S.agents.length && !chipsActive()) return `<div class="empty">${FIRST_RUN}</div>`;
  if (!list.length) return `<div class="empty">No sessions match.${chipsActive() ? ' <button class="btn sm" data-clear>Clear filters</button>' : ""}</div>`;
  const full = mode === "full";
  const cols = [
    ["Status", "state", "", "Triage state.", 160],
    ["Session", "name", "", "Session title, with its short name and project below.", compact() ? 220 : 250],
    ["Current activity", null, "", "The open tool call, the reply that waits for you, or the newest call.", null],
    // The Overview table shares its row with the feed, so the sparkline stays on the Sessions page.
    ...(full ? [["30m", "calls", "", "Tool calls per minute, last 30 min, session and subagents.", 124]] : []),
    ["Subagents", "subs", "num", "Working / shown, last 6h.", 106],
    ["Last event", "last", "num", "Newest event from the session or a working subagent.", 96],
  ];
  if (full) {
    cols.push(["Since", "since", "num", "Time since the current status began.", 80]);
    // Narrow screens drop Context, so Current activity keeps room to read.
    if (!compact()) cols.push(["Context", "ctx", "num", wide() ? "Tokens read by the latest reply." : "Tokens read by the latest reply, with its model below.", 80]);
    if (wide()) cols.push(["Model", null, "", "Model of the latest reply.", 86], ["Host", null, "", "Where the session runs: VS Code or a terminal.", 80]);
  }
  const colgroup = `<colgroup>${cols.map((c) => `<col${c[4] ? ` style="width:${c[4]}px"` : ""}>`).join("")}</colgroup>`;
  const head = `<tr>${cols.map((c) => (c[1] ? th(c[0], c[1], c[2], c[3]) : `<th${c[3] ? ` data-tip="${esc(c[3])}"` : ""}>${c[0]}</th>`)).join("")}</tr>`;
  const rows = sorted(list)
    .map((a) => {
      const cells = [
        `<td>${badge(stateOf(a))}</td>`,
        `<td>${sessionCell(a)}</td>`,
        `<td>${a.repeating ? `<div class="cur">${repeatChip(a.repeating)}${currentCell(a)}</div>` : currentCell(a)}</td>`,
        ...(full ? [`<td data-tip="${esc(`${sum(a.team_activity)} tool calls in 30 min (${sum(a.activity)} by the session itself).`)}">${spark(a.team_activity)}</td>`] : []),
        `<td class="num">${subsCell(a)}</td>`,
        `<td class="num" data-tip="${esc(stamp(lastActive(a)))}">${since(lastActive(a))}</td>`,
      ];
      if (full) {
        cells.push(`<td class="num" data-tip="${esc(stamp(a.since_ms))}">${since(a.since_ms)}</td>`);
        // Without the Model column, the model goes under the token count.
        const model = wide() ? "" : `<div class="cell-sub">${esc(modelName(a.model))}</div>`;
        if (!compact()) cells.push(`<td class="num" data-tip="${esc(a.context_tokens ?? "No")} tokens read by the latest reply.">${esc(ktok(a.context_tokens))}${model}</td>`);
        if (wide()) cells.push(`<td>${esc(modelName(a.model))}</td>`, `<td class="host">${esc(host(a))}</td>`);
      }
      return `<tr data-sid="${esc(a.session_id)}" class="${a.session_id === selected ? "sel" : ""}">${cells.join("")}</tr>`;
    })
    .join("");
  return `<table class="grid sessions">${colgroup}<thead>${head}</thead><tbody>${rows}</tbody></table>`;
}

function openButton(a, cls = "btn sm", label = "Open") {
  // aria-disabled, not disabled: a disabled button gets no hover events, so its tooltip never shows.
  if (a.entrypoint === "cli") return `<button class="${cls}" aria-disabled="true" data-terminal data-tip="Terminal session. Switch to its terminal.">Terminal</button>`;
  return `<button class="${cls}" data-open="${esc(a.session_id)}" data-tip="Bring the VS Code window forward and show this session.">${label}</button>`;
}

function approvalButtons(a, cls = "btn sm") {
  const id = esc(a.approval.request_id);
  return `<button class="${cls} ok" data-approve="${id}" data-behavior="allow" data-tip="Let this tool call run.">Approve</button><button class="${cls} no" data-approve="${id}" data-behavior="deny" data-tip="Block this tool call. The session hears that you said no.">Deny</button>`;
}
// The row shows the command that runs, on one line. Its tooltip holds the whole text.
function approvalCell(a) {
  const r = a.approval;
  const cmd = r.detail || r.summary || "";
  const tool = r.tool ? `<span class="tool">${esc(r.tool)}</span>` : "";
  const desc = r.desc ? `<div class="cell-sub" data-tip="${esc(r.desc)}">${esc(r.desc)}</div>` : "";
  return `<div class="act" data-tip="${esc(cmd)}">${tool}<span class="sum">${esc(cmd.replace(/\s+/g, " "))}</span></div>${desc}`;
}
function rowActions(a) {
  if (a.approval) return approvalButtons(a);
  if (a.reply_ready && !a.reply_pending) return `<button class="btn sm" data-reply-open="${esc(a.session_id)}" data-tip="Write a reply. The session wakes up and reads it.">Reply</button>${openButton(a)}`;
  return openButton(a);
}
// The first two lines of a diff name the file. After them, the first character marks the line.
function diffHtml(diff) {
  const cls = (l, i) => (i < 2 && /^(---|\+\+\+) /.test(l) ? "d-file" : l.startsWith("@@") ? "d-hunk" : l[0] === "+" ? "d-add" : l[0] === "-" ? "d-del" : "");
  return `<pre class="ap-diff">${diff
    .split("\n")
    .map((l, i) => `<span class="${cls(l, i)}">${esc(l)}</span>`)
    .join("")}</pre>`;
}
function similarButton(r) {
  if (!r.similar) return "";
  const n = r.similar + 1;
  const asking = confirmSimilar && confirmSimilar.rid === r.request_id && confirmSimilar.until > Date.now();
  const tip = asking ? `Click again to approve all ${n} calls.` : `Approve this call and the ${r.similar} other waiting calls with the key ${r.rule_key}, in any session. A key names only the start of a command, so read the other calls first.`;
  return `<button class="btn ok${asking ? " on" : ""}" data-approve-similar="${esc(r.request_id)}" data-tip="${esc(tip)}">${asking ? `Confirm: approve all ${esc(n)}` : `Approve all ${esc(n)}`}</button>`;
}
function alwaysAllowButton(r) {
  // A key that grants too much, such as Bash(sudo apt), never becomes a rule, so it gets no button.
  if (!r.rule_ok) return "";
  const tip = `Add the rule ${r.rule_key}. While rules are on, matching calls run at once, even while AgentForeman is closed. This call still waits for your answer.`;
  return `<button class="btn" data-always-allow="${esc(r.rule_key)}" data-rid="${esc(r.request_id)}" data-tip="${esc(tip)}">Always allow ${esc(r.rule_key)}</button>`;
}
function approvalCard(a) {
  const r = a.approval;
  if (!r) return "";
  const sub = r.agent_id && (a.subagents || []).find((s) => s.agent_id === r.agent_id);
  const more = a.approvals_waiting > 1 ? ` · ${esc(a.approvals_waiting)} waiting` : "";
  const hint =
    allowHint && allowHint.rid === r.request_id && !(S.rules && S.rules.enabled)
      ? `<div class="ap-hint">Added ${esc(allowHint.rule)}. Rules are off, so it allows nothing yet. Turn them on in <a href="#rules">Rules</a>.</div>`
      : "";
  return `<div class="approval"><div class="ap-h"><span class="badge b-approval" data-tip="A permission prompt waits for an answer.">Approval needed</span><span class="tool">${esc(r.tool)}</span><span class="ap-desc">${esc(r.desc || r.summary)}</span><span class="muted" data-tip="${esc(stamp(r.created_ms))}">${since(r.created_ms)}${more}</span></div>
    ${sub ? `<div class="muted ap-who">Asked by the subagent ${esc(subTitle(sub))}</div>` : ""}${r.detail ? `<pre class="ap-cmd">${esc(r.detail)}</pre>` : ""}
    ${r.detail_cut ? `<div class="muted ap-cut">The hook stored only the first 4000 characters. The command that runs is longer.</div>` : ""}
    ${r.diff ? diffHtml(r.diff) : ""}${r.diff_note ? `<div class="muted ap-cut">${esc(r.diff_note)}</div>` : ""}
    <div class="ap-actions">${approvalButtons(a, "btn")}${similarButton(r)}${alwaysAllowButton(r)}<span class="muted">You can also answer in ${a.entrypoint === "cli" ? "the terminal" : "VS Code"}. The first answer wins.</span></div>${hint}</div>`;
}
const remoteLink = (a, label = "Open in Claude app") =>
  a.remote_url ? `<a class="btn" href="${esc(a.remote_url)}" target="_blank" rel="noopener" data-tip="Open this session's Remote Control page on claude.ai. You can reply and approve there as yourself.">${label}</a>` : "";
function replyBox(a) {
  if (a.reply_pending) return `<div class="reply sent"><span class="badge b-running" data-tip="The reply waits in the control folder.">Sent</span><span>The session picks up your reply within 2 seconds.</span></div>`;
  if (a.reply_ready) {
    const sid = esc(a.session_id);
    return `<div class="reply"><textarea data-reply="${sid}" rows="3" placeholder="Reply to ${esc(nameOf(a))}" aria-label="Reply">${esc(drafts[a.session_id] || "")}</textarea>
      <div class="reply-a"><span class="muted">Cmd+Enter sends. The session wakes up and reads your reply.</span><button class="btn primary" data-send="${sid}" data-tip="Send this reply to the session.">Send reply</button></div></div>`;
  }
  if (stateOf(a) !== "input") return "";
  const why = a.controls
    ? "The reply box opens when this session's next turn ends. A turn that ended before the install has no reply hook."
    : "Replies from here need the controls. Run agentforeman install.";
  return `<div class="reply off"><span class="muted">${why}</span>${remoteLink(a, "Reply in Claude app")}</div>`;
}
function stopButton(a, s, cls = "btn") {
  const can = s.backend === "tmux" ? subState(s) === "tm-working" : s.kind !== "teammate" && ["running", "stalled"].includes(subState(s));
  if (!can) return "";
  if (!a.controls) return `<button class="${cls} danger" aria-disabled="true" data-tip="Stop needs the controls. Run agentforeman install.">Stop</button>`;
  const asking = confirmStop && confirmStop.aid === s.agent_id && confirmStop.until > Date.now();
  return `<button class="${cls} danger${asking ? " on" : ""}" data-stop="${esc(s.agent_id)}" data-sid="${esc(a.session_id)}" data-tip="${asking ? "Click again to stop it." : "Stop this subagent at its next tool call. A command that already runs keeps running until it ends."}">${asking ? "Confirm stop" : "Stop"}</button>`;
}
function stopSessionButton(a) {
  if (!a.busy) return "";
  if (a.stop_requested) return `<button class="btn danger" aria-disabled="true" data-tip="You stopped it. The session ends its turn at its next tool call.">Stopping</button>`;
  if (!a.controls) return `<button class="btn danger" aria-disabled="true" data-tip="Stop needs the controls. Run agentforeman install.">Stop session</button>`;
  const asking = confirmStop && confirmStop.aid === `session:${a.session_id}` && confirmStop.until > Date.now();
  // An older hook never reads a session stop, so the tooltip asks for a new install.
  const old = S.controls_installed && !S.controls_installed.current ? " The installed hook is outdated: run agentforeman install again first." : "";
  const tip = asking ? "Click again to stop the session." : `End this session's turn at its next tool call. Until then, its subagents cannot call tools. A command that already runs keeps running until it ends. Split-pane teammates have their own Stop.${old}`;
  return `<button class="btn danger${asking ? " on" : ""}" data-stop-session="${esc(a.session_id)}" data-tip="${tip}">${asking ? "Confirm stop" : "Stop session"}</button>`;
}

// A note reaches the model as context at its next tool call. It never blocks that call.
function steerBox(id, label, note, live) {
  let sent = "";
  if (note && note.pending) sent = `<div class="steer sent" data-tip="${esc(note.text)}"><span class="badge b-running" data-tip="The note waits in the control folder.">Sent</span><span>Waits for its next tool call.</span></div>`;
  else if (note && note.delivered_ms) sent = `<div class="steer sent" data-tip="${esc(note.text || "")}"><span class="badge b-done" data-tip="${esc(stamp(note.delivered_ms))}">Delivered</span><span>Delivered at ${esc(clock(note.delivered_ms))}.</span></div>`;
  if (!live) return sent;
  const ci = S.controls_installed;
  const hint = !ci
    ? "Notes need the controls. Run agentforeman install."
    : !ci.current
      ? "Run agentforeman install again, so the hook can deliver notes."
      : "Cmd+Enter sends. It arrives at the next tool call and does not stop the work.";
  return `${sent}<div class="steer"><textarea data-steer="${esc(id)}" rows="2" placeholder="Note to ${esc(label)}" aria-label="Steering note">${esc(steerDrafts[id] || "")}</textarea>
    <div class="reply-a"><span class="muted">${hint}</span><button class="btn" data-steer-send="${esc(id)}" data-tip="Send this note. A newer note replaces one that waits.">Send note</button></div></div>`;
}

const byWait = (x, y) => (x.since_ms ?? Infinity) - (y.since_ms ?? Infinity);

function attentionPanel(list) {
  const need = list.filter(needsYou).sort(byWait);
  if (!need.length) return "";
  const rows = need
    .map((a) => `<tr data-sid="${esc(a.session_id)}">
      <td>${badge(stateOf(a))}</td><td>${sessionCell(a)}</td>
      <td class="num" data-tip="${esc(stamp(a.since_ms))}">${since(a.since_ms)}</td>
      <td>${a.approval ? approvalCell(a) : a.status_guess && a.current_action ? actHtml(a.current_action) : msgCell(a.last_message)}</td>
      <td class="num"><div class="row-actions">${rowActions(a)}</div></td></tr>`)
    .join("");
  return `<section class="panel attn"><div class="panel-h"><h2>Needs attention</h2><span class="count">${need.length} · oldest first</span></div>
    <table class="grid"><colgroup><col style="width:160px"><col style="width:250px"><col style="width:80px"><col><col style="width:176px"></colgroup>
    <thead><tr>${hth("Status", "Triage state.")}${hth("Session", "Session title, with its short name and project below.")}${hth("Waiting", "Time since the session started to wait for you.", "num")}${hth("Last message or pending call", "The end of the last reply, or the tool call that waits for approval.")}${hth("Action", "Approve or deny a waiting tool call, reply, or open the session.", "num")}</tr></thead><tbody>${rows}</tbody></table></section>`;
}

// Every tool call from sessions and their subagents, newest first.
function events(list) {
  const out = [];
  for (const a of list) {
    for (const x of a.timeline || []) out.push({ ...x, sid: a.session_id, source: nameOf(a), project: a.project });
    for (const s of a.subagents || []) {
      for (const x of s.recent_actions || []) out.push({ ...x, sid: a.session_id, aid: s.agent_id, source: `${nameOf(a)} › ${s.name}`, project: a.project });
    }
  }
  return out.filter((e) => e.ts_ms != null).sort((x, y) => y.ts_ms - x.ts_ms);
}

function feed(list, limit) {
  const ev = events(list).slice(0, limit);
  if (!ev.length) return `<div class="empty">No recent tool calls.</div>`;
  return `<ul class="feed">${ev
    .map((e) => `<li data-sid="${esc(e.sid)}"${e.aid ? ` data-aid="${esc(e.aid)}"` : ""}><time data-tip="${esc(stamp(e.ts_ms))}">${since(e.ts_ms)}</time>
      <div>${actHtml(e)}<div class="who">${esc(e.source)}${e.done ? "" : " · running"}</div></div></li>`)
    .join("")}</ul>`;
}

function eventTable(list, limit) {
  const ev = events(list).slice(0, limit);
  if (!ev.length) return `<div class="empty">No recent tool calls.</div>`;
  const rows = ev
    .map((e) => {
      const status = e.error
        ? '<span class="badge b-error" data-tip="The tool returned an error.">Error</span>'
        : e.done
          ? '<span class="badge b-done" data-tip="The tool returned a result.">OK</span>'
          : '<span class="badge b-running" data-tip="No result yet.">Running</span>';
      const dur = `<span data-tip="Time from the call to its result.">${e.end_ms ? fmt(e.end_ms - e.ts_ms) : e.done ? "-" : since(e.ts_ms)}</span>`;
      return `<tr data-sid="${esc(e.sid)}"${e.aid ? ` data-aid="${esc(e.aid)}"` : ""}><td class="num" data-tip="${esc(stamp(e.ts_ms))}">${clock(e.ts_ms)}</td>
        <td><div class="cell-main">${esc(e.source)}</div><div class="cell-sub">${esc(e.project)}</div></td><td><span class="tool">${esc(e.tool)}</span></td>
        <td>${actHtml({ ...e, tool: "", error: false }, "act bare")}</td><td class="num">${dur}</td><td>${status}</td></tr>`;
    })
    .join("");
  return `<table class="grid"><colgroup><col style="width:100px"><col style="width:260px"><col style="width:120px"><col><col style="width:80px"><col style="width:100px"></colgroup>
    <thead><tr>${hth("Time", "When the tool call started.", "num")}${hth("Source", "The session, or session and subagent, that made the call.")}${hth("Tool", "The tool that ran.")}${hth("Target", "The file, command, or pattern, led by its description when it has one.")}${hth("Duration", "Time from the call to its result.", "num")}${hth("Status", "OK, Error, or Running when no result came back yet.")}</tr></thead><tbody>${rows}</tbody></table>`;
}

function chipsActive() {
  return Boolean(filt.q || filt.project);
}
function chips() {
  if (!chipsActive()) return "";
  const c = [];
  if (filt.q) c.push(`<span class="chip">Search: ${esc(filt.q)} <button data-clear="q" aria-label="Clear search">×</button></span>`);
  if (filt.project) c.push(`<span class="chip">Project: ${esc(filt.project.split("/").pop())} <button data-clear="project" aria-label="Clear project">×</button></span>`);
  return `<div class="chips">${c.join("")}<button class="btn sm" data-clear>Clear all</button><span class="muted">Tables show filtered rows. Tiles count everything.</span></div>`;
}

function kpis() {
  const all = S.agents;
  const need = all.filter(needsYou).sort(byWait);
  const running = all.filter((a) => ["running", "stalled"].includes(stateOf(a)));
  const stalled = all.filter((a) => stateOf(a) === "stalled").length;
  const idle = all.filter((a) => stateOf(a) === "idle").length;
  const parked = all.filter((a) => stateOf(a) === "parked").length;
  const allSubs = all.flatMap((a) => a.subagents || []);
  const subRun = allSubs.filter(isWorking).length;
  const subStalled = allSubs.filter((s) => subState(s) === "stalled").length;
  const runNote = [stalled ? `${stalled} stalled` : "", `${subRun} subagents working`, subStalled ? `${subStalled} stalled` : ""].filter(Boolean).join(" · ");
  const agg = new Array(30).fill(0);
  all.forEach((a) => (a.team_activity || []).forEach((v, i) => (agg[i] += v)));
  const calls = sum(agg);
  const own = all.reduce((n, a) => n + sum(a.activity), 0);
  const vs = all.filter((a) => a.entrypoint === "claude-vscode").length;
  const tile = (k, v, s, tip, route_, filter, cls = "") =>
    `<button class="kpi ${cls}" data-go="${route_}" data-filter="${filter}" data-tip="${esc(tip)}"><span class="k">${k}</span><span class="v">${esc(v)}</span><span class="s">${s}</span></button>`;
  return `<section class="kpis">
    ${tile("Needs attention", need.length, need.length ? `Oldest ${since(need[0].since_ms)}` : "Queue clear", "Sessions waiting for your reply or approval. Click to list them.", "sessions", "attention", need.length ? "attn" : "")}
    ${tile("Running", running.length, runNote, `Sessions Claude Code marks busy. ${stalled} stalled sessions, ${subRun} subagents and teammates working, ${subStalled} subagents with no file change in 30+ min. Click to list the sessions.`, "sessions", "running", subStalled ? "warn" : "")}
    ${tile("Idle", idle + parked, parked ? `${parked} parked over 6h` : "-", "Sessions with nothing pending. Click to list them.", "sessions", "idle")}
    ${tile("Tool calls · 30m", calls, `${spark(agg, 140, 18)}`, `${calls} calls in 30 min: ${own} by sessions, ${calls - own} by subagents. Click for the event log.`, "activity", "all")}
    ${tile("Live sessions", all.length, `${vs} VS Code · ${all.length - vs} terminal`, "Claude Code processes alive on this Mac. Click to list them.", "sessions", "all")}
  </section>`;
}

// Files that two or more live sessions edited in the last 30 minutes, with every session.
function sharedPanel() {
  const byPath = new Map();
  for (const a of S.agents) {
    for (const f of a.shared_files || []) {
      if (!byPath.has(f.path)) byPath.set(f.path, new Map());
      const who = byPath.get(f.path);
      who.set(a.session_id, a.name);
      f.with.forEach((w) => who.set(w.session_id, w.name));
    }
  }
  if (!byPath.size) return "";
  const mode = (S.guard && S.guard.mode) || "warn";
  const guard = { off: "The guard is off.", warn: "The guard warns each session before it edits such a file.", block: "The guard asks you before a session edits such a file." }[mode];
  const rows = [...byPath]
    .sort((x, y) => (x[0] < y[0] ? -1 : 1))
    .map(([path, who]) => `<tr><td class="mono" data-tip="${esc(path)}">${esc(path)}</td><td data-tip="${esc([...who.values()].join(", "))}">${esc([...who.values()].join(", "))}</td></tr>`)
    .join("");
  return `<section class="panel shared-files"><div class="panel-h"><h2>Files that two sessions edit</h2><span class="count">${byPath.size} · last 30 min</span><div class="tools"><span class="muted">${esc(guard)}</span><a class="btn sm" href="#rules" data-tip="Choose off, warn, or block on the Rules page.">Guard</a></div></div>
    <table class="grid"><colgroup><col><col style="width:40%"></colgroup><thead><tr>${hth("File", "A file that two or more live sessions edited in the last 30 minutes.")}${hth("Sessions", "Every live session that edited it, with its subagents.")}</tr></thead><tbody>${rows}</tbody></table></section>`;
}

function viewOverview(list) {
  const others = list.filter((a) => !needsYou(a));
  return `<p class="lede">See every Claude Code session on this Mac, and approve, steer, or stop each one from this page.</p>
    ${kpis()}${chips()}${attentionPanel(list)}${sharedPanel()}
    <div class="split">
      <section class="panel"><div class="panel-h"><h2>Other sessions</h2><span class="count">${others.length} · running first</span><div class="tools"><a class="btn sm" href="#sessions" data-go="sessions" data-filter="all" data-tip="List every session.">View all</a></div></div>${sessionsTable(others, "overview")}</section>
      <section class="panel"><div class="panel-h"><h2>Live activity</h2><span class="count">sessions and subagents</span><div class="tools"><a class="btn sm" href="#activity" data-tip="Open the full event log.">View log</a></div></div>${feed(list, 30)}</section>
    </div>`;
}

function viewSessions(list) {
  const shown = list.filter((a) => {
    const k = stateOf(a);
    if (filt.status === "attention") return needsYou(a);
    if (filt.status === "running") return k === "running" || k === "stalled";
    if (filt.status === "idle") return k === "idle" || k === "parked";
    return true;
  });
  const seg = [["all", "All"], ["attention", "Needs attention"], ["running", "Running"], ["idle", "Idle"]]
    .map(([k, l]) => `<button data-status="${k}" class="${filt.status === k ? "on" : ""}">${l}</button>`)
    .join("");
  return `${chips()}<section class="panel"><div class="panel-h"><h2>Sessions</h2><span class="count">${shown.length} of ${list.length}</span><div class="tools"><div class="seg">${seg}</div></div></div>${sessionsTable(shown, "full")}</section>`;
}

// Newest start first. The server lists subagents by their latest file change, which moves
// rows on every tool call, so the page orders them by start time.
const byStart = (x, y) => (y.started_ms || 0) - (x.started_ms || 0) || (x.agent_id < y.agent_id ? -1 : 1);

// Orders subagents so each nested subagent follows the subagent that started it.
function nestSubs(all) {
  const subs = [...all].sort(byStart);
  const ids = new Set(subs.map((s) => s.agent_id));
  const kids = new Map();
  const top = [];
  for (const s of subs) {
    if (s.parent_agent_id && ids.has(s.parent_agent_id)) {
      if (!kids.has(s.parent_agent_id)) kids.set(s.parent_agent_id, []);
      kids.get(s.parent_agent_id).push(s);
    } else top.push(s);
  }
  const out = [];
  const walk = (s, depth) => {
    out.push({ s, depth });
    (kids.get(s.agent_id) || []).forEach((k) => walk(k, depth + 1));
  };
  top.forEach((s) => walk(s, 0));
  return out;
}

function subNameCell(s, depth, byId) {
  const parent = s.parent_agent_id && byId.get(s.parent_agent_id);
  const bits = [];
  if (s.description && s.name !== s.description) bits.push(s.name);
  if (s.agent_type && s.agent_type !== s.name) bits.push(s.agent_type);
  if (s.backend === "tmux") bits.push("split pane");
  if (parent) bits.push(`started by ${subTitle(parent)}`);
  const pad = depth ? ` style="padding-left:${depth * 18}px"` : "";
  const mark = depth ? `<span class="tree">└</span>` : "";
  return `<div${pad}><div class="cell-main" data-tip="${esc(subTitle(s))}">${mark}${esc(subTitle(s))}</div><div class="cell-sub">${esc(bits.join(" · ") || "agent")}</div></div>`;
}

function subRows(a, cols) {
  const byId = new Map((a.subagents || []).map((s) => [s.agent_id, s]));
  return nestSubs(a.subagents || [])
    .map(({ s, depth }) => {
      const cells = {
        status: `<td>${subBadge(s)}</td>`,
        name: `<td>${subNameCell(s, depth, byId)}</td>`,
        started: `<td class="num" data-tip="${esc(stamp(s.started_ms))}">${since(s.started_ms)}</td>`,
        duration: `<td class="num" data-tip="From start to ${s.ended_ms ? "finish" : "now"}.">${span(s.started_ms, s.ended_ms || (subState(s) === "running" ? null : s.updated_ms))}</td>`,
        calls: `<td class="num" data-tip="Tool calls in the transcript tail.">${esc(s.tool_calls ?? "-")}</td>`,
        last: `<td>${actHtml(s.last_action)}</td>`,
        model: `<td>${esc(modelName(s.model))}</td>`,
        updated: `<td class="num" data-tip="${esc(stamp(s.updated_ms))}">${since(s.updated_ms)}</td>`,
        action: `<td class="num">${stopButton(a, s, "btn sm")}</td>`,
      };
      return `<tr data-sid="${esc(a.session_id)}" data-aid="${esc(s.agent_id)}" class="${depth ? "child" : ""}">${cols.map((c) => cells[c]).join("")}</tr>`;
    })
    .join("");
}

function viewSubagents(list) {
  const groups = list
    .filter((a) => (a.subagents || []).length)
    .map((a) => ({ a, latest: Math.max(...a.subagents.map((s) => s.started_ms || 0)) }))
    .sort((x, y) => y.latest - x.latest || (x.a.session_id < y.a.session_id ? -1 : 1));
  const shown = groups.reduce((n, g) => n + g.a.subagents.length, 0);
  if (!shown) return `${chips()}<section class="panel"><div class="panel-h"><h2>Subagents</h2></div><div class="empty">No subagents in the last 6h.</div></section>`;
  const cols = compact() ? ["status", "name", "started", "duration", "calls", "last", "action"] : ["status", "name", "started", "duration", "calls", "last", "model", "action"];
  // A narrow window gives the name less room, so the last action keeps its tool name and the
  // status keeps "Teammate · working" whole.
  const widths = compact()
    ? { status: 168, name: 244, started: 64, duration: 74, calls: 54, last: null, action: 118 }
    : { status: 180, name: 300, started: 80, duration: 84, calls: 70, last: null, model: 86, action: 120 };
  const heads = { status: "Status", name: "Subagent", started: "Started", duration: "Duration", calls: "Calls", last: "Last action", model: "Model", action: "Action" };
  const tips = {
    status: "Subagent state.",
    name: "The subagent's task, with its name, type, and parent below.",
    started: "Time since it started.",
    duration: "From start to finish, or to now.",
    calls: "Tool calls in its transcript.",
    last: "Its newest tool call.",
    model: "Model of the subagent.",
    action: "Stop a running subagent.",
  };
  const body = groups
    .map(({ a }) => {
      // Working teammates count too, so the header agrees with their badges below.
      const run = a.subagents.filter(isWorking).length;
      const total = a.subagents_total ?? a.subagents.length;
      const cut = total > a.subagents.length ? ` · ${a.subagents.length} of ${esc(total)} shown` : "";
      return `<tr class="group" data-sid="${esc(a.session_id)}"><td colspan="${cols.length}">${badge(stateOf(a))}<span class="cell-main">${esc(nameOf(a))}</span><span class="muted">${esc(a.title ? `${a.name} · ` : "")}${esc(a.project)} · ${run} working${cut}</span></td></tr>${subRows(a, cols)}`;
    })
    .join("");
  return `${chips()}<section class="panel"><div class="panel-h"><h2>Subagents</h2><span class="count">${shown} in ${groups.length} sessions · last 6h</span></div>
    <table class="grid"><colgroup>${cols.map((c) => `<col${widths[c] ? ` style="width:${widths[c]}px"` : ""}>`).join("")}</colgroup>
    <thead><tr>${cols.map((c) => hth(heads[c], tips[c], ["started", "duration", "calls", "action"].includes(c) ? "num" : "")).join("")}</tr></thead><tbody>${body}</tbody></table></section>`;
}

function viewActivity(list) {
  const agg = new Array(30).fill(0);
  list.forEach((a) => (a.team_activity || []).forEach((v, i) => (agg[i] += v)));
  const total = sum(agg);
  const peak = Math.max(0, ...agg);
  const errors = events(list).filter((e) => e.error).length;
  return `${chips()}<section class="panel"><div class="panel-h"><h2>Tool calls per minute</h2><span class="count">${esc(total)} in 30m · peak ${esc(peak)}/min · ${errors} errors in view</span></div>
      <div class="chart" data-tip="Sessions and subagents combined. One bar per minute."><div class="yaxis"><span>${esc(peak)}</span><span>0</span></div>${spark(agg, 1000, 90).replace('width="1000"', 'width="100%"')}</div>
      <div class="axis"><span>30m ago</span><span>15m</span><span>now</span></div></section>
    <section class="panel"><div class="panel-h"><h2>Event log</h2><span class="count">latest tool calls, newest first</span></div>${eventTable(list, 300)}</section>`;
}

const NO_RULES = { enabled: false, items: [], suggestions: [], recent: [] };
const GUARD = {
  off: ["Off", "Sessions edit any file without a word from AgentForeman."],
  warn: ["Warn", "Before a session edits a file that another live session edited in the last 30 minutes, it reads which session that was."],
  block: ["Block", "Before such an edit, Claude Code asks you, and the dialog names the other session."],
};

function viewRules() {
  const R = S.rules || NO_RULES;
  const mode = (S.guard && S.guard.mode) || "warn";
  const sessionName = (sid) => (S.agents.find((a) => a.session_id === sid) || {}).name || sid || "-";
  const sw = `<button class="switch${R.enabled ? " on" : ""}" role="switch" aria-checked="${esc(R.enabled)}" data-rules-enabled data-tip="${R.enabled ? "Turn rules off. Every call goes to the page again." : "Turn rules on."}"><span class="knob"></span>${R.enabled ? "Rules on" : "Rules off"}</button>`;
  const items = R.items.length
    ? `<table class="grid"><colgroup><col><col style="width:110px"><col style="width:80px"><col style="width:100px"></colgroup><thead><tr>${hth("Rule", "The calls that this rule allows.")}${hth("Added", "When the rule was added.", "num")}${hth("Hits", "Calls that this rule allowed.", "num")}${hth("Action", "Remove the rule.", "num")}</tr></thead><tbody>${R.items
        .map((i) => `<tr><td class="mono" data-tip="${esc(i.rule)}">${esc(i.rule)}</td><td class="num" data-tip="${esc(stamp(i.added_ms))}">${since(i.added_ms)}</td><td class="num">${esc(i.hits)}</td><td class="num"><button class="btn sm" data-rule-remove="${esc(i.rule)}" data-tip="Remove this rule. Its calls go to the page again.">Remove</button></td></tr>`)
        .join("")}</tbody></table>`
    : `<div class="empty">No rules yet. Click Always allow on an approval card, or add a rule below.</div>`;
  const add = `<div class="rule-add"><input data-rule-input value="${esc(ruleDraft)}" placeholder="Bash(npm test) or Edit(/path/to/project/src/)" aria-label="New rule"><button class="btn" data-rule-add-input data-tip="Add this rule. Adding never turns rules on.">Add rule</button></div>`;
  const sug = R.suggestions.length
    ? `<table class="grid"><colgroup><col><col style="width:110px"><col style="width:100px"></colgroup><thead><tr>${hth("Rule", "A rule that would allow these calls.")}${hth("Approvals", "Times you approved calls with this rule key.", "num")}${hth("Action", "Add the rule.", "num")}</tr></thead><tbody>${R.suggestions
        .map((s) => `<tr><td class="mono" data-tip="${esc(s.rule)}">${esc(s.rule)}</td><td class="num">${esc(s.approvals)}</td><td class="num"><button class="btn sm" data-rule-add="${esc(s.rule)}" data-tip="Add this rule.">Add</button></td></tr>`)
        .join("")}</tbody></table>`
    : `<div class="empty">No suggestions. A call that you approve 3 times shows here.</div>`;
  const recent = R.recent.length
    ? `<table class="grid"><colgroup><col style="width:100px"><col style="width:200px"><col style="width:110px"><col><col style="width:220px"></colgroup><thead><tr>${hth("Time", "When the rule allowed the call.", "num")}${hth("Session", "The session that made the call.")}${hth("Tool", "The tool of the call.")}${hth("Call", "The command, file, or URL.")}${hth("Rule", "The rule that allowed it.")}</tr></thead><tbody>${R.recent
        .map((e) => `<tr><td class="num" data-tip="${esc(stamp(e.at_ms))}">${clock(e.at_ms)}</td><td>${esc(sessionName(e.session_id))}</td><td><span class="tool">${esc(e.tool_name)}</span></td><td class="mono" data-tip="${esc(e.summary)}">${esc(e.summary)}</td><td class="mono">${esc(e.rule)}</td></tr>`)
        .join("")}</tbody></table>`
    : `<div class="empty">No call was allowed by a rule yet.</div>`;
  const seg = Object.entries(GUARD)
    .map(([k, [l, t]]) => `<button data-guard="${k}" class="${mode === k ? "on" : ""}" data-tip="${esc(t)}">${l}</button>`)
    .join("");
  return `<section class="panel"><div class="panel-h"><h2>Always allow rules</h2><span class="count">${R.items.length} rules</span><div class="tools">${sw}</div></div>
      <p class="note">Rules approve matching tool calls at once, even while AgentForeman is closed. They are off until you turn them on here.</p>${items}${add}</section>
    <section class="panel"><div class="panel-h"><h2>Suggested rules</h2><span class="count">keys you approved 3 or more times</span></div>${sug}</section>
    <section class="panel"><div class="panel-h"><h2>Calls that rules allowed</h2><span class="count">newest first, last 20</span></div>${recent}</section>
    <section class="panel"><div class="panel-h"><h2>Same-file guard</h2><span class="count">two sessions, one file</span><div class="tools"><div class="seg">${seg}</div></div></div>
      <p class="note">${esc(GUARD[mode][1])}</p></section>`;
}

const CODEX = {
  completed: ["Completed", "b-done", "The last turn finished."],
  failed: ["Failed", "b-error", "The last turn failed."],
  interrupted: ["Interrupted", "b-idle", "The last turn was stopped."],
  inProgress: ["In progress", "b-running", "The last turn still runs."],
};
function viewCodex() {
  let t = (S && S.codex) || [];
  t = t.filter((x) => (!filt.project || x.cwd === filt.project) && (!filt.q || `${x.title} ${x.cwd} ${x.nickname}`.toLowerCase().includes(filt.q)));
  if (!t.length) return `${chips()}<section class="panel"><div class="panel-h"><h2>Codex threads</h2></div><div class="empty">No Codex threads changed in 48h.</div></section>`;
  const ids = new Set(t.map((x) => x.thread_id));
  const kids = new Map();
  const top = [];
  for (const x of t) {
    if (x.parent_thread_id && ids.has(x.parent_thread_id)) {
      if (!kids.has(x.parent_thread_id)) kids.set(x.parent_thread_id, []);
      kids.get(x.parent_thread_id).push(x);
    } else top.push(x);
  }
  const row = (x, child) => {
    const st = Object.hasOwn(CODEX, x.status) ? CODEX[x.status] : null;
    const b = st ? `<span class="badge ${st[1]}" data-tip="${st[2]}">${st[0]}</span>` : `<span class="muted" data-tip="No turn record for this thread.">-</span>`;
    const name = child ? `<span class="tree">└</span>${esc(x.nickname || "Subagent")}` : esc(x.title || "Untitled");
    const sub = child ? "Codex subagent" : x.source === "subagent" ? "Codex subagent" : esc((x.cwd || "").split("/").pop());
    return `<tr class="${child ? "child" : ""}"><td>${b}</td><td><div${child ? ' style="padding-left:18px"' : ""}><div class="cell-main" data-tip="${esc(x.title)}">${name}</div><div class="cell-sub" data-tip="${esc(x.cwd)}">${sub}</div></div></td><td>${esc(x.model || "-")}</td><td class="num" data-tip="${esc(stamp(x.updated_ms))}">${since(x.updated_ms)}</td></tr>`;
  };
  const rows = top.map((x) => row(x, false) + (kids.get(x.thread_id) || []).map((k) => row(k, true)).join("")).join("");
  return `${chips()}<section class="panel"><div class="panel-h"><h2>Codex threads</h2><span class="count">${t.length} · last 48h</span></div>
    <table class="grid"><colgroup><col style="width:130px"><col><col style="width:140px"><col style="width:100px"></colgroup>
    <thead><tr>${hth("Last turn", "Status of the thread's newest turn.")}${hth("Thread", "Thread title, or the nickname of a Codex subagent.")}${hth("Model", "Model of the thread.")}${hth("Updated", "Time since the thread last changed.", "num")}</tr></thead><tbody>${rows}</tbody></table></section>`;
}

function timelineHtml(acts, emptyText) {
  return `<ul class="timeline">${acts
    .map((x) => {
      // actHtml already shows an Error badge.
      const state = x.done ? "" : '<span class="badge b-running" data-tip="No result yet.">Running</span>';
      const dur = x.end_ms ? `<span class="dur">${fmt(x.end_ms - x.ts_ms)}</span>` : "";
      return `<li class="${x.done ? "" : "open"}"><time data-tip="${esc(stamp(x.ts_ms))}">${clock(x.ts_ms)}</time><span class="node"></span><div class="tl-body">${actHtml(x)}${state}${dur}</div></li>`;
    })
    .join("") || `<li><span></span><span></span><span class="muted">${emptyText}</span></li>`}</ul>`;
}

// One bar per tool call and per subagent, on a shared clock.
function traceHtml(a) {
  const t1 = now();
  const items = [];
  for (const x of a.timeline || []) {
    if (!x.ts_ms || x.tool === "Agent" || x.tool === "Task") continue;
    items.push({ kind: "tool", label: x.tool, text: x.desc || x.summary, start: x.ts_ms, end: x.end_ms || (x.done ? x.ts_ms : t1), open: !x.done, error: x.error, depth: 0 });
  }
  for (const { s, depth } of nestSubs(a.subagents || [])) {
    if (!s.started_ms) continue;
    const st = subState(s);
    const end = s.ended_ms || (["running", "tm-working"].includes(st) ? t1 : s.updated_ms);
    items.push({ kind: "agent", label: s.kind === "teammate" ? "Teammate" : "Subagent", text: subTitle(s), start: s.started_ms, end, open: ["running", "tm-working"].includes(st), error: ["failed", "killed"].includes(st), depth, aid: s.agent_id });
  }
  if (!items.length) return `<div class="empty">No timed events in the last 2h.</div>`;
  const t0 = Math.max(Math.min(...items.map((i) => i.start)), t1 - TRACE_WINDOW_MS);
  const range = Math.max(1, t1 - t0);
  items.sort((x, y) => x.start - y.start);
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => `<span style="left:${f * 100}%">${clock(t0 + f * range).replace(/:\d\d(\s|$)/, "$1")}</span>`).join("");
  const rows = items
    .filter((i) => i.end >= t0)
    .map((i) => {
      const left = Math.max(0, ((i.start - t0) / range) * 100);
      const width = Math.max(0.6, ((Math.min(i.end, t1) - Math.max(i.start, t0)) / range) * 100);
      const cls = `bar ${i.kind}${i.open ? " open" : ""}${i.error ? " err" : ""}`;
      const tip = `${i.label}: ${i.text}. Started ${stamp(i.start)}. ${i.open ? "Still running." : `Took ${fmt(i.end - i.start)}.`}`;
      return `<div class="tr-row"${i.aid ? ` data-aid="${esc(i.aid)}" data-sid="${esc(a.session_id)}"` : ""}><div class="tr-label" style="padding-left:${i.depth * 14}px"><span class="tool">${esc(i.label)}</span><span class="txt">${esc(i.text)}</span></div><div class="track"><span class="${cls}" style="left:${left.toFixed(2)}%;width:${width.toFixed(2)}%" data-tip="${esc(tip)}"></span></div><div class="tr-dur">${i.open ? "running" : fmt(i.end - i.start)}</div></div>`;
    })
    .join("");
  return `<div class="trace"><div class="tr-axis"><div></div><div class="ticks">${ticks}</div><div></div></div>${rows}</div>`;
}

function sessionDrawer(a) {
  const subs = a.subagents || [];
  const tabs = [["timeline", "Timeline"], ["trace", "Trace"], ["subagents", `Subagents (${subs.length})`], ["details", "Details"]]
    .map(([t, l]) => `<button data-tab="${t}" class="${drawerTab === t ? "on" : ""}">${l}</button>`)
    .join("");
  let body;
  if (drawerTab === "trace") body = traceHtml(a);
  else if (drawerTab === "subagents") {
    body = subs.length
      ? `<table class="grid"><colgroup><col style="width:180px"><col><col style="width:84px"><col style="width:64px"></colgroup><thead><tr>${hth("Status", "Subagent state.")}${hth("Subagent", "The subagent's task, with its name and type below.")}${hth("Duration", "From start to finish, or to now.", "num")}${hth("Calls", "Tool calls in its transcript.", "num")}</tr></thead><tbody>${subRows(a, ["status", "name", "duration", "calls"])}</tbody></table>`
      : `<div class="empty">No subagents in the last 6h.</div>`;
  } else if (drawerTab === "details") {
    body = `<dl class="kv"><dt>Folder</dt><dd>${esc(a.cwd)}</dd><dt>Session ID</dt><dd>${esc(a.session_id)}</dd><dt>Process</dt><dd>${esc(a.pid)}</dd>
      <dt>Host</dt><dd>${esc(host(a))}</dd><dt>Model</dt><dd>${esc(a.model || "-")}</dd><dt>Context</dt><dd>${esc(a.context_tokens ?? "-")} tokens</dd>
      <dt>Started</dt><dd>${esc(stamp(a.started_ms))}</dd><dt>Transcript</dt><dd>${esc(a.transcript || "Not found")}</dd></dl>`;
  } else {
    body = `${a.last_prompt ? `<div class="msg"><div class="msg-h">Latest request</div>${esc(a.last_prompt)}</div>` : ""}
      ${a.last_message ? `<div class="msg"><div class="msg-h">Latest reply</div>${esc(a.last_message)}</div>` : ""}
      ${replyBox(a)}
      ${steerBox(a.session_id, nameOf(a), a.note, a.busy)}
      ${timelineHtml(a.timeline || [], "No tool calls in the transcript tail.")}`;
  }
  const run = subs.filter(isWorkingSub).length;
  return `<div class="d-head">
      <div class="d-title">${badge(stateOf(a), run && stateOf(a) === "running" ? ` · ${run} subagents` : "")}<h3 data-tip="${esc(nameOf(a))}">${esc(nameOf(a))}</h3><button class="btn sm x" data-close aria-label="Close" data-tip="Close the panel (Esc).">Close</button></div>
      <div class="d-meta">${a.title ? `<span>${esc(a.name)}</span>` : ""}<span>${esc(a.project)}</span><span>${esc(host(a))}</span><span>${esc(modelName(a.model))}</span><span>${esc(ktok(a.context_tokens))} context</span><span>Since ${since(a.since_ms)}</span></div>
      ${a.repeating || sharedChip(a) ? `<div class="d-flags">${repeatChip(a.repeating)}${sharedChip(a)}</div>` : ""}
      <div class="d-actions">${openButton(a, "btn primary", "Open in VS Code")}${remoteLink(a)}<button class="btn" data-copy="${esc(a.session_id)}" data-tip="${esc(a.session_id)}">Copy session ID</button>${stopSessionButton(a)}</div>
      ${approvalCard(a)}
    </div><div class="tabs">${tabs}</div><div class="d-body">${body}</div>`;
}

function subagentDrawer(a, s) {
  const byId = new Map((a.subagents || []).map((x) => [x.agent_id, x]));
  const parent = s.parent_agent_id && byId.get(s.parent_agent_id);
  const done = !["running", "stalled", "tm-working"].includes(subState(s));
  return `<div class="d-head">
      <div class="d-title">${subBadge(s)}<h3 data-tip="${esc(subTitle(s))}">${esc(subTitle(s))}</h3><button class="btn sm x" data-close aria-label="Close" data-tip="Close the panel.">Close</button></div>
      <div class="d-meta">${s.name !== subTitle(s) ? `<span>${esc(s.name)}</span>` : ""}<span>${esc(s.agent_type || "agent")}</span><span>${esc(modelName(s.model))}</span><span>Started ${since(s.started_ms)} ago</span><span>Duration ${span(s.started_ms, s.ended_ms || (done ? s.updated_ms : null))}</span><span>${esc(s.tool_calls ?? 0)} tool calls</span></div>
      ${s.repeating ? `<div class="d-flags">${repeatChip(s.repeating)}</div>` : ""}
      <div class="d-actions"><button class="btn" data-back="${esc(a.session_id)}" data-tip="Show ${esc(nameOf(a))}, the session that started this subagent (Esc).">Back to ${esc(clip(nameOf(a), 40))}</button>${parent ? `<button class="btn" data-sub="${esc(parent.agent_id)}" data-sid="${esc(a.session_id)}" data-tip="Show the subagent that started this one.">Parent: ${esc(subTitle(parent))}</button>` : ""}${stopButton(a, s)}</div>
    </div><div class="d-body">
      ${s.last_message ? `<div class="msg"><div class="msg-h">${done ? "Result" : "Latest message"}</div>${esc(s.last_message)}</div>` : ""}
      ${steerBox(s.agent_id, subTitle(s), s.note, s.state === "running")}
      ${timelineHtml(s.recent_actions || [], "No tool calls yet.")}
    </div>`;
}

const TITLES = { overview: "Overview", sessions: "Sessions", subagents: "Subagents", activity: "Activity", codex: "Codex", rules: "Rules" };

function render() {
  if (!S) return;
  const list = agents();
  $("#page-title").textContent = TITLES[route];
  document.querySelectorAll("#nav a").forEach((el) => el.classList.toggle("on", el.dataset.route === route));
  const need = S.agents.filter(needsYou).length;
  const navS = $("#nav-sessions");
  navS.textContent = S.agents.length;
  navS.classList.toggle("attn", need > 0);
  navS.dataset.tip = `${S.agents.length} live sessions${need ? `, ${need} need attention` : ""}.`;
  const allSubs = S.agents.flatMap((a) => a.subagents || []);
  $("#nav-subagents").textContent = allSubs.length || "";
  $("#nav-subagents").dataset.tip = `${allSubs.length} subagents in the last 6h, ${allSubs.filter(isWorkingSub).length} working.`;
  $("#nav-codex").textContent = (S.codex || []).length || "";
  $("#nav-codex").dataset.tip = `${(S.codex || []).length} Codex threads changed in 48h.`;
  const rules = S.rules || NO_RULES;
  $("#nav-rules").textContent = rules.enabled ? "On" : "";
  $("#nav-rules").dataset.tip = `Rules are ${rules.enabled ? "on" : "off"}. ${rules.items.length} rules.`;
  const ci = S.controls_installed;
  const pill = $("#ctl-state");
  pill.textContent = ci ? (ci.current ? "Controls on" : "Controls outdated") : "Controls off";
  pill.classList.toggle("on", Boolean(ci && ci.current));
  pill.dataset.tip = ci
    ? ci.current
      ? `Approve, reply, and stop are on. Installed ${new Date(ci.installed_ms).toLocaleString()}.`
      : "The hook changed since the install. Run agentforeman install again."
    : "Run agentforeman install to approve, reply, and stop from here.";
  const view = { overview: viewOverview, sessions: viewSessions, subagents: viewSubagents, activity: viewActivity, codex: viewCodex, rules: viewRules }[route];
  const scrolls = [...document.querySelectorAll("#content .feed")].map((f) => f.scrollTop);
  const focus = focusKey();
  $("#content").innerHTML = view(list);
  document.querySelectorAll("#content .feed").forEach((f, i) => (f.scrollTop = scrolls[i] || 0));
  syncProjects();
  renderDrawer();
  restoreFocus(focus);
  retip();
  document.title = need ? `(${need}) AgentForeman` : "AgentForeman";
}

// A rebuild replaces every element, so focus moves back to the element with the same data keys.
const FOCUS_ATTRS = ["data-reply", "data-steer", "data-rule-input", "data-rules-enabled", "data-guard", "data-go", "data-filter", "data-sort", "data-status", "data-tab", "data-open", "data-terminal", "data-clear", "data-sub", "data-back", "data-copy", "data-close", "data-sid", "data-aid", "href"];
function focusKey() {
  const el = document.activeElement;
  if (!el || !el.closest("#content, #drawer")) return null;
  const sel = FOCUS_ATTRS.filter((n) => el.hasAttribute(n)).map((n) => `[${n}="${CSS.escape(el.getAttribute(n))}"]`).join("");
  if (!sel) return null;
  const caret = el.matches("textarea, input") ? [el.selectionStart, el.selectionEnd, el.scrollTop] : null;
  return { root: el.closest("#drawer") ? "#drawer" : "#content", sel: el.tagName.toLowerCase() + sel, caret };
}
function restoreFocus(k) {
  if (!k) return;
  const el = document.querySelector(`${k.root} ${k.sel}`);
  if (!el) return;
  el.focus({ preventScroll: true });
  if (k.caret) {
    el.setSelectionRange(k.caret[0], k.caret[1]);
    el.scrollTop = k.caret[2];
  }
}

function renderDrawer() {
  const d = $("#drawer");
  const a = drawer && S && S.agents.find((x) => x.session_id === drawer.sid);
  const s = a && drawer.aid ? (a.subagents || []).find((x) => x.agent_id === drawer.aid) : null;
  if (!a || (drawer.aid && !s)) {
    d.hidden = true;
    $("#scrim").hidden = true;
    return;
  }
  const body = d.querySelector(".d-body");
  const top = body ? body.scrollTop : 0;
  d.innerHTML = s ? subagentDrawer(a, s) : sessionDrawer(a);
  d.hidden = false;
  $("#scrim").hidden = false;
  const nb = d.querySelector(".d-body");
  if (nb) nb.scrollTop = top;
}

function syncProjects() {
  const sel = $("#project-filter");
  const cwds = [...new Set(S.agents.map((a) => a.cwd))].sort();
  const key = cwds.join("|");
  if (sel.dataset.keys !== key) {
    sel.dataset.keys = key;
    sel.innerHTML = `<option value="">All projects</option>${cwds.map((c) => `<option value="${esc(c)}">${esc(c.split("/").pop())}</option>`).join("")}`;
  }
  sel.value = cwds.includes(filt.project) ? filt.project : "";
}

function notifyNew(st) {
  const ids = new Set(st.agents.filter(needsYou).map((a) => a.session_id));
  if (prevNeeds && localStorage.getItem("agentforeman-notify") === "on" && "Notification" in window && Notification.permission === "granted") {
    st.agents
      .filter((a) => ids.has(a.session_id) && !prevNeeds.has(a.session_id))
      .forEach((a) => new Notification(`${nameOf(a)}: ${STATES[stateOf(a)].label}`, { body: a.last_message || a.project, tag: a.session_id }));
  }
  prevNeeds = ids;
}

// A rebuild under a pressed pointer swallows the click, and a rebuild under a text
// selection drops it. Both wait, and the newest state renders once they end.
let pointerDown = false;
let pending = false;
const hasSelection = () => {
  const s = getSelection();
  return Boolean(s && !s.isCollapsed && s.anchorNode && s.anchorNode.parentElement?.closest("#content, #drawer"));
};
const holdRender = () => pointerDown || hasSelection();
function flush() {
  if (!pending || holdRender()) return;
  pending = false;
  render();
}
addEventListener("pointerdown", () => (pointerDown = true), true);
addEventListener(
  "pointerup",
  () => {
    pointerDown = false;
    setTimeout(flush, 0);
  },
  true,
);
document.addEventListener("selectionchange", () => setTimeout(flush, 0));

function apply(st) {
  offset = st.generated_at - Date.now();
  lastUpdate = Date.now();
  S = st;
  notifyNew(st);
  if (holdRender()) pending = true;
  else render();
}

function connect() {
  const c = $("#conn");
  const es = new EventSource("/api/events");
  es.addEventListener("state", (e) => {
    c.classList.add("on");
    c.classList.remove("off");
    $("#conn-text").textContent = "Live";
    apply(JSON.parse(e.data));
  });
  es.onerror = () => {
    c.classList.add("off");
    c.classList.remove("on");
    $("#conn-text").textContent = "Reconnecting";
    // A refused stream does not retry. A new token on the server refuses the old cookie.
    if (es.readyState === EventSource.CLOSED) {
      fetch("/api/state", { cache: "no-store" })
        .then((r) => {
          if (r.status === 401) $("#conn-text").textContent = "Signed out: run agentforeman open";
          else setTimeout(connect, 3000);
        })
        .catch(() => setTimeout(connect, 3000));
    }
  };
}

setInterval(() => {
  if (!S) return;
  if ($("#conn").classList.contains("on")) $("#conn-text").textContent = pending ? "Paused while text is selected" : `Live · ${fmt(Date.now() - lastUpdate)} ago`;
  if (!holdRender() && document.querySelector(".tip[hidden]")) render();
}, 5000);

let resizeTimer = null;
addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(render, 150);
});

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (t.hidden = true), 3500);
}

// The token stays the same across restarts unless someone deletes its file. Then a tab left
// open reads the new one from the page and tries once more.
async function freshToken() {
  const html = await (await fetch("/", { cache: "no-store" })).text();
  return new DOMParser().parseFromString(html, "text/html").querySelector('meta[name="foreman-token"]')?.content || "";
}

async function post(path, body, retry = true) {
  try {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Foreman-Token": TOKEN }, body: JSON.stringify(body) });
    if (r.status === 403 && retry) {
      const t = await freshToken();
      if (t && t !== TOKEN) {
        TOKEN = t;
        return post(path, body, false);
      }
    }
    let data = {};
    try {
      data = await r.json();
    } catch (err) {
      data = {};
    }
    return { ok: r.ok, data };
  } catch (err) {
    return { ok: false, data: { error: "Server unreachable." } };
  }
}

async function openSession(sid) {
  const a = S && S.agents.find((x) => x.session_id === sid);
  if (!a) return;
  if (a.entrypoint === "cli") return toast("Terminal session. Switch to its terminal.");
  toast(`Opening ${nameOf(a)} in VS Code`);
  const r = await post("/api/open", { session_id: sid });
  if (!r.ok) toast(r.data.error || "Could not open VS Code.");
}

async function decide(requestId, behavior) {
  const r = await post("/api/approve", { request_id: requestId, behavior });
  toast(r.ok ? (behavior === "allow" ? "Approved. The session continues." : "Denied. The session hears that you said no.") : r.data.error || "Could not send the answer.");
}

async function approveSimilar(rid) {
  confirmSimilar = null;
  const r = await post("/api/approve", { request_id: rid, behavior: "allow", similar: true });
  toast(r.ok ? `Approved ${r.data.count} calls.` : r.data.error || "Could not send the answer.");
  render();
}

async function changeRules(body, done) {
  const r = await post("/api/rules", body);
  if (!r.ok) {
    toast(r.data.error || "Could not change the rules.");
    return false;
  }
  if (done) toast(done);
  return true;
}

async function alwaysAllow(rule, rid) {
  if (!(await changeRules({ action: "add", rule }))) return;
  const on = S.rules && S.rules.enabled;
  allowHint = on ? null : { rid, rule };
  toast(on ? `Rule added: ${rule}. This call still waits for your answer.` : `Rule added: ${rule}. Rules are off, so it allows nothing yet.`);
  render();
}

async function addTypedRule() {
  const rule = ruleDraft.trim();
  if (!rule) return toast("Type a rule first, for example Bash(npm test).");
  if (await changeRules({ action: "add", rule }, `Rule added: ${rule}.`)) {
    ruleDraft = "";
    render();
  }
}

async function setGuard(mode) {
  const r = await post("/api/guard", { mode });
  toast(r.ok ? `Same-file guard: ${GUARD[mode][0]}.` : r.data.error || "Could not change the guard.");
}

async function sendReply(sid) {
  const text = (drafts[sid] || "").trim();
  if (!text) return toast("Write a reply first.");
  const r = await post("/api/reply", { session_id: sid, text });
  if (r.ok) {
    delete drafts[sid];
    toast("Reply sent.");
    render(); // clear the box now; the "Sent" state follows with the next update
  } else toast(r.data.error || "Could not send the reply.");
}

async function sendSteer(id) {
  const text = (steerDrafts[id] || "").trim();
  if (!text) return toast("Write a note first.");
  if (!drawer) return;
  const body = drawer.aid ? { session_id: drawer.sid, agent_id: id, text } : { session_id: id, text };
  const r = await post("/api/steer", body);
  if (r.ok) {
    delete steerDrafts[id];
    toast("Note sent. It arrives at the next tool call.");
    render();
  } else toast(r.data.error || "Could not send the note.");
}

async function stopSession(sid) {
  confirmStop = null;
  const r = await post("/api/stop", { session_id: sid });
  toast(r.ok ? "Stop sent. The session ends its turn at its next tool call." : r.data.error || "Could not stop the session.");
  render();
}

async function stopSubagent(sid, aid) {
  confirmStop = null;
  const r = await post("/api/stop", { session_id: sid, agent_id: aid });
  toast(r.ok ? "Stop sent. The subagent ends at its next tool call." : r.data.error || "Could not stop the subagent.");
  render();
}

function openDrawer(sid, aid = null) {
  // Back from a subagent keeps the session tab you came from. A subagent opened from
  // another page goes back to the Subagents tab, where it is listed.
  if (!drawer || drawer.sid !== sid) drawerTab = aid ? "subagents" : "timeline";
  drawer = { sid, aid };
  selected = sid;
  render();
}
function closeDrawer() {
  drawer = null;
  render();
}

document.addEventListener("click", (e) => {
  const t = e.target;
  if (t.closest("textarea, input[data-rule-input]")) return;
  const ap = t.closest("[data-approve]");
  if (ap) return decide(ap.dataset.approve, ap.dataset.behavior);
  const sim = t.closest("[data-approve-similar]");
  if (sim) {
    const rid = sim.dataset.approveSimilar;
    if (confirmSimilar && confirmSimilar.rid === rid && confirmSimilar.until > Date.now()) return approveSimilar(rid);
    confirmSimilar = { rid, until: Date.now() + 4000 };
    setTimeout(render, 4100);
    return render();
  }
  const aa = t.closest("[data-always-allow]");
  if (aa) return alwaysAllow(aa.dataset.alwaysAllow, aa.dataset.rid);
  if (t.closest("[data-rules-enabled]")) {
    const on = !(S.rules && S.rules.enabled);
    return changeRules({ action: "enable", enabled: on }, on ? "Rules on. Matching calls run at once." : "Rules off. Every call goes to the page.");
  }
  const rr = t.closest("[data-rule-remove]");
  if (rr) return changeRules({ action: "remove", rule: rr.dataset.ruleRemove }, "Rule removed.");
  const ra = t.closest("[data-rule-add]");
  if (ra) return changeRules({ action: "add", rule: ra.dataset.ruleAdd }, `Rule added: ${ra.dataset.ruleAdd}.`);
  if (t.closest("[data-rule-add-input]")) return addTypedRule();
  const gd = t.closest("[data-guard]");
  if (gd) return setGuard(gd.dataset.guard);
  const send = t.closest("[data-send]");
  if (send) return sendReply(send.dataset.send);
  const steer = t.closest("[data-steer-send]");
  if (steer) return sendSteer(steer.dataset.steerSend);
  const stopS = t.closest("[data-stop-session]");
  if (stopS) {
    const key = `session:${stopS.dataset.stopSession}`;
    if (confirmStop && confirmStop.aid === key && confirmStop.until > Date.now()) return stopSession(stopS.dataset.stopSession);
    confirmStop = { aid: key, until: Date.now() + 4000 };
    setTimeout(render, 4100);
    return render();
  }
  const ro = t.closest("[data-reply-open]");
  if (ro) {
    drawerTab = "timeline";
    openDrawer(ro.dataset.replyOpen);
    return document.querySelector("#drawer textarea[data-reply]")?.focus();
  }
  const stopBtn = t.closest("[data-stop]");
  if (stopBtn) {
    if (confirmStop && confirmStop.aid === stopBtn.dataset.stop && confirmStop.until > Date.now()) return stopSubagent(stopBtn.dataset.sid, stopBtn.dataset.stop);
    confirmStop = { aid: stopBtn.dataset.stop, until: Date.now() + 4000 };
    setTimeout(render, 4100);
    return render();
  }
  if (t.closest("a[target=_blank]")) return;
  const open = t.closest("[data-open]");
  if (open) return openSession(open.dataset.open);
  if (t.closest("[data-terminal]")) return toast("Terminal session. Switch to its terminal.");
  if (t.closest("#nav a")) filt.status = "all";
  const copy = t.closest("[data-copy]");
  if (copy) return navigator.clipboard.writeText(copy.dataset.copy).then(() => toast("Session ID copied."));
  if (t.closest("[data-close]") || t.id === "scrim") return closeDrawer();
  const back = t.closest("[data-back]");
  if (back) return openDrawer(back.dataset.back);
  const sub = t.closest("[data-sub]");
  if (sub) return openDrawer(sub.dataset.sid, sub.dataset.sub);
  const clear = t.closest("[data-clear]");
  if (clear) {
    const what = clear.dataset.clear;
    if (!what || what === "q") {
      filt.q = "";
      $("#search").value = "";
    }
    if (!what || what === "project") filt.project = "";
    return render();
  }
  const tab = t.closest("[data-tab]");
  if (tab) {
    drawerTab = tab.dataset.tab;
    return renderDrawer();
  }
  const s = t.closest("[data-sort]");
  if (s) {
    sort.dir = sort.key === s.dataset.sort ? -sort.dir : 1;
    sort.key = s.dataset.sort;
    return render();
  }
  const st = t.closest("[data-status]");
  if (st) {
    filt.status = st.dataset.status;
    return render();
  }
  const go = t.closest("[data-go]");
  if (go) {
    filt.status = go.dataset.filter || "all";
    if (location.hash.slice(1) === go.dataset.go) render();
    else location.hash = go.dataset.go;
    return;
  }
  const row = t.closest("[data-sid]");
  if (row) openDrawer(row.dataset.sid, row.dataset.aid || null);
});

addEventListener("hashchange", () => {
  route = TITLES[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
  render();
});

$("#project-filter").addEventListener("change", (e) => {
  filt.project = e.target.value;
  render();
});
// "search" fires when the field's own clear control or Esc empties it.
for (const type of ["input", "search"]) {
  $("#search").addEventListener(type, (e) => {
    filt.q = e.target.value.trim().toLowerCase();
    render();
  });
}

document.addEventListener("input", (e) => {
  if (e.target.matches("textarea[data-reply]")) drafts[e.target.dataset.reply] = e.target.value;
  if (e.target.matches("textarea[data-steer]")) steerDrafts[e.target.dataset.steer] = e.target.value;
  if (e.target.matches("input[data-rule-input]")) ruleDraft = e.target.value;
});

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && e.target.matches("textarea[data-reply]")) {
    e.preventDefault();
    return sendReply(e.target.dataset.reply);
  }
  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && e.target.matches("textarea[data-steer]")) {
    e.preventDefault();
    return sendSteer(e.target.dataset.steer);
  }
  if (e.key === "Enter" && e.target.matches("input[data-rule-input]")) {
    e.preventDefault();
    return addTypedRule();
  }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const typing = e.target.matches("input, select, textarea");
  if (e.key === "Escape") {
    if (e.target.matches("textarea")) return e.target.blur();
    if (drawer && drawer.aid) return openDrawer(drawer.sid);
    if (drawer) return closeDrawer();
    if (e.target.id === "search" && filt.q) {
      e.target.value = "";
      filt.q = "";
      render();
    }
    if (typing) e.target.blur();
    return;
  }
  if (typing) return;
  // Enter on a focused control presses that control only.
  if (e.key === "Enter" && e.target.closest("button, a, [data-tab]")) return;
  const ids = [...document.querySelectorAll("#content table.sessions tbody tr[data-sid]")].map((r) => r.dataset.sid);
  const i = ids.indexOf(selected);
  if (e.key === "/") {
    e.preventDefault();
    $("#search").focus();
  } else if ((e.key === "j" || e.key === "k") && ids.length) {
    selected = ids[e.key === "j" ? Math.min(ids.length - 1, i + 1) : Math.max(0, i - 1)];
    if (drawer) drawer = { sid: selected, aid: null };
    render();
    document.querySelector(`#content table.sessions tr[data-sid="${CSS.escape(selected)}"]`)?.scrollIntoView({ block: "nearest" });
  } else if (e.key === "Enter" && selected) {
    openDrawer(selected);
  } else if (e.key === "o" && selected) {
    openSession(selected);
  }
});

const tip = $("#tip");
let tipFor = null;
let tipTimer = null;
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest("[data-tip]");
  if (el === tipFor) return;
  tipFor = el;
  clearTimeout(tipTimer);
  tip.hidden = true;
  if (!el || !el.dataset.tip) return;
  tipTimer = setTimeout(() => {
    if (!el.isConnected) return;
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const r = el.getBoundingClientRect();
    const left = Math.min(Math.max(8, r.left + r.width / 2 - tip.offsetWidth / 2), innerWidth - tip.offsetWidth - 8);
    const top = r.bottom + 6 + tip.offsetHeight > innerHeight ? r.top - tip.offsetHeight - 6 : r.bottom + 6;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
  }, 300);
});
addEventListener("scroll", () => (tip.hidden = true), { passive: true, capture: true });
let mouse = null;
addEventListener("mousemove", (e) => (mouse = [e.clientX, e.clientY]), { passive: true });
// A rebuild replaces the hovered element, so a visible tooltip takes the text of the new one.
function retip() {
  if (tip.hidden || !mouse) return;
  const el = document.elementFromPoint(...mouse)?.closest("[data-tip]");
  tipFor = el;
  if (el && el.dataset.tip) tip.textContent = el.dataset.tip;
  else tip.hidden = true;
}

// The theme follows the system until the user picks one. Only a pick is saved.
const DARK = matchMedia("(prefers-color-scheme: dark)");
const systemTheme = () => (DARK.matches ? "dark" : "light");
function setTheme(t, save = false) {
  document.documentElement.dataset.theme = t;
  if (save) localStorage.setItem("agentforeman-theme", t);
  $("#theme").textContent = t === "dark" ? "Switch to light" : "Switch to dark";
}
setTheme(localStorage.getItem("agentforeman-theme") || systemTheme());
DARK.addEventListener("change", () => localStorage.getItem("agentforeman-theme") || setTheme(systemTheme()));
$("#theme").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark", true));

function syncNotify() {
  const on = localStorage.getItem("agentforeman-notify") === "on" && "Notification" in window && Notification.permission === "granted";
  $("#notify").textContent = on ? "Alerts on" : "Alerts off";
}
$("#notify").addEventListener("click", async () => {
  if (!("Notification" in window)) return toast("This browser has no notifications.");
  if (localStorage.getItem("agentforeman-notify") === "on") localStorage.setItem("agentforeman-notify", "off");
  else if ((await Notification.requestPermission()) === "granted") localStorage.setItem("agentforeman-notify", "on");
  else toast("Notifications are blocked for this site.");
  syncNotify();
});
syncNotify();

route = TITLES[location.hash.slice(1)] ? location.hash.slice(1) : "overview";
connect();
