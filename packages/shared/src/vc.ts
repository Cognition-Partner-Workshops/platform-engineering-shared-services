import { SignJWT, jwtVerify, decodeJwt, type JWK } from 'jose';
import { importPrivateKey, importPublicKey, extractPublicKeyFromDidKey } from './did.js';
import type { VerifiableCredential, StatusListEntry } from './types.js';

export async function createVerifiableCredential(params: {
  id: string;
  issuerDid: string;
  subjectDid: string;
  types: string[];
  claims: Record<string, unknown>;
  privateKeyJwk: JWK;
  expirationDate?: string;
  credentialStatus?: StatusListEntry;
}): Promise<{ credential: VerifiableCredential; jwt: string }> {
  const {
    id,
    issuerDid,
    subjectDid,
    types,
    claims,
    privateKeyJwk,
    expirationDate,
    credentialStatus,
  } = params;

  const now = new Date().toISOString();
  const credential: VerifiableCredential = {
    '@context': [
      'https://www.w3.org/2018/credentials/v1',
    ],
    type: ['VerifiableCredential', ...types],
    id,
    issuer: issuerDid,
    issuanceDate: now,
    credentialSubject: {
      id: subjectDid,
      ...claims,
    },
  };

  if (expirationDate) {
    credential.expirationDate = expirationDate;
  }

  if (credentialStatus) {
    credential.credentialStatus = credentialStatus;
  }

  const privateKey = await importPrivateKey(privateKeyJwk);

  const jwtBuilder = new SignJWT({
    vc: credential,
    sub: subjectDid,
    iss: issuerDid,
    jti: id,
  })
    .setProtectedHeader({ alg: 'ES256', typ: 'JWT' })
    .setIssuedAt()
    .setIssuer(issuerDid)
    .setSubject(subjectDid);

  if (expirationDate) {
    jwtBuilder.setExpirationTime(new Date(expirationDate).getTime() / 1000);
  }

  const jwt = await jwtBuilder.sign(privateKey);

  credential.proof = {
    type: 'JwtProof2020',
    jwt,
  };

  return { credential, jwt };
}

export async function verifyCredentialJwt(
  jwt: string,
  issuerPublicKeyJwk?: JWK
): Promise<{
  valid: boolean;
  payload: Record<string, unknown> | null;
  error?: string;
}> {
  try {
    const decoded = decodeJwt(jwt);
    const issuerDid = decoded.iss as string;

    let publicKeyJwk = issuerPublicKeyJwk;
    if (!publicKeyJwk) {
      publicKeyJwk = extractPublicKeyFromDidKey(issuerDid);
    }
    const publicKey = await importPublicKey(publicKeyJwk);
    const { payload } = await jwtVerify(jwt, publicKey);
    return { valid: true, payload: payload as Record<string, unknown> };
  } catch (err) {
    return {
      valid: false,
      payload: null,
      error: err instanceof Error ? err.message : 'Unknown verification error',
    };
  }
}

export function decodeCredentialJwt(jwt: string): Record<string, unknown> {
  return decodeJwt(jwt) as Record<string, unknown>;
}
