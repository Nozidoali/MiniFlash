# feat: orientation — lying-down cells (90° rotation in the I–K plane)

Status: brainstorm / design notes. Nothing implemented yet.

## Idea

Today every synthesized cell stands "upright": qubits are arrayed along I,
J is the thin slab direction, and the SAT depth runs along K — which is the
global time axis. A layer's K thickness is therefore `max(cell depth)` over
the layer, so one deep, narrow cell (depth up to 16 → 31 units after STRIDE,
but only 2–4 qubits → 7–11 units wide) stretches the whole layer in time.

Proposal: after synthesis, rotate such a cell 90° in the I–K plane so it
lies down — its depth becomes I extent, its qubit width becomes K extent —
then reconnect each pin to the vertical qubit wires with an L-shaped elbow
(one I run + one K run). Pure post-processing: no new SAT calls, cached
solutions are reused as-is.

Example: a 4-qubit cell at depth 12 costs K=23 upright; lying down it costs
K≈11 + ~4 for elbows, at the price of I≈23. For deep 2-qubit cells the win
is larger. This is a time↔space trade under the die-width cap.

## Why it is sound

The spacetime diagram of a pipe/cube defect network is topological; slicing
it along a different axis is still a valid lattice-surgery execution. The
one genuinely time-directional object is the Y cube; hadamard walls survive
rotation too (a spatial domain wall is realizable with one XZ merge/split),
they just need backend support (below). The existing
side-port machinery (`_build_spec` `-I` ports, `emit_side_exit/entry`) is
the degenerate cousin of this idea — it asks the SAT solver to place pins
on the side face, which makes solving harder; rotation gets side pins for
free, geometrically.

## Constraints

1. **Y nodes (hard exclusion).** Y cubes are time-directional (Y-basis
   init/measure at a temporal boundary). → `is_spacetime_symmetric` must reject cells
   with ynode blocks.
2. **Hadamard segments rotate too (implemented).** A rotated K-hadamard
   becomes an I-axis (spatial) domain wall, which IS implementable — one
   XZ merge/split realizes it, and it stays a plain pipe with no extra
   cube volume, so the rotate decision does not need to price it
   separately. This turned out to be load-bearing, not polish: empirically
   every small LaSsynth cell carries hadamard segments (a bare CX cell has
   4), so a hadamard exclusion would make rotation never fire. The K-only
   spots in the codebase (`_hadamard_faces_from_cube`, `_resolve_pins`)
   run at `from_lasre` time, before rotation, and `emit_cell_pipes` emits
   hadamard blocks axis-generically — so `rotate_cell` handles them with
   one extra rule: on the axis the rotation sense reverses, the wall's
   ends swap and its own color flip cancels the convention parity flip.
   (Cells with S gates carry ynodes and stay excluded by rule 1.)
3. **Parity flips.** Blocks store `parity`, derived from transverse face
   colors with an axis-dependent "first transverse face" convention
   (`_parity_from_faces`). Under I↔K swap the physical face colors ride
   along with the block, but the convention reads the complementary face
   for all three axes, so the bit flips uniformly: `parity → 1 - parity`.
   Verified against `_pipe_colors`/`_block_faces` for every axis and both
   parities (`test_parity_flip_matches_face_conventions`). Note the wire
   emitters (`emit_move_chain`) keep the bit across I↔K bends and flip on
   J-hops — a different, wire-land convention; the elbow generator must
   follow wire-land rules outside the cell and the flipped cell parity at
   the pin face.
4. **Pin elbows must not cross.** After rotation all in-pins sit on one ±I
   face at distinct K slots, all out-pins on the opposite face. Connecting
   N side pins to N columns on the K boundary with single-bend Ls is
   crossing-free iff the k-order matches the column order (nested Ls).
   The floorplan controls column assignment, so it can enforce
   monotonicity; fallbacks are a J-jog (second wire plane, as grid moves
   already do) or Z-paths.

## Integration sketch

