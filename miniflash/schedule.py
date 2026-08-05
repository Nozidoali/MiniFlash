"""Layer scheduling.

Levelizes the region/injection dependency graph as soon as possible:
regions on disjoint qubits execute side by side in one layer, and every
injection is assigned the channel between its neighboring layers. Die
mode adds an admission test — a region enters a layer only if its box
fits the hard die width, placed area-first with row first-fit.
"""
def _admits(loads, members, box_width, box_qubits, die_dims, num_qubits):
    width, rows = die_dims
    if rows is None:
        return True
    if sum(loads) + box_width + (num_qubits - members - box_qubits) > width * rows:
        return False
    if any(load + box_width <= width for load in loads):
        return True
    return len(loads) < rows


def _region_anchors(region):
    anchors = {}
    if region.gate_indices:
        for index, (_, qubits) in zip(region.gate_indices, region.gates):
            for qubit in qubits:
                if qubit not in anchors or index < anchors[qubit]:
                    anchors[qubit] = index
    return {qubit: anchors.get(qubit, region.first_gate_index) for qubit in region.qubits}


def schedule_layers(regions, events, die_dims=None, num_qubits=0):
    """Assign regions to execution layers and injection events to inter-layer channels.

    Processes items in per-qubit timeline order (fused regions may start, in global
    index, before an injection that must precede their gates on the injected qubit).

    :param regions: list[Region].
    :param events: list[InjectionEvent].
    :param die_dims: (width, rows | None) or None for 1-D.
    :param num_qubits: int, needed for die admission checks.
    :returns: (layers, channels) — layers is list per layer of Region; channels is
        list[int] per event, the layer index before which the injection fires.
    :raises ValueError: if a region cannot fit the die.
    """
    sequence = {}
    entries = []
    for order, region in enumerate(regions):
        entry = ("region", region, (region.first_gate_index, 0, order))
        for qubit, anchor in _region_anchors(region).items():
            sequence.setdefault(qubit, []).append((anchor, order, entry))
        entries.append(entry)
    for order, event in enumerate(events):
        entry = ("event", event, (event.index, 1, order))
        sequence.setdefault(event.qubit, []).append((event.index, len(regions) + order, entry))
        entries.append(entry)

    position = {}
    for qubit in sequence:
        sequence[qubit].sort(key=lambda item: (item[0], item[1]))
        position[qubit] = 0

    def at_front(entry):
        qubits = entry[1].qubits if entry[0] == "region" else [entry[1].qubit]
        return all(sequence[qubit][position[qubit]][2] is entry for qubit in qubits)

    next_free = {}
    layers, loads, members = [], [], []
    channels = {}
    pending = sorted(entries, key=lambda entry: entry[2])
    while pending:
        entry = next((candidate for candidate in pending if at_front(candidate)), None)
        if entry is None:
            raise ValueError("schedule_layers: cyclic region/event dependencies")
        pending.remove(entry)
        kind, node, _ = entry
        if kind == "event":
            channels[node] = next_free.get(node.qubit, 0)
            position[node.qubit] += 1
            continue

        box_width = len(node.qubits) + 2
        if die_dims is not None and box_width > die_dims[0]:
            raise ValueError(f"schedule_layers: box width {box_width} exceeds die width {die_dims[0]}")
        layer = max((next_free.get(qubit, 0) for qubit in node.qubits), default=0)
        if die_dims is not None:
            while layer < len(layers) and not _admits(loads[layer], members[layer], box_width, len(node.qubits), die_dims, num_qubits):
                layer += 1
            if layer >= len(layers) and not _admits([], 0, box_width, len(node.qubits), die_dims, num_qubits):
                raise ValueError(f"schedule_layers: region {node.qubits} does not fit die {die_dims} with {num_qubits} qubits")
        while len(layers) <= layer:
            layers.append([])
            loads.append([])
            members.append(0)

        layers[layer].append(node)
        members[layer] += len(node.qubits)
        for load_index, load in enumerate(loads[layer]):
            if die_dims is not None and load + box_width <= die_dims[0]:
                loads[layer][load_index] += box_width
                break
        else:
            loads[layer].append(box_width)
        for qubit in node.qubits:
            next_free[qubit] = layer + 1
            position[qubit] += 1

    return layers, [channels[event] for event in events]
