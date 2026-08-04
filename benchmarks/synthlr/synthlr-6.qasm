// SynthesizeLR(6): worst-case linear-reversible CNOT circuit (LU, all pairs)
// Source: arXiv:2510.10967 (DQI) Fig 2 SynthesizeLR primitive
// Hand-compiled lattice-surgery volume: 3*(2*6-1) x (6-2) = 132 patch-timesteps
OPENQASM 2.0;
include "qelib1.inc";
qreg q[6];
cx q[0],q[1];
cx q[0],q[2];
cx q[1],q[2];
cx q[0],q[3];
cx q[1],q[3];
cx q[2],q[3];
cx q[0],q[4];
cx q[1],q[4];
cx q[2],q[4];
cx q[3],q[4];
cx q[0],q[5];
cx q[1],q[5];
cx q[2],q[5];
cx q[3],q[5];
cx q[4],q[5];
cx q[5],q[4];
cx q[4],q[3];
cx q[5],q[3];
cx q[3],q[2];
cx q[4],q[2];
cx q[5],q[2];
cx q[2],q[1];
cx q[3],q[1];
cx q[4],q[1];
cx q[5],q[1];
cx q[1],q[0];
cx q[2],q[0];
cx q[3],q[0];
cx q[4],q[0];
cx q[5],q[0];
