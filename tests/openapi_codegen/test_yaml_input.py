"""A YAML spec means what the same document written as JSON would."""

import json
import sys
from pathlib import Path

import pytest
import yaml

from tools.openapi_codegen.__main__ import main
from tools.openapi_codegen.generate import generate, load
from tools.openapi_codegen.schema import SpecError

ROOT = Path(__file__).resolve().parents[2]

HAND_WRITTEN = """\
openapi: 3.1.0
info:
  title: Hand written
  version: 1.0.0
paths:
  /things/{thing_id}:
    get:
      summary: A thing.
      parameters:
        - {name: thing_id, in: path, required: true, schema: {type: string}}
      responses:
        200:
          description: OK
          content:
            application/json:
              schema: {$ref: '#/components/schemas/Thing'}
        404:
          description: Not found.
components:
  schemas:
    Thing:
      type: object
      required: [name, mode]
      properties:
        name: {type: string}
        mode: {type: string, enum: [on, off, no, yes]}
        since: {type: string, example: 2024-01-31}
"""


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return path


@pytest.mark.parametrize("name", ["spec.yaml", "spec.yml", "SPEC.YAML"])
def test_the_repos_own_spec_in_yaml_generates_exactly_what_the_json_does(tmp_path, name):
    document = json.loads((ROOT / "openapi.json").read_text())
    path = _write(tmp_path, name, yaml.safe_dump(document, sort_keys=False))

    assert generate(load(path)) == generate(document)


def test_json_written_to_a_yaml_file_is_read(tmp_path):
    document = json.loads((ROOT / "openapi.json").read_text())
    assert load(_write(tmp_path, "spec.yaml", json.dumps(document))) == document


def test_an_unquoted_status_code_is_a_string_key(tmp_path):
    document = load(_write(tmp_path, "spec.yaml", HAND_WRITTEN))
    assert list(document["paths"]["/things/{thing_id}"]["get"]["responses"]) == ["200", "404"]
    assert generate(document)["models.py"]  # and the generator, which expects "200", copes


def test_yes_no_on_off_stay_strings(tmp_path):
    properties = load(_write(tmp_path, "spec.yaml", HAND_WRITTEN))["components"]["schemas"]["Thing"]["properties"]
    assert properties["mode"]["enum"] == ["on", "off", "no", "yes"]


def test_a_date_stays_a_string(tmp_path):
    properties = load(_write(tmp_path, "spec.yaml", HAND_WRITTEN))["components"]["schemas"]["Thing"]["properties"]
    assert properties["since"]["example"] == "2024-01-31"


def test_true_and_false_are_still_booleans(tmp_path):
    document = load(_write(tmp_path, "spec.yaml", "a: true\nb: false\nc: True\n"))
    assert document == {"a": True, "b": False, "c": True}


def test_a_repeated_key_is_an_error_naming_its_line(tmp_path):
    path = _write(tmp_path, "spec.yaml", "paths:\n  /a: {}\n  /a: {}\n")
    with pytest.raises(SpecError, match=r"line 3.*'/a' appears twice"):
        load(path)


def test_a_key_that_is_not_a_string_or_integer_is_refused(tmp_path):
    with pytest.raises(SpecError, match="isn't a string"):
        load(_write(tmp_path, "spec.yaml", "true: 1\n"))


def test_invalid_yaml_is_a_spec_error(tmp_path):
    with pytest.raises(SpecError, match="isn't valid YAML"):
        load(_write(tmp_path, "spec.yaml", "a: [unclosed\n"))


def test_without_pyyaml_yaml_says_what_to_install_and_json_still_works(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yaml", None)
    with pytest.raises(SpecError, match="needs PyYAML"):
        load(_write(tmp_path, "spec.yaml", "a: 1\n"))
    assert load(_write(tmp_path, "spec.json", '{"a": 1}')) == {"a": 1}


def test_the_command_line_reads_yaml_and_check_works_on_its_output(tmp_path):
    spec = _write(tmp_path, "spec.yaml", HAND_WRITTEN)
    out = tmp_path / "pkg"

    assert main([str(spec), "--out", str(out)]) == 0
    assert main([str(spec), "--out", str(out), "--check"]) == 0
    (out / "models.py").write_text("# changed\n")
    assert main([str(spec), "--out", str(out), "--check"]) == 1


def test_a_bad_yaml_file_on_the_command_line_exits_2(tmp_path, capsys):
    spec = _write(tmp_path, "spec.yaml", "a: [unclosed\n")
    assert main([str(spec), "--out", str(tmp_path / "pkg")]) == 2
    assert "isn't valid YAML" in capsys.readouterr().err
