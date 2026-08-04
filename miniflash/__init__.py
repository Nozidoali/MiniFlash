"""miniflash: compile Clifford+T OpenQASM 2.0 into a lattice-surgery glTF layout.

The pipeline runs to the Program IR (parse -> partition -> schedule ->
floorplan -> synthesis -> program); the IR is the internal contract to the
backend half of the package, ``gltf.py``: ``build_layout`` validates and
lowers a Program to the layout dict, ``write_gltf`` writes the file. The repo-root
``main.py`` is the driver + CLI, ``scripts/`` holds the optional
``download_cache.py`` utility.

The public API mirrors the pipeline stages::

    import miniflash as flash

    circuit = flash.parse("circuit.qasm")
    pc = flash.partition(circuit)   # PartitionedCircuit: .regions / .events / .to_text() / .save_png()
    floorplan, cells, channels = flash.synthesize(pc)
    program = flash.elaborate(floorplan, cells, events=pc.events, channels=channels)
    layout = flash.build_layout(program)   # check -> lower
    flash.write_gltf(layout, "layout.gltf")

Every name below is importable both here and from its home module
(``flash.parse`` is ``miniflash.parse.parse``); the home modules stay the
reference for docstrings and internals.
"""

from .factory import FACTORIES, FactorySpec, correction_for, get_factory
from .gltf import build_layout, write_gltf
from .parse import parse
from .partition import partition, split_region
from .program import Program, elaborate
from .synthesis import SynthTimeout, SynthUnsat, VerifyFailed, synthesize

__version__ = "0.0.1"

__all__ = [
    "FACTORIES",
    "FactorySpec",
    "Program",
    "SynthTimeout",
    "SynthUnsat",
    "VerifyFailed",
    "build_layout",
    "correction_for",
    "elaborate",
    "get_factory",
    "parse",
    "partition",
    "split_region",
    "synthesize",
    "write_gltf",
]
