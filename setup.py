from setuptools import setup

setup(
    name="miniflash",
    version="1.0.0a0",
    description="QASM -> lattice-surgery tile program compiler with a glTF renderer",
    license="MIT",
    packages=["miniflash", "miniflash.solver"],
    python_requires=">=3.9",
    install_requires=["qiskit", "numpy>=1.20"],
    extras_require={"test": ["pytest>=7.0"]},
)
