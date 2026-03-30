/**
 * StatusList2021 implementation
 * A bitstring-based revocation mechanism where each credential gets an index.
 * 0 = valid, 1 = revoked
 */

const DEFAULT_LIST_SIZE = 131072; // 16KB * 8 bits

export class StatusList {
  private bitstring: Uint8Array;

  constructor(size: number = DEFAULT_LIST_SIZE) {
    this.bitstring = new Uint8Array(Math.ceil(size / 8));
  }

  static fromEncoded(encoded: string): StatusList {
    const decoded = Buffer.from(encoded, 'base64url');
    const list = new StatusList(decoded.length * 8);
    list.bitstring = new Uint8Array(decoded);
    return list;
  }

  setStatus(index: number, revoked: boolean): void {
    const byteIndex = Math.floor(index / 8);
    const bitIndex = index % 8;
    if (byteIndex >= this.bitstring.length) {
      throw new Error(`Index ${index} out of range`);
    }
    if (revoked) {
      this.bitstring[byteIndex] |= (1 << bitIndex);
    } else {
      this.bitstring[byteIndex] &= ~(1 << bitIndex);
    }
  }

  getStatus(index: number): boolean {
    const byteIndex = Math.floor(index / 8);
    const bitIndex = index % 8;
    if (byteIndex >= this.bitstring.length) {
      throw new Error(`Index ${index} out of range`);
    }
    return (this.bitstring[byteIndex] & (1 << bitIndex)) !== 0;
  }

  encode(): string {
    return Buffer.from(this.bitstring).toString('base64url');
  }

  get size(): number {
    return this.bitstring.length * 8;
  }
}

let currentIndex = 0;

export function allocateStatusIndex(): number {
  return currentIndex++;
}

export function resetStatusIndex(value: number = 0): void {
  currentIndex = value;
}
