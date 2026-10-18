"use strict";

const projects = new Map();
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const summary = document.getElementById("summary");
const errorBanner = document.getElementById("error");
let discovering = false;
let pausedUntil = 0;

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
        // A missing or invalid check URL is rendered as text, not as a navigation.
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
        ...options, credentials: "same-origin", headers: { "X-CSRF-Token": csrf },
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

function renderPull(pr) {
    const row = element("article", undefined, "pull");
    const heading = element("div", undefined, "row-heading");
    heading.append(link(`#${pr.number} ${pr.title}`, pr.url));
    if (pr.draft) heading.append(badge("Draft"));
    heading.append(pr.mergeable === false
        ? badge("Merge conflicts", "failed")
        : pr.mergeable === true
            ? badge("No merge conflicts", "done")
            : badge("Checking merge conflicts", "pending"));
    if (pr.merge_state === "blocked") heading.append(badge("Merge blocked", "pending"));
    row.append(heading, element("p", `${pr.author} · ${pr.head} → ${pr.base}`, "muted"));
    const remaining = pr.checks.filter(check => check.pending);
    row.append(element("p", remaining.length
        ? `Remaining checks: ${remaining.map(check => check.name).join(", ")}`
        : pr.checks.length ? "No checks remaining." : "No checks reported yet.", "hint"));
    const checks = element("ul", undefined, "checks");
    for (const check of pr.checks) {
        const item = element("li", undefined, check.state);
        item.append(link(`${check.name}: ${stateLabel(check.state)}`, check.url));
        checks.append(item);
    }
    row.append(checks);
    return row;
}

function renderGraph(nodes) {
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
        positions.set(node.key, { x: column * 270, y: row * 108 });
    }
    const width = (Math.max(0, ...depths.values()) + 1) * 270;
    const height = Math.max(1, ...rows.values()) * 108;
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
            const x = start.x + 210, y = start.y + 38;
            path.setAttribute("d", `M${x},${y} C${x + 30},${y} ${pos.x - 30},${pos.y + 38} ${pos.x},${pos.y + 38}`);
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

function renderWorkflow(run) {
    const section = element("article", undefined, "workflow");
    const heading = element("div", undefined, "row-heading");
    heading.append(link(`${run.name} #${run.number}`, run.url),
        badge(stateLabel(run.status), run.status), element("span", run.branch, "muted"));
    section.append(heading, renderGraph(run.nodes));
    const legend = element("div", undefined, "legend");
    for (const state of ["done", "running", "approve", "queued", "failed", "skipped"]) {
        legend.append(element("span", stateLabel(state), state));
    }
    section.append(legend);
    const details = element("details");
    details.dataset.runId = String(run.id);
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

function renderProject(project, data) {
    const expanded = new Set([...project.body.querySelectorAll("details[open]")].map(node => node.dataset.runId));
    project.body.replaceChildren();
    project.body.append(element("h3", `Open pull requests (${data.pulls.length})`));
    if (!data.pulls.length) project.body.append(element("p", "No open pull requests.", "muted"));
    for (const pr of data.pulls) project.body.append(renderPull(pr));
    project.body.append(element("h3", `Ongoing workflows (${data.workflows.length})`));
    if (!data.workflows.length) project.body.append(element("p", "No ongoing workflows.", "muted"));
    for (const run of data.workflows) project.body.append(renderWorkflow(run));
    for (const details of project.body.querySelectorAll("details")) details.open = expanded.has(details.dataset.runId);
    project.updated.textContent = `Updated ${new Date().toLocaleTimeString()} · ${data.active ? "10-second" : "60-second"} refresh`;
}

async function refreshProject(project) {
    if (project.loading || document.hidden) return;
    project.loading = true;
    try {
        const data = await api(projectPath(project.repo.full_name));
        renderProject(project, data);
        project.error.hidden = true;
        project.active = data.active;
    } catch (error) {
        project.error.textContent = error.message;
        project.error.hidden = false;
        project.updated.textContent = "Refresh failed. Previously shown data may be stale.";
    } finally {
        project.loading = false;
        project.nextRefresh = Math.max(pausedUntil, Date.now() + (project.active ? 10000 : 60000));
    }
}

async function publish(project, button) {
    button.disabled = true;
    project.result.replaceChildren(element("span", "Generating notes and publishing..."));
    try {
        const data = await api(`${projectPath(project.repo.full_name)}/release`, { method: "POST" });
        project.result.replaceChildren(link(`Published ${data.tag} — view release`, data.url));
        // A subsequent click must be an intentional reload, not a duplicate publication.
        button.textContent = "Release published";
        project.active = true;
        project.nextRefresh = Date.now() + 10000;
    } catch (error) {
        project.result.replaceChildren(element("span",
            `${error.message} Check releases on GitHub before reloading to try again.`, "error"));
        button.textContent = "Check GitHub releases";
    }
}

function addProject(repo) {
    const card = element("section", undefined, "project");
    const heading = element("div", undefined, "project-header");
    const title = element("h2");
    title.append(link(repo.full_name, repo.html_url));
    const button = element("button", "New Release");
    button.type = "button";
    button.title = `Immediately publish the next minor release from ${repo.default_branch}`;
    heading.append(title, button);
    const result = element("p", undefined, "release-result");
    result.setAttribute("role", "status");
    const error = element("p", undefined, "error");
    error.setAttribute("role", "alert");
    error.hidden = true;
    const body = element("div", "Loading pull requests and workflows...");
    const updated = element("p", undefined, "updated");
    card.append(heading, element("p", repo.description || "", "project-description"), result, error, body, updated);
    document.getElementById("projects").append(card);
    const project = { repo, card, body, updated, error, result, active: true, loading: false, nextRefresh: 0 };
    button.addEventListener("click", () => publish(project, button));
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
        for (const repo of data.repositories) if (!projects.has(repo.full_name)) addProject(repo);
        summary.textContent = `${projects.size} owned ${projects.size === 1 ? "repository" : "repositories"}`;
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
    if (document.hidden || Date.now() < pausedUntil) return;
    const available = 3 - [...projects.values()].filter(project => project.loading).length;
    const due = [...projects.values()].filter(project => !project.loading && project.nextRefresh <= Date.now());
    await Promise.all(due.slice(0, Math.max(0, available)).map(refreshProject));
}

document.getElementById("refresh").addEventListener("click", async () => {
    for (const project of projects.values()) project.nextRefresh = 0;
    await discover();
    await tick();
});
document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
        for (const project of projects.values()) project.nextRefresh = 0;
        discover().then(tick);
    }
});
discover().then(tick);
setInterval(tick, 1000);
setInterval(discover, 60000);
