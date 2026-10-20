const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../../broker/static/github_dashboard.js"), "utf8");

class Element {
    constructor(tag) {
        this.tag = tag;
        this.children = [];
        this.style = {};
        this.dataset = {};
        this.listeners = {};
        this.attributes = {};
        this.clientWidth = 640;
    }
    append(...children) {
        for (const child of children) {
            if (child instanceof Element) {
                child.remove();
                child.parentElement = this;
            }
            this.children.push(child);
        }
    }
    prepend(...children) {
        for (const child of children.reverse()) this.insertBefore(child, this.children[0] || null);
    }
    insertBefore(child, ref) {
        if (child === ref) return;
        if (child instanceof Element) {
            child.remove();
            child.parentElement = this;
        }
        this.children.splice(ref ? this.children.indexOf(ref) : this.children.length, 0, child);
    }
    replaceChildren(...children) {
        for (const child of [...this.children]) if (child instanceof Element) child.remove();
        this.children = [];
        this.append(...children);
    }
    get firstElementChild() { return this.children.find(child => child instanceof Element); }
    get lastElementChild() { return this.children.findLast(child => child instanceof Element); }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    remove() {
        if (this.parentElement) {
            const siblings = this.parentElement.children;
            siblings.splice(siblings.indexOf(this), 1);
            this.parentElement = null;
        }
    }
    matches(selector) {
        if (selector.startsWith(".")) return (this.className || "").split(" ").includes(selector.slice(1));
        return this.tag === selector.replace("[open]", "") && (!selector.endsWith("[open]") || this.open);
    }
    querySelectorAll(selector) {
        return this.children.filter(child => child instanceof Element).flatMap(child => [
            ...(child.matches(selector) ? [child] : []),
            ...child.querySelectorAll(selector),
        ]);
    }
}

const repo = {
    full_name: "alice/project", html_url: "https://github.com/alice/project",
    description: "A project", default_branch: "main",
};
const idle = { pulls: [], workflows: [], active: false };
const active = {
    active: true, workflows: [],
    pulls: [{
        number: 4, title: "A change", url: "https://github.com/alice/project/pull/4",
        author: "alice", head: "feature", base: "main", mergeable: false,
        checks: [{ name: "Build", state: "in_progress", pending: true }], refresh: true,
        created_at: "2026-10-18T08:00:00Z",
    }],
};
// Whole-project reads, leaving out quick polls of single runs and PRs.
const fullCalls = (app, name = "alice/project") => app.calls.filter(call => new RegExp(`/${name}(\\?|$)`).test(call.url));

async function harness(initial = active, repositories = [repo], storage = null) {
    let now = 100000;
    let data = initial;
    let failure = null;
    const calls = [];
    const ids = new Map(["summary", "error", "projects", "refresh", "pulls", "workflows",
        "pull-count", "workflow-count", "project-count", "refresh-pulls", "refresh-workflows", "hidden-projects", "pull-order"].map(id => [id, new Element("div")]));
    const intervals = [];
    const document = {
        hidden: false,
        querySelector: () => ({ content: "csrf-token" }),
        getElementById: id => ids.get(id),
        createElement: tag => new Element(tag),
        createElementNS: (namespace, tag) => Object.assign(new Element(tag), { namespaceURI: namespace }),
        createTextNode: text => ({ textContent: text }),
        addEventListener: () => {},
    };
    const context = vm.createContext({
        document, URL, localStorage: storage, window: { addEventListener: () => {} },
        requestAnimationFrame: callback => callback(),
        Date: class extends Date { static now() { return now; } },
        setInterval: (callback, interval) => intervals.push({ callback, interval }),
        fetch: async (url, options) => {
            calls.push({ url, options });
            const error = url !== "/github/repositories" ? failure : null;
            // One run or PR is served from the project data, unless a test answers it itself.
            const item = typeof data !== "function" && !data.workflow && !data.pull
                && url.match(/\/(runs|pulls)\/(\d+)$/);
            const found = item && (item[1] === "runs"
                ? (data.workflows || []).find(run => run.id === Number(item[2]))
                : (data.pulls || []).find(pr => pr.number === Number(item[2])));
            if (item && !error) {
                return {
                    ok: !!found, status: found ? 200 : 404, headers: { get: () => "application/json" },
                    json: async () => !found ? { error: "Not found." } : item[1] === "runs" ? { workflow: found } : { pull: found },
                };
            }
            return {
                ok: !error,
                status: error ? error.status : 200,
                headers: { get: name => name === "content-type" ? "application/json" : error?.retryAfter },
                json: async () => error ? { error: error.message }
                    : url === "/github/repositories" ? { repositories } : typeof data === "function" ? data(url) : data,
            };
        },
    });
    vm.runInContext(source, context);
    await new Promise(setImmediate);
    return {
        calls, ids, context, document, intervals,
        setData: value => { data = value; },
        setFailure: value => { failure = value; },
        advance: ms => { now += ms; },
        tick: () => vm.runInContext("tick()", context),
        evaluate: expression => vm.runInContext(expression, context),
    };
}

