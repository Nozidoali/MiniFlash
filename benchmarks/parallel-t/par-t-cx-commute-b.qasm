OPENQASM 2.0;
include "qelib1.inc";

qreg q[4];

cx q[0], q[1];
t q[0];
t q[2];
t q[3];
