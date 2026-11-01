import json

from evals.model import ToolSpec
from evals.tests.fakes import call
from evals.world import World

TOOLS = [
    ToolSpec(
        "get_task",
        "Read a task.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    ),
    ToolSpec("leave", "Leave.", {"type": "object", "properties": {}}),
]


def test_serves_first_matching_rule_and_exhausts_times():
    world = World.from_data(
        TOOLS,
        [
            {
                "tool": "get_task",
                "when": {"task_id": "t1"},
                "result": {"n": 1},
                "times": 1,
            },
            {"tool": "get_task", "result": {"n": 2}},
        ],
    )
    first = world.call(call("get_task", task_id="t1"))
    second = world.call(call("get_task", task_id="t1"))
    assert json.loads(first.text) == {"n": 1}
    assert json.loads(second.text) == {"n": 2}
    assert not first.is_error


def test_error_rule_is_an_error_result():
    world = World.from_data(TOOLS, [{"tool": "leave", "error": "409 nope"}])
    result = world.call(call("leave"))
    assert (result.text, result.is_error) == ("409 nope", True)


def test_rejects_unknown_tool_invalid_args_and_unrecorded_calls():
    world = World.from_data(TOOLS, [{"tool": "get_task", "result": {}}])
    assert "unknown tool" in world.call(call("steer_task")).text
    bad = world.call(call("get_task"))
    assert bad.is_error and "invalid arguments" in bad.text
    unrecorded = world.call(call("leave"))
    assert unrecorded.is_error and "not recorded" in unrecorded.text


def test_wildcard_requires_the_arg_to_be_present():
    world = World.from_data(
        TOOLS, [{"tool": "get_task", "when": {"task_id": "*"}, "result": 1}]
    )
    assert not world.call(call("get_task", task_id="anything")).is_error
