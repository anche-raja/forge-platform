"""A unit may not take away the constructor the units around it call.

On AMS ``spring-to-spring6`` rewrote the abstract ``InstallOrderBaseAction`` to
constructor injection and left no no-arg constructor, while the three actions
that extend it are their own units, each still relying on the implicit
``super()``. Its own javadoc said "subclasses must forward their own injected
CustomerService" -- the model knew, and a unit cannot edit the files around it.
Every file reviewed PASS and the reactor did not compile.
"""

import contextlib

from forge.verify import syntax
from tests.conftest import make_state, write_config
from tests.test_syntax import _graph

BASE_BROKEN = """package org.example.web;

public abstract class BaseAction {
    private final transient CustomerService customerService;

    /** Subclasses must forward their own injected {@link CustomerService}. */
    protected BaseAction(final CustomerService customerService) {
        this.customerService = customerService;
    }
}
"""
BASE_KEPT = """package org.example.web;

public abstract class BaseAction {
    @Autowired
    private transient CustomerService customerService;

    protected BaseAction() {
    }
}
"""
SUBCLASS = "package org.example.web;\npublic class DeviceAction extends BaseAction {\n    public DeviceAction() {\n    }\n}\n"
FORWARDING = ("package org.example.web;\npublic class SiteAction extends BaseAction {\n"
              "    public SiteAction(CustomerService s) {\n        super(s);\n    }\n}\n")


def _tree(tmp_path, **files):
    src = tmp_path / "src"
    (src / "org" / "example" / "web").mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (src / "org" / "example" / "web" / name).write_text(content, encoding="utf-8")
    syntax.forget_subclasses(str(src))
    return str(src)


def test_taking_the_no_arg_constructor_away_names_the_subclass(tmp_path):
    source = _tree(tmp_path, **{"BaseAction.java": "", "DeviceAction.java": SUBCLASS})
    errors = syntax.subclass_break_errors({"BaseAction.java": BASE_BROKEN}, source)
    assert len(errors) == 1
    assert "BaseAction no longer accepts `super()`" in errors[0]
    assert "DeviceAction.java extends it" in errors[0] and "keep a no-arg constructor" in errors[0]


def test_keeping_one_passes_and_so_does_a_subclass_that_forwards(tmp_path):
    source = _tree(tmp_path, **{"BaseAction.java": "", "DeviceAction.java": SUBCLASS})
    assert syntax.subclass_break_errors({"BaseAction.java": BASE_KEPT}, source) == []

    forwarding = _tree(tmp_path / "b", **{"BaseAction.java": "", "SiteAction.java": FORWARDING})
    assert syntax.subclass_break_errors({"BaseAction.java": BASE_BROKEN}, forwarding) == []


def test_a_class_nothing_extends_may_change_its_constructor(tmp_path):
    source = _tree(tmp_path, **{"BaseAction.java": ""})
    assert syntax.subclass_break_errors({"BaseAction.java": BASE_BROKEN}, source) == []


def test_the_same_unit_rewriting_both_is_left_alone(tmp_path):
    source = _tree(tmp_path, **{"BaseAction.java": "", "DeviceAction.java": SUBCLASS})
    both = {"BaseAction.java": BASE_BROKEN, "DeviceAction.java": FORWARDING}
    assert syntax.subclass_break_errors(both, source) == []


def test_a_comment_a_string_and_a_new_expression_are_not_constructors(tmp_path):
    source = _tree(tmp_path, **{"BaseAction.java": "", "DeviceAction.java": SUBCLASS})
    quoted = ('package org.example.web;\npublic abstract class BaseAction {\n'
              '    // protected BaseAction(CustomerService c) { }\n'
              '    private static final String S = "BaseAction(x) {";\n'
              '    static BaseAction of() { return new BaseAction(null) { }; }\n}\n')
    assert syntax.subclass_break_errors({"BaseAction.java": quoted}, source) == [], \
        "no real constructor is declared, so the implicit one is still there"


def test_no_source_tree_is_not_an_error():
    assert syntax.subclass_break_errors({"BaseAction.java": BASE_BROKEN}, None) == []
    assert syntax.subclass_break_errors({"BaseAction.java": BASE_BROKEN}, "/nope/nowhere") == []


def test_the_graph_sends_it_back_with_the_subclass_named_and_never_reviews_it(tmp_path, java_file):
    """The whole loop: refused before the review, retried, and the retry lands."""
    from forge.graph import build_graph

    source = _tree(tmp_path, **{"BaseAction.java": "", "DeviceAction.java": SUBCLASS})
    cfg = write_config(tmp_path, syntax_check=True)
    with contextlib.ExitStack() as stack:
        up, rev = _graph(stack, java_file, [BASE_BROKEN, BASE_KEPT])
        state = make_state(java_file, tmp_path)
        state["source_dir"] = source
        result = build_graph(cfg).invoke(state, config={"configurable": {"thread_id": java_file}})

    fs = result["current_file"]
    assert fs["status"] == "DONE" and fs["retry_count"] == 1
    assert fs["syntax_verdict"] == syntax.PASS and not fs.get("error")
    assert rev.return_value.invoke.call_count == 1, "the broken base class cost no review"
    retry_prompt = up.return_value.invoke.call_args_list[1][0][0][1].content
    assert "no longer accepts `super()`" in retry_prompt and "DeviceAction.java" in retry_prompt
