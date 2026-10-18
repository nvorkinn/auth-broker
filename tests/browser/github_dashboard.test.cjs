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
        checks: [{ name: "Build", state: "in_progress", pending: true }],
    }],
};

async function harness(initial = active, repositories = [repo]) {
    let now = 100000;
    let data = initial;
    let failure = null;
    const calls = [];
    const ids = new Map(["summary", "error", "projects", "refresh", "pulls", "workflows",
        "pull-count", "workflow-count", "project-count"].map(id => [id, new Element("div")]));
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
        document, URL, window: { addEventListener: () => {} },
        requestAnimationFrame: callback => callback(),
        Date: class extends Date { static now() { return now; } },
        setInterval: (callback, interval) => intervals.push({ callback, interval }),
        fetch: async (url, options) => {
            calls.push({ url, options });
            const error = url !== "/github/repositories" ? failure : null;
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

test("pending PR checks refresh after 10 seconds, switching to 60 seconds when idle", async () => {
    const app = await harness();
    const projectCalls = () => app.calls.filter(call => call.url.endsWith("/alice/project")).length;
    assert.equal(projectCalls(), 1);
    app.advance(9999);
    await app.tick();
    assert.equal(projectCalls(), 1);
    app.setData(idle);
    app.advance(1);
    await app.tick();
    assert.equal(projectCalls(), 2);
    app.advance(59999);
    await app.tick();
    assert.equal(projectCalls(), 2);
    app.advance(1);
    await app.tick();
    assert.equal(projectCalls(), 3);
    assert.ok(app.intervals.some(timer => timer.interval === 60000));
});

test("hidden tabs pause polling and rate-limit backoff prevents further requests", async () => {
    const app = await harness();
    app.document.hidden = true;
    app.advance(10000);
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

test("PRs from all repositories share one list with repository labels and attention first", async () => {
    const other = { ...repo, full_name: "alice/another" };
    const app = await harness(url => url.endsWith("/another")
        ? { ...idle, pulls: [{ ...active.pulls[0], number: 2, title: "Ready", mergeable: true, checks: [] }] }
        : active, [other, repo]);
    const rows = app.ids.get("pulls").children;
    assert.equal(rows.length, 2);
    assert.equal(rows[0].dataset.key, "alice/project#4");
    assert.equal(rows[1].dataset.key, "alice/another#2");
    assert.match(text(rows[0]), /alice\/project/);
    assert.match(text(rows[1]), /alice\/another/);
    assert.match(text(rows[0]), /Build: Running/);
    assert.equal(app.ids.get("pull-count").textContent, "2");
    assert.equal(app.ids.get("projects").querySelectorAll(".pull").length, 0);
    assert.equal(app.ids.get("projects").querySelectorAll("button").length, 2);
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
    app.advance(10000);
    await app.tick();
    assert.equal(app.ids.get("pull-count").textContent, "1");
    assert.match(text(app.ids.get("pulls")), /Stale data/);
    assert.match(app.ids.get("summary").textContent, /1 refresh errors/);
    app.setFailure(null);
    app.advance(10000);
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
    failed.advance(10000);
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
    assert.equal(app.ids.get("refresh").disabled, true);
    assert.match(app.evaluate('projects.get("alice/project").result.children[0].textContent'), /Check releases/);
    app.advance(10000);
    await app.tick();
    assert.equal(app.calls.filter(call => call.options.method === "POST").length, 1);
});
