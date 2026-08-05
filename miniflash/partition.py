"""Circuit partitioning.

Cuts the Clifford part of a circuit into connected gate windows, each of
which becomes one synthesized cell. The cut is *per-qubit*: a ``t``,
``tdg`` or ``measure`` gate interrupts only its own qubit's timeline, so
Clifford context on the remaining qubits fuses across it. T gates leave
the windows entirely and become :class:`InjectionEvent` records,
scheduled between cells as magic-state injections.

The result is a :class:`PartitionedCircuit` — regions, injection events
and the flattened gate list — which also renders itself as a terminal
timeline (:meth:`PartitionedCircuit.to_text`) or a figure
(:meth:`PartitionedCircuit.save_png`).
"""
import bisect
from dataclasses import dataclass, field

from qiskit.circuit import QuantumCircuit

from .parse import CLIFFORD_GATES


@dataclass
class Region:
    qubits: list
    gates: list
    in_pins: dict
    out_pins: dict
    first_gate_index: int = -1
    gate_indices: list = field(default_factory=list)


@dataclass(frozen=True)
class InjectionEvent:
    index: int
    qubit: int
    dagger: bool


def injection_events(circuit) -> list:
    """Extract every t/tdg gate as a magic-state injection event.

    :param circuit: qiskit QuantumCircuit (already parse()d).
    :returns: list[InjectionEvent] in gate order.
    """
    return [InjectionEvent(gate.index, gate.qubits[0], gate.name == "tdg")
            for gate in extract_gates(circuit) if gate.name in ("t", "tdg")]


_ANSI_PALETTE = [31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96]


def _ansi(text, region, color):
    if not color or region is None:
        return text
    return f"\033[{_ANSI_PALETTE[region % len(_ANSI_PALETTE)]}m{text}\033[0m"


_PNG_PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
_PNG_SURFACE = "#fcfcfb"
_PNG_TEXT = "#0b0b0b"
_PNG_TEXT2 = "#52514e"
_PNG_RAIL = "#e5e4e0"


