"use strict";

const projects = new Map();
// Every repository discovery found, in its order; the picker shows or hides each of them.
const knownRepos = new Map();
const PREFS_KEY = "github-dashboard:prefs";
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const summary = document.getElementById("summary");
const errorBanner = document.getElementById("error");
const pullsList = document.getElementById("pulls");
const workflowsList = document.getElementById("workflows");
const FAILED_CHECKS = new Set(["failure", "timed_out", "cancelled", "error", "startup_failure", "action_required", "stale"]);
let discovering = false;
let pausedUntil = 0;
// With the GitHub App's webhook, the broker notes what changed and the page asks it every few seconds, re-reading only
// those items; a full read every five minutes covers a lost delivery. Without it, projects are read in full once a
// minute, and their running releases and busy PRs join a quick-poll pool, read one by one every few seconds until
// they finish or stop changing; a full read that finds them changed brings them back. Repository discovery follows the
// same pace: the webhook's installation events ask for it at once, so it otherwise only catches what they miss.
const WEBHOOKS = document.querySelector('meta[name="github-webhooks"]')?.content === "on";
const FULL_POLL_MS = WEBHOOKS ? 300000 : 60000;
const DISCOVER_POLL_MS = WEBHOOKS ? 300000 : 60000;
const QUICK_POLL_MS = 5000;
const QUICK_STRIKES = 10;
const QUICK_IN_FLIGHT = 4;
// After New Release, full reads come this often until its run appears, for at most PUBLISH_WATCH_MS.
const PUBLISH_POLL_MS = 10000;
const PUBLISH_WATCH_MS = 300000;

// How the PR list can be ordered; ISO timestamps sort as text, and ties go by repository and number.
const PULL_ORDERS = {
    newest: (a, b) => b.dataset.created.localeCompare(a.dataset.created),
    oldest: (a, b) => a.dataset.created.localeCompare(b.dataset.created),
    attention: (a, b) => Number(a.dataset.priority) - Number(b.dataset.priority),
};

// Per-browser choices: hidden projects are neither shown nor fetched; background ones keep auto-refreshing but are
// left out of the Refresh buttons; and the PR list's order. Storage can be blocked, in which case the choices last
// until the page closes.
function loadPrefs() {
    try {
        const saved = JSON.parse(localStorage.getItem(PREFS_KEY));
        const order = Object.hasOwn(PULL_ORDERS, saved?.order) ? saved.order : "newest";
        return { hidden: new Set(saved?.hidden), background: new Set(saved?.background), order };
    } catch {
        return { hidden: new Set(), background: new Set(), order: "newest" };
    }
}

const prefs = loadPrefs();

function savePrefs() {
    try {
        localStorage.setItem(PREFS_KEY, JSON.stringify({ hidden: [...prefs.hidden], background: [...prefs.background], order: prefs.order }));
    } catch {
    }
}

function setMember(set, name, member) {
    if (member) set.add(name); else set.delete(name);
    savePrefs();
}

function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
}

function link(text, url) {
    const node = element("a", text);
    // GitHub can return check target URLs supplied by third-party integrations.
    try {
        const parsed = new URL(url);
        if (parsed.protocol === "https:" || parsed.protocol === "http:") {
            node.href = parsed.href;
            node.target = "_blank";
            node.rel = "noopener noreferrer";
        }
    } catch {
    }
    return node;
}

function stateLabel(state) {
    return ({
        done: "Done", approve: "Waiting for approval", queued: "Not started",
        running: "Running", in_progress: "Running", waiting: "Waiting",
        requested: "Requested", pending: "Pending", skipped: "Skipped",
    })[state] || state.replaceAll("_", " ");
}

function badge(text, state) {
    return element("span", text, `badge ${state || ""}`);
}

// Authentik's forward-auth session for this site runs out while the page polls, and its answer is then a redirect to
// sso.nikolaivorkinn.com. A fetch can't follow it there (another origin, and these requests need a preflight, which
// Authentik answers with another redirect), so the redirect is left unfollowed and the page reloads instead: a page
// load goes through the sign-in, without a prompt while the SSO session lasts. A hidden tab waits until it is shown.
let signedOut = false;

function sessionExpired() {
    signedOut = true;
    errorBanner.textContent = "Your sign-in expired. Reloading the dashboard to sign in again.";
    errorBanner.hidden = false;
    if (!document.hidden) location.reload();
    return new Error(errorBanner.textContent);
}

async function api(path, options = {}) {
    if (signedOut) throw sessionExpired();
    if (Date.now() < pausedUntil) {
        throw new Error("GitHub rate limit reached. Waiting before the next request.");
    }
    const response = await fetch(path, {
        ...options, credentials: "same-origin", redirect: "manual",
        headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" },
    });
    if (response.type === "opaqueredirect") throw sessionExpired();
    if (!response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("Your SSO session may have expired. Reload the dashboard to sign in.");
    }
    const data = await response.json();
    if (!response.ok) {
        if (response.status === 429) {
            pausedUntil = Date.now() + Math.max(60, Number(response.headers.get("Retry-After")) || 60) * 1000;
        }
        throw Object.assign(new Error(data.error || `Request failed (${response.status}).`), { status: response.status });
    }
    return data;
}

