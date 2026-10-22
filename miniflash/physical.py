"""Explicit factory fabrics with fixed sites, finite storage and planar routing.

Coordinates are integer logical tiles and logical time steps (two native cube
units). Cells are translated, never synthesized again or rotated. Long-range
paths occupy their entire ancilla corridor for the two-step injection gadget;
this is MiniFlash's lattice-surgery abstraction, not a hopping simulator.
"""
from collections import Counter, OrderedDict, defaultdict, deque
import math
import heapq

MODES = ("in_place", "backside_vertical", "backside_horizontal")
HORIZONTAL_CORRIDOR_ROWS = 4
SCHEMA = "miniflash_physical_fabric_v1"


class LayoutInfeasible(RuntimeError):
    pass


def validate_mode(mode):
    if mode not in MODES:
        raise ValueError(f"unknown factory_mode {mode!r}; expected one of {MODES}")


def resolve_mode(mode, factories):
    """Resolve the omitted policy once from the requested hardware budget."""
    if type(factories) is not int or factories < 1:
        raise ValueError("factories must be a positive integer")
    if mode is None:
        return "in_place" if factories <= 2 else "backside_horizontal"
    validate_mode(mode)
    return mode


def rectangle(rect):
    x, y, w, h = rect
    return {(i, j) for i in range(x, x + w) for j in range(y, y + h)}


