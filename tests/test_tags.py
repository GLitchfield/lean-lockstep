from lockstep.tags import parse_docstring, render_docstring, validate_tags

DOC = """Every 15-vertex coloring has an eight-point support.
With `x` this closes `y`.
@node
@ws A
@kind formalization
@depends Demo.a, Demo.b
@consumer Demo.c
@falsifier fin15_endpoint.py
"""


def test_parse_all_tags():
    t = parse_docstring(DOC)
    assert t.is_node
    assert t.description.startswith("Every 15-vertex")
    assert t.tags["ws"] == "A"
    assert t.tags["depends"] == ["Demo.a", "Demo.b"]
    assert t.tags["consumer"] == "Demo.c"
    assert validate_tags(t, ["A", "B"]) == []


def test_missing_required_and_unknown():
    t = parse_docstring("desc\n@node\n@kind conjecture\n@bogus x\n")
    errs = validate_tags(t, ["A"])
    assert any("missing required tag @ws" in e for e in errs)
    assert any("@falsifier" in e for e in errs)
    assert any("@consumer" in e for e in errs)
    assert any("unknown tag @bogus" in e for e in errs)


def test_dangling_ok_needs_reason_and_bad_kind():
    t = parse_docstring("d\n@node\n@ws Z\n@kind weird\n@dangling-ok\n")
    errs = validate_tags(t, ["A"])
    assert any("unknown workstream" in e for e in errs)
    assert any("unknown @kind" in e for e in errs)
    assert any("@dangling-ok needs a reason" in e for e in errs)


def test_not_a_node():
    t = parse_docstring("plain docstring without tags")
    assert not t.is_node and validate_tags(t, ["A"]) == []


def test_render_roundtrip():
    doc = render_docstring("desc", {"ws": "A", "kind": "conjecture", "depends": ["x", "y"], "falsifier": "f.py", "dangling-ok": "why"})
    t = parse_docstring(doc)
    assert t.tags["depends"] == ["x", "y"] and t.tags["dangling-ok"] == "why" and t.is_node
