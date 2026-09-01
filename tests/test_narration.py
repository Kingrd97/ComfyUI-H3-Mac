from __future__ import annotations

import pytest

from h3_bridge.narration import _edge_tts_executable, parse_timed_script


def test_parse_timed_script_supports_comments_and_chinese_dialogue():
    cues = parse_timed_script(
        "# opening\n0.55|大家好，我是旅行猫。\n\n5.73 | 下一站，出发！\n"
    )
    assert [(cue.start_seconds, cue.text) for cue in cues] == [
        (0.55, "大家好，我是旅行猫。"),
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


def test_edge_tts_falls_back_to_current_virtualenv(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    edge_tts = bin_dir / "edge-tts"
    edge_tts.touch()

    monkeypatch.setattr("h3_bridge.narration.shutil.which", lambda _name: None)
    monkeypatch.setattr("h3_bridge.narration.sys.prefix", str(tmp_path))

    assert _edge_tts_executable() == str(edge_tts)
