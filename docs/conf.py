import os
import sys

sys.path.insert(0, os.path.abspath(".."))

project = "MiniFlash"
author = "hanyu"
release = "0.0.1"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_design",
    "sphinx_copybutton",
]

# Heavy runtime deps are mocked so the docs build needs no qiskit/stim wheel.
autodoc_mock_imports = ["qiskit", "stim", "z3", "matplotlib", "lassynth", "stimzx", "networkx"]
autodoc_member_order = "bysource"
autodoc_default_options = {"members": True, "show-inheritance": True}

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

copybutton_prompt_text = r">>> |\.\.\. |\$ "
copybutton_prompt_is_regexp = True

html_theme = "furo"
html_title = "MiniFlash"
html_logo = "logo.svg"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_theme_options = {
    "light_css_variables": {
        "color-brand-primary": "#2626d9",
        "color-brand-content": "#2626d9",
    },
    "dark_css_variables": {
        "color-brand-primary": "#8a9df0",
        "color-brand-content": "#8a9df0",
    },
}