test("projects are read in full every 60 seconds, whatever is running", async () => {
    const app = await harness();
    assert.equal(fullCalls(app).length, 1);
    app.advance(59999);
    await app.tick();
    assert.equal(fullCalls(app).length, 1);
    app.advance(1);
    await app.tick();
    assert.equal(fullCalls(app).length, 2);
    assert.ok(app.intervals.some(timer => timer.interval === 60000));
});

test("hidden tabs pause polling and rate-limit backoff prevents further requests", async () => {
    const app = await harness(idle);
    app.document.hidden = true;
    app.advance(60000);
    await app.tick();
    assert.equal(app.calls.length, 2);
    app.document.hidden = false;
    app.setFailure({ status: 429, message: "Rate limited", retryAfter: "120" });
    await app.tick();
    assert.equal(app.calls.length, 3);
    app.advance(119999);
    await app.tick();
    assert.equal(app.calls.length, 3);
    app.advance(1);
    app.setFailure(null);
    await app.tick();
    assert.equal(app.calls.length, 4);
});

test("diagram retains unstarted stages and draws dependency edges; unsafe check URLs are inert", async () => {
    const app = await harness();
    const graph = app.evaluate(`renderGraph([
        {key: "build", name: "Build", state: "done", needs: []},
        {key: "deploy", name: "Deploy", state: "queued", needs: ["build"]}
    ])`).children[0];
    const svg = graph.children[0];
    assert.equal(svg.children.length, 1);
    assert.match(svg.children[0].attributes.d, /^M144,26 C/);
    assert.equal(graph.children[1].children[1].textContent, "Done");
    assert.equal(graph.children[2].children[1].textContent, "Not started");
    assert.equal(app.evaluate('link("check", "javascript:alert(1)")').href, undefined);
    assert.equal(app.evaluate('link("check", "https://github.com/job/1")').rel, "noopener noreferrer");
});

function text(node) {
    return [node.textContent || "", ...(node.children || []).map(text)].join(" ");
}

test("PRs from all repositories share one list with repository labels, newest first", async () => {
    const other = { ...repo, full_name: "alice/another" };
    // The newer PR is ready and the older one has conflicts: opening time decides, not what needs attention.
    const app = await harness(url => url.endsWith("/another")
        ? { ...idle, pulls: [{ ...active.pulls[0], number: 2, title: "Ready", mergeable: true, checks: [], created_at: "2026-10-19T08:00:00Z" }] }
        : active, [other, repo]);
    const rows = app.ids.get("pulls").children;
    assert.equal(rows.length, 2);
    assert.equal(rows[0].dataset.key, "alice/another#2");
    assert.equal(rows[1].dataset.key, "alice/project#4");
    assert.match(text(rows[0]), /alice\/another/);
    assert.match(text(rows[1]), /alice\/project/);
    assert.match(text(rows[1]), /Build: Running/);
    assert.equal(app.ids.get("pull-count").textContent, "2");
    assert.equal(app.ids.get("projects").querySelectorAll(".pull").length, 0);
    assert.equal(app.ids.get("projects").querySelectorAll("button").length, 4);
});

test("completed check details stay inline and open across refreshes; completed-only rows are compact", async () => {
    const checks = [{ name: "Tests", state: "success", pending: false, url: "https://github.com/check/1" }];
    const app = await harness({ ...idle, pulls: [{ ...active.pulls[0], mergeable: true, checks }] });
    let row = app.ids.get("pulls").children[0];
    const details = row.querySelectorAll("details")[0];
    assert.equal(details.open, false);
    details.open = true;
    app.advance(60000);
    await app.tick();
    row = app.ids.get("pulls").children[0];
    assert.equal(row.querySelectorAll("details")[0].open, true);
    assert.match(text(row), /Tests: success/);
    app.setData(idle);
    app.advance(60000);
    await app.tick();
    assert.equal(app.ids.get("pull-count").textContent, "0");
    assert.match(text(app.ids.get("pulls")), /No open pull requests/);
});