function projectPath(name) {
    return `/github/repositories/${name.split("/").map(encodeURIComponent).join("/")}`;
}

function checkList(checks) {
    const list = element("ul", undefined, "checks");
    for (const check of checks) {
        const item = element("li", undefined, check.state);
        item.append(link(`${check.name}: ${stateLabel(check.state)}`, check.url));
        list.append(item);
    }
    return list;
}

// The Claude Code session that opened the PR: claude:// opens it in the Claude app, and the claude.ai link is the
// fallback, which the phone apps also open as a universal link.
function sessionLinks(session) {
    const links = element("p", undefined, "session-links");
    const app = element("a", "Open in Claude", "session-app");
    app.href = `claude://code/${session}`;
    app.title = "Open the Claude Code session behind this PR in the Claude app";
    const web = link("web", `https://claude.ai/code/${session}`);
    web.title = "Open the session on claude.ai";
    links.append(app, document.createTextNode(" · "), web);
    return links;
}

function renderPull(pr, project) {
    const row = element("article", undefined, "pull");
    row.dataset.key = `${project.repo.full_name}#${pr.number}`;
    const identity = element("div", undefined, "pull-identity");
    const title = link(`#${pr.number} ${pr.title}`, pr.url);
    title.className = "pull-title";
    identity.append(element("span", project.repo.full_name, "repo-label"), title,
        element("p", `${pr.author} · ${pr.head} → ${pr.base}`, "pull-meta"));
    if (/^session_[A-Za-z0-9]+$/.test(pr.session || "")) identity.append(sessionLinks(pr.session));
    const readiness = element("div", undefined, "readiness");
    if (pr.draft) readiness.append(badge("Draft"));
    readiness.append(pr.mergeable === false
        ? badge("Merge conflicts", "failed")
        : pr.mergeable === true
            ? badge("No conflicts", "done")
            : badge("Checking conflicts", "pending"));
    if (pr.merge_state === "blocked") readiness.append(badge("Merge blocked", "pending"));
    if (project.errorText) readiness.append(badge("Stale data", "failed"));
    if (!pr.draft) readiness.append(autoMergeControl(pr, project));
    const remaining = pr.checks.filter(check => check.pending);
    const failed = pr.checks.filter(check => FAILED_CHECKS.has(check.state));
    const completed = pr.checks.filter(check => !check.pending && !FAILED_CHECKS.has(check.state));
    const checks = element("div", undefined, "pull-checks");
    const counts = [];
    if (remaining.length) counts.push(`${remaining.length} remaining`);
    if (failed.length) counts.push(`${failed.length} failed`);
    if (counts.length || !pr.checks.length) {
        checks.append(element("p", counts.join(" · ") || "No checks reported yet.", "check-summary"));
    }
    const attention = pr.checks.filter(check => check.pending || FAILED_CHECKS.has(check.state));
    checks.append(checkList(attention));
    if (completed.length) {
        const details = element("details");
        details.dataset.detailKey = `checks:${row.dataset.key}`;
        details.append(element("summary", `${completed.length} completed checks`), checkList(completed));
        checks.append(details);
    }
    row.dataset.created = pr.created_at;
    row.dataset.priority = pr.mergeable === false || failed.length ? "0"
        : remaining.length || pr.mergeable === null ? "1" : pr.draft ? "3" : "2";
    row.append(identity, readiness, checks, filesChanged(pr, project, row.dataset.key));
    return row;
}

// Auto-merge on or off; GitHub merges at once instead when the base branch requires nothing and the checks are done.
function autoMergeControl(pr, project) {
    const box = element("div", undefined, "auto-merge");
    const state = project.merges.get(pr.number);
    const on = state?.on ?? pr.auto_merge;
    if (state?.merged) {
        box.append(badge("Merged", "done"));
        return box;
    }
    if (on) box.append(badge("Auto-merge on", "done"));
    const button = element("button", state?.busy ? "Working..." : on ? "Cancel auto-merge" : "Auto-merge", "auto-merge-button");
    button.type = "button";
    button.disabled = !!state?.busy;
    button.title = on ? "Stop GitHub merging this PR when it's ready"
        : "Merge when the required checks pass, or now if nothing is required and the checks are done";
    button.addEventListener("click", () => setAutoMerge(project, pr, !on));
    box.append(button);
    if (state?.message) {
        const message = element("p", state.message, "error");
        message.setAttribute("role", "alert");
        box.append(message);
    }
    return box;
}

async function setAutoMerge(project, pr, enabled) {
    if (project.merges.get(pr.number)?.busy) return;
    project.merges.set(pr.number, { busy: true, on: !enabled });
    renderProject(project);
    try {
        const result = await api(`${projectPath(project.repo.full_name)}/pulls/${pr.number}/auto-merge`, {
            method: "POST", body: JSON.stringify({ enabled }),
        });
        project.merges.set(pr.number, { on: result.auto_merge, merged: result.merged });
    } catch (error) {
        project.merges.set(pr.number, { on: !enabled, message: error.message });
    } finally {
        project.nextRefresh = 0;
        renderProject(project);
    }
}

