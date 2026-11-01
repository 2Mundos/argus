// Node preload (--require) from argus-cpu-count.sh: trims os.cpus() to the CPU
// quota in ARGUS_CPU_COUNT (docs/specs/agent-resource-hygiene.spec.md 3.2).
// Does nothing when the variable is not a positive integer. Prints nothing.
"use strict";
const os = require("node:os");

const n = Number.parseInt(process.env.ARGUS_CPU_COUNT ?? "", 10);
if (/^[0-9]+$/.test(process.env.ARGUS_CPU_COUNT ?? "") && n >= 1) {
  const cpus = os.cpus.bind(os);
  os.cpus = () => cpus().slice(0, n);
}