@dataclass
class PartitionedCircuit:
    """partition() output: Clifford regions + T injection events + the flattened gate list."""

    num_qubits: int
    gates: list
    regions: list
    events: list

    def to_text(self, color=False):
        """Render the region-tagged per-qubit timeline plus a region table as terminal text.

        :param color: bool, ANSI-color the region tags.
        :returns: str.
        """
        by_index = {gate.index: gate for gate in self.gates}
        region_gates = [[by_index[index] for index in region.gate_indices] for region in self.regions]
        gate_region = {gate.index: region_index for region_index, gates in enumerate(region_gates) for gate in gates}

        cols = []
        for gate in self.gates:
            region_index = gate_region.get(gate.index)
            tag = f"r{region_index}" if region_index is not None else "--"
            width = max(3, len(tag) + 1)
            col = {}
            if gate.name == "cx":
                control, target = gate.qubits
                col[control] = "\u25cf"
                col[target] = "\u2295"
                for qubit in range(min(control, target) + 1, max(control, target)):
                    col.setdefault(qubit, "\u2502")
            else:
                for qubit in gate.qubits:
                    col[qubit] = gate.name[0].upper()
            cols.append((col, region_index, tag, width))

        lines = ["        " + "".join(_ansi(tag.ljust(width), region_index, color) for _, region_index, tag, width in cols)]
        for qubit in range(self.num_qubits):
            row = f"q{qubit:<3}  \u2500\u2500"
            for col, region_index, _, width in cols:
                symbol = col.get(qubit, "\u2500")
                cell = (symbol + "\u2500" * (width - 1)) if symbol != "\u2500" else "\u2500" * width
                row += _ansi(cell, region_index if qubit in col else None, color)
            lines.append(row)

        lines.append("")
        for region_index, gates in enumerate(region_gates):
            description = " ".join(f"{gate.name}({','.join(map(str, gate.qubits))})" for gate in gates)
            label = _ansi(f"r{region_index}", region_index, color)
            lines.append(f"{label}: qubits={self.regions[region_index].qubits}  gates[{len(gates)}]: {description}")
        return "\n".join(lines)

    def save_png(self, path, title=None):
        """Render the partition as a PNG figure: region bands over a gate timeline.

        Lazy-imports matplotlib (optional dependency).

        :param path: str | Path of the output ``.png``.
        :param title: str | None, figure title (default: region/injection counts).
        :returns: None.
        """
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import FancyBboxPatch, Patch

        by_index = {gate.index: gate for gate in self.gates}
        windows = [[by_index[index] for index in region.gate_indices] for region in self.regions]
        gate_region = {gate.index: region_index for region_index, window in enumerate(windows) for gate in window}
        col_of = {gate.index: column for column, gate in enumerate(self.gates)}
        num_gates = len(self.gates)
        if title is None:
            title = f"miniflash partition: {len(self.regions)} regions, {len(self.events)} injections"

        fig_w = max(7.0, 0.34 * num_gates + 2.8)
        fig_h = max(3.0, 0.55 * self.num_qubits + 1.6)
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=150)
        fig.patch.set_facecolor(_PNG_SURFACE)
        ax.set_facecolor(_PNG_SURFACE)

        for qubit in range(self.num_qubits):
            ax.axhline(qubit, color=_PNG_RAIL, lw=1, zorder=1)

        for region_index, window in enumerate(windows):
            color = _PNG_PALETTE[region_index % len(_PNG_PALETTE)]
            columns = [col_of[gate.index] for gate in window]
            qubits = self.regions[region_index].qubits
            box = FancyBboxPatch(
                (min(columns) - 0.38, min(qubits) - 0.32),
                (max(columns) - min(columns)) + 0.76, (max(qubits) - min(qubits)) + 0.64,
                boxstyle="round,pad=0.02,rounding_size=0.15",
                facecolor=color, alpha=0.10, edgecolor=color, lw=1.2, zorder=2)
            ax.add_patch(box)
            red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            chip_text = "#ffffff" if (0.299 * red + 0.587 * green + 0.114 * blue) < 150 else _PNG_TEXT
            ax.text(min(columns) - 0.38, min(qubits) - 0.62, f"r{region_index}",
                    fontsize=8, fontweight="bold", color=chip_text, ha="left", va="center", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.22", fc=color, ec="none"))

        for column, gate in enumerate(self.gates):
            region_index = gate_region.get(gate.index)
            color = _PNG_PALETTE[region_index % len(_PNG_PALETTE)] if region_index is not None else _PNG_TEXT2
            if gate.name == "cx":
                control, target = gate.qubits
                ax.plot([column, column], [control, target], color=color, lw=2, zorder=3)
                ax.plot(column, control, "o", ms=6, color=color, zorder=4)
                ax.plot(column, target, "o", ms=10, mfc="none", mec=color, mew=2, zorder=4)
                ax.plot(column, target, "+", ms=6, color=color, mew=2, zorder=4)
            else:
                ax.plot(column, gate.qubits[0], "s", ms=9, mfc=_PNG_SURFACE, mec=color, mew=2, zorder=4)
                ax.text(column, gate.qubits[0], gate.name[0].upper(), fontsize=6, ha="center", va="center", color=color, zorder=5)

        ax.set_yticks(range(self.num_qubits))
        ax.set_yticklabels([f"q{qubit}" for qubit in range(self.num_qubits)], fontsize=9, color=_PNG_TEXT)
        ax.set_xlabel("gate index", fontsize=9, color=_PNG_TEXT2)
        ax.set_xlim(-0.9, num_gates - 0.1)
        ax.set_ylim(self.num_qubits - 0.5, -1.0)
        ax.tick_params(axis="x", labelsize=8, colors=_PNG_TEXT2)
        ax.tick_params(axis="y", length=0)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.set_title(title, fontsize=11, color=_PNG_TEXT, loc="left", pad=12)

        handles = [
            Patch(facecolor=_PNG_PALETTE[region_index % len(_PNG_PALETTE)],
                  label=f"r{region_index}: q{{{','.join(map(str, region.qubits))}}} · "
                        f"{len(region.gate_indices)} gate{'s' if len(region.gate_indices) > 1 else ''}")
            for region_index, region in enumerate(self.regions)
        ]
        ax.legend(handles=handles, loc="center left", bbox_to_anchor=(1.01, 0.5), fontsize=8, frameon=False, labelcolor=_PNG_TEXT)

        fig.tight_layout()
        fig.savefig(path, facecolor=_PNG_SURFACE, bbox_inches="tight")
        plt.close(fig)


@dataclass
class _Gate:
    index: int
    name: str
    qubits: tuple


@dataclass
class _Window:
    gates: list = field(default_factory=list)
    qubits: list = field(default_factory=list)


def extract_gates(circuit):
    """Flatten a circuit into indexed gates (barriers dropped).

    :param circuit: qiskit QuantumCircuit.
    :returns: list of gate records with .index/.name/.qubits.
    """
    gates = []
    index = 0
    for instruction in circuit.data:
        name = instruction.operation.name
        if name == "barrier":
            continue
        qubits = tuple(circuit.find_bit(qubit).index for qubit in instruction.qubits)
        gates.append(_Gate(index, name, qubits))
        index += 1
    return gates


class _UnionFind:
    def __init__(self):
        self._parent = {}

    def find(self, node):
        self._parent.setdefault(node, node)
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, node_a, node_b):
        root_a, root_b = self.find(node_a), self.find(node_b)
        if root_a != root_b:
            self._parent[root_b] = root_a


