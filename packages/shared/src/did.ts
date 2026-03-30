import { exportJWK, generateKeyPair, importJWK, type JWK, type KeyLike } from 'jose';

const MULTICODEC_P256_PREFIX = new Uint8Array([0x80, 0x24]);
const DID_KEY_PREFIX = 'did:key:z';

function base58Encode(bytes: Uint8Array): string {
  const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  let num = BigInt('0x' + Buffer.from(bytes).toString('hex'));
  let result = '';
  while (num > 0n) {
    const mod = Number(num % 58n);
    result = ALPHABET[mod] + result;
    num = num / 58n;
  }
  for (const byte of bytes) {
    if (byte === 0) result = '1' + result;
    else break;
  }
  return result;
}

function base58Decode(str: string): Uint8Array {
  const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  let num = 0n;
  for (const char of str) {
    const index = ALPHABET.indexOf(char);
    if (index === -1) throw new Error(`Invalid base58 character: ${char}`);
    num = num * 58n + BigInt(index);
  }
  const hex = num.toString(16).padStart(2, '0');
  const bytes = Buffer.from(hex.length % 2 ? '0' + hex : hex, 'hex');
  const leadingZeros = [...str].findIndex(c => c !== '1');
  const prefix = new Uint8Array(leadingZeros < 0 ? str.length : leadingZeros);
  return new Uint8Array([...prefix, ...bytes]);
}

export async function generateES256KeyPair(): Promise<{
  publicKeyJwk: JWK;
  privateKeyJwk: JWK;
}> {
  const { publicKey, privateKey } = await generateKeyPair('ES256');
  const publicKeyJwk = await exportJWK(publicKey);
  const privateKeyJwk = await exportJWK(privateKey);
  return { publicKeyJwk, privateKeyJwk };
}

export function deriveDidKey(publicKeyJwk: JWK): string {
  const xBytes = Buffer.from(publicKeyJwk.x!, 'base64url');
  const yBytes = Buffer.from(publicKeyJwk.y!, 'base64url');
  const uncompressed = new Uint8Array([0x04, ...xBytes, ...yBytes]);
  const multicodec = new Uint8Array([...MULTICODEC_P256_PREFIX, ...uncompressed]);
  return DID_KEY_PREFIX + base58Encode(multicodec);
}

export function extractPublicKeyFromDidKey(did: string): JWK {
  if (!did.startsWith(DID_KEY_PREFIX)) {
    throw new Error('Not a did:key identifier');
  }
  const encoded = did.slice(DID_KEY_PREFIX.length);
  const decoded = base58Decode(encoded);
  const keyBytes = decoded.slice(2);
  const x = Buffer.from(keyBytes.slice(1, 33)).toString('base64url');
  const y = Buffer.from(keyBytes.slice(33, 65)).toString('base64url');
  return { kty: 'EC', crv: 'P-256', x, y };
}

export function buildDidDocument(did: string, publicKeyJwk: JWK): Record<string, unknown> {
  return {
    '@context': ['https://www.w3.org/ns/did/v1'],
    id: did,
    verificationMethod: [
      {
        id: `${did}#key-1`,
        type: 'JsonWebKey2020',
        controller: did,
        publicKeyJwk: {
          kty: publicKeyJwk.kty,
          crv: publicKeyJwk.crv,
          x: publicKeyJwk.x,
          y: publicKeyJwk.y,
        },
      },
    ],
    assertionMethod: [`${did}#key-1`],
    authentication: [`${did}#key-1`],
  };
}

export async function importPrivateKey(jwk: JWK): Promise<KeyLike | Uint8Array> {
  return importJWK(jwk, 'ES256');
}

export async function importPublicKey(jwk: JWK): Promise<KeyLike | Uint8Array> {
  return importJWK(jwk, 'ES256');
}