// Files changed, read when first opened and again after a new push; the details stay open across refreshes.
function filesChanged(pr, project, key) {
    const details = element("details", undefined, "pull-files");
    details.dataset.detailKey = `files:${key}`;
    const cacheKey = `${pr.number}@${pr.sha}`;
    const loaded = project.files.get(cacheKey);
    const counts = loaded?.files ? ` (${loaded.files.length}${loaded.truncated ? "+" : ""})` : "";
    details.append(element("summary", `Files changed${counts}`));
    if (loaded?.files) {
        details.append(...loaded.files.map((file, index) => fileDiff(file, `${details.dataset.detailKey}:${index}`)));
        if (loaded.truncated) details.append(link("More files on GitHub", `${pr.url}/files`));
    } else {
        details.append(element("p", loaded?.error || "Loading files...", loaded?.error ? "error" : "empty"));
    }
    details.addEventListener("toggle", () => details.open && (!loaded || loaded.error) ? loadFiles(project, pr, cacheKey) : null);
    return details;
}

async function loadFiles(project, pr, cacheKey) {
    if (project.files.get(cacheKey)?.busy) return;
    project.files.set(cacheKey, { busy: true });
    try {
        const data = await api(`${projectPath(project.repo.full_name)}/pulls/${pr.number}/files`);
        project.files.set(cacheKey, data);
    } catch (error) {
        project.files.set(cacheKey, { error: `${error.message} Close and open again to retry.` });
    }
    renderProject(project);
}

function fileDiff(file, key) {
    const details = element("details", undefined, "file-diff");
    details.dataset.detailKey = key;
    const summaryLine = element("summary");
    const name = file.previous_filename ? `${file.previous_filename} → ${file.filename}` : file.filename;
    summaryLine.append(element("span", name, "file-name"), element("span", `+${file.additions}`, "diff-add-count"),
        element("span", `−${file.deletions}`, "diff-del-count"));
    details.append(summaryLine);
    if (file.patch) {
        const patch = element("pre", undefined, "patch");
        for (const line of file.patch.split("\n")) {
            const kind = line.startsWith("@@") ? "diff-hunk" : line.startsWith("+") ? "diff-add"
                : line.startsWith("-") ? "diff-del" : "diff-same";
            patch.append(element("span", `${line}\n`, kind));
        }
        details.append(patch);
    } else {
        const note = element("p", file.status === "renamed" ? "Renamed without changes. " : "No diff shown here. ", "empty");
        if (file.url) note.append(link("View on GitHub", file.url));
        details.append(note);
    }
    return details;
}

const MATRIX_EXPANDED_MAX = 6;

// Small matrices are drawn a card per job, unless every job was skipped; the toggle overrides either way.
function matrixExpanded(node, mode) {
    return !!node.jobs && (mode ? mode === "expanded" : node.total <= MATRIX_EXPANDED_MAX && node.state !== "skipped");
}

function explode(nodes, mode) {
    const parts = new Map(nodes.filter(node => matrixExpanded(node, mode)).map(node => [node.key,
        node.jobs.map((job, index) => ({ key: `${node.key}#${index}`, name: job.name, state: job.state,
            needs: node.needs, done: null, total: null }))]));
    const through = needs => needs.flatMap(key => parts.has(key) ? parts.get(key).map(part => part.key) : [key]);
    return nodes.flatMap(node => (parts.get(node.key) || [node]).map(part => ({ ...part, needs: through(part.needs) })));
}

function renderGraph(allNodes, mode) {
    const nodes = explode(allNodes, mode);
    const scroll = element("div", undefined, "graph-scroll");
    scroll.setAttribute("aria-label", "Workflow job dependency diagram");
    const graph = element("div", undefined, "graph");
    const byKey = new Map(nodes.map(node => [node.key, node]));
    const depths = new Map();
    function depth(node, visiting = new Set()) {
        if (depths.has(node.key)) return depths.get(node.key);
        if (visiting.has(node.key)) return 0;
        const seen = new Set([...visiting, node.key]);
        const parents = node.needs.filter(key => byKey.has(key));
        const value = parents.length ? 1 + Math.max(...parents.map(key => depth(byKey.get(key), seen))) : 0;
        depths.set(node.key, value);
        return value;
    }
    const rows = new Map();
    const positions = new Map();
    for (const node of nodes) {
        const column = depth(node);
        const row = rows.get(column) || 0;
        rows.set(column, row + 1);
        positions.set(node.key, { x: column * 168, y: row * 70 });
    }
    const width = Math.max(0, ...depths.values()) * 168 + 144;
    const height = (Math.max(1, ...rows.values()) - 1) * 70 + 52;
    graph.dataset.width = String(width);
    graph.dataset.height = String(height);
    graph.style.width = `${width}px`;
    graph.style.height = `${height}px`;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", width);
    svg.setAttribute("height", height);
    svg.setAttribute("aria-hidden", "true");
    for (const node of nodes) {
        const pos = positions.get(node.key);
        for (const parent of node.needs) {
            if (!positions.has(parent)) continue;
            const start = positions.get(parent);
            const path = document.createElementNS(svg.namespaceURI, "path");
            const x = start.x + 144, y = start.y + 26;
            path.setAttribute("d", `M${x},${y} C${x + 12},${y} ${pos.x - 12},${pos.y + 26} ${pos.x},${pos.y + 26}`);
            svg.append(path);
        }
        const card = element("div", undefined, `node ${node.state}`);
        card.style.left = `${pos.x}px`;
        card.style.top = `${pos.y}px`;
        card.title = node.name;
        card.append(element("strong", node.name),
            element("span", `${stateLabel(node.state)}${node.total ? ` · ${node.done}/${node.total}` : ""}`));
        graph.append(card);
    }
    graph.prepend(svg);
    scroll.append(graph);
    return scroll;
}

