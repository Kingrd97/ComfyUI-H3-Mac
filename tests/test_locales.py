from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_NODES = {
    "H3BuildShotPrompt",
    "H3EmptyReferences",
    "H3AddImageReference",
    "H3AddAudioReference",
    "H3AddMediaFileReference",
    "H3GenerateVideo",
    "H3GenerateVideoVPipe",
    "H3AddFixedNarration",
    "H3AssembleStoryboard",
}


def test_english_and_chinese_node_locales_are_complete_and_valid():
    locales = {}
    for language in ("en", "zh"):
        path = ROOT / "locales" / language / "nodeDefs.json"
        locales[language] = json.loads(path.read_text(encoding="utf-8"))
        assert set(locales[language]) == EXPECTED_NODES
        for node in locales[language].values():
            assert node["display_name"]
            assert node["description"]

    assert locales["en"]["H3GenerateVideo"]["display_name"] != locales["zh"][
        "H3GenerateVideo"
    ]["display_name"]


def test_beginner_storyboard_template_is_valid_and_contains_the_guided_flow():
    path = ROOT / "example_workflows" / "H3_Beginner_2_Shot_Storyboard.json"
    workflow = json.loads(path.read_text(encoding="utf-8"))
    node_types = [node["type"] for node in workflow["nodes"]]
    assert node_types.count("H3BuildShotPrompt") == 2
    assert node_types.count("H3GenerateVideo") == 2
    assert node_types.count("H3AssembleStoryboard") == 1
    generators = [node for node in workflow["nodes"] if node["type"] == "H3GenerateVideo"]
    assert all(node["widgets_values"][6] == "auto" for node in generators)
    assert workflow["version"] == 0.4


def test_vpipe_fixed_voice_template_contains_the_full_postproduction_flow():
    path = ROOT / "example_workflows" / "H3_vpipe_Q8_2_Shot_Fixed_Voice.json"
    workflow = json.loads(path.read_text(encoding="utf-8"))
    node_types = [node["type"] for node in workflow["nodes"]]
    assert node_types.count("H3GenerateVideoVPipe") == 2
    assert node_types.count("H3AssembleStoryboard") == 1
    assert node_types.count("H3AddFixedNarration") == 1
    assert workflow["version"] == 0.4
