const ISSUER_URL = 'http://localhost:3002';
const VERIFIER_URL = 'http://localhost:3003';

export async function fetchCredentialOffer(offerUri: string) {
  const res = await fetch(offerUri);
  if (!res.ok) throw new Error('Failed to fetch offer');
  return res.json();
}

export async function exchangeToken(preAuthCode: string, pin?: string) {
  const body: Record<string, string> = { 'pre-authorized_code': preAuthCode };
  if (pin) body['user_pin'] = pin;

  const res = await fetch(`${ISSUER_URL}/oid4vci/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.error || 'Token exchange failed');
  }
  return res.json();
}

export async function fetchCredential(accessToken: string) {
  const res = await fetch(`${ISSUER_URL}/oid4vci/credential`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${accessToken}`,
    },
    body: JSON.stringify({ format: 'jwt_vc_json' }),
  });
  if (!res.ok) throw new Error('Failed to fetch credential');
  return res.json();
}

export async function fetchPresentationRequest(requestUri: string) {
  const res = await fetch(requestUri);
  if (!res.ok) throw new Error('Failed to fetch presentation request');
  return res.json();
}

export async function submitPresentation(vpToken: string, state: string) {
  const res = await fetch(`${VERIFIER_URL}/oid4vp/response`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vp_token: vpToken, state }),
  });
  if (!res.ok) throw new Error('Submission failed');
  return res.json();
}

export async function submitPresentationDirect(vpToken: string, sessionId: string) {
  const res = await fetch(`${VERIFIER_URL}/presentations/submit`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ vp_token: vpToken, sessionId }),
  });
  if (!res.ok) throw new Error('Submission failed');
  return res.json();
}

export async function getPresentationStatus(sessionId: string) {
  const res = await fetch(`${VERIFIER_URL}/presentations/request/${sessionId}`);
  if (!res.ok) throw new Error('Failed to get status');
  return res.json();
}