def _qubit_union_find(gates):
    union_find = _UnionFind()
    for gate in gates:
        for qubit in gate.qubits:
            union_find.find(qubit)
        for qubit in gate.qubits[1:]:
            union_find.union(gate.qubits[0], qubit)
    return union_find


def _is_qubit_connected(gates):
    if not gates:
        return False
    union_find = _qubit_union_find(gates)
    active = {qubit for gate in gates for qubit in gate.qubits}
    if not active:
        return False
    return len({union_find.find(qubit) for qubit in active}) == 1


def _split_connected_components(run):
    union_find = _qubit_union_find(run)
    groups = {}
    order = []
    for gate in run:
        root = union_find.find(gate.qubits[0])
        if root not in groups:
            groups[root] = []
            order.append(root)
        groups[root].append(gate)
    return [groups[root] for root in order]


def _score_window(window, qubits):
    two_qubit_count = sum(1 for gate in window if len(gate.qubits) >= 2)
    span = max(window[-1].index - window[0].index + 1, 1)
    density = len(window) / span
    return 5.0 * two_qubit_count + 1.25 * len(window) + 2.0 * density - 1.5 * len(qubits)


def _t_between(t_indices, low, high):
    if not t_indices:
        return False
    position = bisect.bisect_right(t_indices, low)
    return position < len(t_indices) and t_indices[position] < high


def _select_windows(run, max_qubits, max_gates, t_by_qubit=None):
    num_gates = len(run)
    candidates = []
    for start in range(num_gates):
        active = set()
        last_on_qubit = {}
        upper = min(num_gates, start + max_gates)
        for end in range(start, upper):
            gate = run[end]
            if t_by_qubit and any(qubit in last_on_qubit and _t_between(t_by_qubit.get(qubit), last_on_qubit[qubit], gate.index) for qubit in gate.qubits):
                break
            for qubit in gate.qubits:
                last_on_qubit[qubit] = gate.index
            active.update(gate.qubits)
            if len(active) > max_qubits:
                break
            window = run[start : end + 1]
            if not _is_qubit_connected(window):
                continue
            qubits = sorted(active)
            score = _score_window(window, qubits)
            candidates.append((score, len(window), window[0].index, len(qubits), _Window(window, qubits)))

    candidates.sort(key=lambda candidate: (-candidate[0], -candidate[1], candidate[2], candidate[3]))
    selected = []
    used = set()
    for _, _, _, _, window in candidates:
        indices = [gate.index for gate in window.gates]
        if any(index in used for index in indices):
            continue
        used.update(indices)
        selected.append(window)

    selected.sort(key=lambda window: window.gates[0].index)
    return selected


def _absorb_adjacent_gates(all_gates, windows, max_gates):
    covered = {gate.index for window in windows for gate in window.gates}
    uncovered = [gate for gate in all_gates if gate.index not in covered and gate.name == "h" and len(gate.qubits) == 1]
    if not uncovered:
        return

    separators_by_qubit = {}
    for gate in all_gates:
        if gate.name not in CLIFFORD_GATES:
            for gate_qubit in gate.qubits:
                separators_by_qubit.setdefault(gate_qubit, []).append(gate.index)
    windows_by_qubit = {}
    for window_index, window in enumerate(windows):
        for qubit in window.qubits:
            windows_by_qubit.setdefault(qubit, []).append(window_index)

    for gate in uncovered:
        qubit = gate.qubits[0]
        best_window_index, best_distance = -1, None
        for window_index in windows_by_qubit.get(qubit, []):
            window = windows[window_index]
            if len(window.gates) >= max_gates:
                continue
            for member_gate in window.gates:
                if qubit not in member_gate.qubits:
                    continue
                low, high = min(gate.index, member_gate.index), max(gate.index, member_gate.index)
                if any(low < separator < high for separator in separators_by_qubit.get(qubit, ())):
                    continue
                distance = high - low
                if best_distance is None or distance < best_distance:
                    best_distance, best_window_index = distance, window_index
        if best_window_index >= 0:
            windows[best_window_index].gates.append(gate)
            windows[best_window_index].gates.sort(key=lambda member: member.index)


def _rescue_orphans(all_gates, windows):
    covered = {gate.index for window in windows for gate in window.gates}
    orphans = [gate for gate in all_gates if gate.name in CLIFFORD_GATES and len(gate.qubits) == 1 and gate.index not in covered]
    if not orphans:
        return

    touching = {}
    for gate in all_gates:
        for qubit in gate.qubits:
            touching.setdefault(qubit, []).append(gate.index)
    groups = []
    for gate in orphans:
        previous = groups[-1][-1] if groups else None
        if previous is not None and previous.qubits == gate.qubits and not any(previous.index < index < gate.index for index in touching[gate.qubits[0]]):
            groups[-1].append(gate)
        else:
            groups.append([gate])

    for group in groups:
        windows.append(_Window(gates=group, qubits=[group[0].qubits[0]]))
    windows.sort(key=lambda window: window.gates[0].index)