function fitGraphs() {
    for (const viewport of workflowsList.querySelectorAll(".graph-scroll")) {
        const graph = viewport.firstElementChild;
        if (!viewport.clientWidth) continue;
        const scale = Math.min(1, viewport.clientWidth / Number(graph.dataset.width));
        graph.style.transform = `scale(${scale})`;
        viewport.style.height = `${Number(graph.dataset.height) * scale}px`;
    }
}

// The latest finished release stays in full for this long, then as one line until the next release finishes.
const FINISHED_SHOWN_MS = 120000;

function finished(run) {
    return run.status === "completed";
}

// Ongoing runs, then the latest finished one until a newer run starts: a run refreshed on its own can finish while
// an older one is shown.
function shownRuns(project) {
    const runs = project.data?.workflows || [];
    const ongoing = runs.filter(run => !finished(run));
    const latest = runs.filter(finished).sort((a, b) => b.id - a.id)[0];
    const replaced = !latest || ongoing.some(run => run.id > latest.id);
    return [...ongoing, ...(replaced ? [] : [latest])];
}

// Collapsed once two minutes have passed since it finished.
function collapseAt(run) {
    return Date.parse(run.finished_at) + FINISHED_SHOWN_MS;
}

function renderWorkflow(run, project) {
    const section = element("article", undefined, "workflow");
    const heading = element("div", undefined, "row-heading");
    const title = link(`${run.name} #${run.number}`, run.url);
    title.className = "workflow-title";
    const state = finished(run) ? run.conclusion || "completed" : run.status;
    heading.append(title, badge(({ success: "Succeeded", failure: "Failed" })[state] || stateLabel(state), state));
    if (project.errorText) heading.append(badge("Stale data", "failed"));
    const refreshing = project.runRefresh.get(run.id);
    if (!finished(run)) {
        const refresh = element("button", refreshing?.busy ? "Refreshing..." : "Refresh", "run-refresh");
        refresh.type = "button";
        refresh.title = "Get this run's latest state now";
        refresh.disabled = !!refreshing?.busy;
        refresh.addEventListener("click", () => refreshRun(project, run));
        heading.append(refresh);
    }
    section.append(element("p", `${project.repo.full_name} · ${run.branch}`, "repo-label"), heading);
    if (refreshing?.message) {
        const message = element("p", refreshing.message, "error run-error");
        message.setAttribute("role", "alert");
        section.append(message);
    }
    if (finished(run)) {
        const choice = project.finishedMode.get(run.id);
        const collapsed = choice ? choice === "collapsed" : Date.now() >= collapseAt(run);
        const toggle = element("button", collapsed ? "Show jobs" : "Collapse", "finished-toggle");
        toggle.type = "button";
        toggle.addEventListener("click", () => {
            project.finishedMode.set(run.id, collapsed ? "expanded" : "collapsed");
            renderProject(project);
        });
        heading.append(element("span", `finished ${new Date(run.finished_at).toLocaleTimeString()}`, "muted"), toggle);
        if (collapsed) {
            section.className = "workflow collapsed";
            return section;
        }
    }
    const mode = project.matrixMode.get(run.id);
    const matrices = run.nodes.filter(node => node.jobs);
    if (matrices.length) {
        const expanded = matrices.every(node => matrixExpanded(node, mode));
        const toggle = element("button", expanded ? "Collapse matrix jobs" : "Expand matrix jobs", "matrix-toggle");
        toggle.type = "button";
        toggle.addEventListener("click", () => {
            project.matrixMode.set(run.id, expanded ? "collapsed" : "expanded");
            renderProject(project);
        });
        section.append(toggle);
    }
    section.append(renderGraph(run.nodes, mode));
    for (const deployment of run.pending_deployments || []) {
        const key = `${run.id}:${run.attempt}:${deployment.id}`;
        const state = project.approvals.get(key);
        const controls = element("div", undefined, "deployment-controls");
        const button = element("button", state?.busy ? "Approving..." : state?.approved
            ? "Approved" : `Approve ${deployment.name}`);
        button.type = "button";
        button.disabled = !deployment.can_approve || !!state?.busy || !!state?.approved;
        button.addEventListener("click", () => approveDeployment(project, run, deployment));
        controls.append(button);
        if (!deployment.can_approve) {
            controls.append(element("span", `Required reviewer approval${deployment.wait_timer ? ` · ${deployment.wait_timer}-minute wait timer` : ""}`, "muted"));
        }
        if (state?.message) {
            const message = element("span", state.message, state.approved ? "done" : "error");
            message.setAttribute("role", state.approved ? "status" : "alert");
            controls.append(message);
        }
        section.append(controls);
    }
    const details = element("details");
    details.dataset.detailKey = `jobs:${project.repo.full_name}:${run.id}`;
    details.append(element("summary", `All jobs and steps · attempt ${run.attempt}`));
    for (const job of run.jobs) {
        const item = element("div", undefined, "job");
        item.append(link(job.name, job.url), document.createTextNode(` · ${stateLabel(job.conclusion || job.status)}`));
        const steps = element("ol", undefined, "steps");
        for (const step of job.steps) {
            steps.append(element("li", `${step.name} · ${stateLabel(step.conclusion || step.status)}`, step.conclusion || step.status));
        }
        item.append(steps);
        details.append(item);
    }
    section.append(details);
    return section;
}

