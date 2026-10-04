"""Yard definition model and import-time validation.

Yard JSON schema (see examples/demo_yard.json)::

    {
      "id": "demo", "name": "示例站",
      "endpoints": [{"id": "X", "x": 0, "y": 60, "signal": "X"}],
      "signals": [{"id": "X", "name": "下行进站"}],
      "sections": [
        {"id": "1DG", "a": "X",  "b": "3#", "b_leg": "R",
         "detector": "1D", "length": 1}
      ],
      "switches": [{"id": "3#", "x": 90, "y": 60}],
      "resources": [{"id": "P1", "name": "安全线"}],
      "routes": [
        {"id": "X-SN-I", "from": "X", "to": "SN",
         "sections": ["1DG", "3DG", "5DG", "IIG"],
         "switches": [{"id": "3#", "position": "N"}, ...],
         "flanks": ["P1"], "signal": "X"}
      ]
    }

A section connects two nodes. A node is either a boundary endpoint or a
switch. When the node is a switch, the section additionally records which
switch leg it attaches to ("N" straight / "R" diverging); the section that
attaches to the switch root carries no leg label.

Import is rejected (ValueError) when a route is disconnected or references
missing objects.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

LEGS = ("N", "R")
VALID_POSITIONS = {"N", "R"}


class YardError(ValueError):
    """Import rejected."""


@dataclass
class Section:
    id: str
    a: str
    b: str
    a_leg: Optional[str]
    b_leg: Optional[str]
    detector: Optional[str] = None
    length: Optional[float] = None
    x: Optional[float] = None
    y: Optional[float] = None

    def other(self, node: str) -> str:
        return self.b if node == self.a else self.a

    def leg_at(self, node: str) -> Optional[str]:
        if node == self.a:
            return self.a_leg
        if node == self.b:
            return self.b_leg
        raise KeyError(node)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "a": self.a, "b": self.b,
            "a_leg": self.a_leg, "b_leg": self.b_leg,
            "detector": self.detector, "length": self.length,
            "x": self.x, "y": self.y,
        }


@dataclass
class Switch:
    id: str
    x: Optional[float] = None
    y: Optional[float] = None


@dataclass
class Endpoint:
    id: str
    x: Optional[float] = None
    y: Optional[float] = None
    signal: Optional[str] = None


@dataclass
class Route:
    id: str
    fr: str
    to: str
    sections: list[str]
    switches: dict[str, str]          # switch id -> required position
    flanks: list[str]
    signal: Optional[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "from": self.fr, "to": self.to,
            "sections": list(self.sections),
            "switches": [{"id": k, "position": v}
                         for k, v in self.switches.items()],
            "flanks": list(self.flanks), "signal": self.signal,
        }


@dataclass
class Station:
    yard_id: Optional[str]
    name: str
    endpoints: dict[str, Endpoint]
    signals: dict[str, dict[str, Any]]
    sections: dict[str, Section]
    switches: dict[str, Switch]
    resources: dict[str, dict[str, Any]]
    routes: dict[str, Route]
    # switch id -> {"N": section_id, "R": section_id, "root": section_id}
    switch_legs: dict[str, dict[str, Optional[str]]] = field(default_factory=dict)

    # -- view models for the frontend -------------------------------------

    def detector_of(self, section_id: str) -> Optional[str]:
        return self.sections[section_id].detector

    def section_by_detector(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for sid, s in self.sections.items():
            if s.detector:
                out[s.detector] = sid
        return out

    def to_view(self) -> dict[str, Any]:
        return {
            "id": self.yard_id,
            "name": self.name,
            "endpoints": [vars(e) for e in self.endpoints.values()],
            "signals": list(self.signals.values()),
            "sections": [s.as_dict() for s in self.sections.values()],
            "switches": [vars(w) for w in self.switches.values()],
            "resources": list(self.resources.values()),
            "routes": [r.as_dict() for r in self.routes.values()],
        }


# ---------------------------------------------------------------------------
# Parsing & validation
# ---------------------------------------------------------------------------

def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise YardError(msg)


def _str_list(value: Any, what: str) -> list[str]:
    _require(isinstance(value, list) and value, f"{what} 必须是非空数组")
    out: list[str] = []
    for item in value:
        _require(isinstance(item, str) and item.strip(),
                 f"{what} 中的元素必须是非空字符串: {item!r}")
        out.append(item)
    return out


def parse_station(raw: Any) -> Station:
    _require(isinstance(raw, dict), "站场定义必须是 JSON 对象")

    endpoints: dict[str, Endpoint] = {}
    for ep in raw.get("endpoints", []):
        _require(isinstance(ep, dict) and isinstance(ep.get("id"), str)
                 and ep["id"].strip(), "端点定义无效: 需要字符串 id")
        eid = ep["id"]
        _require(eid not in endpoints, f"端点 id 重复: {eid}")
        endpoints[eid] = Endpoint(
            eid, ep.get("x"), ep.get("y"), ep.get("signal"))

    signals: dict[str, dict[str, Any]] = {}
    for sg in raw.get("signals", []):
        _require(isinstance(sg, dict) and isinstance(sg.get("id"), str)
                 and sg["id"].strip(), "信号机定义无效: 需要字符串 id")
        _require(sg["id"] not in signals, f"信号机 id 重复: {sg['id']}")
        signals[sg["id"]] = {"id": sg["id"], "name": sg.get("name", sg["id"])}

    switches: dict[str, Switch] = {}
    for sw in raw.get("switches", []):
        _require(isinstance(sw, dict) and isinstance(sw.get("id"), str)
                 and sw["id"].strip(), "道岔定义无效: 需要字符串 id")
        wid = sw["id"]
        _require(wid not in switches and wid not in endpoints,
                 f"道岔 id 重复或与端点冲突: {wid}")
        switches[wid] = Switch(wid, sw.get("x"), sw.get("y"))

    sections: dict[str, Section] = {}
    detectors: set[str] = set()
    for sec in raw.get("sections", []):
        _require(isinstance(sec, dict), "区段定义必须是对象")
        sid = sec.get("id")
        _require(isinstance(sid, str) and sid.strip(), "区段需要字符串 id")
        _require(sid not in sections, f"区段 id 重复: {sid}")
        a, b = sec.get("a"), sec.get("b")
        _require(isinstance(a, str) and isinstance(b, str) and a != b,
                 f"区段 {sid} 的 a/b 必须是两个不同的节点 id")
        for node, leg_attr in ((a, "a_leg"), (b, "b_leg")):
            leg = sec.get(leg_attr)
            _require(leg is None or leg in VALID_POSITIONS,
                     f"区段 {sid} 的 {leg_attr} 只能是 N/R 或缺省")
            if node in switches:
                _require(leg is None or leg in VALID_POSITIONS,
                         f"区段 {sid} 在道岔 {node} 处的腿位无效")
            else:
                _require(node in endpoints,
                         f"区段 {sid} 引用不存在的节点: {node}")
                _require(leg is None,
                         f"区段 {sid} 在端点 {node} 处不能声明腿位")
        det = sec.get("detector")
        if det is not None:
            _require(isinstance(det, str) and det.strip(),
                     f"区段 {sid} 的检测器 id 无效")
            _require(det not in detectors,
                     f"检测器 id 重复: {det}")
            detectors.add(det)
        sections[sid] = Section(
            sid, a, b, sec.get("a_leg"), sec.get("b_leg"),
            det, sec.get("length"), sec.get("x"), sec.get("y"))

    _require(sections, "站场至少需要一个区段")

    # Switch topology: exactly one root + N + R leg.
    switch_legs: dict[str, dict[str, Optional[str]]] = {
        wid: {"N": None, "R": None, "root": None} for wid in switches}
    for sid, sec in sections.items():
        for node, leg in ((sec.a, sec.a_leg), (sec.b, sec.b_leg)):
            if node not in switch_legs:
                continue
            slot = "root" if leg is None else leg
            _require(switch_legs[node][slot] is None,
                     f"道岔 {node} 的 {slot} 腿被多个区段占用: "
                     f"{switch_legs[node][slot]}, {sid}")
            switch_legs[node][slot] = sid
    for wid, legs in switch_legs.items():
        for slot, sid in legs.items():
            _require(sid is not None,
                     f"道岔 {wid} 缺少 {slot} 腿区段，拓扑不完整")

    # Flank / siding resources (generic lockable objects).
    resources: dict[str, dict[str, Any]] = {}
    for res in raw.get("resources", []):
        _require(isinstance(res, dict) and isinstance(res.get("id"), str)
                 and res["id"].strip(), "侧防资源定义无效: 需要字符串 id")
        rid = res["id"]
        _require(rid not in resources, f"侧防资源 id 重复: {rid}")
        resources[rid] = {"id": rid, "name": res.get("name", rid)}

    # Routes ----------------------------------------------------------------
    routes_raw = raw.get("routes", [])
    _require(isinstance(routes_raw, list) and routes_raw,
             "进路列表必须是非空数组")
    routes: dict[str, Route] = {}
    for rt in routes_raw:
        _require(isinstance(rt, dict), "进路定义必须是对象")
        rid = rt.get("id")
        _require(isinstance(rid, str) and rid.strip(), "进路需要字符串 id")
        _require(rid not in routes, f"进路 id 重复: {rid}")
        fr, to = rt.get("from"), rt.get("to")
        _require(fr in endpoints, f"进路 {rid} 的起点不存在或不是端点: {fr}")
        _require(to in endpoints, f"进路 {rid} 的终点不存在或不是端点: {to}")
        _require(fr != to, f"进路 {rid} 起终点相同")

        path = _str_list(rt.get("sections"), f"进路 {rid} 的 sections")
        _require(len(path) == len(set(path)),
                 f"进路 {rid} 的区段重复")
        for sid in path:
            _require(sid in sections,
                     f"进路 {rid} 引用不存在的区段: {sid}")
        first, last = sections[path[0]], sections[path[-1]]
        _require(fr in (first.a, first.b),
                 f"进路 {rid} 首段 {first.id} 不与起点 {fr} 相连")
        _require(to in (last.a, last.b),
                 f"进路 {rid} 末段 {last.id} 不与终点 {to} 相连")

        required_positions = _validate_path_continuity(rid, path, sections)

        sw_list = rt.get("switches", [])
        _require(isinstance(sw_list, list),
                 f"进路 {rid} 的 switches 必须是数组")
        sw_positions: dict[str, str] = {}
        for item in sw_list:
            _require(isinstance(item, dict)
                     and isinstance(item.get("id"), str)
                     and item.get("position") in VALID_POSITIONS,
                     f"进路 {rid} 的道岔位置项无效: {item!r}")
            wid, pos = item["id"], item["position"]
            _require(wid in switches,
                     f"进路 {rid} 引用不存在的道岔: {wid}")
            _require(wid not in sw_positions,
                     f"进路 {rid} 道岔 {wid} 重复列出")
            _require(wid in required_positions,
                     f"进路 {rid} 不经过道岔 {wid}，却声明了其位置")
            _require(required_positions[wid] == pos,
                     f"进路 {rid} 道岔 {wid} 要求 {pos}，"
                     f"但区段连通位置为 {required_positions[wid]}")
            sw_positions[wid] = pos
        for wid, pos in required_positions.items():
            _require(wid in sw_positions,
                     f"进路 {rid} 经过道岔 {wid}({pos}) 但未列出道岔位置")

        flanks = []
        for item in rt.get("flanks", []):
            _require(isinstance(item, str),
                     f"进路 {rid} 的 flanks 必须是字符串数组")
            _require(item in resources,
                     f"进路 {rid} 引用不存在的侧防资源: {item}")
            _require(item not in flanks,
                     f"进路 {rid} 侧防资源 {item} 重复")
            flanks.append(item)

        signal = rt.get("signal")
        _require(signal is None or isinstance(signal, str),
                 f"进路 {rid} 的 signal 必须是字符串")
        if signal is not None and signals:
            _require(signal in signals,
                     f"进路 {rid} 引用不存在的信号机: {signal}")

        routes[rid] = Route(rid, fr, to, path, sw_positions, flanks, signal)

    name = raw.get("name")
    _require(isinstance(name, str) and name.strip() or name is None,
             "站场 name 必须是非空字符串")

    return Station(
        yard_id=raw.get("id"),
        name=name or (raw.get("id") or "未命名站场"),
        endpoints=endpoints, signals=signals, sections=sections,
        switches=switches, resources=resources, routes=routes,
        switch_legs=switch_legs)


def _validate_path_continuity(
        route_id: str, path: list[str],
        sections: dict[str, Section]) -> dict[str, str]:
    """Walk consecutive sections; return {switch_id: required position}.

    Raises YardError when a pair does not meet at an endpoint/switch or
    tries to pass between two branch legs (route 不连通).
    """
    required: dict[str, str] = {}
    for s1_id, s2_id in zip(path, path[1:]):
        s1, s2 = sections[s1_id], sections[s2_id]
        common = {s1.a, s1.b} & {s2.a, s2.b}
        _require(len(common) == 1,
                 f"进路 {route_id} 中 {s1_id} 与 {s2_id} 不共端点，进路不连通")
        node = common.pop()
        leg1, leg2 = s1.leg_at(node), s2.leg_at(node)
        # Endpoint boundary: legs must both be None and the path can only
        # touch it here (handled by from/to checks for chain ends).
        if leg1 is None and leg2 is None:
            continue
        # Two different branch legs (N<->R) without the root: disconnected.
        _require(leg1 is None or leg2 is None,
                 f"进路 {route_id} 在道岔 {node} 处由 {leg1} 腿直通 {leg2} 腿，"
                 f"进路不连通（必须经过岔根）")
        pos = leg1 or leg2
        assert pos in VALID_POSITIONS
        _require(node not in required or required[node] == pos,
                 f"进路 {route_id} 多次经过道岔 {node} 且位置不一致")
        required[node] = pos
    return required