test("failed refreshes retain aggregated PRs with a stale marker and recover without duplicates", async () => {
    const app = await harness();
    app.setFailure({ status: 502, message: "GitHub unavailable" });
    app.advance(60000);
    await app.tick();
    assert.equal(app.ids.get("pull-count").textContent, "1");
    assert.match(text(app.ids.get("pulls")), /Stale data/);
    assert.match(app.ids.get("summary").textContent, /1 refresh errors/);
    app.setFailure(null);
    app.advance(60000);
    await app.tick();
    assert.equal(app.ids.get("pulls").children.length, 1);
    assert.doesNotMatch(text(app.ids.get("pulls")), /Stale data/);
});

test("workflow diagrams fit the panel and expanded job details survive refreshes", async () => {
    const run = {
        id: 7, name: "Release", number: 5, status: "waiting", branch: "main", attempt: 1,
        url: "https://github.com/run/7", jobs: [],
        nodes: Array.from({ length: 6 }, (_, index) => ({
            key: `job${index}`, name: `Job ${index}`, state: index ? "queued" : "done",
            needs: index ? [`job${index - 1}`] : [],
        })),
    };
    const app = await harness({ ...idle, active: true, workflows: [run] });
    const list = app.ids.get("workflows");
    assert.equal(app.ids.get("workflow-count").textContent, "1");
    assert.match(text(list), /alice\/project/);
    assert.match(text(list), /Waiting/);
    const viewport = list.querySelectorAll(".graph-scroll")[0];
    assert.equal(viewport.firstElementChild.dataset.width, "984");
    assert.equal(viewport.firstElementChild.style.transform, `scale(${640 / 984})`);
    assert.equal(viewport.style.height, `${52 * 640 / 984}px`);
    list.querySelectorAll("details")[0].open = true;
    app.advance(10000);
    await app.tick();
    assert.equal(list.querySelectorAll("details")[0].open, true);
});

test("repository removal drops its aggregate rows and initial failures do not claim no PRs exist", async () => {
    const repositories = [repo, { ...repo, full_name: "alice/another" }];
    const app = await harness(active, repositories);
    assert.equal(app.ids.get("pull-count").textContent, "2");
    repositories.splice(0, 1);
    await app.evaluate("discover()");
    assert.equal(app.ids.get("pull-count").textContent, "1");
    assert.equal(app.ids.get("projects").children.length, 1);
    const failed = await harness();
    failed.evaluate('projects.get("alice/project").data = null');
    failed.setFailure({ status: 502, message: "GitHub unavailable" });
    failed.advance(60000);
    await failed.tick();
    assert.match(text(failed.ids.get("pulls")), /Waiting for project data/);
    assert.doesNotMatch(text(failed.ids.get("pulls")), /No open pull requests/);
});

test("publishing is a CSRF-protected POST with no automatic retries after an ambiguous failure", async () => {
    const app = await harness();
    app.setFailure({ status: 502, message: "Publication may have succeeded" });
    await app.evaluate(`publish(projects.get("alice/project"), document.getElementById("refresh"))`);
    const write = app.calls.find(call => call.options.method === "POST");
    assert.equal(write.url, "/github/repositories/alice/project/release");
    assert.equal(write.options.headers["X-CSRF-Token"], "csrf-token");
    assert.equal(app.ids.get("refresh").disabled, false);
    assert.equal(app.ids.get("refresh").textContent, "New Release");
    assert.match(app.evaluate('projects.get("alice/project").result.children[0].textContent'), /Check GitHub Actions/);
    app.advance(10000);
    await app.tick();
    assert.equal(app.calls.filter(call => call.options.method === "POST").length, 1);
});

test("release buttons reset after success and can publish again without reloading", async () => {
    const app = await harness();
    app.setData({ tag: "v1.20.0", url: "https://github.com/alice/project/actions?query=branch%3Av1.20.0" });
    await app.evaluate('publish(projects.get("alice/project"), document.getElementById("refresh"))');
    const button = app.ids.get("refresh");
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, "New Release");
    const result = app.evaluate('projects.get("alice/project").result.children[0]');
    assert.match(result.href, /\/actions\?/);
    assert.match(result.textContent, /view workflow/);
    app.setData({ tag: "v1.21.0", url: "https://github.com/alice/project/actions?query=branch%3Av1.21.0" });
    await app.evaluate('publish(projects.get("alice/project"), document.getElementById("refresh"))');
    assert.equal(app.calls.filter(call => call.options.method === "POST").length, 2);
    assert.match(app.evaluate('projects.get("alice/project").result.children[0].textContent'), /v1.21.0/);
});