function reconcile(container, nodes) {
    for (const [index, node] of nodes.entries()) {
        if (container.children[index] !== node) container.insertBefore(node, container.children[index] || null);
    }
    while (container.children.length > nodes.length) container.lastElementChild.remove();
}

function renderOverview() {
    const all = [...projects.values()];
    const pulls = all.flatMap(project => project.pullRows).sort((a, b) =>
        PULL_ORDERS[prefs.order](a, b) || a.dataset.key.localeCompare(b.dataset.key, undefined, { numeric: true }));
    const workflows = all.flatMap(project => project.workflowRows);
    const ongoing = all.flatMap(project => shownRuns(project).filter(run => !finished(run))).length;
    const unloaded = all.filter(project => !project.data).length;
    const errors = all.filter(project => project.errorText).length;
    const emptyLabel = unloaded ? "Waiting for project data..." : errors ? "Refresh failed; results may be incomplete." : null;
    reconcile(pullsList, pulls.length ? pulls : [element("p", emptyLabel || "No open pull requests.", "empty")]);
    reconcile(workflowsList, workflows.length ? workflows : [element("p", emptyLabel || "No ongoing release workflows.", "empty")]);
    document.getElementById("pull-count").textContent = String(pulls.length);
    document.getElementById("workflow-count").textContent = String(workflows.length);
    document.getElementById("project-count").textContent = String(all.length);
    summary.textContent = `${all.length} repositories · ${pulls.length} open PRs · ${ongoing} ongoing releases`
        + (knownRepos.size > all.length ? ` · ${knownRepos.size - all.length} hidden` : "")
        + (unloaded ? ` · ${unloaded} not loaded` : "") + (errors ? ` · ${errors} refresh errors` : "");
    renderRefreshLabels();
    requestAnimationFrame(fitGraphs);
}

function renderRefreshLabels() {
    const all = [...projects.values()];
    const included = all.filter(project => !project.background).length;
    const button = document.getElementById("refresh");
    button.textContent = included === all.length ? "Refresh all" : `Refresh ${included} of ${all.length}`;
    button.title = included === all.length ? "Refresh every project now"
        : "Refresh the projects marked Refresh now; the others still update in the background";
}

// A checkbox per discovered repository in the Projects & releases header; unticked ones are neither shown nor fetched.
function renderPicker() {
    const picker = document.getElementById("project-picker");
    const summaryLine = element("summary", `Projects (${projects.size} of ${knownRepos.size})`);
    summaryLine.title = "Pick which projects are shown and refreshed";
    const options = [...knownRepos.values()].map(repo => {
        const option = element("label", undefined, "project-option");
        const checkbox = element("input");
        checkbox.type = "checkbox";
        checkbox.checked = projects.has(repo.full_name);
        checkbox.dataset.repo = repo.full_name;
        checkbox.addEventListener("change", () => {
            if (checkbox.checked) showProject(repo); else hideProject(projects.get(repo.full_name));
        });
        option.append(checkbox, document.createTextNode(` ${repo.full_name}`));
        return option;
    });
    picker.replaceChildren(summaryLine, element("div", undefined, "project-options"));
    picker.lastElementChild.append(...options);
}

function hideProject(project) {
    project.card.remove();
    projects.delete(project.repo.full_name);
    setMember(prefs.hidden, project.repo.full_name, true);
    renderPicker();
    renderOverview();
}

function showProject(repo) {
    setMember(prefs.hidden, repo.full_name, false);
    addProject(repo);
    renderPicker();
    renderOverview();
    tick();
}

