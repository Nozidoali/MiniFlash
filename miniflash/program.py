"""The Program IR.

The compiler's midpoint: a :class:`Program` holds macros (cells,
injections, factories), parity-colored inter-layer nets, channels,
parking and exit colors — everything the backend needs and nothing left
for it to decide. :func:`elaborate` fuses the floorplan, the synthesized
cells and the injection points into a Program; :meth:`Program.stats`
reports volume metrics, per-region Pauli frames and injection
correction tables. The IR round-trips through JSON via ``to_dict`` /
``from_dict``.
"""
from dataclasses import dataclass, field

from .synthesis import Cell
from .floorplan import place_injections
from .factory import FactorySpec, correction_for


def _deep_tuple(value):
    return tuple(_deep_tuple(item) for item in value) if isinstance(value, (list, tuple)) else value


def _deep_list(value):
    return [_deep_list(item) for item in value] if isinstance(value, (list, tuple)) else value


@dataclass(frozen=True)
class MacroInstance:
    """One placed macro: a synthesized cell, an injection crossbar or a factory."""

    #: "cell" | "injection" | "factory"
    kind: str
    #: geometry payload — Cell for cells, FactorySpec for factories
    ref: object
    #: layer index for cells, channel index for injections
    layer: int
    #: die row
    row: int
    #: column offset of the macro's left edge
    offset: int
    #: qubits the macro acts on
    qubits: tuple = ()
    #: injection round within the channel
    round: int = 0
    #: injection crossbar slot (0 | 1)
    slot: int = 0
    #: in-gap track level, -1 for block injections
    level: int = -1


@dataclass(frozen=True)
class Net:
    """One qubit's wire through one channel, source endpoint to sink endpoint."""

    #: qubit the wire carries
    qubit: int
    #: endpoint at the upper boundary (pin or parked-slot descriptor)
    source: tuple
    #: endpoint at the lower boundary
    sink: tuple
    #: channel (inter-layer gap) index
    channel: int
    #: entry track level of the jog chain, -1 for a straight run
    track: int
    #: jog tuples (level, from, to, plane); die mode appends route segments
    path: tuple
    #: wire color bit entering the next layer (0 | 1)
    color: int
    #: True when a pending Hadamard must land before the next cell
    flip: bool


@dataclass(frozen=True)
class Channel:
    """An inter-layer routing gap."""

    #: channel index (between layer ``index`` and ``index + 1``)
    index: int
    #: number of jog track levels
    tracks: int


