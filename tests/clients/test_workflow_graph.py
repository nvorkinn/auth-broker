import pytest

from broker.clients import workflow_graph

# The shapes the real release workflows have: plain jobs, a static matrix named from its values, a matrix crossed
# with an include, a reusable workflow, and needs as a string or a list.
WORKFLOW = """
name: Release
on:
  push:
    tags: ["v*"]
jobs:
  tag:
    runs-on: ubuntu-latest
  images:
    name: image ${{ matrix.image }}
    needs: tag
    strategy:
      matrix:
        include:
          - image: broker
          - image: caddy
  build:
    name: build ${{ matrix.image }} (${{ matrix.platform }})
    needs: [tag]
    strategy:
      matrix:
        image: [server, providers]
        platform: [linux/amd64, linux/arm64]
        include:
          - platform: linux/arm64
            runner: ubuntu-24.04-arm
  deploy:
    name: ${{ matrix.environment }}
    needs: [tag, images, build]
    strategy:
      matrix:
        include:
          - environment: deploy-caddy
          - environment: deploy-config
  terraform:
    needs: deploy
    uses: ./.github/workflows/terraform.yml
"""


def _run(name, status="completed", conclusion="success"):
    return {"name": name, "status": status, "conclusion": conclusion}


def _by_key(nodes):
    return {node["key"]: node for node in nodes}


def test_workflow_jobs_reads_each_jobs_needs_and_the_names_its_runs_get():
    jobs = {job.key: job for job in workflow_graph.workflow_jobs(WORKFLOW)}

    assert list(jobs) == ["tag", "images", "build", "deploy", "terraform"]
    assert jobs["images"].needs == ("tag",)
    assert jobs["deploy"].needs == ("tag", "images", "build")
    assert jobs["images"].names == {"image broker", "image caddy"}
    assert jobs["build"].names == {
        "build server (linux/amd64)",
        "build server (linux/arm64)",
        "build providers (linux/amd64)",
        "build providers (linux/arm64)",
    }
    assert jobs["build"].size == 4
    assert jobs["deploy"].names == {"deploy-caddy", "deploy-config"}


def test_a_job_named_at_run_time_is_called_by_its_id_and_a_static_name_is_kept():
    jobs = workflow_graph.workflow_jobs(
        "jobs:\n  lint:\n    name: Lint and format\n  deploy:\n    name: ${{ inputs.env }}\n"
    )

    assert [job.label for job in jobs] == ["Lint and format", "deploy"]


def test_a_matrix_include_that_fits_no_combination_is_one_of_its_own_and_exclude_drops_one():
    matrix = {
        "python": ["3.13", "3.14"],
        "os": ["linux", "mac"],
        "exclude": [{"python": "3.13", "os": "mac"}],
        "include": [{"python": "3.15", "os": "linux"}],
    }

    combos = workflow_graph._combinations(matrix)

    assert len(combos) == 4
    assert {"python": "3.15", "os": "linux"} in combos
    assert {"python": "3.13", "os": "mac"} not in combos


def test_a_matrix_from_an_expression_has_no_size_and_its_runs_are_matched_by_pattern():
    jobs = workflow_graph.workflow_jobs(
        "jobs:\n  test:\n    strategy:\n      matrix: ${{ fromJSON(needs.x.outputs.m) }}\n"
    )
    nodes = workflow_graph.nodes(
        jobs, [_run("test (3.14, linux)"), _run("test (3.13, linux)", "in_progress", None)], list_jobs=True
    )

    assert jobs[0].size is None
    assert nodes == [
        {
            "key": "test",
            "name": "test",
            "state": "running",
            "needs": [],
            "done": 1,
            "total": 2,
            "jobs": [
                {"name": "test (3.14, linux)", "state": "done"},
                {"name": "test (3.13, linux)", "state": "running"},
            ],
        }
    ]


def test_nodes_give_each_job_its_runs_state_in_the_files_order():
    runs = [
        _run("tag"),
        _run("image broker"),
        _run("image caddy", "in_progress", None),
        _run("build server (linux/amd64)"),
        _run("build server (linux/arm64)", "completed", "failure"),
        _run("deploy-caddy", "waiting", None),
        _run("terraform / plan", "queued", None),
    ]

    nodes = _by_key(workflow_graph.nodes(workflow_graph.workflow_jobs(WORKFLOW), runs))

    assert list(nodes) == ["tag", "images", "build", "deploy", "terraform"]
    assert nodes["tag"] == {"key": "tag", "name": "tag", "state": "done", "needs": [], "done": None, "total": None}
    assert (nodes["images"]["state"], nodes["images"]["done"], nodes["images"]["total"]) == ("running", 1, 2)
    # A failure beats the rest of a matrix still going, and the count covers the jobs not started yet.
    assert (nodes["build"]["state"], nodes["build"]["done"], nodes["build"]["total"]) == ("failed", 2, 4)
    assert (nodes["deploy"]["state"], nodes["deploy"]["done"], nodes["deploy"]["total"]) == ("approve", 0, 2)
    # One job of a reusable workflow so far: no count yet.
    assert (nodes["terraform"]["state"], nodes["terraform"]["total"]) == ("queued", None)