function renderProject(project) {
    const oldRows = [...project.pullRows, ...project.workflowRows];
    const expanded = new Set(oldRows.flatMap(row => [...row.querySelectorAll("details[open]")])
        .map(node => node.dataset.detailKey));
    project.pullRows = (project.data?.pulls || []).map(pr => renderPull(pr, project));
    project.workflowRows = shownRuns(project).map(run => renderWorkflow(run, project));
    // Draw again when a finished run is due to collapse; tick() checks this every second.
    project.redrawAt = Math.min(Infinity, ...shownRuns(project).filter(run => finished(run)
        && !project.finishedMode.has(run.id)).map(collapseAt).filter(at => at > Date.now()));
    for (const row of [...project.pullRows, ...project.workflowRows]) {
        for (const details of row.querySelectorAll("details")) details.open = expanded.has(details.dataset.detailKey);
    }
    renderOverview();
}

async function refreshProject(project) {
    if (project.loading || document.hidden) return;
    project.loading = true;
    const partial = project.data ? project.partial : null;
    project.partial = null;
    const section = partial?.section || "all";
    try {
        const sections = await api(`${projectPath(project.repo.full_name)}${section === "all" ? "" : `?section=${section}`}`);
        const data = { ...project.data, ...sections };
        project.data = data;
        project.errorText = "";
        project.error.hidden = true;
        track(project, [...(sections.pulls || []).map(pullItem), ...(sections.workflows || []).map(runItem)]);
        if (sections.pulls) forgetMissing(project, "pull", sections.pulls.map(pr => pr.number));
        // Fresh PR data says whether auto-merge is on; only a change still being sent overrides it.
        if (sections.pulls) for (const state of project.merges.values()) if (!state.busy) delete state.on;
        if (sections.workflows) forgetMissing(project, "run", sections.workflows.map(run => run.id));
        if (project.publishedTag) {
            const run = data.workflows.find(run => run.branch === project.publishedTag);
            if (run) {
                project.result.replaceChildren(link(`Published ${project.publishedTag} — view workflow`, run.url));
                project.watchUntil = 0;
            }
        }
        project.card.title = `${project.repo.description || project.repo.full_name} · Updated ${new Date().toLocaleTimeString()}`;
    } catch (error) {
        project.error.textContent = error.message;
        project.errorText = error.message;
        project.error.hidden = false;
        project.card.title = "Refresh failed; previously shown data is stale.";
    } finally {
        project.loading = false;
        // A refresh of one section leaves the other's turn where it was, unless a new release is awaited.
        const next = Date.now() + (Date.now() < project.watchUntil ? PUBLISH_POLL_MS : FULL_POLL_MS);
        project.nextRefresh = Math.max(pausedUntil, partial ? Math.min(partial.resume, next) : next);
        renderProject(project);
    }
}

const runItem = run => ({ key: `run:${run.id}`, kind: "run", id: run.id, active: !finished(run), value: run });
const pullItem = pr => ({ key: `pull:${pr.number}`, kind: "pull", id: pr.number, active: pr.refresh, value: pr });

// Pools what is active and new or changed; a pooled item that comes back the same QUICK_STRIKES times in a row, or
// is no longer active, leaves the pool until a read finds it changed again.
function track(project, items) {
    for (const item of items) {
        const print = JSON.stringify(item.value);
        const changed = project.prints.get(item.key) !== print;
        project.prints.set(item.key, print);
        const entry = project.pool.get(item.key);
        if (!item.active) {
            project.pool.delete(item.key);
        } else if (changed && entry) {
            entry.strikes = 0;
        } else if (changed) {
            project.pool.set(item.key, { kind: item.kind, id: item.id, strikes: 0, next: Date.now() + QUICK_POLL_MS });
        } else if (entry && ++entry.strikes >= QUICK_STRIKES) {
            project.pool.delete(item.key);
        }
    }
}

function forgetMissing(project, kind, ids) {
    const kept = new Set(ids.map(id => `${kind}:${id}`));
    for (const map of [project.pool, project.prints]) {
        for (const key of map.keys()) if (key.startsWith(`${kind}:`) && !kept.has(key)) map.delete(key);
    }
}

// Reads one run or PR and merges it in, leaving the project's own schedule alone. A PR that has closed goes.
async function pollItem(project, kind, id) {
    const base = projectPath(project.repo.full_name);
    try {
        if (kind === "run") {
            const { workflow } = await api(`${base}/runs/${id}`);
            const runs = project.data?.workflows || [];
            project.data = { ...project.data, workflows: runs.map(other => other.id === workflow.id ? workflow : other) };
            track(project, [runItem(workflow)]);
        } else {
            const { pull } = await api(`${base}/pulls/${id}`);
            const pulls = project.data?.pulls || [];
            project.data = { ...project.data, pulls: pulls.map(other => other.number === pull.number ? pull : other) };
            track(project, [pullItem(pull)]);
        }
    } catch (error) {
        if (error.status === 404) {
            if (kind === "pull") project.data = { ...project.data, pulls: project.data.pulls.filter(pr => pr.number !== id) };
            project.pool.delete(`${kind}:${id}`);
            project.prints.delete(`${kind}:${id}`);
        }
        throw error;
    } finally {
        const entry = project.pool.get(`${kind}:${id}`);
        if (entry) entry.next = Math.max(pausedUntil, Date.now() + QUICK_POLL_MS);
        renderProject(project);
    }
}

