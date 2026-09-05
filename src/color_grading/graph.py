"""Operation graph: nodes with deterministic ids, dependencies, topological order.

Node kinds:
  source node  "source"      the one ColorSource of the project
  op node      "op:<id>"     one ColorOperation (exactly one input: the source or another operation)
Every node has: node_id, type, inputs (node ids, 0 for source / 1 for an operation), parameters, and an identity:
sha256 of canonical {type, parameters, input identity, tool version, source fingerprint}, computed by the executor
once the source fingerprint is known (graph.identities()). Nothing here depends on time or randomness.

Colour operations never branch into more than one input or merge two videos (no MIX / CONCAT analogue exists in
this skill), so the graph is a set of simple chains rooted at "source"; it is still validated as a general DAG
(duplicate ids, cycles, unreachable nodes) for the same reasons audio-production-skill validates its richer graph:
honesty about what is connected to an output, and a stable place to compute identities."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

from .canonical import stable_hash
from .errors import ColorError
from .model import ColorProject

SOURCE_NODE = "source"


@dataclass
class Node:
    node_id: str
    type: str                       # "SOURCE" or an operation type
    inputs: List[str]               # node ids (0 or 1)
    parameters: Dict[str, Any] = field(default_factory=dict)
    consumers: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"node_id": self.node_id, "type": self.type, "inputs": list(self.inputs), "parameters": self.parameters}


class OperationGraph:
    def __init__(self, project: ColorProject):
        self.project = project
        self.nodes: Dict[str, Node] = {}
        self.order: List[str] = []
        self.output_nodes: Dict[str, str] = {}     # output_id -> node id
        self._build()

    def _add(self, node: Node) -> None:
        if node.node_id in self.nodes:
            raise ColorError("DEPENDENCY_ERROR", f"duplicate node id {node.node_id!r}", {"node_id": node.node_id})
        self.nodes[node.node_id] = node

    def _ref_node(self, ref: str) -> str:
        return SOURCE_NODE if ref == SOURCE_NODE else ref  # "op:<id>" is already a node id

    def _build(self) -> None:
        p = self.project
        self._add(Node(SOURCE_NODE, "SOURCE", [], {"source_id": p.source.source_id}))
        for op in p.operations:
            nid = f"op:{op.op_id}"
            self._add(Node(nid, op.type, [self._ref_node(op.input)], dict(op.parameters)))
        for node in self.nodes.values():
            for i in node.inputs:
                if i not in self.nodes:
                    raise ColorError("MISSING_INPUT", f"node {node.node_id!r} depends on unknown node {i!r}", {"node_id": node.node_id, "ref": i})
                self.nodes[i].consumers.append(node.node_id)
        for out in p.outputs:
            self.output_nodes[out.output_id] = self._ref_node(out.operation)
        self.order = self._toposort()
        needed = self._reachable(list(self.output_nodes.values()))
        unused = [n for n in self.order if n not in needed and n != SOURCE_NODE]
        if unused:
            raise ColorError("DEPENDENCY_ERROR", f"operations not connected to any output: {unused}", {"unreachable": unused})

    def _toposort(self) -> List[str]:
        """Deterministic Kahn ordering: among ready nodes, the smallest id first (stable across processes)."""
        indeg = {n: len(node.inputs) for n, node in self.nodes.items()}
        ready = sorted(n for n, d in indeg.items() if d == 0)
        order: List[str] = []
        while ready:
            n = ready.pop(0)
            order.append(n)
            for c in self.nodes[n].consumers:
                indeg[c] -= 1
                if indeg[c] == 0:
                    ready.append(c)
                    ready.sort()
        if len(order) != len(self.nodes):
            cyc = sorted(n for n, d in indeg.items() if d > 0)
            raise ColorError("DEPENDENCY_ERROR", f"operation graph has a cycle among {cyc}", {"cycle": cyc})
        return order

    def _reachable(self, starts: List[str]) -> set:
        seen: set = set()
        stack = list(starts)
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            stack.extend(self.nodes[n].inputs)
        return seen

    # ---- identity
    def identities(self, source_fingerprint: str, tool_versions: Dict[str, str], content_overrides: Any = None) -> Dict[str, str]:
        """Deterministic operation identity per node, in topological order. The source contributes its sha256; each
        operation contributes type + effective parameters + input identity + the tool version that will run it.

        `content_overrides[node_id]` replaces named parameter keys with a content hash before hashing (used for
        LUT_APPLY's `lut_path`, so identity depends on the LUT's bytes, not the path it happened to be read from on
        this machine; a LUT moved without changing content keeps its identity, one edited at the same path does not)."""
        overrides: Dict[str, Dict[str, Any]] = content_overrides or {}
        ids: Dict[str, str] = {}
        for n in self.order:
            node = self.nodes[n]
            if node.type == "SOURCE":
                ids[n] = stable_hash({"kind": "source", "source_sha256": source_fingerprint})
            else:
                params = dict(node.parameters)
                params.update(overrides.get(n, {}))
                ids[n] = stable_hash({"kind": "operation", "type": node.type, "parameters": params,
                                      "input": ids[node.inputs[0]], "tool_versions": tool_versions})
        return ids

    def to_dict(self) -> Dict[str, Any]:
        return {"order": list(self.order), "nodes": [self.nodes[n].to_dict() for n in self.order], "outputs": dict(self.output_nodes)}
