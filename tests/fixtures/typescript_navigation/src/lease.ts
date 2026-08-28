export interface Lease {
  ownerNonce: string;
  expirySeconds: number;
}

export function renewLease(lease: Lease, now: number): Lease {
  return { ownerNonce: lease.ownerNonce, expirySeconds: now + 30 };
}