def partition(circuit: QuantumCircuit, max_qubits=4, max_gates=16) -> list:
    """Cut the Clifford part of a circuit into connected windows for cell synthesis.

    t/tdg/measure cut ONLY their own qubit's timeline (per-qubit T cut):
    Clifford context on other qubits fuses across them.

    :param circuit: qiskit QuantumCircuit.
    :param max_qubits: int, cap on qubits per region.
    :param max_gates: int, cap on gates per region.
    :returns: PartitionedCircuit — regions ordered by first gate index, plus
        the t/tdg injection events and the flattened gate list.
    :raises ValueError: if a Clifford gate cannot be covered under the caps.
    """
    gates = extract_gates(circuit)
    clifford_gates = [gate for gate in gates if gate.name in CLIFFORD_GATES]
    t_by_qubit = {}
    for gate in gates:
        if gate.name not in CLIFFORD_GATES:
            for gate_qubit in gate.qubits:
                t_by_qubit.setdefault(gate_qubit, []).append(gate.index)
    windows = []
    for component in _split_connected_components(clifford_gates):
        windows.extend(_select_windows(component, max_qubits, max_gates, t_by_qubit))
    windows = [window for window in windows if any(len(gate.qubits) >= 2 for gate in window.gates)]
    _absorb_adjacent_gates(gates, windows, max_gates)
    _rescue_orphans(gates, windows)

    covered = {gate.index for window in windows for gate in window.gates}
    for gate in gates:
        if gate.name in CLIFFORD_GATES and gate.index not in covered:
            qubits = ",".join(str(qubit) for qubit in gate.qubits)
            raise ValueError(f"partition: gate '{gate.name}' (index {gate.index}, qubits {qubits}) not covered by any region — region caps too tight (max_qubits={max_qubits}, max_gates={max_gates})")

    regions = []
    for window in windows:
        in_pins = {qubit: f"in_{local_index}" for local_index, qubit in enumerate(window.qubits)}
        out_pins = {qubit: f"out_{local_index}" for local_index, qubit in enumerate(window.qubits)}
        gate_tuples = [(gate.name, gate.qubits) for gate in window.gates]
        regions.append(Region(qubits=window.qubits, gates=gate_tuples, in_pins=in_pins, out_pins=out_pins, first_gate_index=window.gates[0].index, gate_indices=[gate.index for gate in window.gates]))

    events = [InjectionEvent(gate.index, gate.qubits[0], gate.name == "tdg") for gate in gates if gate.name in ("t", "tdg")]
    return PartitionedCircuit(num_qubits=circuit.num_qubits, gates=gates, regions=regions, events=events)


def split_region(region, max_qubits, max_gates) -> list:
    """Re-partition one Region under tighter caps (coarse-portfolio fallback).

    :param region: Region.
    :param max_qubits: int, cap on qubits per sub-region.
    :param max_gates: int, cap on gates per sub-region.
    :returns: list[Region] covering the same gates.
    :raises ValueError: if the caps are too tight.
    """
    indices = region.gate_indices or list(range(len(region.gates)))
    gates = [_Gate(index, name, qubits) for index, (name, qubits) in zip(indices, region.gates)]
    windows = []
    for component in _split_connected_components(gates):
        windows.extend(_select_windows(component, max_qubits, max_gates))
    windows = [window for window in windows if any(len(gate.qubits) >= 2 for gate in window.gates)]
    _absorb_adjacent_gates(gates, windows, max_gates)
    _rescue_orphans(gates, windows)
    covered = {gate.index for window in windows for gate in window.gates}
    if any(gate.index not in covered for gate in gates):
        raise ValueError(f"split_region: caps too tight (max_qubits={max_qubits}, max_gates={max_gates})")
    windows.sort(key=lambda window: window.gates[0].index)
    subs = []
    for window in windows:
        in_pins = {qubit: f"in_{local_index}" for local_index, qubit in enumerate(window.qubits)}
        out_pins = {qubit: f"out_{local_index}" for local_index, qubit in enumerate(window.qubits)}
        subs.append(Region(qubits=window.qubits, gates=[(gate.name, gate.qubits) for gate in window.gates], in_pins=in_pins, out_pins=out_pins, first_gate_index=region.first_gate_index, gate_indices=[gate.index for gate in window.gates]))
    return subs
