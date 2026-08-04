OPENQASM 2.0;
include "qelib1.inc";
qreg q[12];

ccx q[0],q[2],q[4];
ccx q[1],q[3],q[5];
cx q[0],q[6];
cx q[1],q[6];
cx q[2],q[7];
cx q[3],q[7];
ccx q[6],q[7],q[8];
cx q[4],q[9];
cx q[5],q[11];
cx q[8],q[10];
cx q[4],q[10];
cx q[5],q[10];
cx q[11],q[9];
cx q[11],q[10];
