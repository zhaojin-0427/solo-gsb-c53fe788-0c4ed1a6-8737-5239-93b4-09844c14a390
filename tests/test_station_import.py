"""Import-time validation: references must exist; routes must be connected."""
import copy
import pytest

from app.station import YardError, parse_station


def test_demo_station_imports(demo_raw):
    st = parse_station(demo_raw)
    assert len(st.routes) == 8
    assert set(st.sections) == {
        "1DG", "3DG", "5DG", "7DG", "9DG", "11DG", "15DG", "13DG"}


def test_missing_section_rejected(demo_raw):
    demo_raw["routes"][0]["sections"].append("NOPE")
    with pytest.raises(YardError, match="不存在的区段"):
        parse_station(demo_raw)


def test_missing_switch_rejected(demo_raw):
    demo_raw["routes"][0]["switches"].append({"id": "99#", "position": "N"})
    with pytest.raises(YardError, match="不存在的道岔"):
        parse_station(demo_raw)


def test_missing_flank_rejected(demo_raw):
    demo_raw["routes"][0]["flanks"].append("NOPE")
    with pytest.raises(YardError, match="不存在的侧防资源"):
        parse_station(demo_raw)


def test_disconnected_route_rejected(demo_raw):
    # X-XN-I with the middle section 3DG removed is not contiguous.
    demo_raw["routes"][0]["sections"] = ["1DG", "5DG"]
    with pytest.raises(YardError, match="不共端点"):
        parse_station(demo_raw)


def test_branch_to_branch_switch_rejected(demo_raw):
    # Impossible manoeuvre: enter switch 7# on its R leg (15DG) and leave on
    # its N leg (9DG) without passing the root — R<->N is not connected.
    bad = {
        "id": "BAD", "from": "X", "to": "SN",
        "sections": ["1DG", "15DG", "9DG", "11DG"],
        "switches": [{"id": "3#", "position": "R"},
                     {"id": "7#", "position": "R"},
                     {"id": "11#", "position": "N"}],
        "flanks": [], "signal": "X"}
    demo_raw["routes"].append(bad)
    with pytest.raises(YardError, match="进路不连通"):
        parse_station(demo_raw)


def test_wrong_switch_position_rejected(demo_raw):
    demo_raw["routes"][0]["switches"][0]["position"] = "R"
    with pytest.raises(YardError, match="连通位置"):
        parse_station(demo_raw)


def test_missing_endpoint_rejected(demo_raw):
    demo_raw["routes"][0]["to"] = "ZZ"
    with pytest.raises(YardError, match="终点不存在"):
        parse_station(demo_raw)


def test_section_unknown_node_rejected(demo_raw):
    demo_raw["sections"][0]["b"] = "GHOST"
    with pytest.raises(YardError, match="引用不存在的节点"):
        parse_station(demo_raw)


def test_duplicate_detector_rejected(demo_raw):
    demo_raw["sections"][1]["detector"] = "D1"
    with pytest.raises(YardError, match="检测器 id 重复"):
        parse_station(demo_raw)
