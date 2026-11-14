/**
 * SHA-256 (FIPS 180-4), fed in pieces, for checking uploads (PLAINNOTES_CHECK_ATTACHMENTS_HASH).
 *
 * The browser's crypto.subtle.digest() needs the whole input in one buffer, so a large file
 * would have to fit in memory, and it exists only on HTTPS pages. This reads a file 1 MiB at a
 * time instead.
 */

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);
const INITIAL_STATE = [
  0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
];
const BLOCK_BYTES = 64;
const CHUNK_BYTES = 1024 * 1024;

/** An incremental SHA-256: update() any number of times, then digest() once. */
export class Sha256 {
  #state = Uint32Array.from(INITIAL_STATE);
  #words = new Uint32Array(64);
  #pending = new Uint8Array(BLOCK_BYTES); // input that does not fill a block yet
  #pendingLength = 0;
  #length = 0; // bytes hashed so far; the length in bits stays exact up to 1 PiB
  #finished = false;

  /**
   * Add bytes to the hash.
   * @param {Uint8Array} bytes
   * @returns {Sha256} this
   */
  update(bytes) {
    if (this.#finished) throw new Error("This SHA-256 is finished; start a new one.");
    this.#length += bytes.length;
    let offset = 0;
    if (this.#pendingLength > 0) {
      offset = Math.min(BLOCK_BYTES - this.#pendingLength, bytes.length);
      this.#pending.set(bytes.subarray(0, offset), this.#pendingLength);
      this.#pendingLength += offset;
      if (this.#pendingLength < BLOCK_BYTES) return this;
      this.#compress(this.#pending, 0);
      this.#pendingLength = 0;
    }
    for (; offset + BLOCK_BYTES <= bytes.length; offset += BLOCK_BYTES) this.#compress(bytes, offset);
    this.#pending.set(bytes.subarray(offset));
    this.#pendingLength = bytes.length - offset;
    return this;
  }

  /**
   * Finish the hash.
   * @returns {Uint8Array} the 32-byte digest
   */
  digest() {
    if (this.#finished) throw new Error("This SHA-256 is finished; start a new one.");
    this.#finished = true;
    // Padding: a 1 bit, zeros, then the length in bits as a 64-bit big-endian number. When
    // fewer than 9 bytes are left in the last block, the padding spills into one more.
    const blocks = this.#pendingLength < BLOCK_BYTES - 8 ? 1 : 2;
    const tail = new Uint8Array(blocks * BLOCK_BYTES);
    tail.set(this.#pending.subarray(0, this.#pendingLength));
    tail[this.#pendingLength] = 0x80;
    const bits = this.#length * 8;
    const view = new DataView(tail.buffer);
    view.setUint32(tail.length - 8, Math.floor(bits / 2 ** 32));
    view.setUint32(tail.length - 4, bits >>> 0);
    for (let offset = 0; offset < tail.length; offset += BLOCK_BYTES) this.#compress(tail, offset);
    const digest = new Uint8Array(32);
    const out = new DataView(digest.buffer);
    this.#state.forEach((word, index) => out.setUint32(index * 4, word));
    return digest;
  }

  /** Process one 64-byte block of bytes, starting at offset. */
  #compress(bytes, offset) {
    const w = this.#words;
    for (let i = 0; i < 16; i += 1, offset += 4) {
      w[i] =
        (bytes[offset] << 24) | (bytes[offset + 1] << 16) | (bytes[offset + 2] << 8) | bytes[offset + 3];
    }
    for (let i = 16; i < 64; i += 1) {
      const x = w[i - 15];
      const y = w[i - 2];
      const s0 = ((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3);
      const s1 = ((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10);
      w[i] = w[i - 16] + s0 + w[i - 7] + s1; // A Uint32Array keeps the sum modulo 2^32.
    }
    const state = this.#state;
    let a = state[0];
    let b = state[1];
    let c = state[2];
    let d = state[3];
    let e = state[4];
    let f = state[5];
    let g = state[6];
    let h = state[7];
    for (let i = 0; i < 64; i += 1) {
      const sum1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
      const choose = (e & f) ^ (~e & g);
      const t1 = (h + sum1 + choose + K[i] + w[i]) | 0;
      const sum0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (sum0 + majority) | 0;
      h = g;
      g = f;
      f = e;
      e = (d + t1) | 0;
      d = c;
      c = b;
      b = a;
      a = (t1 + t2) | 0;
    }
    state[0] += a;
    state[1] += b;
    state[2] += c;
    state[3] += d;
    state[4] += e;
    state[5] += f;
    state[6] += g;
    state[7] += h;
  }
}

/**
 * False in a browser too old to run blobSha256() with a signal: it needs
 * AbortSignal.throwIfAborted, which Chrome (100), Firefox (97) and Safari (15.4) gained in 2022.
 */
export const blobSha256Supported = typeof AbortSignal.prototype.throwIfAborted === "function";

/**
 * The SHA-256 of a file, read 1 MiB at a time, so the page stays responsive and memory use stays
 * small whatever the file size.
 * @param {Blob} blob
 * @param {{signal?: AbortSignal, onProgress?: (fraction: number) => void}} [options] signal stops
 *   the work with its abort reason; onProgress receives the share of the file hashed so far
 * @returns {Promise<Uint8Array>} the 32-byte digest
 */
export async function blobSha256(blob, { signal, onProgress } = {}) {
  const hash = new Sha256();
  for (let offset = 0; offset < blob.size; offset += CHUNK_BYTES) {
    signal?.throwIfAborted();
    const chunk = await blob.slice(offset, offset + CHUNK_BYTES).arrayBuffer();
    signal?.throwIfAborted();
    hash.update(new Uint8Array(chunk));
    onProgress?.(Math.min(offset + CHUNK_BYTES, blob.size) / blob.size);
  }
  signal?.throwIfAborted();
  return hash.digest();
}
