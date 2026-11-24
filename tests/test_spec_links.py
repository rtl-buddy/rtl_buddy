"""``links:`` on a spec block: load and validation, warnings, graph nodes and ``rb spec links``.

Fixture ``tests/fixtures/spec_links/`` has blocks ``arb`` and ``fifo``. Both link the
arch page ``pages/123``; ``fifo`` also links ``pages/456`` and has a local doc, and
``arb`` links ``pages/789`` with no kind or title.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from rtl_buddy.config.spec import SpecConfig
from rtl_buddy.errors import FatalRtlBuddyError
from rtl_buddy.graph import build_config_tier
from rtl_buddy.rtl_buddy import RtlBuddy

_FIXTURE = Path(__file__).parent / "fixtures" / "spec_links"
_SPEC_DIR = _FIXTURE / "spec"

_ARCH = "https://wiki.example.com/pages/123"
_MAS = "https://wiki.example.com/pages/456"
_PLAIN = "https://wiki.example.com/pages/789"

_SPEC = """\
rtl-buddy-filetype: spec_config
blocks:
  - name: fifo
    desc: example
    links:
      - kind: arch
        title: "Arch: flow control"
        url: "https://wiki.example.com/pages/123"
      - url: "https://wiki.example.com/pages/456"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "spec" / "specs.yaml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def _one_link(**fields: str) -> str:
    """A spec whose block ``fifo`` has one link entry made of the raw YAML ``fields``."""
    body = "\n".join(
        f"        {key}: {value}" if i else f"      - {key}: {value}"
        for i, (key, value) in enumerate(fields.items())
    )
    return (
        "rtl-buddy-filetype: spec_config\n"
        "blocks:\n"
        "  - name: fifo\n"
        "    desc: example\n"
        "    links:\n"
        f"{body}\n"
    )


def _load_error(path: Path) -> str:
    """Load ``path``, which must fail, and return the message of the underlying error."""
    with pytest.raises(FatalRtlBuddyError) as exc_info:
        SpecConfig(str(path))
    assert exc_info.value.__cause__ is not None
    return str(exc_info.value.__cause__)


def _events(caplog, event: str):
    return [r for r in caplog.records if getattr(r, "rtl_event", None) == event]


def _last_json(output: str) -> dict:
    """Parse the last non-empty stdout line as the machine-mode JSON envelope."""
    lines = [line for line in output.splitlines() if line.strip()]
    return json.loads(lines[-1])


# Load and validation


def test_links_load_with_their_defaults(tmp_path):
    block = SpecConfig(str(_write(tmp_path, _SPEC))).get_blocks()[0]
    arch, plain = block.links
    assert (arch.url, arch.kind, arch.title) == (_ARCH, "arch", "Arch: flow control")
    assert (plain.url, plain.kind, plain.title) == (_MAS, "doc", _MAS)


def test_a_block_without_links_has_none(tmp_path):
    text = _SPEC.split("    links:\n")[0]
    assert SpecConfig(str(_write(tmp_path, text))).get_blocks()[0].links == []


def test_empty_title_and_kind_mean_the_defaults(tmp_path):
    path = _write(tmp_path, _one_link(url=f'"{_ARCH}"', title="~", kind=""))
    (link,) = SpecConfig(str(path)).get_blocks()[0].links
    assert (link.kind, link.title) == ("doc", _ARCH)


@pytest.mark.parametrize(
    "kind", ["arch", "mas", "rtl", "dv", "pd", "sub-block_2"], ids=lambda k: k
)
def test_a_lowercase_token_is_a_kind(tmp_path, kind):
    path = _write(tmp_path, _one_link(url=f'"{_ARCH}"', kind=kind))
    assert SpecConfig(str(path)).get_blocks()[0].links[0].kind == kind


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ('"ftp://wiki.example.com/pages/1"', "ftp://wiki.example.com/pages/1"),
        ('"pages/123"', "pages/123"),
        ('"wiki.example.com/pages/1"', "wiki.example.com/pages/1"),
        ('"https://"', "https://"),
        ('"https:///pages/1"', "https:///pages/1"),
        ('"https://wiki.example.com/a b"', "https://wiki.example.com/a b"),
        ("123", 123),
        ("~", None),
    ],
    ids=[
        "ftp",
        "relative",
        "no-scheme",
        "scheme-only",
        "no-host",
        "whitespace",
        "number",
        "null",
    ],
)
def test_a_url_that_is_not_absolute_http_fails_naming_file_block_and_value(
    tmp_path, caplog, raw, value
):
    path = _write(tmp_path, _one_link(url=raw))
    with caplog.at_level(logging.ERROR):
        message = _load_error(path)
    assert str(path) in message
    assert "block 'fifo'" in message
    assert f"url {value!r} is not an absolute http:// or https:// URL" in message
    (failed,) = _events(caplog, "spec_config.load_failed")
    assert failed.rtl_fields["path"] == str(path)


