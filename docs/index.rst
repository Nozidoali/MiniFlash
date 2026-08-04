miniflash
=========

A pure-Python compiler from Clifford+T OpenQASM 2.0 circuits to
lattice-surgery layouts (glTF 2.0 scenes), built on **stack-and-swap**:
routing is decided symbolically *before* synthesis, and each cell is
synthesized by LaSsynth SAT to match its pre-assigned port geometry —
the wire never adapts to the cell; the cell adapts to the wire.

The ``miniflash`` package compiles to the macro/channel **Program IR**
(parse -> partition -> schedule -> floorplan -> synthesis -> program)
and carries its own backend (``gltf``: check -> lower -> glTF); the
repo-root ``main.py`` is the driver + CLI. Cell synthesis runs on
LaSsynth, fetched by ``scripts/download_lassynth.py``.

Quickstart
----------

CLI::

    python main.py circuit.qasm -o layout.gltf

Library::

    import miniflash as flash

    circuit = flash.parse("circuit.qasm")
    pc = flash.partition(circuit)          # PartitionedCircuit: .regions / .events / .to_text() / .save_png()
    floorplan, cells, channels = flash.synthesize(pc)
    program = flash.elaborate(floorplan, cells, events=pc.events, channels=channels)

    layout = flash.build_layout(program)       # check -> lower
    flash.write_gltf(layout, "layout.gltf")

.. toctree::
   :maxdepth: 2

   api
   backend
