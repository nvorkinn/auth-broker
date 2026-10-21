// The guided tour of a screen's settings page: driver.js walks through the sections, and beside each one shows
// a sample picture of the screen with the part that section sets up picked out. It starts on its own the first
// time a browser opens a screen's page, and "Take the tour" plays it again.
(function () {
    "use strict";

    // The sample picture is a real frame from the renderer, 800 by 480 like the screen. Each region is a box in
    // its pixels, [left, top, right, bottom]; the parts outside a step's regions are faded.
    const SCREEN = { width: 800, height: 480 };
    const REGIONS = {
        departures: [0, 0, 552, 250],
        weather: [556, 0, 800, 196],
        energy: [548, 198, 800, 480],
        music: [0, 270, 548, 400],
        notices: [0, 402, 548, 480],
        clock: [500, 280, 548, 306],
        axis: [556, 456, 800, 480],
    };

    const STEPS = [
        {
            section: "hero",
            title: "Your screen, section by section",
            text: "Everything on the e-paper screen is set up on this page. This tour takes a minute and shows what each "
                + "section puts on the screen, using a sample picture like this one.",
            regions: [],
        },
        {
            section: "general",
            title: "General",
            text: "The clock format sets every time on the screen, like the one by the music and the hours under "
                + "the energy chart. The language changes all of the screen's text.",
            regions: ["clock", "axis"],
        },
        {
            section: "style",
            title: "Style",
            text: "The style is the look of the departure rows, the notices and the rest: airy, bold headers or "
                + "rounded cards. The pictures here show each one.",
            regions: ["departures", "notices"],
        },
        {
            section: "weather",
            title: "Weather",
            text: "The forecast in the top right is for this town or city: now, tonight, tomorrow and the weekend.",
            regions: ["weather"],
        },
        {
            section: "notice_board",
            title: "Notice board",
            text: "The notices along the bottom: line disruptions for your stops, weather warnings and public holidays. "
                + "The postcode picks the local ones.",
            regions: ["notices"],
        },
        {
            section: "departures",
            title: "Departures",
            text: "Your stops take the top left, in the order you put them in, counting down the minutes to each "
                + "bus, Tube train, metro or tram.",
            regions: ["departures"],
        },
        {
            section: "music",
            title: "Music",
            text: "With Spotify connected the screen shows what's playing and what's next. Apple Music shows what "
                + "you played last. Between songs, your top tracks and artists.",
            regions: ["music"],
        },
        {
            section: "developer",
            title: "Developer",
            text: "With a GitHub token the screen can show a developer panel: your pull requests, their checks "
                + "and the releases running.",
            regions: [],
            picture: false,
        },
        {
            section: "energy",
            title: "Energy",
            text: "A Glowmarkt (Bright) account puts your home's electricity use, hour by hour over the last day, "
                + "down the right.",
            regions: ["energy"],
        },
        {
            section: "screen",
            title: "Screen",
            text: "The name is just for this site, so you can tell your screens apart. Press Save at the bottom "
                + "when you're done, and the screen picks the changes up on its next refresh.",
            regions: [],
            picture: false,
        },
    ];

    const SEEN_KEY = "countdown-config-tour-seen";
    const SVG = "http://www.w3.org/2000/svg";

    function svgElement(name, attributes) {
        const element = document.createElementNS(SVG, name);
        for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, value);
        return element;
    }

    // The sample picture with a step's regions left clear and everything else faded to paper: an SVG so the boxes
    // scale with the popover, which is narrow on a phone.
    function picture(step, src, index) {
        const svg = svgElement("svg", {
            class: "tour-picture",
            viewBox: `0 0 ${SCREEN.width} ${SCREEN.height}`,
            role: "img",
            "aria-label": "A sample screen with the part this section sets up picked out",
        });
        const image = svgElement("image", { href: src, width: SCREEN.width, height: SCREEN.height });
        svg.append(image);
        if (step.regions.length === 0) return svg;

        const maskId = `tour-mask-${index}`;
        const mask = svgElement("mask", { id: maskId });
        mask.append(svgElement("rect", { width: SCREEN.width, height: SCREEN.height, fill: "#fff" }));
        for (const name of step.regions) {
            const [left, top, right, bottom] = REGIONS[name];
            mask.append(svgElement("rect", { x: left, y: top, width: right - left, height: bottom - top, fill: "#000" }));
        }
        svg.append(mask);
        svg.append(svgElement("rect", {
            class: "tour-fade", width: SCREEN.width, height: SCREEN.height, mask: `url(#${maskId})`,
        }));
        for (const name of step.regions) {
            const [left, top, right, bottom] = REGIONS[name];
            svg.append(svgElement("rect", {
                class: "tour-box", x: left, y: top, width: right - left, height: bottom - top, rx: 4,
            }));
        }
        return svg;
    }

    function start(options) {
        const driver = window.driver.js.driver;
        const steps = STEPS.map((step, index) => ({
            element: `[data-tour="${step.section}"]`,
            popover: {
                title: step.title,
                description: step.text,
                side: options.wide ? "left" : "bottom",
                align: "start",
                onPopoverRender(popover) {
                    if (step.picture === false) return;
                    popover.description.before(picture(step, options.picture, index));
                },
            },
        }));
        const tour = driver({
            steps,
            showProgress: true,
            progressText: "{{current}} of {{total}}",
            nextBtnText: "Next",
            prevBtnText: "Back",
            doneBtnText: "Done",
            popoverClass: "tour-popover",
            overlayColor: "#141517",
            overlayOpacity: 0.55,
            stagePadding: 6,
            stageRadius: 10,
            smoothScroll: true,
            onDestroyed() {
                try {
                    localStorage.setItem(SEEN_KEY, "1");
                } catch {
                    // Private browsing may refuse: the tour then plays again next time, which is fine.
                }
            },
        });
        tour.drive();
        return tour;
    }

    function seen() {
        try {
            return localStorage.getItem(SEEN_KEY) === "1";
        } catch {
            return false;
        }
    }

    function setUp() {
        const script = document.currentScript;
        const options = {
            picture: script.dataset.picture,
            // On a wide window the sections sit in two columns, and the popover fits beside one.
            wide: () => window.matchMedia("(min-width: 1100px)").matches,
        };
        const play = () => start({ picture: options.picture, wide: options.wide() });
        const link = document.getElementById("tourStart");
        if (link) link.addEventListener("click", play);
        // The first visit starts the tour once the page's pictures and fonts are in, so the highlights fit.
        if (!seen()) window.addEventListener("load", play, { once: true });
    }

    if (typeof module !== "undefined") {
        module.exports = { STEPS, REGIONS, SCREEN, SEEN_KEY, picture };
    } else {
        setUp();
    }
})();