@pytest.mark.parametrize(
    ("raw", "value"),
    [("Arch", "Arch"), ('"arch page"', "arch page"), ("arch.v2", "arch.v2"), ("7", 7)],
    ids=["uppercase", "space", "dot", "number"],
)
def test_a_kind_that_is_not_a_lowercase_token_fails(tmp_path, raw, value):
    path = _write(tmp_path, _one_link(url=f'"{_ARCH}"', kind=raw))
    message = _load_error(path)
    assert str(path) in message
    assert "block 'fifo'" in message
    assert f"kind {value!r}, which is not a lowercase token" in message


def test_a_title_that_is_not_a_string_fails(tmp_path):
    path = _write(tmp_path, _one_link(url=f'"{_ARCH}"', title="[a, b]"))
    message = _load_error(path)
    assert str(path) in message
    assert "block 'fifo'" in message
    assert "title ['a', 'b'], which is not a string" in message


def test_a_bare_url_entry_fails_asking_for_a_mapping(tmp_path):
    path = _write(tmp_path, _SPEC.replace("      - url:", "      -"))
    message = _load_error(path)
    assert str(path) in message
    assert "block 'fifo'" in message
    assert f"links entry {_MAS!r} is not a mapping" in message


def test_a_link_without_a_url_fails_naming_the_block(tmp_path):
    path = _write(tmp_path, _one_link(kind="arch", title="Arch"))
    message = _load_error(path)
    assert "'fifo'" in message
    assert "missing required key 'url'" in message


# Warnings


def test_known_link_keys_do_not_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        SpecConfig(str(_write(tmp_path, _SPEC)))
    assert not _events(caplog, "spec_config.unknown_key")
    assert not _events(caplog, "spec_config.doc_is_url")


def test_unknown_link_key_warns_once_per_process_and_loads(tmp_path, caplog):
    path = _write(tmp_path, _SPEC + "        owner: alice\n")
    with caplog.at_level(logging.WARNING):
        links = SpecConfig(str(path)).get_blocks()[0].links
    assert [link.url for link in links] == [_ARCH, _MAS]
    (warned,) = _events(caplog, "spec_config.unknown_key")
    assert warned.levelno == logging.WARNING
    assert warned.rtl_fields["key"] == "owner"
    assert warned.rtl_fields["where"] == f"link {_MAS!r} in block 'fifo'"
    assert str(path) in warned.getMessage()

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        SpecConfig(str(path))
    assert not _events(caplog, "spec_config.unknown_key")


def test_a_url_under_docs_warns_once_per_process_to_use_links(tmp_path, caplog):
    text = _SPEC.replace(
        "    links:\n", f'    docs: [README.md, "{_PLAIN}"]\n    links:\n', 1
    )
    path = _write(tmp_path, text)
    with caplog.at_level(logging.WARNING):
        block = SpecConfig(str(path)).get_blocks()[0]
    # `docs` keeps meaning file paths: the entry is kept, not moved.
    assert block.docs == ["README.md", _PLAIN]
    assert [link.url for link in block.links] == [_ARCH, _MAS]
    (warned,) = _events(caplog, "spec_config.doc_is_url")
    assert warned.levelno == logging.WARNING
    assert warned.rtl_fields["block"] == "fifo"
    assert warned.rtl_fields["doc"] == _PLAIN
    message = warned.getMessage()
    assert str(path) in message
    assert "links:" in message

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        SpecConfig(str(path))
    assert not _events(caplog, "spec_config.doc_is_url")


# The knowledge graph


@pytest.fixture(scope="module")
def graph() -> dict:
    return build_config_tier(_FIXTURE)


def _nodes_by_type(graph: dict, node_type: str) -> dict[str, dict]:
    return {n["id"]: n for n in graph["nodes"] if n["type"] == node_type}


def _links_of_type(graph: dict, link_type: str) -> set[tuple[str, str]]:
    return {
        (link["source"], link["target"])
        for link in graph["links"]
        if link["type"] == link_type
    }


