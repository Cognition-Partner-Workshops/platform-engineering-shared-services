import { SignJWT, jwtVerify, decodeJwt, type JWK } from 'jose';
import { importPrivateKey, importPublicKey, extractPublicKeyFromDidKey } from './did.js';
import type { VerifiablePresentation, VerifiableCredential } from './types.js';

export async function createVerifiablePresentation(params: {
  holderDid: string;
  credentials: VerifiableCredential[];
  privateKeyJwk: JWK;
  nonce?: string;
  audience?: string;
}): Promise<{ presentation: VerifiablePresentation; jwt: string }> {
  const { holderDid, credentials, privateKeyJwk, nonce, audience } = params;

  const presentation: VerifiablePresentation = {
    '@context': ['https://www.w3.org/2018/credentials/v1'],
    type: ['VerifiablePresentation'],
    holder: holderDid,
    verifiableCredential: credentials,
  };

  const privateKey = await importPrivateKey(privateKeyJwk);

  const jwtBuilder = new SignJWT({
    vp: {
      '@context': ['https://www.w3.org/2018/credentials/v1'],
      type: ['VerifiablePresentation'],
      verifiableCredential: credentials.map(c => c.proof?.jwt ?? ''),
    },
    nonce,
  })
    .setProtectedHeader({ alg: 'ES256', typ: 'JWT' })
    .setIssuedAt()
    .setIssuer(holderDid)
    .setSubject(holderDid);

  if (audience) {
    jwtBuilder.setAudience(audience);
  }

  const jwt = await jwtBuilder.sign(privateKey);

  presentation.proof = {
    type: 'JwtProof2020',
    jwt,
  };

  return { presentation, jwt };
}

export async function verifyPresentationJwt(
  jwt: string,
  holderPublicKeyJwk?: JWK
): Promise<{
  valid: boolean;
  payload: Record<string, unknown> | null;
  error?: string;
}> {
  try {
    const decoded = decodeJwt(jwt);
    const holderDid = decoded.iss as string;

    let publicKeyJwk = holderPublicKeyJwk;
    if (!publicKeyJwk) {
      publicKeyJwk = extractPublicKeyFromDidKey(holderDid);
    }
    const publicKey = await importPublicKey(publicKeyJwk);
    const { payload } = await jwtVerify(jwt, publicKey, {
      clockTolerance: 60,
    });
    return { valid: true, payload: payload as Record<string, unknown> };
  } catch (err) {
    return {
      valid: false,
      payload: null,
      error: err instanceof Error ? err.message : 'Unknown verification error',
    };
  }
}

export function decodePresentationJwt(jwt: string): Record<string, unknown> {
  return decodeJwt(jwt) as Record<string, unknown>;
}