test("concurrent publish calls produce only one write", async () => {
    const app = await harness();
    app.setData({ tag: "v1.20.0", url: "https://github.com/alice/project/actions" });
    await app.evaluate(`Promise.all([
        publish(projects.get("alice/project"), document.getElementById("refresh")),
        publish(projects.get("alice/project"), document.getElementById("refresh"))
    ])`);
    assert.equal(app.calls.filter(call => call.options.method === "POST").length, 1);
});

test("a failed subsequent release keeps its error visible when an older workflow refreshes", async () => {
    const app = await harness();
    app.setData({ tag: "v1.20.0", url: "https://github.com/alice/project/actions" });
    await app.evaluate('publish(projects.get("alice/project"), document.getElementById("refresh"))');
    app.setFailure({ status: 502, message: "Publication uncertain" });
    await app.evaluate('publish(projects.get("alice/project"), document.getElementById("refresh"))');
    app.setFailure(null);
    app.setData({ ...idle, active: true, workflows: [waitingRelease] });
    app.advance(10000);
    await app.tick();
    assert.match(text(app.evaluate('projects.get("alice/project").result')), /Publication uncertain/);
});

const waitingRelease = {
    id: 7, name: "Release", number: 5, status: "waiting", branch: "v1.20.0", attempt: 1,
    url: "https://github.com/alice/project/actions/runs/7", jobs: [], nodes: [],
    pending_deployments: [
        { id: 10, name: "production", can_approve: true, wait_timer: 0 },
        { id: 11, name: "protected", can_approve: false, wait_timer: 30 },
    ],
};

test("published link resolves to the discovered Actions run", async () => {
    const app = await harness();
    app.setData({ tag: "v1.20.0", url: "https://github.com/alice/project/actions?query=branch%3Av1.20.0" });
    await app.evaluate('publish(projects.get("alice/project"), document.getElementById("refresh"))');
    app.setData({ ...idle, active: true, workflows: [waitingRelease] });
    app.advance(10000);
    await app.tick();
    assert.equal(app.evaluate('projects.get("alice/project").result.children[0].href'), waitingRelease.url);
});

// The approval buttons, leaving out each running workflow's own Refresh button.
const approveButtons = app => app.ids.get("workflows").querySelectorAll("button")
    .filter(button => button.className !== "run-refresh");

test("waiting deployments have per-environment controls respecting reviewer eligibility", async () => {
    const app = await harness({ ...idle, active: true, workflows: [waitingRelease] });
    const buttons = approveButtons(app);
    assert.equal(buttons[0].textContent, "Approve production");
    assert.equal(buttons[0].disabled, false);
    assert.equal(buttons[1].disabled, true);
    assert.match(text(app.ids.get("workflows")), /30-minute wait timer/);
    await Promise.all([buttons[0].listeners.click(), buttons[0].listeners.click()]);
    const writes = app.calls.filter(call => call.options.method === "POST");
    assert.equal(writes.length, 1);
    assert.equal(writes[0].url, "/github/repositories/alice/project/runs/7/approve");
    assert.deepEqual(JSON.parse(writes[0].options.body), { environment_id: 10 });
    assert.equal(writes[0].options.headers["X-CSRF-Token"], "csrf-token");
    assert.equal(writes[0].options.headers["Content-Type"], "application/json");
    assert.equal(approveButtons(app)[0].textContent, "Approved");
    assert.equal(approveButtons(app)[0].disabled, true);
    await app.tick();
    assert.equal(app.calls.filter(call => call.options.method === "POST").length, 1);
    assert.equal(approveButtons(app)[0].disabled, true);
});

test("approval failures are visible, explicitly retryable and never automatically retried", async () => {
    const app = await harness({ ...idle, active: true, workflows: [waitingRelease] });
    app.setFailure({ status: 502, message: "The action may have succeeded" });
    await approveButtons(app)[0].listeners.click();
    assert.match(text(app.ids.get("workflows")), /may have succeeded/);
    assert.equal(approveButtons(app)[0].disabled, false);
    app.setFailure(null);
    await app.tick();
    assert.equal(app.calls.filter(call => call.options.method === "POST").length, 1);
    assert.match(text(app.ids.get("workflows")), /check deployment status/);
    await approveButtons(app)[0].listeners.click();
    assert.equal(app.calls.filter(call => call.options.method === "POST").length, 2);
});

test("refreshes recheck the release runs shown last time by ID", async () => {
    const app = await harness({ ...idle, active: true, workflows: [waitingRelease] });
    assert.equal(app.calls.at(-1).url, "/github/repositories/alice/project");
    await approveButtons(app)[0].listeners.click();
    await app.tick();
    assert.equal(app.calls.at(-1).url, "/github/repositories/alice/project?runs=7");
});

