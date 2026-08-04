import os
import sys

sys.path.insert(0, os.path.abspath(".."))

project = "miniflash"
author = "hanyu"
release = "0.0.1"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

# Heavy runtime deps are mocked so the docs build needs no qiskit/stim wheel.
autodoc_mock_imports = ["qiskit", "stim", "z3", "matplotlib"]
autodoc_member_order = "bysource"
autodoc_default_options = {"members": True, "show-inheritance": True}

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

html_theme = "furo"
html_title = "miniflash"
html_logo = "logo.svg"
