import { SignJWT, jwtVerify } from 'jose';
import { createHash } from 'node:crypto';

const DEFAULT_SECRET = 'trustvault-dev-secret-key-change-in-production';
const ALGORITHM = 'HS256';

let secretKeyCache: Uint8Array | undefined;

function getSecretKey(secret?: string): Uint8Array {
  if (secret) {
    return new TextEncoder().encode(secret);
  }
  if (!secretKeyCache) {
    secretKeyCache = new TextEncoder().encode(DEFAULT_SECRET);
  }
  return secretKeyCache;
}

export async function generateAuthToken(payload: {
  sub: string;
  username: string;
  role: string;
}, secret?: string): Promise<string> {
  const key = getSecretKey(secret);
  return new SignJWT({ ...payload })
    .setProtectedHeader({ alg: ALGORITHM })
    .setIssuedAt()
    .setExpirationTime('24h')
    .sign(key);
}

export async function verifyAuthToken(token: string, secret?: string): Promise<{
  valid: boolean;
  payload: Record<string, unknown> | null;
  error?: string;
}> {
  try {
    const key = getSecretKey(secret);
    const { payload } = await jwtVerify(token, key);
    return { valid: true, payload: payload as Record<string, unknown> };
  } catch (err) {
    return {
      valid: false,
      payload: null,
      error: err instanceof Error ? err.message : 'Token verification failed',
    };
  }
}

export function hashPassword(password: string): string {
  return createHash('sha256')
    .update(password + 'trustvault-salt')
    .digest('hex');
}