const another = { ...repo, full_name: "alice/another" };
const memory = (saved = {}) => ({
    saved,
    getItem(key) { return this.saved[key] ?? null; },
    setItem(key, value) { this.saved[key] = value; },
});
const PREFS = "github-dashboard:prefs";
const checkboxOf = (app, name) => app.evaluate(`projects.get("${name}")`).card.querySelectorAll("input")[0];
const projectCalls = (app, name) => app.calls.filter(call => call.url.includes(`/${name}`));

test("Refresh all skips projects marked as background-only, which still refresh on their own schedule", async () => {
    const storage = memory({ [PREFS]: JSON.stringify({ background: ["alice/another"] }) });
    const app = await harness(idle, [another, repo], storage);
    assert.equal(app.ids.get("refresh").textContent, "Refresh 1 of 2");
    assert.equal(checkboxOf(app, "alice/another").checked, false);
    assert.equal(projectCalls(app, "another").length, 1);
    await app.ids.get("refresh").listeners.click();
    await app.tick();
    assert.equal(projectCalls(app, "project").length, 2);
    assert.equal(projectCalls(app, "another").length, 1);
    app.advance(60000);
    await app.tick();
    assert.equal(projectCalls(app, "another").length, 2);
});

test("ticking the box changes what Refresh covers and is remembered", async () => {
    const storage = memory();
    const app = await harness(idle, [another, repo], storage);
    assert.equal(app.ids.get("refresh").textContent, "Refresh all");
    const box = checkboxOf(app, "alice/another");
    box.checked = false;
    box.listeners.change();
    assert.equal(app.ids.get("refresh").textContent, "Refresh 1 of 2");
    assert.deepEqual(JSON.parse(storage.saved[PREFS]).background, ["alice/another"]);
    box.checked = true;
    box.listeners.change();
    assert.equal(app.ids.get("refresh").textContent, "Refresh all");
    assert.deepEqual(JSON.parse(storage.saved[PREFS]).background, []);
});

test("hidden projects are neither shown nor fetched, and can be shown again", async () => {
    const storage = memory({ [PREFS]: JSON.stringify({ hidden: ["alice/another"] }) });
    const app = await harness(idle, [another, repo], storage);
    assert.equal(app.evaluate("projects.has('alice/another')"), false);
    assert.equal(projectCalls(app, "another").length, 0);
    assert.equal(app.ids.get("hidden-projects").hidden, false);
    assert.match(app.ids.get("summary").textContent, /1 repositories .* 1 hidden/);
    app.ids.get("hidden-projects").querySelectorAll("button")[0].listeners.click();
    await new Promise(setImmediate);
    assert.equal(app.evaluate("projects.has('alice/another')"), true);
    assert.equal(projectCalls(app, "another").length, 1);
    assert.deepEqual(JSON.parse(storage.saved[PREFS]).hidden, []);
    checkboxOf(app, "alice/another").parentElement.parentElement.querySelectorAll("button")[1].listeners.click();
    assert.equal(app.evaluate("projects.has('alice/another')"), false);
    assert.deepEqual(JSON.parse(storage.saved[PREFS]).hidden, ["alice/another"]);
    assert.equal(app.ids.get("project-count").textContent, "1");
});

test("preferences survive storage that is missing or unreadable", async () => {
    const broken = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
    for (const storage of [null, broken]) {
        const app = await harness(idle, [repo], storage);
        const box = checkboxOf(app, "alice/project");
        box.checked = false;
        box.listeners.change();
        assert.equal(app.ids.get("refresh").textContent, "Refresh 0 of 1");
    }
});

test("a section's Refresh reads only that section and leaves the other's schedule alone", async () => {
    const app = await harness({ ...idle, active: true, workflows: [waitingRelease] });
    app.advance(4000);
    await app.ids.get("refresh-pulls").listeners.click();
    assert.equal(fullCalls(app).at(-1).url, "/github/repositories/alice/project?section=pulls");
    app.advance(55999);
    await app.tick();
    assert.equal(fullCalls(app).length, 2, "the full refresh due 60s after the first is not pushed back");
    app.advance(1);
    await app.tick();
    assert.equal(fullCalls(app).at(-1).url, "/github/repositories/alice/project?runs=7");
    await app.ids.get("refresh-workflows").listeners.click();
    assert.equal(fullCalls(app).at(-1).url, "/github/repositories/alice/project?section=workflows&runs=7");
});

