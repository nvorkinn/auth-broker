const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const tour = require("../../broker/static/config_tour.js");
const template = fs.readFileSync(path.join(__dirname, "../../broker/templates/device.html"), "utf8");

test("every step points at a section of the page, in the page's order", () => {
    const sections = [...template.matchAll(/data-tour="([a-z_]+)"/g)].map(m => m[1]);
    assert.deepEqual(tour.STEPS.map(step => step.section), sections);
});

test("every region is a box inside the sample picture", () => {
    for (const [name, [left, top, right, bottom]] of Object.entries(tour.REGIONS)) {
        assert.ok(left >= 0 && top >= 0 && left < right && top < bottom, name);
        assert.ok(right <= tour.SCREEN.width && bottom <= tour.SCREEN.height, name);
    }
    for (const step of tour.STEPS) for (const region of step.regions) assert.ok(region in tour.REGIONS, region);
});

// Just enough of the DOM to see what picture() draws.
class Node {
    constructor(name) {
        this.name = name;
        this.attributes = {};
        this.children = [];
    }
    setAttribute(key, value) {
        this.attributes[key] = String(value);
    }
    append(...children) {
        this.children.push(...children);
    }
    find(name) {
        return this.children.flatMap(child => [...(child.name === name ? [child] : []), ...child.find(name)]);
    }
}
global.document = { createElementNS: (_ns, name) => new Node(name) };

test("a step's picture fades everything but its regions and outlines them", () => {
    const step = tour.STEPS.find(s => s.regions.length === 2);
    const svg = tour.picture(step, "/static/display/tour.png", 3);
    assert.equal(svg.attributes.viewBox, "0 0 800 480");
    assert.equal(svg.find("image")[0].attributes.href, "/static/display/tour.png");
    const [mask] = svg.find("mask");
    assert.equal(mask.attributes.id, "tour-mask-3");
    assert.equal(mask.children.length, 3);
    const boxes = svg.find("rect").filter(rect => rect.attributes.class === "tour-box");
    assert.deepEqual(boxes.map(box => [Number(box.attributes.x), Number(box.attributes.y)]),
        step.regions.map(name => tour.REGIONS[name].slice(0, 2)));
    assert.equal(svg.find("rect").find(rect => rect.attributes.class === "tour-fade").attributes.mask, "url(#tour-mask-3)");
});

test("a step without regions shows the whole picture clear", () => {
    const svg = tour.picture(tour.STEPS[0], "/static/display/tour.png", 0);
    assert.equal(svg.find("mask").length, 0);
    assert.equal(svg.find("rect").length, 0);
});