// The Refresh button on a running workflow.
async function refreshRun(project, run) {
    if (project.runRefresh.get(run.id)?.busy) return;
    project.runRefresh.set(run.id, { busy: true });
    renderProject(project);
    try {
        await pollItem(project, "run", run.id);
        project.runRefresh.delete(run.id);
    } catch (error) {
        project.runRefresh.set(run.id, { message: error.message });
    }
    renderProject(project);
}

let quickInFlight = 0;

// Quick polls run alongside full reads, but never for an item already being read or a project mid full read.
async function quickPoll(project, entry) {
    entry.busy = true;
    quickInFlight += 1;
    try {
        await pollItem(project, entry.kind, entry.id);
    } catch {
        // A failed quick poll counts as unchanged, so a broken item leaves the pool; the full read reports errors.
        if (++entry.strikes >= QUICK_STRIKES) project.pool.delete(`${entry.kind}:${entry.id}`);
    } finally {
        entry.busy = false;
        quickInFlight -= 1;
    }
}

async function publish(project, button) {
    if (project.publishing) return;
    project.publishing = true;
    project.publishedTag = null;
    button.disabled = true;
    project.result.replaceChildren(element("span", "Generating notes and publishing..."));
    try {
        const data = await api(`${projectPath(project.repo.full_name)}/release`, { method: "POST" });
        project.publishedTag = data.tag;
        project.result.replaceChildren(link(`Published ${data.tag} — view workflow`, data.url));
        project.watchUntil = Date.now() + PUBLISH_WATCH_MS;
        project.nextRefresh = Date.now() + PUBLISH_POLL_MS;
    } catch (error) {
        project.result.replaceChildren(element("span",
            `${error.message} Check GitHub Actions and releases before trying again.`, "error"));
    } finally {
        project.publishing = false;
        button.disabled = false;
        button.textContent = "New Release";
    }
}

async function approveDeployment(project, run, deployment) {
    const key = `${run.id}:${run.attempt}:${deployment.id}`;
    if (project.approvals.get(key)?.busy || project.approvals.get(key)?.approved) return;
    project.approvals.set(key, { busy: true });
    renderProject(project);
    try {
        await api(`${projectPath(project.repo.full_name)}/runs/${run.id}/approve`, {
            method: "POST", body: JSON.stringify({ environment_id: deployment.id }),
        });
        project.approvals.set(key, { approved: true, message: "Deployment approved." });
    } catch (error) {
        project.approvals.set(key, { message: `${error.message} Refresh to check deployment status before retrying.` });
    } finally {
        project.nextRefresh = 0;
        renderProject(project);
    }
}

function addProject(repo) {
    const card = element("section", undefined, "project");
    const heading = element("div", undefined, "project-header");
    const title = element("h3", undefined, "project-name");
    title.append(link(repo.full_name, repo.html_url));
    const button = element("button", "New Release");
    button.type = "button";
    button.title = `Immediately publish the next minor release from ${repo.default_branch}`;
    const include = element("label", undefined, "include");
    const checkbox = element("input");
    checkbox.type = "checkbox";
    checkbox.checked = !prefs.background.has(repo.full_name);
    include.title = "Include in the Refresh buttons. Unticked projects still update in the background.";
    include.append(checkbox, document.createTextNode(" Refresh now"));
    heading.append(title, button, include);
    const result = element("p", undefined, "release-result");
    result.setAttribute("role", "status");
    const error = element("p", undefined, "error");
    error.setAttribute("role", "alert");
    error.hidden = true;
    title.title = repo.description || repo.full_name;
    card.title = `${repo.description || repo.full_name} · Loading...`;
    card.append(heading, result, error);
    document.getElementById("projects").append(card);
    const project = { repo, card, error, result, data: null, pullRows: [], workflowRows: [],
        errorText: "", loading: false, nextRefresh: 0, approvals: new Map(), publishing: false,
        background: !checkbox.checked, partial: null, matrixMode: new Map(), runRefresh: new Map(),
        finishedMode: new Map(), redrawAt: Infinity, pool: new Map(), prints: new Map(), watchUntil: 0,
        files: new Map(), merges: new Map() };
    button.addEventListener("click", () => publish(project, button));
    checkbox.addEventListener("change", () => {
        project.background = !checkbox.checked;
        card.className = project.background ? "project background" : "project";
        setMember(prefs.background, repo.full_name, project.background);
        renderRefreshLabels();
    });
    if (project.background) card.className = "project background";
    projects.set(repo.full_name, project);
}

async function discover() {
    if (discovering || document.hidden) return;
    discovering = true;
    try {
        const data = await api("/github/repositories");
        const names = new Set(data.repositories.map(repo => repo.full_name));
        for (const [name, project] of projects) {
            if (!names.has(name)) {
                project.card.remove();
                projects.delete(name);
            }
        }
        knownRepos.clear();
        for (const repo of data.repositories) {
            knownRepos.set(repo.full_name, repo);
            if (!prefs.hidden.has(repo.full_name) && !projects.has(repo.full_name)) addProject(repo);
        }
        renderPicker();
        renderOverview();
        errorBanner.hidden = true;
    } catch (error) {
        errorBanner.textContent = error.message;
        errorBanner.hidden = false;
        summary.textContent = "Repository discovery failed. Displayed projects may be stale.";
    } finally {
        discovering = false;
    }
}