test("a section's data is merged into the other's", async () => {
    const app = await harness({ ...idle, workflows: [waitingRelease] });
    app.setData({ pulls: [] });
    app.advance(1000);
    await app.ids.get("refresh-pulls").listeners.click();
    assert.equal(app.ids.get("workflows").children.length, 1);
});

test("Refresh for a section skips background projects and asks for everything when a project has no data yet", async () => {
    const storage = memory({ [PREFS]: JSON.stringify({ background: ["alice/another"] }) });
    const app = await harness(idle, [another, repo], storage);
    await app.ids.get("refresh-pulls").listeners.click();
    assert.equal(projectCalls(app, "another").length, 1);
    app.setFailure({ status: 500, message: "boom" });
    app.advance(60000);
    await app.tick();
    app.setFailure(null);
    app.evaluate("projects.get('alice/project').data = null");
    await app.ids.get("refresh-pulls").listeners.click();
    assert.equal(app.calls.at(-1).url, "/github/repositories/alice/project");
});

function matrixRun(total, jobs = total) {
    return {
        id: 9, name: "Release", number: 6, status: "in_progress", branch: "main", attempt: 1,
        url: "https://github.com/run/9", jobs: [],
        nodes: [
            { key: "test", name: "test", state: "running", needs: [], done: 1, total,
                jobs: Array.from({ length: jobs }, (_, index) => ({ name: `test (${index})`, state: index ? "queued" : "done" })) },
            { key: "deploy", name: "deploy", state: "queued", needs: ["test"], done: null, total: null },
        ],
    };
}
const cards = app => app.ids.get("workflows").querySelectorAll(".node");
const toggle = app => app.ids.get("workflows").querySelectorAll(".matrix-toggle")[0];

test("a matrix of six or fewer jobs is drawn as a card per job, each feeding what needs the matrix", async () => {
    const app = await harness({ ...idle, active: true, workflows: [matrixRun(6)] });
    assert.equal(cards(app).length, 7);
    assert.deepEqual(cards(app).slice(0, 6).map(card => card.title),
        ["test (0)", "test (1)", "test (2)", "test (3)", "test (4)", "test (5)"]);
    assert.equal(app.ids.get("workflows").querySelectorAll("path").length, 6);
    assert.equal(toggle(app).textContent, "Collapse matrix jobs");
});

test("a matrix of more than six jobs stays one card with a count until expanded", async () => {
    const app = await harness({ ...idle, active: true, workflows: [matrixRun(7)] });
    assert.equal(cards(app).length, 2);
    assert.match(text(cards(app)[0]), /1\/7/);
    assert.equal(toggle(app).textContent, "Expand matrix jobs");
    await toggle(app).listeners.click();
    assert.equal(cards(app).length, 8);
    assert.equal(toggle(app).textContent, "Collapse matrix jobs");
});

test("the matrix choice survives refreshes, and small matrices can be collapsed", async () => {
    const app = await harness({ ...idle, active: true, workflows: [matrixRun(3)] });
    assert.equal(cards(app).length, 4);
    await toggle(app).listeners.click();
    assert.equal(cards(app).length, 2);
    app.advance(10000);
    await app.tick();
    assert.equal(cards(app).length, 2);
    assert.equal(toggle(app).textContent, "Expand matrix jobs");
});

test("workflows without a matrix have no matrix toggle", async () => {
    const run = matrixRun(3);
    run.nodes = run.nodes.slice(1);
    const app = await harness({ ...idle, active: true, workflows: [run] });
    assert.equal(toggle(app), undefined);
});

const finishedRelease = (id, finishedAt, conclusion = "success") => ({
    ...waitingRelease, id, number: id, status: "completed", conclusion, pending_deployments: [],
    finished_at: new Date(finishedAt).toISOString(),
});
const workflowsText = app => text(app.ids.get("workflows"));

test("a running workflow's Refresh reads only that run and leaves the project's schedule alone", async () => {
    const app = await harness({ ...idle, active: true, workflows: [waitingRelease] });
    const due = app.evaluate('projects.get("alice/project").nextRefresh');
    const running = { ...waitingRelease, status: "in_progress", pending_deployments: [] };
    app.setData(url => url.endsWith("/runs/7") ? { workflow: running } : idle);
    const refresh = app.ids.get("workflows").querySelectorAll(".run-refresh")[0];
    assert.equal(refresh.textContent, "Refresh");
    const pending = refresh.listeners.click();
    assert.equal(app.ids.get("workflows").querySelectorAll(".run-refresh")[0].disabled, true);
    await pending;
    assert.equal(app.calls.at(-1).url, "/github/repositories/alice/project/runs/7");
    assert.equal(app.calls.at(-1).options.method, undefined);
    assert.match(workflowsText(app), /Running/);
    assert.doesNotMatch(workflowsText(app), /Approve/);
    assert.equal(app.evaluate('projects.get("alice/project").nextRefresh'), due);
});