def test_a_job_with_no_runs_yet_is_queued_and_a_job_all_skipped_is_skipped():
    jobs = workflow_graph.workflow_jobs(WORKFLOW)

    nodes = _by_key(workflow_graph.nodes(jobs, [_run("tag", "completed", "skipped")]))

    assert nodes["tag"]["state"] == "skipped"
    assert nodes["images"]["state"] == "queued"
    assert (nodes["images"]["done"], nodes["images"]["total"]) == (0, 2)


def test_a_matrix_is_not_done_while_known_jobs_have_not_started():
    jobs = workflow_graph.workflow_jobs(WORKFLOW)
    nodes = _by_key(workflow_graph.nodes(jobs, [_run("image broker")]))
    assert (nodes["images"]["state"], nodes["images"]["done"], nodes["images"]["total"]) == ("running", 1, 2)


def test_a_run_the_file_doesnt_name_is_a_node_of_its_own():
    nodes = workflow_graph.nodes(workflow_graph.workflow_jobs(WORKFLOW), [_run("cleanup", "in_progress", None)])

    assert nodes[-1] == {
        "key": "cleanup",
        "name": "cleanup",
        "state": "running",
        "needs": [],
        "done": None,
        "total": None,
    }


def test_without_the_workflow_file_each_run_is_a_node_with_no_lines():
    nodes = workflow_graph.nodes([], [_run("tag"), _run("package", "in_progress", None)])

    assert [(node["key"], node["state"], node["needs"]) for node in nodes] == [
        ("tag", "done", []),
        ("package", "running", []),
    ]


@pytest.mark.parametrize("text", ["jobs: [", "name: no jobs\n", "- a list\n"])
def test_a_file_that_isnt_a_workflow_raises_value_error(text):
    with pytest.raises(ValueError, match=r"not YAML|no jobs"):
        workflow_graph.workflow_jobs(text)


@pytest.mark.parametrize(
    ("status", "conclusion", "state"),
    [
        ("in_progress", None, "running"),
        ("waiting", None, "running"),
        ("completed", "success", "done"),
        ("completed", "failure", "failed"),
        ("completed", "cancelled", "failed"),
    ],
)
def test_run_state(status, conclusion, state):
    assert workflow_graph.run_state({"status": status, "conclusion": conclusion}) == state


def test_a_matrix_with_a_value_from_an_expression_has_no_size():
    jobs = workflow_graph.workflow_jobs(
        "jobs:\n  build:\n    strategy:\n      matrix:\n        image: ${{ fromJSON(needs.x.outputs.images) }}\n"
    )

    assert (jobs[0].size, jobs[0].names) == (None, None)


def test_a_name_that_needs_more_than_the_matrix_is_matched_by_pattern():
    text = """
jobs:
  deploy:
    name: deploy ${{ needs.tag.outputs.value }} to ${{ matrix.env }}
    strategy:
      matrix:
        env: [staging, production]
"""
    jobs = workflow_graph.workflow_jobs(text)
    nodes = workflow_graph.nodes(jobs, [_run("deploy v1.2.3 to staging")])

    assert jobs[0].names is None
    assert (nodes[0]["key"], nodes[0]["done"], nodes[0]["total"]) == ("deploy", 1, 2)


def test_a_matrix_lists_its_jobs_with_a_placeholder_for_each_not_started():
    jobs = workflow_graph.workflow_jobs("jobs:\n  test:\n    strategy:\n      matrix:\n        v: [1, 2, 3]\n")
    nodes = workflow_graph.nodes(jobs, [_run("test (1)", "in_progress", None)], list_jobs=True)

    assert nodes[0]["jobs"] == [
        {"name": "test (1)", "state": "running"},
        {"name": "test (not started)", "state": "queued"},
        {"name": "test (not started)", "state": "queued"},
    ]


def test_jobs_are_listed_only_when_asked_and_only_for_nodes_of_several():
    jobs = workflow_graph.workflow_jobs(WORKFLOW)
    runs = [_run("cleanup"), _run("images (a)"), _run("images (b)")]

    assert all("jobs" not in node for node in workflow_graph.nodes(jobs, runs))
    listed = workflow_graph.nodes(jobs, runs, list_jobs=True)
    assert all(("jobs" in node) == (node["total"] is not None) for node in listed)
