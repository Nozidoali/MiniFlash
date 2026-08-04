// SynthesizeLR(4): worst-case linear-reversible CNOT circuit (LU, all pairs)
// Source: arXiv:2510.10967 (DQI) Fig 2 SynthesizeLR primitive
// Hand-compiled lattice-surgery volume: 3*(2*4-1) x (4-2) = 42 patch-timesteps
OPENQASM 2.0;
include "qelib1.inc";
qreg q[4];
cx q[0],q[1];
cx q[0],q[2];
cx q[1],q[2];
cx q[0],q[3];
cx q[1],q[3];
cx q[2],q[3];
cx q[3],q[2];
cx q[2],q[1];
cx q[3],q[1];
cx q[1],q[0];
cx q[2],q[0];
cx q[3],q[0];