@dataclass
class Program:
    """The compiled IR: placed macros, routed nets and channel geometry."""

    #: (width, rows | None) die constraint, None for 1-D
    die_dims: tuple
    #: number of die rows used
    rows: int = 0
    #: placed MacroInstance list (cells, injections, factories)
    macros: list = field(default_factory=list)
    #: routed Net list, one per qubit per channel
    nets: list = field(default_factory=list)
    #: Channel list, one per inter-layer gap
    channels: list = field(default_factory=list)
    #: number of logical qubits
    num_qubits: int = 0
    #: leftmost magic-lane column, None without injections
    magic_column: int = None
    #: per layer, cell-band width in columns
    box_widths: list = field(default_factory=list)
    #: per layer, {qubit: (row, column)} parked slots
    parking: list = field(default_factory=list)
    #: per layer, [exit map, entry map] of side moves as (lane, slot, plane)
    sides: list = field(default_factory=list)
    #: {qubit: color bit} at the final boundary
    exit_colors: dict = field(default_factory=dict)

    def to_dict(self):
        """Serialize the Program to a JSON-safe dict (inverse of from_dict)."""
        def ref_dict(macro):
            if macro.kind == "cell":
                return macro.ref.to_dict()
            if macro.kind == "factory":
                return {"name": macro.ref.name, "dim_i": macro.ref.dim_i, "dim_j": macro.ref.dim_j, "interval_k": macro.ref.interval_k}
            return list(macro.ref)
        return {
            "die_dims": list(self.die_dims) if self.die_dims else None,
            "rows": self.rows,
            "num_qubits": self.num_qubits,
            "magic_column": self.magic_column,
            "box_widths": list(self.box_widths),
            "parking": [[[qubit, list(slot)] for qubit, slot in layer.items()] for layer in self.parking],
            "sides": [[[[qubit, list(entry)] for qubit, entry in group.items()] for group in layer] for layer in self.sides],
            "exit_colors": [[qubit, color] for qubit, color in self.exit_colors.items()],
            "macros": [{"kind": m.kind, "ref": ref_dict(m), "layer": m.layer, "row": m.row, "offset": m.offset, "qubits": list(m.qubits), "round": m.round, "slot": m.slot, "level": m.level} for m in self.macros],
            "nets": [{"qubit": n.qubit, "source": list(n.source), "sink": list(n.sink), "channel": n.channel, "track": n.track, "path": _deep_list(n.path), "color": n.color, "flip": n.flip} for n in self.nets],
            "channels": [{"index": c.index, "tracks": c.tracks} for c in self.channels],
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild a Program from a to_dict() payload.

        :param data: dict from :meth:`to_dict`.
        :returns: Program.
        """
        def ref_load(kind, ref):
            if kind == "cell":
                return Cell.from_dict(ref)
            if kind == "factory":
                return FactorySpec(**ref)
            return tuple(ref)
        return cls(
            die_dims=tuple(data["die_dims"]) if data["die_dims"] else None,
            rows=data.get("rows", 0),
            num_qubits=data["num_qubits"],
            magic_column=data["magic_column"],
            box_widths=list(data["box_widths"]),
            parking=[{qubit: tuple(slot) for qubit, slot in layer} for layer in data["parking"]],
            sides=[[{qubit: tuple(entry) for qubit, entry in group} for group in layer] for layer in data["sides"]],
            exit_colors={qubit: color for qubit, color in data["exit_colors"]},
            macros=[MacroInstance(kind=m["kind"], ref=ref_load(m["kind"], m["ref"]), layer=m["layer"], row=m["row"], offset=m["offset"], qubits=tuple(m["qubits"]), round=m.get("round", 0), slot=m.get("slot", 0), level=m.get("level", -1)) for m in data["macros"]],
            nets=[Net(qubit=n["qubit"], source=tuple(n["source"]), sink=tuple(n["sink"]), channel=n["channel"], track=n["track"], path=_deep_tuple(n["path"]), color=n["color"], flip=n["flip"]) for n in data["nets"]],
            channels=[Channel(**c) for c in data["channels"]],
        )


    def stats(self):
        """Compile-quality metrics of this Program: volumes, parity frames, corrections.

        Lowers the IR internally to measure geometry.

        :returns: dict — route_stats, volume, occupied_volume, cube_envelope,
            bbox, pauli_frames; die_dims when die-constrained; factory, t_count,
            wait_levels, compute_volume and per-injection corrections when the
            circuit has T gates.
        """
        from .gltf import build_layout

        layout = build_layout(self)
        cell_macros = [macro for macro in self.macros if macro.kind == "cell"]
        injection_macros = [macro for macro in self.macros if macro.kind == "injection"]
        factory_spec = next((macro.ref for macro in self.macros if macro.kind == "factory"), None)

        stats = {"route_stats": layout["route_stats"], "volume": layout["volume"], "occupied_volume": layout["occupied_volume"], "cube_envelope": layout["cube_envelope"], "bbox": list(layout["bbox"]), "pauli_frames": {f"region_{index}": macro.ref.pauli_frame for index, macro in enumerate(cell_macros)}, "factory": None}
        if self.die_dims is not None:
            stats["die_dims"] = {"width": self.die_dims[0], "rows_cap": self.die_dims[1], "rows": self.rows}
        if injection_macros:
            stats["factory"] = {"name": factory_spec.name, "dim_i": factory_spec.dim_i, "dim_j": factory_spec.dim_j, "interval_k": factory_spec.interval_k}
            stats["t_count"] = layout["t_count"]
            stats["wait_levels"] = layout["wait_levels"]
            stats["compute_volume"] = layout["compute_volume"]
            stats["injections"] = [
                {
                    "qubit": macro.qubits[0],
                    "channel": macro.layer,
                    "dagger": macro.ref[1],
                    "readout": "X if s==0 else Y",
                    "corrections": {f"{s}{r}": correction_for((s, r), macro.ref[1]) for s in (0, 1) for r in (0, 1)},
                }
                for macro in injection_macros
            ]
        return stats


def _normalized_placements(floorplan, layer_cells):
    placements = []
    for layer, entry in enumerate(layer_cells):
        cells_list = [entry] if isinstance(entry, Cell) else list(entry)
        layouts = floorplan.placements[layer] if floorplan.placements else [(0, 0, None)] * len(cells_list)
        placements.append(list(zip(cells_list, layouts)))
    return placements


def _parity_ledger(floorplan, placements):
    num_layers = len(placements)
    side_exits = floorplan.side_exits if floorplan.side_exits else [{} for _ in range(num_layers)]
    exit_bits = []
    wire_bits = {qubit: 0 for qubit in range(floorplan.num_qubits)}
    for layer in range(num_layers):
        for cell, (row, offset, box_qubits) in placements[layer]:
            qubits = box_qubits if box_qubits is not None else list(floorplan.out_port_columns[layer])
            wire_bits.update(cell.pin_parities({qubit: floorplan.out_port_columns[layer][qubit] - offset for qubit in qubits if qubit in floorplan.out_port_columns[layer]}, "out"))
        for qubit in side_exits[layer]:
            wire_bits[qubit] = 0
        exit_bits.append(dict(wire_bits))
    return exit_bits


def _slots(floorplan, layer, use_out):
    columns = floorplan.out_port_columns[layer] if use_out else floorplan.in_port_columns[layer]
    slots = {}
    if floorplan.die_dims is not None:
        for row, _, box in floorplan.placements[layer]:
            for qubit in box:
                if qubit in columns:
                    slots[qubit] = ("pin", row, columns[qubit])
        for qubit, (row, col) in floorplan.parked_slots[layer].items():
            slots[qubit] = ("park", row, col)
    else:
        for qubit, col in columns.items():
            slots[qubit] = ("pin", 0, col)
        for qubit, col in floorplan.lane_columns[layer].items():
            slots[qubit] = ("park", 0, col)
    return slots


def elaborate(floorplan, layer_cells, events=(), channels=(), factory=None, factories=1):
    """Fuse the floorplan, synthesized cells and injections into the Program IR.

    :param floorplan: Floorplan.
    :param layer_cells: list per layer of Cell (or a single Cell).
    :param events: list[InjectionEvent].
    :param channels: list[int] from synthesize, parallel to events.
    :param factory: FactorySpec | None.
    :param factories: int, factory units.
    :returns: Program — cell/factory/injection macros, parity-colored inter-layer
        nets (flip marks a Hadamard landing), channels, parking, sides and exit colors.
    """
    points = place_injections(events, channels, floorplan, factory=factory, factories=factories) if events else []
    placements = _normalized_placements(floorplan, layer_cells)
    num_layers = len(placements)
    exit_bits = _parity_ledger(floorplan, placements)

    macros = []
    for layer in range(num_layers):
        for cell, (row, offset, box_qubits) in placements[layer]:
            macros.append(MacroInstance(kind="cell", ref=cell, layer=layer, row=row, offset=offset, qubits=tuple(box_qubits if box_qubits is not None else sorted(floorplan.out_port_columns[layer]))))
    if factory is not None and points:
        for unit in range(factories):
            macros.append(MacroInstance(kind="factory", ref=factory, layer=-1, row=unit, offset=0))
        for point in points:
            macros.append(MacroInstance(kind="injection", ref=("gadget", point.dagger), layer=point.channel, row=point.row, offset=point.column, qubits=(point.qubit,), round=point.round, slot=point.slot, level=point.level))

    nets = []
    for channel in range(num_layers - 1):
        before = _slots(floorplan, channel, use_out=True)
        after = _slots(floorplan, channel + 1, use_out=False)
        consumed_next = set(floorplan.in_port_columns[channel + 1])
        if floorplan.die_dims is not None:
            moved = {move.qubit: [] for move in floorplan.grid_moves[channel]}
            for move in sorted(floorplan.grid_moves[channel], key=lambda move: move.level):
                moved[move.qubit].append(move)
        else:
            moved = {}
            for move in sorted(floorplan.moves[channel], key=lambda move: move.level):
                moved.setdefault(move.qubit, []).append(move)

        for qubit in sorted(before):
            if qubit not in after:
                continue
            chain = moved.get(qubit, [])
            if floorplan.die_dims is not None:
                path = tuple((move.level, move.src, move.dst) + tuple(move.path) for move in chain)
            else:
                path = tuple((move.level, move.from_column, move.to_column, move.plane) for move in chain)
            color = exit_bits[channel][qubit]
            nets.append(Net(qubit=qubit, source=before[qubit], sink=after[qubit], channel=channel, track=chain[0].level if chain else -1, path=path, color=color, flip=color == 1 and qubit in consumed_next))

    channels = [Channel(index=channel, tracks=floorplan.gap_levels[channel]) for channel in range(num_layers - 1)]
    if floorplan.die_dims is not None:
        parking = [dict(floorplan.parked_slots[layer]) for layer in range(num_layers)]
    else:
        parking = [{qubit: (0, col) for qubit, col in floorplan.lane_columns[layer].items()} for layer in range(num_layers)]
    side_exits = floorplan.side_exits if floorplan.side_exits else [{} for _ in range(num_layers)]
    side_entries = floorplan.side_entries if floorplan.side_entries else [{} for _ in range(num_layers)]
    sides = [[{qubit: (move.lane_column, move.slot, move.plane) for qubit, move in side_exits[layer].items()}, {qubit: (move.lane_column, move.slot, move.plane) for qubit, move in side_entries[layer].items()}] for layer in range(num_layers)]

    return Program(die_dims=floorplan.die_dims, rows=floorplan.rows, macros=macros, nets=nets, channels=channels, num_qubits=floorplan.num_qubits, magic_column=floorplan.magic_column, box_widths=list(floorplan.box_widths), parking=parking, sides=sides, exit_colors=dict(exit_bits[-1]) if exit_bits else {})
