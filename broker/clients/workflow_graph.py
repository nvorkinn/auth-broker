"""A workflow run's jobs as GitHub's graph draws them: which job waits for which, and how far each has got.

GitHub's API doesn't give the graph, only the run's jobs and their states. The lines between jobs (`needs:`) come
from the workflow file at the run's commit, and the jobs not started yet come from it too, so the whole graph shows
from the run's first minute. A matrix, or a reusable workflow's jobs, is one node with a count (and, when asked, its
jobs listed so the dashboard can draw them apart).
"""

import itertools
import re
from dataclasses import dataclass

import yaml

# How a finished job ends when it didn't pass.
FAILED = {"failure", "timed_out", "startup_failure", "cancelled"}
EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
MATRIX_KEY = re.compile(r"matrix\.([A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class WorkflowJob:
    """A job as the workflow file has it: its id, what GitHub will call its runs, and what it waits for."""

    key: str
    # What the graph calls it: its name, unless that's worked out at run time (then its id, as GitHub's graph does).
    label: str
    needs: tuple[str, ...]
    # The names its runs get, when the file says (a static matrix spelt out); else None, and `pattern` decides.
    names: frozenset[str] | None
    pattern: re.Pattern[str]
    # A matrix's jobs, when the file says how many; None for a plain job.
    size: int | None


def workflow_jobs(text: str) -> list[WorkflowJob]:
    """Return the jobs of the workflow file `text`, in the file's order.

    Raises ValueError if it isn't a workflow.
    """
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError as e:
        msg = f"not YAML: {e}"
        raise ValueError(msg) from e
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    if not isinstance(jobs, dict):
        msg = "no jobs in the workflow file"
        raise ValueError(msg)  # noqa: TRY004 -- the caller treats every unreadable file alike
    return [_job(str(key), job or {}) for key, job in jobs.items()]


def _job(key: str, job: dict) -> WorkflowJob:
    needs = job.get("needs") or []
    needs = (needs,) if isinstance(needs, str) else tuple(needs)
    template = str(job.get("name", key))
    matrix = (job.get("strategy") or {}).get("matrix")
    combos = _combinations(matrix) if matrix is not None else None
    names = None
    if "uses" in job:
        pattern = re.compile(f"^{_regex(template)} / .+$")
    elif matrix is not None:
        pattern = re.compile(f"^{_regex(template)}( \\(.*\\))?$")
        if combos and EXPRESSION.search(template):
            names = _rendered(template, combos)
    else:
        pattern = re.compile(f"^{_regex(template)}$")
        if not EXPRESSION.search(template):
            names = frozenset({template})
    return WorkflowJob(
        key=key,
        label=key if EXPRESSION.search(template) else template,
        needs=needs,
        names=names,
        pattern=pattern,
        size=len(combos) if combos else None,
    )


def _regex(template: str) -> str:
    """Return a pattern for the names `template` can render to: its text as is, each ${{ }} as any text."""
    return ".+?".join(re.escape(part) for part in EXPRESSION.split(template)[::2])


def _combinations(matrix: object) -> list[dict] | None:
    """Return a static matrix's combinations, as GitHub expands them; None if it's an expression."""
    if not isinstance(matrix, dict):
        return None
    dims = {k: v for k, v in matrix.items() if k not in {"include", "exclude"}}
    include, exclude = matrix.get("include") or [], matrix.get("exclude") or []
    if not all(isinstance(v, list) for v in dims.values()) or not isinstance(include, list):
        return None
    base = [dict(zip(dims, values, strict=True)) for values in itertools.product(*dims.values())] if dims else []
    base = [c for c in base if not any(all(c.get(k) == v for k, v in e.items()) for e in exclude)]
    combos = [dict(c) for c in base]
    for entry in include:
        # Added to every combination it doesn't contradict; a combination of its own if there's none.
        fits = [i for i, c in enumerate(base) if all(c.get(k, v) == v for k, v in entry.items() if k in dims)]
        for i in fits:
            combos[i] |= entry
        if not fits:
            combos.append(dict(entry))
    return combos


def _rendered(template: str, combos: list[dict]) -> frozenset[str] | None:
    """Return the names `template` renders to, a combination each; None if it uses more than matrix values."""
    names = set()
    for combo in combos:

        def value(match: re.Match[str], combo: dict = combo) -> str:
            key = MATRIX_KEY.fullmatch(match.group(1))
            if key is None or key.group(1) not in combo:
                raise LookupError(match.group(1))
            return str(combo[key.group(1)])

        try:
            names.add(EXPRESSION.sub(value, template))
        except LookupError:
            return None
    return frozenset(names)


def nodes(jobs: list[WorkflowJob], runs: list[dict], *, list_jobs: bool = False) -> list[dict]:
    """Return the graph's nodes: each workflow job with the state of its runs (GitHub's jobs), in the file's order.

    A run that matches no job in the file is a node of its own, so nothing that's running goes unseen. With `list_jobs`,
    a node of several jobs also lists them.
    """
    matched: dict[str, list[dict]] = {job.key: [] for job in jobs}
    left = []
    for run in runs:
        job = _owner(jobs, run["name"])
        if job is None:
            left.append(run)
        else:
            matched[job.key].append(run)
    found = [_node(job.key, job.label, list(job.needs), matched[job.key], job.size) for job in jobs]
    found += [_node(run["name"], run["name"], [], [run], None) for run in left]
    if not list_jobs:
        for node in found:
            node.pop("jobs", None)
    return found


def _owner(jobs: list[WorkflowJob], name: str) -> WorkflowJob | None:
    """Return the job a run named `name` belongs to: one naming it outright, else the most specific pattern."""
    for job in jobs:
        if job.names is not None and name in job.names:
            return job
    candidates = [job for job in jobs if job.names is None and job.pattern.match(name)]
    return max(candidates, key=lambda job: len(job.pattern.pattern), default=None)


def _node(key: str, name: str, needs: list[str], runs: list[dict], size: int | None) -> dict:
    # A count only for a node of several jobs (a matrix, or a reusable workflow's), as one job is done or it isn't.
    total = max(len(runs), size or 0)
    counted = total > 1
    state = _state(runs)
    if len(runs) < total and state in {"done", "skipped"}:
        state = "running"
    node = {
        "key": key,
        "name": name,
        "state": state,
        "needs": needs,
        "done": sum(run["status"] == "completed" for run in runs) if counted else None,
        "total": total if counted else None,
    }
    if counted:
        # Each job of the node, so the page can draw them apart; those the matrix hasn't started yet are only a count.
        node["jobs"] = [{"name": run["name"], "state": _state([run])} for run in runs]
        node["jobs"] += [{"name": f"{name} (not started)", "state": "queued"}] * (total - len(runs))
    return node


def _state(runs: list[dict]) -> str:
    """Return one state for a job's runs: failed beats waiting for approval, which beats running."""
    if not runs:
        return "queued"
    if any(run.get("conclusion") in FAILED for run in runs):
        return "failed"
    if any(run["status"] == "waiting" for run in runs):
        return "approve"
    if all(run["status"] == "completed" for run in runs):
        return "skipped" if all(run.get("conclusion") == "skipped" for run in runs) else "done"
    if any(run["status"] in {"in_progress", "completed"} for run in runs):
        return "running"
    return "queued"


def run_state(run: dict) -> str:
    """Return the whole run's state: done, failed, or running (which covers queued and waiting)."""
    if run["status"] != "completed":
        return "running"
    return "done" if run.get("conclusion") in {"success", "skipped", "neutral"} else "failed"