def distance(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def shortest_path(source, goal, blocked, width, height):
    """Deterministic Manhattan A*: exact shortest paths without flooding open chips."""
    source, goal = tuple(source), tuple(goal)
    if source in blocked or goal in blocked:
        return None
    if not all(0 <= x < width and 0 <= y < height for x, y in (source, goal)):
        return None
    parents, costs = {source: None}, {source: 0}
    queue = [(distance(source, goal), distance(source, goal), source)]
    while queue:
        priority, heuristic, p = heapq.heappop(queue)
        g = priority - heuristic
        if g != costs[p]:
            continue
        if p == goal:
            path = []
            while p is not None:
                path.append(p)
                p = parents[p]
            return path[::-1]
        x, y = p
        for nxt in ((x + 1, y), (x, y + 1), (x - 1, y), (x, y - 1)):
            if (0 <= nxt[0] < width and 0 <= nxt[1] < height
                    and nxt not in blocked and g + 1 < costs.get(nxt, float('inf'))):
                parents[nxt] = p
                costs[nxt] = g + 1
                h = distance(nxt, goal)
                heapq.heappush(queue, (g + 1 + h, h, nxt))
    return None


def _turn_points(path):
    if len(path) < 3:
        return tuple(path)
    result = [path[0]]
    for a, b, c in zip(path, path[1:], path[2:]):
        if (b[0]-a[0], b[1]-a[1]) != (c[0]-b[0], c[1]-b[1]):
            result.append(b)
    result.append(path[-1])
    return tuple(result)


def _expand_route(route):
    if not route:
        return ()
    expanded = [tuple(route[0])]
    for a, b in zip(route, route[1:]):
        dx, dy = b[0]-a[0], b[1]-a[1]
        if (dx == 0) == (dy == 0):
            raise ValueError("route has a diagonal or zero-length segment")
        steps = abs(dx)+abs(dy)
        sx, sy = dx//steps, dy//steps
        expanded.extend((a[0]+k*sx,a[1]+k*sy) for k in range(1,steps+1))
    return tuple(expanded)


def _path_mask(path, width, height):
    bits = bytearray((width*height+7)//8)
    for x,y in path:
        index = y*width+x
        bits[index >> 3] |= 1 << (index & 7)
    return int.from_bytes(bits,'little')


def _descriptors(program, floorplan):
    result = []
    for index, macro in enumerate(program.macros):
        if macro.kind != "cell":
            continue
        cell = macro.ref
        if any(pin.get("dir") != "K" for pin in cell.pins):
            raise LayoutInfeasible("physical fabrics require upright cells with K-face pins")
        bindings = []
        for q in macro.qubits:
            pins = []
            for prefix, columns in (("in", floorplan.in_port_columns),
                                    ("out", floorplan.out_port_columns)):
                local_x = 2 * (columns[macro.layer][q] - macro.offset)
                matches = [p for p in cell.pins if p["name"].startswith(prefix)
                           and p["offset"][0] == local_x]
                if len(matches) != 1 or any(v % 2 for v in matches[0]["offset"][:2]):
                    raise LayoutInfeasible("physical fabrics require unique upright K pins on the tile grid")
                pin = matches[0]
                pins.append([pin["offset"][0] // 2, pin["offset"][1] // 2,
                             max(0, int(pin.get("parity", 0)))])
            bindings.append([q, *pins])
        result.append({"macro": index, "layer": macro.layer,
                       "size": [(v + 1) // 2 for v in cell.dims[:2]],
                       "duration": (cell.dims[2] - 1) // 2,
                       "bindings": bindings,
                       "legacy_origin": [macro.offset, macro.row * 3],
                       "pauli_frame": cell.pauli_frame,
                       "in_bases": cell.in_bases})
    return result


def _site(fid, x, y, fw, fh, enabled=True):
    # A fixed buffer one tile to the left of the factory's output face.
    return {"id": fid, "rect": [x, y, fw, fh], "buffer": [x - 1, y],
            "port": [x, y], "enabled": enabled}


def _candidate(program, floorplan, desc, mode, variant):
    n, count = program.num_qubits, program.factories
    fw = max(1, program.factory.dim_i) if program.factory else 1
    fh = max(1, program.factory.dim_j) if program.factory else 1
    sites, cell_layouts, homes = [], [], []
    by_layer = defaultdict(list)
    for d in desc:
        by_layer[d["layer"]].append(d)
    layers = len(floorplan.box_widths)
    # Preserve the original data/cell placement. Factories have real,
    # separate sites; inactive in-place budget units are still reserved.
    left = (math.ceil(count / 2) * (fw + 3) + 2) if mode == "in_place" else 2
    data_width = max([n + 3, *floorplan.box_widths,
                      *(d["legacy_origin"][0] + d["size"][0] for d in desc)])
    data_height = max([3, *(d["legacy_origin"][1] + d["size"][1] for d in desc)])
    width = left + data_width + 3
    height = data_height + 4
    if mode != "in_place":
        for fid in range(count):
            sites.append(_site(fid, width + 2, 2 + fid * (fh + 3), fw, fh))
        width += fw + 5
        height = max(height, count * (fh + 3) + 4)
    else:
        for fid in range(count):
            rank, side = divmod(fid, 2)
            x = left - (rank + 1) * (fw + 3) if side == 0 else left + data_width + 3 + rank * (fw + 3)
            sites.append(_site(fid, x, 2, fw, fh, enabled=fid < 2))
        width += math.ceil(count / 2) * (fw + 3) + 2
        height = max(height, fh + 4)
    for layer in range(layers):
        cell_layouts.append([{**d, "origin": [left + d["legacy_origin"][0], 2 + d["legacy_origin"][1]]}
                             for d in by_layer[layer]])
        if floorplan.die_dims is None:
            homes.append({q: [left + col, 3] for q, col in floorplan.lane_columns[layer].items()})
        else:
            homes.append({q: [left + col, 3 + 3 * row] for q, (row, col) in floorplan.parked_slots[layer].items()})
    initial = {q: [left + q + 1, 3] for q in range(n)}
    # Input ports are free placement variables. Start each input at its first
    # cell pin (or parking slot), rather than paying an artificial relocation.
    if cell_layouts:
        initial = dict(homes[0])
        for d in cell_layouts[0]:
            for q, ip, _ in d["bindings"]:
                initial[q] = [d["origin"][0] + ip[0], d["origin"][1] + ip[1]]
    layout = {"schema": SCHEMA, "mode": mode, "width": width, "height": height,
            "sites": sites, "layers": cell_layouts, "parking": homes,
            "initial": [[q, pos] for q, pos in sorted(initial.items())],
            "candidate": variant, "scratch": [0, 0]}

    return _horizontal_bank(layout, fw, fh) if mode == "backside_horizontal" else layout


def _horizontal_bank(layout, fw, fh):
    """Preserve the compute footprint; put the factory bank four rows below it."""
    cells = [d for layer in layout["layers"] for d in layer]
    positions = [xy for _, xy in layout["initial"]]
    positions += [xy for homes in layout["parking"] for xy in homes.values()]
    right = max([3, *(d["origin"][0] + d["size"][0] for d in cells), *(xy[0] + 1 for xy in positions)])
    bottom = max([3, *(d["origin"][1] + d["size"][1] for d in cells), *(xy[1] + 1 for xy in positions)])
    count = len(layout["sites"])
    width = max(right + 2, count * (fw + 3) + 2)
    bank_y = bottom + HORIZONTAL_CORRIDOR_ROWS
    sites = []
    for fid in range(count):
        x = 3 if count == 1 else 3 + fid * (width - fw - 5) // (count - 1)
        sites.append(_site(fid, x, bank_y, fw, fh))
    layout.update(width=width, height=bank_y + fh + 2 if count else bottom + 2, sites=sites,
                  horizontal_bank={"corridor_rows": HORIZONTAL_CORRIDOR_ROWS, "data_bottom": bottom,
                                   "placement": "one_row_below_preserved_data", "ports": "left"})
    return layout


class _Scheduler:
    def __init__(self, layout, n, period):
        self.layout, self.n, self.period = layout, n, period
        self.width, self.height = layout["width"], layout["height"]
        self.sites = layout["sites"]
        self.factory_obstacles = set().union(*(rectangle(s["rect"]) for s in self.sites))
        self.buffers = {tuple(s["buffer"]) for s in self.sites}
        self.blocked = self.factory_obstacles | self.buffers
        self.pos = {q: tuple(p) for q, p in layout["initial"]}
        self.bits = {q: 0 for q in range(n)}
        self.time = 0
        self.ready = [period] * len(self.sites)
        self.production_start = [0] * len(self.sites)
        self.routes, self.route_ids = [], {}
        self.path_cache = OrderedDict()
        self.moves, self.flips, self.cells, self.injections = [], [], [], []
        self.wait = 0
        self.max_parallel = 0

    def intern(self, route):
        key = _turn_points(route)
        if key not in self.route_ids:
            self.route_ids[key] = len(self.routes)
            self.routes.append([list(p) for p in key])
        return self.route_ids[key]

    def move(self, q, dest):
        dest = tuple(dest)
        occupied = set(self.pos.values()) - {self.pos[q]}
        path = shortest_path(self.pos[q], dest, self.blocked | occupied, self.width, self.height)
        if path is None:
            raise LayoutInfeasible(f"unreachable data move q{q}: {self.pos[q]} -> {dest}")
        self.moves.append([q, self.time, self.time + 2, self.intern(path), self.bits[q]])
        self.pos[q] = dest
        self.time += 2

    def rearrange(self, targets):
        targets = {int(q): tuple(p) for q, p in targets.items()}
        if len(set(targets.values())) != self.n:
            raise LayoutInfeasible("duplicate target data slot")
        pending = {q for q in targets if self.pos[q] != targets[q]}
        limit = 3 * self.n + 1
        while pending and limit:
            limit -= 1
            occupied = set(self.pos.values())
            free = sorted(q for q in pending if targets[q] not in occupied)
            if free:
                self.move(free[0], targets[free[0]])
            else:
                q = min(pending)
                # A fixed, charged scratch tile breaks a permutation cycle.
                scratch = tuple(self.layout["scratch"])
                if scratch in occupied or scratch in targets.values():
                    raise LayoutInfeasible("scratch occupied during permutation cycle")
                self.move(q, scratch)
            pending = {q for q in targets if self.pos[q] != targets[q]}
        if pending:
            raise LayoutInfeasible("data permutation failed to converge")

    def inject(self, channel, events):
        queues = defaultdict(deque)
        for index, q, dagger in events:
            queues[q].append((index, dagger))
        sources = [s for s in self.sites if s["enabled"]]
        if events and not sources:
            raise LayoutInfeasible("T injection without an enabled source")
        occupied = set(self.pos.values())
        base_blocked = self.blocked | occupied
        path_cache = self.path_cache
        topology = tuple(sorted(occupied))
        target_orders = {s["id"]: sorted(queues, key=lambda q: (distance(s["buffer"], self.pos[q]), queues[q][0][0])) for s in sources}
        pending_count = len(events)
        while pending_count:
            earliest = min(self.ready[s["id"]] for s in sources)
            if self.time < earliest:
                self.wait += earliest - self.time
                self.time = earliest
            available = {s["id"] for s in sources if self.ready[s["id"]] <= self.time}
            reserved = 0
            selected_qubits = set()
            batch = []
            # Source-first ordering spreads consumption over all available
            # factories. No global or per-row injection cardinality cap.
            for fid in sorted(available, key=lambda f: (self.ready[f], f)):
                source = tuple(self.sites[fid]["buffer"])
                chosen = None
                for q in target_orders[fid]:
                    if not queues[q] or q in selected_qubits:
                        continue
                    target = self.pos[q]
                    # A J-adjacent ancilla is required by the merge gadget.
                    for goal in ((target[0], target[1] + 1), (target[0], target[1] - 1)):
                        if not (0 <= goal[1] < self.height):
                            continue
                        key = (topology, fid, goal)
                        if key not in path_cache:
                            found = shortest_path(source, goal, base_blocked - {source}, self.width, self.height)
                            path_cache[key] = (found, _path_mask(found,self.width,self.height) if found is not None else 0)
                        path, mask = path_cache[key]
                        path_cache.move_to_end(key)
                        if len(path_cache) > 8192:
                            path_cache.popitem(last=False)
                        if path is None or reserved & mask:
                            continue
                        chosen = (q, path, mask)
                        break
                    if chosen:
                        break
                if chosen:
                    q, path, mask = chosen
                    index, dagger = queues[q][0]
                    batch.append((index, q, dagger, fid, path))
                    reserved |= mask
                    selected_qubits.add(q)
            if not batch:
                future = [self.ready[s["id"]] for s in sources if self.ready[s["id"]] > self.time]
                if future:
                    new_time = min(future)
                    self.wait += new_time - self.time
                    self.time = new_time
                    continue
                raise LayoutInfeasible(f"no source can reach a pending T at channel {channel}")
            self.max_parallel = max(self.max_parallel, len(batch))
            for index, q, dagger, fid, path in batch:
                # Record token production, storage and consumption explicitly.
                self.injections.append([index, q, channel, int(dagger), fid,
                                        self.intern(path), self.time,
                                        self.production_start[fid], self.ready[fid], self.bits[q]])
                self.production_start[fid] = self.time
                self.ready[fid] = self.time + self.period
                queues[q].popleft()
                pending_count -= 1
            self.time += 2

    def run(self, event_groups):
        for layer, cells in enumerate(self.layout["layers"]):
            targets = dict(self.layout["parking"][layer])
            for d in cells:
                x, y = d["origin"]
                for q, ip, _ in d["bindings"]:
                    targets[q] = [x + ip[0], y + ip[1]]
            self.rearrange(targets)
            self.inject(layer, event_groups.get(layer, ()))
            needs_flip = []
            for d in cells:
                for q, ip, _ in d["bindings"]:
                    if self.bits[q] != ip[2]:
                        needs_flip.append(q)
            for q in needs_flip:
                self.flips.append([q, self.time, list(self.pos[q]), self.bits[q]])
                self.bits[q] ^= 1
            if needs_flip:
                self.time += 1
            start = self.time
            for d in cells:
                self.cells.append([d["macro"], layer, start])
                for q, _, op in d["bindings"]:
                    self.pos[q] = (d["origin"][0] + op[0], d["origin"][1] + op[1])
                    self.bits[q] = op[2]
            self.time += max((d["duration"] for d in cells), default=0)
        self.inject(len(self.layout["layers"]), event_groups.get(len(self.layout["layers"]), ()))
        self.layout.update(routes=self.routes, route_encoding="orthogonal_turn_points_v1", moves=self.moves, flips=self.flips,
                           cell_schedule=self.cells, injections=self.injections,
                           runtime_steps=self.time, wait_levels=self.wait,
                           peak_concurrent_injections=self.max_parallel,
                           final=[[q, list(p), self.bits[q]] for q, p in sorted(self.pos.items())])
        return self.layout


def compile_physical(floorplan, layer_cells, events, channels, factory, factories, mode):
    from .program import _elaborate_cells, MacroInstance
    validate_mode(mode)
    if isinstance(factories, bool) or not isinstance(factories, int) or factories < 1:
        raise ValueError("factories must be a positive integer")
    if len(events) != len(channels):
        raise ValueError("events/channels length mismatch")
    if events and factory is None:
        raise ValueError("physical injection modes require a FactorySpec")
    if factory is not None and (min(factory.dim_i, factory.dim_j, factory.interval_k) < 0
                                or (factory.render and min(factory.dim_i, factory.dim_j, factory.interval_k) < 1)):
        raise ValueError("rendered factories need positive dimensions and period; external sources need nonnegative values")
    program = _elaborate_cells(floorplan, layer_cells)
    program.factory_mode = mode
    program.factory = factory if events else None
    program.factories = factories if events else 0
    desc = _descriptors(program, floorplan)
    groups = defaultdict(list)
    for i, (event, channel) in enumerate(zip(events, channels)):
        if not 0 <= channel <= len(layer_cells):
            raise ValueError("injection channel outside the program")
        groups[channel].append((i, event.qubit, event.dagger))
        program.macros.append(MacroInstance("injection", ("gadget", event.dagger), channel, 0, 0, (event.qubit,)))
    layout = _candidate(program, floorplan, desc, mode, 0)
    plan = _Scheduler(layout, program.num_qubits, factory.interval_k if factory else 0).run(groups)
    program.physical_layout = plan
    errors = check(program)
    if errors:
        raise LayoutInfeasible("; ".join(errors[:8]))
    plan["search"] = {"objective": "fixed_placement", "optimality": "not_optimized",
                      "candidate_budget": 1, "evaluated": [{"candidate": 0, "status": "ok",
                      "patch_cell_slices": plan["width"] * plan["height"] * plan["runtime_steps"]}]}
    return program


def check(program):
    """Independently replay occupancy, data trajectories, tokens and gadgets."""
    try:
        return _check(program)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as error:
        return [f"malformed physical IR: {type(error).__name__}: {error}"]


def _check(program):
    if program.factory_mode not in MODES:
        return ["unsupported factory mode; recompile with an explicit physical layout"]
    p = program.physical_layout
    if not isinstance(p, dict) or p.get("schema") != SCHEMA:
        return ["missing or unsupported physical layout"]
    errors = []
    def require(ok, message):
        if not ok:
            errors.append(message)
    w, h = p["width"], p["height"]
    require(isinstance(w, int) and isinstance(h, int) and w > 0 and h > 0, "invalid chip dimensions")
    require(isinstance(p["runtime_steps"], int) and p["runtime_steps"] >= 0, "invalid runtime")
    if p["injections"] and program.factory is None:
        return ["injections without a factory specification"]
    sites = p["sites"]
    fixed = set()
    for fid, s in enumerate(sites):
        area = rectangle(s["rect"])
        buffer = tuple(s["buffer"])
        require(s["id"] == fid, "factory IDs must be contiguous")
        require(s["rect"][2:] == [max(1, program.factory.dim_i), max(1, program.factory.dim_j)], "factory footprint differs from specification")
        require(not fixed.intersection(area | {buffer}), "factory/buffer overlap")
        require(buffer not in area and distance(buffer, s["port"]) == 1 and tuple(s["port"]) in area, "disconnected factory output")
        require(all(0 <= x < w and 0 <= y < h for x, y in area | {buffer}), "factory outside chip")
        fixed.update(area | {buffer})
    require(len(sites) == program.factories, "factory budget mismatch")
    require(p["mode"] == program.factory_mode, "mode mismatch")
    enabled = sum(s["enabled"] for s in sites)
    require(enabled == (min(2, len(sites)) if program.factory_mode == "in_place" else len(sites)), "incorrect enabled factory count")
    descriptors = {d["macro"]: d for layer in p["layers"] for d in layer}
    expected_cells = {i for i, m in enumerate(program.macros) if m.kind == "cell"}
    require(set(descriptors) == expected_cells, "missing or unexpected cell descriptor")
    scheduled_cells = [m for m, _, _ in p["cell_schedule"]]
    require(set(scheduled_cells) == expected_cells and len(scheduled_cells) == len(expected_cells), "missing or duplicate scheduled Clifford cell")
    for layer, cells in enumerate(p["layers"]):
        occupied = set(fixed)
        for d in cells:
            macro = program.macros[d["macro"]]
            require(macro.kind == "cell" and macro.layer == layer, "cell identity/layer changed")
            require(d["size"] == [(v + 1) // 2 for v in macro.ref.dims[:2]], "cell size changed")
            area = rectangle([*d["origin"], *d["size"]])
            require(not area.intersection(occupied), "cell/factory/cell overlap")
            require(all(0 <= x < w and 0 <= y < h for x, y in area), "cell outside chip")
            occupied.update(area)
            require(set(b[0] for b in d["bindings"]) == set(macro.qubits), "cell qubit binding mismatch")
            for q, ip, op in d["bindings"]:
                for prefix, pin in (("in", ip), ("out", op)):
                    require(any(v["name"].startswith(prefix) and v["offset"][:2] == [2 * pin[0], 2 * pin[1]]
                                and max(0, int(v.get("parity", 0))) == pin[2] for v in macro.ref.pins), "invalid cell pin binding")
        for q, xy in p["parking"][layer].items():
            require(tuple(xy) not in occupied, "parked data overlaps occupied tile")
            require(0 <= xy[0] < w and 0 <= xy[1] < h, "parked data outside chip")
            occupied.add(tuple(xy))
    require(p.get("route_encoding") == "orthogonal_turn_points_v1", "unsupported route encoding")
    routes = [_expand_route(path) for path in p["routes"]]
    for route in routes:
        require(bool(route), "empty route")
        require(all(distance(a, b) == 1 for a, b in zip(route, route[1:])), "nonlocal/disconnected route")
        require(len(set(route)) == len(route), "route contains a cycle")
        require(all(0 <= x < w and 0 <= y < h for x, y in route), "route outside chip")
    position = {q: tuple(xy) for q, xy in p["initial"]}
    bits = {q: 0 for q in position}
    require(len(position) == program.num_qubits and len(set(position.values())) == len(position), "invalid initial data placement")
    require(not fixed.intersection(position.values()), "initial data overlaps factory/buffer")
    require(all(0 <= x < w and 0 <= y < h for x, y in position.values()), "initial data outside chip")
    sx, sy = p["scratch"]
    require(0 <= sx < w and 0 <= sy < h and (sx,sy) not in fixed, "invalid scratch tile")
    # End events precede start events at the same instant.
    timeline = []
    for m in p["moves"]:
        timeline.extend([(m[1], 1, "move", m), (m[2], 0, "move_end", m)])
    for f in p["flips"]:
        timeline.extend([(f[1], 1, "flip", f), (f[1] + 1, 0, "flip_end", f)])
    for mid, layer, start in p["cell_schedule"]:
        d = descriptors[mid]
        if d["duration"] == 0:
            timeline.append((start, 0.5, "zero_cell", (d, start)))
        else:
            timeline.extend([(start, 2, "cell", (d, start)), (start + d["duration"], 0, "cell_end", (d, start))])
    for inj in p["injections"]:
        timeline.append((inj[6], 1, "injection", inj))
        timeline.append((inj[6] + 2, 0, "injection_end", inj))
    timeline.sort(key=lambda item: item[:2])
    busy = {}
    token_ready = [program.factory.interval_k if program.factory else 0] * len(sites)
    production_start = [0] * len(sites)
    by_channel = defaultdict(dict)
    for i, m in enumerate(m for m in program.macros if m.kind == "injection"):
        by_channel[m.layer][i] = (m.qubits[0], bool(m.ref[1]))
    layer_starts, layer_ends = {}, {}
    for mid, layer, start in p["cell_schedule"]:
        layer_starts[layer] = min(layer_starts.get(layer, start), start)
        layer_ends[layer] = max(layer_ends.get(layer, 0), start + descriptors[mid]["duration"])
    for layer in sorted(layer_starts):
        require(layer_starts[layer] >= layer_ends.get(layer - 1, 0), "overlapping logical Clifford layers")
    seen = set()
    last_q = {}
    batch_time, batch_tiles, batch_factories = None, set(), set()
    for t, _, kind, item in timeline:
        require(t <= p["runtime_steps"], "event exceeds runtime")
        if kind == "move":
            q, start, end, rid, parity = item
            path = routes[rid]
            require(not busy and end == start + 2, "invalid data move timing")
            require(position[q] == path[0] and bits[q] == parity, "data route start/parity mismatch")
            require(not (fixed | (set(position.values()) - {position[q]})).intersection(path), "data route crosses obstacle")
            busy[q] = end
        elif kind == "move_end":
            q, _, _, rid, _ = item
            position[q] = routes[rid][-1]
            busy.pop(q, None)
        elif kind == "flip":
            q, _, xy, parity = item
            require(q not in busy and position[q] == tuple(xy) and bits[q] == parity, "invalid Hadamard landing")
            busy[q] = t + 1
        elif kind == "flip_end":
            bits[item[0]] ^= 1
            busy.pop(item[0], None)
        elif kind in ("cell", "zero_cell"):
            d, start = item
            x, y = d["origin"]
            for q, ip, _ in d["bindings"]:
                require(q not in busy and position[q] == (x + ip[0], y + ip[1]) and bits[q] == ip[2], "cell input continuity/parity violation")
                if d["duration"]:
                    busy[q] = start + d["duration"]
            if kind == "zero_cell":
                for q, _, op in d["bindings"]:
                    position[q] = (x + op[0], y + op[1])
                    bits[q] = op[2]
        elif kind == "cell_end":
            d, _ = item
            x, y = d["origin"]
            for q, _, op in d["bindings"]:
                position[q] = (x + op[0], y + op[1])
                bits[q] = op[2]
                busy.pop(q, None)
        elif kind == "injection_end":
            busy.pop(item[1], None)
        elif kind == "injection":
            index, q, channel, dagger, fid, rid, start, produced, ready, parity = item
            path = routes[rid]
            require(index not in seen and by_channel[channel].get(index) == (q, bool(dagger)), "injection identity/channel changed")
            require(q not in busy and bits[q] == parity, "injection targets busy data or wrong parity")
            require(start >= layer_ends.get(channel - 1, 0) and start + 2 <= layer_starts.get(channel, p["runtime_steps"]), "injection crosses its Clifford dependency boundary")
            busy[q] = start + 2
            require(index > last_q.get(q, -1), "per-qubit T order changed")
            require(sites[fid]["enabled"] and ready == token_ready[fid] and produced == production_start[fid] and ready <= start, "invalid factory token production/consumption")
            require(path[0] == tuple(sites[fid]["buffer"]), "injection source disconnected")
            require(path[-1][0] == position[q][0] and abs(path[-1][1] - position[q][1]) == 1, "missing J merge ancilla")
            require(not ((fixed - {path[0]}) | set(position.values())).intersection(path), "injection route crosses obstacle")
            if batch_time != start:
                batch_time, batch_tiles, batch_factories = start, set(), set()
            require(fid not in batch_factories and not batch_tiles.intersection(path), "simultaneous injection conflict")
            batch_factories.add(fid)
            batch_tiles.update(path)
            batch_tiles.add(position[q])
            seen.add(index)
            last_q[q] = index
            production_start[fid] = start
            token_ready[fid] = start + program.factory.interval_k
    actual_peak = max(Counter(i[6] for i in p["injections"]).values(), default=0)
    require(actual_peak == p["peak_concurrent_injections"], "incorrect peak parallelism")
    require(not busy, "unfinished physical operation")
    require(len(seen) == sum(m.kind == "injection" for m in program.macros), "missing T injection")
    require(all(position[q] == tuple(xy) and bits[q] == bit for q, xy, bit in p["final"]), "final data state mismatch")
    return errors


def statistics(program):
    errors = check(program)
    if errors:
        raise RuntimeError("physical layout validation: " + "; ".join(errors[:8]))
    p = program.physical_layout
    area, duration = p["width"] * p["height"], p["runtime_steps"]
    total = area * duration
    used = {i[4] for i in p["injections"]}
    factory = program.factory
    from .factory import correction_for
    return {"injection_corrections": {str(d): {f"{a}{b}": correction_for((a,b), bool(d)) for a in (0,1) for b in (0,1)} for d in (0,1)},"factory_mode": program.factory_mode, "physical_schema": SCHEMA,
            "geometry_validated": True, "resource_model": "allocated_chip_rectangle_v1",
            "factory": ({"name": factory.name, "dim_i": factory.dim_i, "dim_j": factory.dim_j,
                         "interval_k": factory.interval_k} if factory else None),
            "reserved_factory_count": program.factories,
            "enabled_factory_count": sum(s["enabled"] for s in p["sites"]),
            "used_factory_count": len(used), "buffer_count": len(p["sites"]),
            "peak_concurrent_injections": p["peak_concurrent_injections"],
            "backside_injection_count": len(p["injections"]) if program.factory_mode in ("backside_vertical", "backside_horizontal") else 0,
            "in_place_injection_count": len(p["injections"]) if program.factory_mode == "in_place" else 0,
            "t_count": len(p["injections"]), "wait_levels": p["wait_levels"],
            "runtime_steps": duration, "allocated_area_patches": area,
            "patch_cell_slices": total, "physical_qubit_seconds": total * 450 * 15e-6,
            "runtime_seconds": duration * 15e-6, "physical_qubits": area * 450,
            "volume": total * 8,
            "bbox": [p["width"] * 2, p["height"] * 2, duration * 2], "cube_envelope": total,
            "route_stats": {"nets_unrouted": 0, "feedthroughs_unrouted": 0,
                            "ports_total": 2 * program.num_qubits, "surface_pins_routed": 2 * program.num_qubits},
            "pauli_frames": {f"region_{d['macro']}": d["pauli_frame"] for layer in p["layers"] for d in layer},
            "search": p["search"], "route_count": len(p["routes"]),
            "data_move_count": len(p["moves"]), "geometry_model": "upright_cells_planar_paths_two_step_injection"}


def _last_production(program):
    starts = [0] * program.factories
    for inj in program.physical_layout["injections"]:
        starts[inj[4]] = inj[6]
    return starts


def production_boxes(program):
    p = program.physical_layout
    if not program.factory or not program.factory.render:
        return
    for inj in p["injections"]:
        x, y, w, h = p["sites"][inj[4]]["rect"]
        yield {"lo": [2*x, 2*y, 2*inj[7]], "hi": [2*(x+w), 2*(y+h), 2*inj[8]]}
    # Include unconsumed outputs and production still in flight at shutdown.
    for site, start in zip(p["sites"], _last_production(program)):
        end = min(start + program.factory.interval_k, p["runtime_steps"])
        if site["enabled"] and start < end:
            x, y, w, h = site["rect"]
            yield {"lo": [2*x, 2*y, 2*start], "hi": [2*(x+w), 2*(y+h), 2*end]}


def pipes_of(program):
    """Render the checked stored schedule; never run the planner here."""
    from .lower import _make_pipe, _Emitter
    errors = check(program)
    if errors:
        raise RuntimeError("; ".join(errors[:8]))
    p = program.physical_layout
    routes = p["routes"]
    def run(a, b, parity=0, **flags):
        axis = next((k for k in range(3) if a[k] != b[k]), None)
        if axis is None:
            return
        low, high = sorted((a[axis], b[axis]))
        for v in range(low, high, 2):
            lo, hi = list(a), list(a)
            lo[axis], hi[axis] = v, v+2
            yield _make_pipe(lo, hi, "IJK"[axis], parity, **flags)
    def planar(path, z, parity, **flags):
        for a, b in zip(path, path[1:]):
            yield from run([2*a[0], 2*a[1], 2*z], [2*b[0], 2*b[1], 2*z],
                           parity if flags.get("t_volume") or a[1] == b[1] else 1-parity, **flags)
    trajectories = defaultdict(list)
    descriptors = {d["macro"]: d for layer in p["layers"] for d in layer}
    for mid, _, start in p["cell_schedule"]:
        d = descriptors[mid]
        x, y = d["origin"]
        cell = program.macros[mid].ref
        yield from _Emitter.cell_pipes(None, cell.blocks, 2*start, 2*x, 2*y)
        for q, ip, op in d["bindings"]:
            trajectories[q].append((start, start+d["duration"], (x+ip[0], y+ip[1]), (x+op[0], y+op[1]), op[2]))
    for q, start, end, rid, parity in p["moves"]:
        path = routes[rid]
        yield from run([2*path[0][0], 2*path[0][1], 2*start], [2*path[0][0], 2*path[0][1], 2*(start+1)], parity)
        yield from planar(path, start+1, parity)
        yield from run([2*path[-1][0], 2*path[-1][1], 2*(start+1)], [2*path[-1][0], 2*path[-1][1], 2*end], parity)
        trajectories[q].append((start, end, tuple(path[0]), tuple(path[-1]), parity))
    for q, start, xy, parity in p["flips"]:
        yield from run([2*xy[0], 2*xy[1], 2*start], [2*xy[0], 2*xy[1], 2*(start+1)], parity, hadamard=True)
        trajectories[q].append((start, start+1, tuple(xy), tuple(xy), 1-parity))
    initial = dict(p["initial"])
    for q in range(program.num_qubits):
        cursor, xy, bit = 0, initial[q], 0
        for start, end, src, dst, out_bit in sorted(trajectories[q]):
            yield from run([2*xy[0], 2*xy[1], 2*cursor], [2*xy[0], 2*xy[1], 2*start], bit)
            cursor, xy, bit = end, dst, out_bit
        yield from run([2*xy[0], 2*xy[1], 2*cursor], [2*xy[0], 2*xy[1], 2*p["runtime_steps"]], bit)
    # Positions at injection are recoverable from the route's merge ancilla
    # and independently replayed data trajectories.
    data_pos = {q: tuple(xy) for q, xy in p["initial"]}
    changes = []
    for q, items in trajectories.items():
        changes.extend((end, q, dst) for _, end, _, dst, _ in items)
    changes.sort()
    cursor = 0
    for inj in sorted(p["injections"], key=lambda i: (i[6], i[0])):
        index, q, _, _, fid, rid, start, _, ready, parity = inj
        while cursor < len(changes) and changes[cursor][0] <= start:
            _, cq, xy = changes[cursor]
            data_pos[cq] = xy
            cursor += 1
        site, path = p["sites"][fid], routes[rid]
        port, buffer, target = site["port"], site["buffer"], data_pos[q]
        flags = {"t_volume": True, "inj": index}
        yield from planar([port, buffer], ready, 0, **flags)
        yield from run([2*buffer[0], 2*buffer[1], 2*ready], [2*buffer[0], 2*buffer[1], 2*start], 0, **flags)
        yield from planar(path, start, 0, **flags)
        anc = path[-1]
        yield from run([2*anc[0], 2*anc[1], 2*start], [2*anc[0], 2*anc[1], 2*(start+1)], 0, **flags)
        yield from run([2*anc[0], 2*anc[1], 2*(start+1)], [2*target[0], 2*target[1], 2*(start+1)], 1-parity, **flags)

    if program.factory:
        for site, start in zip(p["sites"], _last_production(program)):
            ready = start + program.factory.interval_k
            if site["enabled"] and ready <= p["runtime_steps"]:
                port, buffer = site["port"], site["buffer"]
                yield from planar([port, buffer], ready, 0, t_volume=True)
                yield from run([2*buffer[0], 2*buffer[1], 2*ready],
                               [2*buffer[0], 2*buffer[1], 2*p["runtime_steps"]], 0, t_volume=True)
