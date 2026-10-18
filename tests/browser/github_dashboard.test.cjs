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
    }
    append(...children) { this.children.push(...children); }
    prepend(...children) { this.children.unshift(...children); }
    replaceChildren(...children) { this.children = children; }
    setAttribute(name, value) { this.attributes[name] = value; }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    remove() { this.removed = true; }
    querySelectorAll(selector) {
        return this.children.filter(child => child instanceof Element).flatMap(child => [
            ...(child.tag === "details" && (selector !== "details[open]" || child.open) ? [child] : []),
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

async function harness(initial = active) {
    let now = 100000;
    let data = initial;
    let failure = null;
    const calls = [];
    const ids = new Map(["summary", "error", "projects", "refresh"].map(id => [id, new Element("div")]));
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
        document, URL,
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
                    : url === "/github/repositories" ? { repositories: [repo] } : data,
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
    assert.match(svg.children[0].attributes.d, /^M210,38 C/);
    assert.equal(graph.children[1].children[1].textContent, "Done");
    assert.equal(graph.children[2].children[1].textContent, "Not started");
    assert.equal(app.evaluate('link("check", "javascript:alert(1)")').href, undefined);
    assert.equal(app.evaluate('link("check", "https://github.com/job/1")').rel, "noopener noreferrer");
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
