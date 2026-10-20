"use strict";

const projects = new Map();
const hiddenRepos = new Map();
const PREFS_KEY = "github-dashboard:prefs";
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const summary = document.getElementById("summary");
const errorBanner = document.getElementById("error");
const pullsList = document.getElementById("pulls");
const workflowsList = document.getElementById("workflows");
const FAILED_CHECKS = new Set(["failure", "timed_out", "cancelled", "error", "startup_failure", "action_required", "stale"]);
let discovering = false;
let pausedUntil = 0;

// Per-browser choices: hidden projects are neither shown nor fetched; background ones keep auto-refreshing but are
// left out of the Refresh buttons. Storage can be blocked, in which case the choices last until the page closes.
function loadPrefs() {
    try {
        const saved = JSON.parse(localStorage.getItem(PREFS_KEY));
        return { hidden: new Set(saved?.hidden), background: new Set(saved?.background) };
    } catch {
        return { hidden: new Set(), background: new Set() };
    }
}

const prefs = loadPrefs();

function savePrefs() {
    try {
        localStorage.setItem(PREFS_KEY, JSON.stringify({ hidden: [...prefs.hidden], background: [...prefs.background] }));
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

async function api(path, options = {}) {
    if (Date.now() < pausedUntil) {
        throw new Error("GitHub rate limit reached. Waiting before the next request.");
    }
    const response = await fetch(path, {
        ...options, credentials: "same-origin", headers: { "X-CSRF-Token": csrf, "Content-Type": "application/json" },
    });
    if (!response.headers.get("content-type")?.includes("application/json")) {
        throw new Error("Your SSO session may have expired. Reload the dashboard to sign in.");
    }
    const data = await response.json();
    if (!response.ok) {
        if (response.status === 429) {
            pausedUntil = Date.now() + Math.max(60, Number(response.headers.get("Retry-After")) || 60) * 1000;
        }
        throw new Error(data.error || `Request failed (${response.status}).`);
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

function renderPull(pr, project) {
    const row = element("article", undefined, "pull");
    row.dataset.key = `${project.repo.full_name}#${pr.number}`;
    const identity = element("div", undefined, "pull-identity");
    const title = link(`#${pr.number} ${pr.title}`, pr.url);
    title.className = "pull-title";
    identity.append(element("span", project.repo.full_name, "repo-label"), title,
        element("p", `${pr.author} · ${pr.head} → ${pr.base}`, "pull-meta"));
    const readiness = element("div", undefined, "readiness");
    if (pr.draft) readiness.append(badge("Draft"));
    readiness.append(pr.mergeable === false
        ? badge("Merge conflicts", "failed")
        : pr.mergeable === true
            ? badge("No conflicts", "done")
            : badge("Checking conflicts", "pending"));
    if (pr.merge_state === "blocked") readiness.append(badge("Merge blocked", "pending"));
    if (project.errorText) readiness.append(badge("Stale data", "failed"));
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
    row.dataset.priority = pr.mergeable === false || failed.length ? "0"
        : remaining.length || pr.mergeable === null ? "1" : pr.draft ? "3" : "2";
    row.append(identity, readiness, checks);
    return row;
}

const MATRIX_EXPANDED_MAX = 5;

function matrixExpanded(node, mode) {
    return !!node.jobs && (mode ? mode === "expanded" : node.total <= MATRIX_EXPANDED_MAX);
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

// Ongoing runs, then the latest finished one: a run refreshed on its own can finish while an older one is shown.
function shownRuns(project) {
    const runs = project.data?.workflows || [];
    const latest = runs.filter(finished).sort((a, b) => b.id - a.id)[0];
    return [...runs.filter(run => !finished(run)), ...(latest ? [latest] : [])];
}

// Collapsed once two minutes have passed since it finished, or as soon as a newer release is running.
function collapseAt(project, run) {
    const newer = (project.data?.workflows || []).some(other => !finished(other) && other.id > run.id);
    return newer ? 0 : Date.parse(run.finished_at) + FINISHED_SHOWN_MS;
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
        const collapsed = choice ? choice === "collapsed" : Date.now() >= collapseAt(project, run);
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
        Number(a.dataset.priority) - Number(b.dataset.priority) || a.dataset.key.localeCompare(b.dataset.key, undefined, { numeric: true }));
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
        + (hiddenRepos.size ? ` · ${hiddenRepos.size} hidden` : "")
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

function renderHidden() {
    const box = document.getElementById("hidden-projects");
    box.hidden = !hiddenRepos.size;
    const nodes = [...hiddenRepos.values()].map(repo => {
        const row = element("div", undefined, "hidden-project");
        const show = element("button", "Show");
        show.type = "button";
        show.addEventListener("click", () => showProject(repo));
        row.append(element("span", repo.full_name), show);
        return row;
    });
    box.replaceChildren(element("p", `Hidden projects (${hiddenRepos.size}) are not refreshed.`, "release-hint"), ...nodes);
}

function hideProject(project) {
    project.card.remove();
    projects.delete(project.repo.full_name);
    hiddenRepos.set(project.repo.full_name, project.repo);
    setMember(prefs.hidden, project.repo.full_name, true);
    renderHidden();
    renderOverview();
}

function showProject(repo) {
    hiddenRepos.delete(repo.full_name);
    setMember(prefs.hidden, repo.full_name, false);
    addProject(repo);
    renderHidden();
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
        && !project.finishedMode.has(run.id)).map(run => collapseAt(project, run)).filter(at => at > Date.now()));
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
        // Runs shown last time are rechecked by ID, since GitHub's status lists can briefly miss one just approved.
        const known = (project.data?.workflows || []).filter(run => !finished(run)).map(run => run.id);
        const parts = [];
        if (section !== "all") parts.push(`section=${section}`);
        if (known.length && section !== "pulls") parts.push(`runs=${known.slice(0, 20).join(",")}`);
        const { active, ...sections } = await api(`${projectPath(project.repo.full_name)}${parts.length ? `?${parts.join("&")}` : ""}`);
        const data = { ...project.data, ...sections };
        project.data = data;
        project.errorText = "";
        project.error.hidden = true;
        for (const name of section === "all" ? ["pulls", "workflows"] : [section]) project.sectionActive[name] = active;
        project.active = project.sectionActive.pulls || project.sectionActive.workflows;
        if (project.publishedTag) {
            const run = data.workflows.find(run => run.branch === project.publishedTag);
            if (run) project.result.replaceChildren(link(`Published ${project.publishedTag} — view workflow`, run.url));
        }
        project.card.title = `${project.repo.description || project.repo.full_name} · Updated ${new Date().toLocaleTimeString()}`;
    } catch (error) {
        project.error.textContent = error.message;
        project.errorText = error.message;
        project.error.hidden = false;
        project.card.title = "Refresh failed; previously shown data is stale.";
    } finally {
        project.loading = false;
        // A refresh of one section leaves the other's turn where it was, unless activity means sooner.
        const next = Date.now() + (project.active ? 10000 : 60000);
        project.nextRefresh = Math.max(pausedUntil, partial ? Math.min(partial.resume, next) : next);
        renderProject(project);
    }
}

// Reads just this run, leaving the project's own schedule alone.
async function refreshRun(project, run) {
    if (project.runRefresh.get(run.id)?.busy) return;
    project.runRefresh.set(run.id, { busy: true });
    renderProject(project);
    try {
        const { workflow } = await api(`${projectPath(project.repo.full_name)}/runs/${run.id}`);
        const runs = project.data?.workflows || [];
        project.data = { ...project.data, workflows: runs.map(other => other.id === workflow.id ? workflow : other) };
        project.runRefresh.delete(run.id);
    } catch (error) {
        project.runRefresh.set(run.id, { message: error.message });
    }
    renderProject(project);
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
        project.active = true;
        project.nextRefresh = Date.now() + 10000;
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
        project.active = true;
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
    const hide = element("button", "Hide");
    hide.type = "button";
    hide.title = "Stop showing and refreshing this project";
    heading.append(title, button, include, hide);
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
        errorText: "", active: true, loading: false, nextRefresh: 0, approvals: new Map(), publishing: false,
        background: !checkbox.checked, partial: null, matrixMode: new Map(), sectionActive: { pulls: true, workflows: true },
        runRefresh: new Map(), finishedMode: new Map(), redrawAt: Infinity };
    button.addEventListener("click", () => publish(project, button));
    checkbox.addEventListener("change", () => {
        project.background = !checkbox.checked;
        card.className = project.background ? "project background" : "project";
        setMember(prefs.background, repo.full_name, project.background);
        renderRefreshLabels();
    });
    hide.addEventListener("click", () => hideProject(project));
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
        for (const name of hiddenRepos.keys()) if (!names.has(name)) hiddenRepos.delete(name);
        for (const repo of data.repositories) {
            if (prefs.hidden.has(repo.full_name)) hiddenRepos.set(repo.full_name, repo);
            else if (!projects.has(repo.full_name)) addProject(repo);
        }
        renderHidden();
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
        .sort((a, b) => Number(a.background) - Number(b.background) || a.nextRefresh - b.nextRefresh);
    await Promise.all(due.slice(0, Math.max(0, available)).map(refreshProject));
}

// Brings one section forward alone, remembering when the full refresh was due, so the other never waits on it.
function requestRefresh(section) {
    for (const project of projects.values()) {
        if (project.background) continue;
        if (section === "all" || project.nextRefresh <= Date.now() || !project.data
            || (project.partial && project.partial.section !== section)) {
            project.partial = null;
        } else if (!project.partial) {
            project.partial = { section, resume: project.nextRefresh };
        }
        project.nextRefresh = 0;
    }
}

document.getElementById("refresh").addEventListener("click", async () => {
    requestRefresh("all");
    await Promise.all([discover(), tick()]);
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
window.addEventListener("resize", fitGraphs);
discover().then(tick);
setInterval(tick, 1000);
setInterval(discover, 60000);
