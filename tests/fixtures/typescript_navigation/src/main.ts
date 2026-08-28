import { Lease, renewLease } from "./lease";

const seed: Lease = { ownerNonce: "abc", expirySeconds: 0 };
const next = renewLease(seed, 10);
const again = renewLease(next, 20);
console.log(again.expirySeconds);