- `miniflash/orientation.py` (**done**): `is_spacetime_symmetric(cell)` (ynode is the
  only blocker), `rotate_cell(cell, sense=±1)` → new `Cell` with dims
  `[dk, dj, di]`, positions `(i,j,k) → (k, j, di-1-i)` for `sense=1`
  (proper rotation, not a mirror), axes I↔K, parities recomputed
  (hadamards end-aware), pin ends recomputed on the ±I faces. Unit tests
  in `tests/test_orientation.py`; verified against a real z3-synthesized
  CX cell (round-trip, bounds, grid structure, gltf emission).
- `_fuse_chain([cell])` (**done**, private since the API shrink): rotate + elbows appended as plain pipe
  blocks, so `emit_cell_pipes` and the wire hookup need no changes. In-pins
  reclaim their upright columns (K-levels decrease with column, so the
  nested Ls cannot cross and the floorplan's in-port contract survives);
  out-pins exit on fresh columns right of the body
  (`max_in_col + depth + local_col`). Elbow corners flip the parity bit
  (face-convention continuity), cancelling the rotation flip — a laid-down
  cell reports the same pin parities as its upright original, so the wire
  ledger, `in_bases` threading and the synthesis cache are untouched.
- Per-layer surgery (**subsumed**): the original `maybe_lay_down_layer`
  (per-layer decision + in-place floorplan rewrite) has been deleted —
  `apply_orientation` handles the single-layer case as a length-1 chain
  with the identical rule set (single box, spacetime-symmetric K-pin cell,
  no parked lanes or side moves, clear of the magic column, strict depth
  win `depth > n+2`; rewrite `box_widths`/`out_port_columns`, re-solve the
  downstream channel via `solver1d.solve_channel`).
- Empirical note: LaSsynth keeps small-n cells at depth 2-3, so the win
  condition rarely fires there — deep cells come from large coarse-portfolio
  regions (8-16 qubits). Benchmarking with kissat on real circuits is the
  natural next step.
- Second empirical blocker: LaSsynth uses Y cubes freely as geometric
  terminators (a bare h/cx cell came back with 4 of them), so nearly every
  solution failed `is_spacetime_symmetric` regardless of gates. Fix: a miniflash-side
  hook on the vendored solver (`constraint_forbid_cube` monkeypatch in
  synthesis.py) honors a new `optional.forbid_y_cubes` spec key that adds
  `¬NodeY` for every cube. `synthesize(orientation=True)` pre-computes the
  structurally chainable layers (`plan_chain_layers`) and synthesizes them
  Y-free (own cache key, graceful fallback to a normal solve on
  UNSAT/timeout), so chains actually rotate.
- (superseded) Elbow emission: either append adapter blocks to the rotated `Cell` so
  `emit_cell_pipes` needs no changes, or emit them in `gltf.build_layout`
  next to the existing side-run emitters. The wire hookup currently assumes
  out-pins at `k = depth`; elbows restore that contract at the cell
  boundary, so downstream (nets, parity ledger, injections) is untouched.
- Decision pass: in `synthesize()`, after cells are built, per layer:
  if every cell is rotatable and
  `rotated K footprint + elbow overhead < upright depth` and the lying
  footprints fit the die width → rotate the layer. v1 can even restrict to
  single-cell layers, which is where deep cells live anyway.
- Stats: `Program.stats()` volume/depth numbers pick the change up for
  free; add an `orientation` tag per cell macro for debugging.

## Hybrid orientation (multi-box layers)

A layer's K thickness is `max(cell depth)`, so in a multi-box layer one
deep cell taxes every column: wasted volume ≈ layer width ×
(d_max − d_second). Laying down just the depth tail thins the whole
layer — e.g. A (n=2, d=10) beside B (n=3, d=3): upright K=19, A lying
K=max(7,5)=7, at the cost of A widening from 4 to 16 columns. The
per-cell win condition generalizes from `d > n+2` to
`d > max(remaining layer depth, n+2)`; laying shallow cells down is never
useful. Width only costs through the global bbox max, so greedy per-layer
decisions with a global width check are near-optimal.

Implementation is an extension of the v1 surgery: subset choice per layer
(depth-descending, marginal condition), box reflow (laid boxes widen →
offsets shift → in AND out port columns move → re-solve both adjacent
channels; cell-local columns are unchanged, so no re-synthesis and the
cache is untouched), and a parked-lane guard — lanes can sit between
boxes, so either skip when the growth span hits one, or relocate the lane
at the cost of a few extra jogs. Longer term the scheduler could
co-schedule deep cells with shallow ones deliberately, since a laid-down
deep cell shares a thin layer for free.

## Chain fusion (stacking across layers, user proposal 2026-08-05) — IMPLEMENTED

Implementation: `_fuse_chain(cells)` builds the fused Cell (rotated bodies
butted left to right, one connector pipe per qubit at each seam — a
hadamard wall when that qubit's wire bit is 1, i.e. the rotated flip
landing — elbows only at the chain ends; a single-cell chain is the length-1
case). `apply_orientation(floorplan, cell_types, channels)` is the
post-synthesis pass wired into `synthesize(orientation=True)`: it groups
maximal chains (single-box layers, rotatable K-pin cells, no lanes/side
moves, matching port columns and offsets at every seam, no injection at
interior boundaries, width under the magic column), fuses winners
(lying K < Σ dims_k + interior gaps; singletons reduce to `depth > n+2`),
rebuilds the floorplan without the swallowed layers, re-solves the
boundary after each fused group and remaps the surviving injection
channels. Fused `pauli_frame`s are reported `|`-joined (frames do not
compose without conjugation).

First e2e (kissat, 12-gate h/cx 2-qubit circuit forced into 3 regions):
3 layers → 1, bbox K 19 → 7 (2.7× faster execution), occupied volume
210 → 220 (elbows + seams only — material is conserved). Bbox volume
456 → 728 though: the lone thin band is wide (6 → 26), and nothing else
amortizes the width on a 3-layer toy. K is the honest win; bbox volume
needs the boustrophedon fold (or a width-budget criterion) to also win —
that is the next knob.

Sequential cells on the same qubits (different layers) can share ONE thin
K band once laid down: A's +I out face butts against B's -I in face, so
the qubit worldlines flow horizontally through the chain. Validity is
compose-then-rotate: `chain(rotate A, rotate B) ≡ rotate(A∘B)` — the same
topological diagram as the vertical stack. Matching k slots join with
short I runs; permutation mismatches route through a seam jog (second J
plane, the channel-jog trick).

This collapses the win condition from per-cell `d > n+2` to chain-level
`Σd_i + gaps > n+2 + seam overhead` — shallow-cell chains win too, which
directly answers the empirical problem above (LaSsynth keeps every small
cell at depth 2-3, so per-cell rotation never fires; chains are
everywhere). Elbow overhead amortizes to the chain's two ends; the
inter-layer channel gaps disappear.

Chain breakers: ynode cells (stay upright), T injections (physically
sound breakers — Clifford segments compress freely per the constant-depth
Clifford space-time tradeoff, but T consumption in a thin K band is
bounded by factory rate, and a seam injection needs new gadget geometry),
and the width budget. Overwide chains fold boustrophedon-style: each fold
row costs ~n+2 in K, total K ≈ ⌈Σd/W⌉ × (n+2 + turn) — a continuous
area-time dial.

## v2 direction (bigger prize)

Synthesize every region against *canonical* ports (drop `in_cols`/
`out_cols` from the cache key), then let the floorplan pick orientation and
placement, with elbows absorbing all port mismatch. Today the cache misses
whenever placement shifts a column; canonical-port synthesis collapses each
region to one cache entry and turns orientation into a pure floorplan
degree of freedom. Mixed-orientation packing inside a layer slab then
becomes 2-D rectangle packing in (I, K).

## Open questions

- Rotation sense: +90° vs −90° decides whether in-pins land on the −I or
  +I face; pick per cell based on which side the wires/magic lane are on?
- Decision granularity: whole-layer v1 vs per-cell packing v2.
- Is a J rotation ever useful? (J is the thin/row direction; probably not.)
- Can ynode cells be salvaged by re-synthesizing without Y cubes (extra
  volume) when rotation would win big? S-gate cells are common.
- Elbow parity bookkeeping: elbows are plain pipes, so pin parity threading
  through `pin_parities` must account for the extra bends' face colors.
