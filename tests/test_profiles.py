from __future__ import annotations

from unittest.mock import patch

from h3_bridge.profiles import QUALITY_PROFILES, process_prefix, should_stream


def test_quality_presets_keep_resource_and_quality_separate():
    assert QUALITY_PROFILES["preview"].steps == 4
    assert QUALITY_PROFILES["quality"].layers == 50
    assert QUALITY_PROFILES["reference"].steps == 50


def test_resource_profiles_on_macos():
    with patch("platform.system", return_value="Darwin"):
        assert process_prefix("low")[0] == "/usr/sbin/taskpolicy"
        assert process_prefix("auto") == ["/usr/sbin/taskpolicy", "-b"]
        assert process_prefix("max") == []


def test_auto_streaming_uses_ram_threshold():
    with patch("h3_bridge.profiles.physical_ram_gib", return_value=0):
        assert should_stream("auto", 64)
    with patch("h3_bridge.profiles.physical_ram_gib", return_value=48):
        assert should_stream("auto", 64)
    with patch("h3_bridge.profiles.physical_ram_gib", return_value=64):
        assert not should_stream("auto", 64)
    assert should_stream("low", 64)
    assert not should_stream("max", 64)
