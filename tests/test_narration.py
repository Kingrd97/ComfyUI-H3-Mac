from __future__ import annotations

import pytest

from h3_bridge.narration import parse_timed_script


def test_parse_timed_script_supports_comments_and_chinese_dialogue():
    cues = parse_timed_script(
        "# opening\n0.55|大家好，我是土豆。\n\n5.73 | 下一站，出发！\n"
    )
    assert [(cue.start_seconds, cue.text) for cue in cues] == [
        (0.55, "大家好，我是土豆。"),
        (5.73, "下一站，出发！"),
    ]


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("hello", "seconds\\|dialogue"),
        ("x|hello", "invalid start"),
        ("-1|hello", "before zero"),
        ("1|", "no dialogue"),
        ("2|later\n1|earlier", "ordered"),
        ("# comment only", "no cues"),
    ],
)
def test_parse_timed_script_rejects_invalid_cues(script: str, message: str):
    with pytest.raises(ValueError, match=message):
        parse_timed_script(script)
