import pytest

from graphx.model.exprs import ExprError, build_namespace, evaluate
from graphx.model.refs import RefContext, RefError, resolve
from graphx.model.state import ChannelSpec, ChannelTypeError, State


def make_state(**channels):
    specs = {}
    for name, spec in channels.items():
        specs[name] = spec if isinstance(spec, ChannelSpec) else ChannelSpec(name=name)
    return State.initial(specs)


class TestState:
    def test_last_reducer_overwrites(self):
        s = make_state(x=ChannelSpec("x"))
        s2 = s.apply({"x": 1}).apply({"x": 2})
        assert s2.get("x") == 2
        assert s.get("x") is None  # immutability

    def test_append_and_extend(self):
        s = make_state(
            a=ChannelSpec("a", reducer="append"),
            e=ChannelSpec("e", reducer="extend"),
        )
        s = s.apply({"a": 1, "e": [1, 2]}).apply({"a": [9], "e": [3]})
        assert s.get("a") == [1, [9]]      # append nests
        assert s.get("e") == [1, 2, 3]     # extend flattens

    def test_sum_and_merge_dict(self):
        s = make_state(
            n=ChannelSpec("n", reducer="sum"),
            d=ChannelSpec("d", reducer="merge_dict"),
        )
        s = s.apply({"n": 2, "d": {"a": 1}}).apply({"n": 3, "d": {"b": 2}})
        assert s.get("n") == 5
        assert s.get("d") == {"a": 1, "b": 2}

    def test_type_check(self):
        s = make_state(x=ChannelSpec("x", type_="int"))
        with pytest.raises(ChannelTypeError):
            s.apply({"x": "nope"})

    def test_unknown_channel(self):
        with pytest.raises(KeyError):
            make_state().apply({"ghost": 1})

    def test_roundtrip(self):
        spec = {"x": ChannelSpec("x", default=5), "y": ChannelSpec("y")}
        s = State.initial(spec, {"y": "hi"})
        restored = State.from_json(spec, s.to_json())
        assert restored.get("x") == 5 and restored.get("y") == "hi"


class TestRefs:
    def setup_method(self):
        self.ctx = RefContext(
            state={"topic": "ai", "items": [10, 20]},
            node_outputs={"fetch": {"data": {"count": 7}, "list": ["a", "b"]}},
            item={"word": "hey"},
            self_output={"notes": "n1"},
        )

    def test_whole_string_preserves_type(self):
        assert resolve("<fetch.data.count>", self.ctx) == 7
        assert resolve("<state.items>", self.ctx) == [10, 20]

    def test_interpolation(self):
        assert resolve("topic=<state.topic>!", self.ctx) == "topic=ai!"

    def test_item_and_self(self):
        assert resolve("<item.word>", self.ctx) == "hey"
        assert resolve("<self.notes>", self.ctx) == "n1"

    def test_list_index(self):
        assert resolve("<fetch.list.1>", self.ctx) == "b"

    def test_nested_containers(self):
        out = resolve({"a": ["<state.topic>", {"b": "<fetch.data.count>"}]}, self.ctx)
        assert out == {"a": ["ai", {"b": 7}]}

    def test_unknown_node(self):
        with pytest.raises(RefError):
            resolve("<ghost.x>", self.ctx)

    def test_unknown_state_key(self):
        with pytest.raises(RefError):
            resolve("<state.ghost>", self.ctx)


class TestExprs:
    ns = {"score": 0.9, "draft": {"len": 100}, "tags": ["a", "b"], "flag": True}

    def test_comparisons_and_bool(self):
        assert evaluate("score >= 0.8 and flag", self.ns) is True
        assert evaluate("score < 0.5 or 'a' in tags", self.ns) is True
        assert evaluate("not flag", self.ns) is False

    def test_dotted_lookup(self):
        assert evaluate("draft.len == 100", self.ns)

    def test_builtins(self):
        assert evaluate("len(tags) == 2", self.ns)

    def test_disallowed(self):
        for expr in ("__import__('os')", "tags.__class__", "(lambda: 1)()",
                     "[x for x in tags]", "open('/etc/passwd')"):
            with pytest.raises(ExprError):
                evaluate(expr, self.ns)

    def test_unknown_name(self):
        with pytest.raises(ExprError):
            evaluate("ghost > 1", self.ns)

    def test_namespace_precedence(self):
        ns = build_namespace({"x": 1}, {"x": {"out": 2}, "y": {"out": 3}})
        assert evaluate("x == 1 and y.out == 3", ns)
