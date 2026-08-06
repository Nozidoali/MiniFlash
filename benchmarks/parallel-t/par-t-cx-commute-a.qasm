OPENQASM 2.0;
include "qelib1.inc";

qreg q[8];

t q[0];
cx q[0], q[1];
t q[2];
cx q[2], q[3];
t q[4];
cx q[4], q[5];
t q[6];
cx q[6], q[7];
