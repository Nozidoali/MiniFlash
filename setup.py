from setuptools import setup

setup(
    name="miniflash",
    version="0.0.1",
    description="QASM -> glTF lattice-surgery layout compiler",
    license="MIT",
    packages=["miniflash"],
    python_requires=">=3.9",
    install_requires=[
        "qiskit",
        "stim",
        "z3-solver>=4.12.1",
        "networkx",
        "stimzx @ git+https://github.com/quantumlib/Stim.git@0fdddef863cfe777f3f2086a092ba99785725c07#subdirectory=glue/zx",
    ],
)