// Bound concurrency to avoid a burst of hundreds of GitHub requests for a large account.
async function tick() {
    for (const project of projects.values()) if (project.redrawAt <= Date.now()) renderProject(project);
    if (document.hidden || Date.now() < pausedUntil) return;
    const available = 3 - [...projects.values()].filter(project => project.loading).length;
    const due = [...projects.values()].filter(project => !project.loading && project.nextRefresh <= Date.now())
        .sort((a, b) => Number(a.background) - Number(b.background) || a.nextRefresh - b.nextRefresh)
        .slice(0, Math.max(0, available));
    // A full read covers its project's pooled items, so they wait for the next tick.
    const quick = WEBHOOKS ? [] : [...projects.values()].filter(project => !project.loading && !due.includes(project))
        .flatMap(project => [...project.pool.values()].filter(entry => !entry.busy && entry.next <= Date.now())
            .map(entry => ({ project, entry })))
        .sort((a, b) => a.entry.next - b.entry.next).slice(0, Math.max(0, QUICK_IN_FLIGHT - quickInFlight));
    await Promise.all([...due.map(refreshProject), ...quick.map(({ project, entry }) => quickPoll(project, entry)),
        followChanges()]);
}

let changesAfter = null;
let changesAt = 0;
let readingChanges = false;

// Asks the broker what the webhook saw change; the first answer only marks where to start.
async function followChanges() {
    if (!WEBHOOKS || readingChanges || Date.now() < changesAt) return;
    readingChanges = true;
    try {
        const query = changesAfter === null ? "" : `?after=${encodeURIComponent(changesAfter)}`;
        const feed = await api(`/github/changes${query}`);
        const first = changesAfter === null;
        changesAfter = feed.last;
        if (feed.reset) {
            requestRefresh("all");
            discover();
        } else if (!first) {
            applyChanges(feed.changes);
        }
    } catch {
        // The five-minute full reads carry on; the next tick asks again.
    } finally {
        readingChanges = false;
        changesAt = Date.now() + QUICK_POLL_MS;
    }
}

// Re-reads each changed PR or release run once, or the section when it's new to the page. A commit is the
// PR it heads; one the page doesn't know may head a PR pushed since the last read, so the PR list is read.
function applyChanges(changes) {
    const unique = new Map();
    for (const change of changes) {
        if (change.kind === "repositories") {
            discover();
            continue;
        }
        const project = projects.get(change.repo);
        if (project) unique.set(`${change.repo} ${change.kind} ${change.id || ""}`, { project, change });
    }
    for (const { project, change } of unique.values()) {
        const id = change.kind === "commit"
            ? project.data?.pulls?.find(pr => pr.sha === change.id)?.number : Number(change.id);
        if ((change.kind === "pull" || change.kind === "commit") && project.data?.pulls?.some(pr => pr.number === id)) {
            pollItem(project, "pull", id).catch(() => {});
        } else if (change.kind === "run" && project.data?.workflows?.some(run => run.id === id)) {
            pollItem(project, "run", id).catch(() => {});
        } else {
            refreshSoon(project, ({ pull: "pulls", commit: "pulls", run: "workflows" })[change.kind] || "all");
        }
    }
}

// Brings one section forward alone, remembering when the full refresh was due, so the other never waits on it.
function requestRefresh(section) {
    for (const project of projects.values()) if (!project.background) refreshSoon(project, section);
}

function refreshSoon(project, section) {
    if (section === "all" || project.nextRefresh <= Date.now() || !project.data
        || (project.partial && project.partial.section !== section)) {
        project.partial = null;
    } else if (!project.partial) {
        project.partial = { section, resume: project.nextRefresh };
    }
    project.nextRefresh = 0;
}

document.getElementById("refresh").addEventListener("click", async () => {
    requestRefresh("all");
    await Promise.all([discover(), tick()]);
});
const pullOrder = document.getElementById("pull-order");
pullOrder.value = prefs.order;
pullOrder.addEventListener("change", () => {
    prefs.order = Object.hasOwn(PULL_ORDERS, pullOrder.value) ? pullOrder.value : "newest";
    savePrefs();
    renderOverview();
});
document.getElementById("refresh-pulls").addEventListener("click", () => {
    requestRefresh("pulls");
    return tick();
});
document.getElementById("refresh-workflows").addEventListener("click", () => {
    requestRefresh("workflows");
    return tick();
});
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
        requestRefresh("all");
        discover().then(tick);
    }
});
// The project picker closes on a click anywhere else, like a select would.
document.addEventListener("click", event => {
    const picker = document.getElementById("project-picker");
    if (picker.open && !picker.contains(event.target)) picker.open = false;
});
window.addEventListener("resize", fitGraphs);
discover().then(tick);
setInterval(tick, 1000);
setInterval(discover, DISCOVER_POLL_MS);