def test_each_distinct_url_is_one_spec_link_node(graph):
    nodes = _nodes_by_type(graph, "spec_link")
    assert set(nodes) == {f"link:{_ARCH}", f"link:{_MAS}", f"link:{_PLAIN}"}
    assert nodes[f"link:{_ARCH}"] == {
        "id": f"link:{_ARCH}",
        "type": "spec_link",
        "label": "Arch: flow control",
        "tier": "config",
        "url": _ARCH,
        "kind": "arch",
        "title": "Arch: flow control",
    }
    assert nodes[f"link:{_MAS}"]["kind"] == "mas"
    plain = nodes[f"link:{_PLAIN}"]
    assert (plain["kind"], plain["title"], plain["label"]) == ("doc", _PLAIN, _PLAIN)


def test_blocks_reach_docs_and_links_through_documented_by(graph):
    assert _links_of_type(graph, "documented_by") == {
        ("spec:fifo", "doc:spec/fifo/README.md"),
        ("spec:fifo", f"link:{_ARCH}"),
        ("spec:fifo", f"link:{_MAS}"),
        ("spec:arb", f"link:{_ARCH}"),
        ("spec:arb", f"link:{_PLAIN}"),
    }


def test_two_blocks_naming_one_url_share_its_node(graph):
    ids = [n["id"] for n in graph["nodes"]]
    assert ids.count(f"link:{_ARCH}") == 1
    sources = {
        link["source"] for link in graph["links"] if link["target"] == f"link:{_ARCH}"
    }
    assert sources == {"spec:arb", "spec:fifo"}


def test_a_shared_url_keeps_the_first_blocks_title_and_kind(tmp_path):
    second = _SPEC.split("    links:\n")[1].replace("Arch: flow control", "Other")
    second = second.replace("kind: arch", "kind: mas")
    _write(
        tmp_path, _SPEC + "  - name: later\n    desc: example\n    links:\n" + second
    )
    node = _nodes_by_type(build_config_tier(tmp_path), "spec_link")[f"link:{_ARCH}"]
    assert (node["kind"], node["title"]) == ("arch", "Arch: flow control")


# rb spec links


def _invoke(*args: str):
    return CliRunner().invoke(RtlBuddy(name="test_spec_links").app, list(args))


def _row(block: str, kind: str, title: str, url: str) -> dict:
    path = os.path.abspath(_SPEC_DIR / block / "specs.yaml")
    return {"block": block, "kind": kind, "title": title, "url": url, "path": path}


def test_spec_links_lists_every_link_of_every_spec_config():
    result = _invoke("--machine", "spec", "links", "--spec-dir", str(_SPEC_DIR))
    assert result.exit_code == 0, result.output
    envelope = _last_json(result.output)
    assert envelope["command"] == "spec links"
    assert envelope["exit_code"] == 0
    assert envelope["payload"]["links"] == [
        _row("arb", "arch", "Arch: flow control", _ARCH),
        _row("arb", "doc", _PLAIN, _PLAIN),
        _row("fifo", "arch", "Arch: flow control", _ARCH),
        _row("fifo", "mas", "MAS: fifo", _MAS),
    ]


def test_spec_links_block_filter():
    result = _invoke(
        "--machine", "spec", "links", "--spec-dir", str(_SPEC_DIR), "--block", "fifo"
    )
    assert result.exit_code == 0, result.output
    links = _last_json(result.output)["payload"]["links"]
    assert [(row["block"], row["url"]) for row in links] == [
        ("fifo", _ARCH),
        ("fifo", _MAS),
    ]


def test_spec_links_unknown_block_is_a_configuration_error():
    result = _invoke("spec", "links", "--spec-dir", str(_SPEC_DIR), "--block", "nope")
    assert result.exit_code != 0
    assert isinstance(result.exception, FatalRtlBuddyError)
    assert "Unknown spec block(s): nope" in str(result.exception)


def test_spec_links_with_no_links_is_an_empty_list(tmp_path):
    _write(tmp_path, _SPEC.split("    links:\n")[0])
    result = _invoke("--machine", "spec", "links", "--spec-dir", str(tmp_path / "spec"))
    assert result.exit_code == 0, result.output
    assert _last_json(result.output)["payload"] == {"links": []}


def test_spec_links_missing_spec_dir_fails(tmp_path):
    missing = tmp_path / "nowhere"
    result = _invoke("--machine", "spec", "links", "--spec-dir", str(missing))
    assert result.exit_code == 1
    envelope = _last_json(result.output)
    assert envelope["command"] == "spec links"
    assert envelope["exit_code"] == 1
    assert envelope["payload"] == {"error": "Spec directory not found"}