test("a failed run Refresh shows its error on that run and can be tried again", async () => {
    const app = await harness({ ...idle, active: true, workflows: [waitingRelease] });
    app.setFailure({ status: 404, message: "This release workflow run no longer exists." });
    await app.ids.get("workflows").querySelectorAll(".run-refresh")[0].listeners.click();
    assert.match(workflowsText(app), /no longer exists/);
    assert.equal(app.ids.get("workflows").querySelectorAll(".run-refresh")[0].disabled, false);
    assert.match(workflowsText(app), /Approve production/);
});

test("the latest finished release shows in full for two minutes, then as one line", async () => {
    const app = await harness({ ...idle, workflows: [finishedRelease(7, 100000 - 60000)] });
    assert.match(workflowsText(app), /Succeeded/);
    assert.equal(app.ids.get("workflows").querySelectorAll(".run-refresh").length, 0);
    assert.equal(app.ids.get("workflows").querySelectorAll(".graph-scroll").length, 1);
    assert.match(app.ids.get("summary").textContent, /0 ongoing releases/);
    app.advance(59000);
    await app.tick();
    assert.equal(app.ids.get("workflows").querySelectorAll(".graph-scroll").length, 1);
    app.advance(1000);
    await app.tick();
    assert.equal(app.ids.get("workflows").querySelectorAll(".graph-scroll").length, 0);
    assert.equal(app.ids.get("workflows").querySelectorAll(".collapsed").length, 1);
    assert.match(workflowsText(app), /Release #7 .*Succeeded/);
    assert.equal(app.ids.get("workflow-count").textContent, "1");
});

test("a finished release collapses at once when a newer one runs, and can be opened again", async () => {
    const app = await harness({ ...idle, active: true, workflows: [{ ...waitingRelease, id: 9 }, finishedRelease(8, 100000, "failure")] });
    const list = app.ids.get("workflows");
    assert.equal(list.querySelectorAll(".collapsed").length, 1);
    assert.match(workflowsText(app), /Failed/);
    assert.match(app.ids.get("summary").textContent, /1 ongoing releases/);
    list.querySelectorAll(".finished-toggle")[0].listeners.click();
    assert.equal(list.querySelectorAll(".collapsed").length, 0);
    assert.equal(list.querySelectorAll(".finished-toggle")[0].textContent, "Collapse");
    app.advance(60000);
    await app.tick();
    assert.equal(list.querySelectorAll(".collapsed").length, 0);
    assert.equal(fullCalls(app).at(-1).url, "/github/repositories/alice/project?runs=9", "finished runs aren't rechecked");
});

test("a run refreshed to finished replaces the older finished release", async () => {
    const app = await harness({ ...idle, active: true, workflows: [{ ...waitingRelease, id: 9, number: 9 }, finishedRelease(8, 0)] });
    app.setData({ workflow: finishedRelease(9, 100000) });
    await app.ids.get("workflows").querySelectorAll(".run-refresh")[0].listeners.click();
    assert.match(workflowsText(app), /Release #9/);
    assert.doesNotMatch(workflowsText(app), /Release #8/);
    assert.equal(app.ids.get("workflows").querySelectorAll(".graph-scroll").length, 1);
});

const quickCalls = (app, item) => app.calls.filter(call => call.url.endsWith(`/alice/project/${item}`));
const pool = app => [...app.evaluate('projects.get("alice/project")').pool.keys()];
async function steps(app, count, ms = 5000) {
    for (let index = 0; index < count; index += 1) {
        app.advance(ms);
        await app.tick();
    }
}
const busyPull = (checks = "in_progress") => ({ ...active.pulls[0], checks: [{ name: "Build", state: checks, pending: true }] });

test("a busy PR and a running release are polled on their own every 5 seconds, and leave once done", async () => {
    const app = await harness({ ...idle, pulls: [busyPull()], workflows: [waitingRelease] });
    assert.deepEqual(pool(app).sort(), ["pull:4", "run:7"]);
    let step = 0;
    app.setData(url => {
        step += 1;
        if (url.endsWith("/pulls/4")) return { pull: { ...busyPull(`step ${step}`), refresh: step < 3 } };
        if (url.endsWith("/runs/7")) return { workflow: { ...waitingRelease, status: "in_progress", attempt: step } };
        return idle;
    });
    await steps(app, 1, 4999);
    assert.equal(quickCalls(app, "pulls/4").length, 0);
    await steps(app, 1, 1);
    assert.equal(quickCalls(app, "pulls/4").length, 1);
    assert.equal(quickCalls(app, "runs/7").length, 1);
    await steps(app, 2);
    assert.equal(quickCalls(app, "pulls/4").length, 2, "the PR left the pool once its checks were done");
    assert.deepEqual(pool(app), ["run:7"]);
    assert.equal(quickCalls(app, "runs/7").length, 3);
    assert.equal(fullCalls(app).length, 1);
});

test("an item that stops changing leaves after 10 polls, and a full read brings it back only once it changes", async () => {
    const app = await harness({ ...idle, workflows: [waitingRelease] });
    await steps(app, 10);
    assert.equal(quickCalls(app, "runs/7").length, 10);
    assert.deepEqual(pool(app), []);
    await steps(app, 2);
    assert.equal(quickCalls(app, "runs/7").length, 10);
    await steps(app, 1, 60000);
    assert.equal(fullCalls(app).length, 3);
    assert.deepEqual(pool(app), [], "full reads that find it the same leave it out");
    app.setData({ ...idle, workflows: [{ ...waitingRelease, status: "in_progress" }] });
    await steps(app, 1, 60000);
    assert.deepEqual(pool(app), ["run:7"]);
    await steps(app, 1);
    assert.equal(quickCalls(app, "runs/7").length, 11);
});

test("a PR that closes between full reads goes from the list", async () => {
    const app = await harness({ ...idle, pulls: [busyPull()] });
    app.setData(idle);
    await steps(app, 1);
    assert.equal(app.ids.get("pull-count").textContent, "0");
    assert.deepEqual(pool(app), []);
});

test("after New Release, full reads come every 10 seconds until its run appears", async () => {
    const app = await harness(idle);
    app.setData({ tag: "v1.20.0", url: "https://github.com/alice/project/actions" });
    await app.evaluate('publish(projects.get("alice/project"), document.getElementById("refresh"))');
    app.setData(idle);
    await steps(app, 2, 10000);
    assert.equal(fullCalls(app).length, 3);
    app.setData({ ...idle, workflows: [waitingRelease] });
    await steps(app, 1, 10000);
    assert.equal(fullCalls(app).length, 4);
    await steps(app, 5, 10000);
    assert.equal(fullCalls(app).length, 4, "back to once a minute once the run is shown");
});

test("the PR order can be picked, and is remembered", async () => {
    const storage = memory();
    const other = { ...repo, full_name: "alice/another" };
    const ready = { ...active.pulls[0], number: 2, title: "Ready", mergeable: true, checks: [], created_at: "2026-10-19T08:00:00Z" };
    const app = await harness(url => url.endsWith("/another") ? { ...idle, pulls: [ready] } : active, [other, repo], storage);
    const keys = () => app.ids.get("pulls").children.map(row => row.dataset.key);
    const order = app.ids.get("pull-order");
    assert.equal(order.value, "newest");
    assert.deepEqual(keys(), ["alice/another#2", "alice/project#4"]);
    for (const [value, expected] of [["oldest", ["alice/project#4", "alice/another#2"]],
        ["attention", ["alice/project#4", "alice/another#2"]], ["newest", ["alice/another#2", "alice/project#4"]]]) {
        order.value = value;
        order.listeners.change();
        assert.deepEqual(keys(), expected, value);
    }
    order.value = "oldest";
    order.listeners.change();
    assert.equal(JSON.parse(storage.saved[PREFS]).order, "oldest");
    const again = await harness(active, [repo], storage);
    assert.equal(again.ids.get("pull-order").value, "oldest");
    const unknown = await harness(active, [repo], memory({ [PREFS]: JSON.stringify({ order: "constructor" }) }));
    assert.equal(unknown.ids.get("pull-order").value, "newest");
});

test("a small matrix whose jobs were all skipped stays one card until expanded", async () => {
    const run = matrixRun(3);
    run.nodes[0] = { ...run.nodes[0], state: "skipped", done: 3,
        jobs: run.nodes[0].jobs.map(job => ({ ...job, state: "skipped" })) };
    const app = await harness({ ...idle, workflows: [run] });
    assert.equal(cards(app).length, 2);
    assert.match(text(cards(app)[0]), /Skipped · 3\/3/);
    assert.equal(toggle(app).textContent, "Expand matrix jobs");
    toggle(app).listeners.click();
    assert.equal(cards(app).length, 4);
});
