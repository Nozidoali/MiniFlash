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


def _component(boxes, low, high, size, key):
    lo, hi, total, keys = low, high, size, {key}
    changed = True
    while changed:
        changed = False
        for box_low, box_high, box_size, box_key in boxes:
            if box_key not in keys and not (hi < box_low or box_high < lo):
                keys.add(box_key)
                total += box_size
                lo, hi = min(lo, box_low), max(hi, box_high)
                changed = True
    return total, keys


def _region_anchors(region):
    anchors = {}
    if region.gate_indices:
        for index, (_, qubits) in zip(region.gate_indices, region.gates):
            for qubit in qubits:
                if qubit not in anchors or index < anchors[qubit]:
                    anchors[qubit] = index
    return {qubit: anchors.get(qubit, region.first_gate_index) for qubit in region.qubits}


def schedule_layers(partitioned, die_dims=None, disjoint_columns=False, column_of=None, merge_cap=None, no_merge=()):
    """Assign regions to execution layers and injection events to inter-layer channels.

    Processes items in per-qubit timeline order (fused regions may start, in global
    index, before an injection that must precede their gates on the injected qubit).

    :param partitioned: PartitionedCircuit.
    :param die_dims: (width, rows | None) or None for 1-D.
    :param disjoint_columns: bool, admit a region to a layer only if its qubit
        interval does not interleave with regions already in the layer
        (fixed-column mode: stretched boxes must not overlap).
    :param merge_cap: int | None, in disjoint-column mode also admit an
        interleaving region when the resulting overlap component stays within
        this many qubits (the component is later synthesized as one joint cell).
    :param no_merge: iterable of frozensets, overlap components (keyed by each
        member's first gate index) that must not form — failed joint cells.
    :returns: (layers, channels) — layers is list per layer of Region; channels is
        list[int] per event, the layer index before which the injection fires.
    :raises ValueError: if a region cannot fit the die.
    """
    regions, events, num_qubits = partitioned.regions, partitioned.events, partitioned.num_qubits
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
    layers, loads, members, intervals = [], [], [], []
    channels = {}
    region_layer = {}
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
        if disjoint_columns:
            spans = [column_of[qubit] for qubit in node.qubits] if column_of else node.qubits
            low, high = min(spans), max(spans)
            key = node.gate_indices[0] if node.gate_indices else node.first_gate_index
            while layer < len(layers):
                total, keys = _component(intervals[layer], low, high, len(node.qubits), key)
                if total == len(node.qubits):
                    break
                if merge_cap and total <= merge_cap and not any(banned <= keys for banned in no_merge):
                    break
                layer += 1
        while len(layers) <= layer:
            layers.append([])
            loads.append([])
            members.append(0)
            intervals.append([])

        layers[layer].append(node)
        region_layer[id(node)] = layer
        members[layer] += len(node.qubits)
        if disjoint_columns:
            intervals[layer].append((low, high, len(node.qubits), key))
        for load_index, load in enumerate(loads[layer]):
            if die_dims is not None and load + box_width <= die_dims[0]:
                loads[layer][load_index] += box_width
                break
        else:
            loads[layer].append(box_width)
        for qubit in node.qubits:
            next_free[qubit] = layer + 1
            position[qubit] += 1

    if disjoint_columns and events:
        _pack_events(sequence, channels, region_layer, len(layers))
    return layers, [channels[event] for event in events]


def _pack_events(sequence, channels, region_layer, num_layers):
    windows = []
    for items in sequence.values():
        next_layer = num_layers
        for _, _, (kind, node, _) in reversed(items):
            if kind == "region":
                next_layer = region_layer[id(node)]
            else:
                windows.append((node.index, node, next_layer))

    counts, stacks, floors = {}, {}, {}

    def bands(channel):
        stack = stacks.get(channel, {})
        return max((counts.get(channel, 0) + 1) // 2, max(stack.values(), default=0))

    def occupy(channel, qubit):
        counts[channel] = counts.get(channel, 0) + 1
        stack = stacks.setdefault(channel, {})
        stack[qubit] = stack.get(qubit, 0) + 1

    for _, event, high in sorted(windows):
        low = max(channels[event], floors.get(event.qubit, 0))
        best, best_cost = channels[event], None
        for channel in range(max(low, 1), min(high, num_layers - 1) + 1):
            before = bands(channel)
            occupy(channel, event.qubit)
            cost = bands(channel) - before
            counts[channel] -= 1
            stacks[channel][event.qubit] -= 1
            if best_cost is None or cost < best_cost:
                best, best_cost = channel, cost
                if cost == 0:
                    break
        occupy(best, event.qubit)
        channels[event] = best
        floors[event.qubit] = best
