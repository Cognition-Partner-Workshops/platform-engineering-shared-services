import { useState, useEffect } from 'react';
import { fetchCredentialOffer, exchangeToken, fetchCredential, submitPresentation, getPresentationStatus } from './api';

interface StoredCredential {
  id: string;
  jwt: string;
  issuer: string;
  types: string[];
  claims: Record<string, unknown>;
  issuanceDate: string;
  expirationDate?: string;
  status?: string;
}

interface WalletIdentity {
  did: string;
  publicKeyJwk: JsonWebKey;
  privateKeyJwk: JsonWebKey;
}

function decodeJwt(jwt: string): Record<string, unknown> {
  const parts = jwt.split('.');
  if (parts.length !== 3) throw new Error('Invalid JWT');
  const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
  return payload;
}

async function generateWalletIdentity(): Promise<WalletIdentity> {
  const keyPair = await crypto.subtle.generateKey(
    { name: 'ECDSA', namedCurve: 'P-256' },
    true,
    ['sign', 'verify']
  );
  const publicKeyJwk = await crypto.subtle.exportKey('jwk', keyPair.publicKey);
  const privateKeyJwk = await crypto.subtle.exportKey('jwk', keyPair.privateKey);

  // Derive did:key
  const xBytes = Uint8Array.from(atob(publicKeyJwk.x!.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  const yBytes = Uint8Array.from(atob(publicKeyJwk.y!.replace(/-/g, '+').replace(/_/g, '/')), c => c.charCodeAt(0));
  const uncompressed = new Uint8Array([0x04, ...xBytes, ...yBytes]);
  const multicodec = new Uint8Array([0x80, 0x24, ...uncompressed]);

  const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  let hex = '';
  for (const b of multicodec) hex += b.toString(16).padStart(2, '0');
  let num = BigInt('0x' + hex);
  let encoded = '';
  while (num > 0n) {
    encoded = ALPHABET[Number(num % 58n)] + encoded;
    num = num / 58n;
  }
  const did = `did:key:z${encoded}`;

  return { did, publicKeyJwk, privateKeyJwk };
}

async function createVpJwt(identity: WalletIdentity, vcJwts: string[], nonce: string, audience?: string): Promise<string> {
  const privateKey = await crypto.subtle.importKey(
    'jwk', identity.privateKeyJwk, { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign']
  );

  const header = { alg: 'ES256', typ: 'JWT' };
  const now = Math.floor(Date.now() / 1000);
  const payload: Record<string, unknown> = {
    iss: identity.did,
    sub: identity.did,
    iat: now,
    nonce,
    vp: {
      '@context': ['https://www.w3.org/2018/credentials/v1'],
      type: ['VerifiablePresentation'],
      verifiableCredential: vcJwts,
    },
  };
  if (audience) payload.aud = audience;

  const encode = (obj: unknown) => btoa(JSON.stringify(obj)).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');
  const headerB64 = encode(header);
  const payloadB64 = encode(payload);
  const signingInput = `${headerB64}.${payloadB64}`;

  const signature = await crypto.subtle.sign(
    { name: 'ECDSA', hash: 'SHA-256' },
    privateKey,
    new TextEncoder().encode(signingInput)
  );

  const sigBytes = new Uint8Array(signature);
  let sigB64 = btoa(String.fromCharCode(...sigBytes)).replace(/=/g, '').replace(/\+/g, '-').replace(/\//g, '_');

  return `${signingInput}.${sigB64}`;
}

function App() {
  const [identity, setIdentity] = useState<WalletIdentity | null>(null);
  const [credentials, setCredentials] = useState<StoredCredential[]>([]);
  const [tab, setTab] = useState<'wallet' | 'scan' | 'present'>('wallet');
  const [offerInput, setOfferInput] = useState('');
  const [pinInput, setPinInput] = useState('');
  const [sessionInput, setSessionInput] = useState('');
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(false);
  const [presentResult, setPresentResult] = useState<Record<string, unknown> | null>(null);
  const [selectedCreds, setSelectedCreds] = useState<Set<number>>(new Set());

  useEffect(() => {
    const saved = localStorage.getItem('wallet_identity');
    if (saved) {
      setIdentity(JSON.parse(saved));
    }
    const savedCreds = localStorage.getItem('wallet_credentials');
    if (savedCreds) {
      setCredentials(JSON.parse(savedCreds));
    }
  }, []);

  const initWallet = async () => {
    setLoading(true);
    try {
      const id = await generateWalletIdentity();
      setIdentity(id);
      localStorage.setItem('wallet_identity', JSON.stringify(id));
      setStatus('Wallet initialized!');
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const claimCredential = async () => {
    if (!identity) return;
    setLoading(true);
    setStatus('');
    try {
      // Parse the offer URL to get the offer URI
      let offerUri = offerInput.trim();
      if (offerUri.startsWith('openid-credential-offer://')) {
        const url = new URL(offerUri.replace('openid-credential-offer://', 'https://placeholder/'));
        offerUri = url.searchParams.get('credential_offer_uri') || offerUri;
      }
      // If it's just an offer ID, construct the URL
      if (!offerUri.startsWith('http')) {
        offerUri = `http://localhost:3002/oid4vci/offers/${offerUri}`;
      }

      setStatus('Fetching offer...');
      const offer = await fetchCredentialOffer(offerUri);

      const preAuthCode = offer.grants?.['urn:ietf:params:oauth:grant-type:pre-authorized_code']?.['pre-authorized_code'];
      const pinRequired = offer.grants?.['urn:ietf:params:oauth:grant-type:pre-authorized_code']?.user_pin_required;

      if (!preAuthCode) throw new Error('No pre-authorized code in offer');
      if (pinRequired && !pinInput) throw new Error('PIN is required for this offer');

      setStatus('Exchanging token...');
      const tokenRes = await exchangeToken(preAuthCode, pinRequired ? pinInput : undefined);

      setStatus('Fetching credential...');
      const credRes = await fetchCredential(tokenRes.access_token);

      const jwt = credRes.credential;
      const decoded = decodeJwt(jwt);
      const vc = decoded.vc as Record<string, unknown>;

      const newCred: StoredCredential = {
        id: (vc.id as string) || `cred-${Date.now()}`,
        jwt,
        issuer: decoded.iss as string,
        types: (vc.type as string[]) || ['VerifiableCredential'],
        claims: (vc.credentialSubject as Record<string, unknown>) || {},
        issuanceDate: (vc.issuanceDate as string) || new Date().toISOString(),
        expirationDate: vc.expirationDate as string | undefined,
      };

      const updated = [...credentials, newCred];
      setCredentials(updated);
      localStorage.setItem('wallet_credentials', JSON.stringify(updated));
      setStatus('Credential claimed successfully!');
      setOfferInput('');
      setPinInput('');
      setTab('wallet');
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const presentCredentials = async () => {
    if (!identity || selectedCreds.size === 0) return;
    setLoading(true);
    setStatus('');
    setPresentResult(null);
    try {
      const sessionId = sessionInput.trim();
      if (!sessionId) throw new Error('Please enter a session ID');

      setStatus('Fetching presentation request...');
      const requestData = await getPresentationStatus(sessionId);

      setStatus('Creating Verifiable Presentation...');
      const selected = [...selectedCreds].map(i => credentials[i]);
      const vcJwts = selected.map(c => c.jwt);

      const vpJwt = await createVpJwt(identity, vcJwts, requestData.nonce, 'http://localhost:3003');

      setStatus('Submitting presentation...');
      const result = await submitPresentation(vpJwt, sessionId);

      setPresentResult(result);
      setStatus(result.decision === 'accepted' ? 'Presentation ACCEPTED!' : `Presentation REJECTED: ${result.failReason}`);
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const removeCredential = (index: number) => {
    const updated = credentials.filter((_, i) => i !== index);
    setCredentials(updated);
    localStorage.setItem('wallet_credentials', JSON.stringify(updated));
    selectedCreds.delete(index);
    setSelectedCreds(new Set(selectedCreds));
  };

  const resetWallet = () => {
    localStorage.removeItem('wallet_identity');
    localStorage.removeItem('wallet_credentials');
    setIdentity(null);
    setCredentials([]);
    setStatus('Wallet reset.');
  };

  const toggleCredSelection = (index: number) => {
    const newSet = new Set(selectedCreds);
    if (newSet.has(index)) newSet.delete(index);
    else newSet.add(index);
    setSelectedCreds(newSet);
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100">
      <header className="bg-white shadow-sm border-b border-blue-200">
        <div className="max-w-4xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-blue-600 rounded-xl flex items-center justify-center text-white font-bold text-lg">W</div>
            <div>
              <h1 className="text-xl font-bold text-gray-900">TrustVault Wallet</h1>
              <p className="text-xs text-gray-500">Verifiable Credential Wallet</p>
            </div>
          </div>
          {identity && (
            <button onClick={resetWallet} className="text-xs text-red-500 hover:text-red-700">Reset</button>
          )}
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-6">
        {!identity ? (
          <div className="text-center py-20">
            <div className="w-20 h-20 bg-blue-100 rounded-full flex items-center justify-center mx-auto mb-6">
              <span className="text-4xl">🔐</span>
            </div>
            <h2 className="text-2xl font-bold text-gray-900 mb-2">Welcome to TrustVault</h2>
            <p className="text-gray-600 mb-8">Create your digital identity wallet to receive and present verifiable credentials.</p>
            <button
              onClick={initWallet}
              disabled={loading}
              className="bg-blue-600 text-white px-8 py-3 rounded-xl font-semibold hover:bg-blue-700 disabled:opacity-50 transition"
            >
              {loading ? 'Creating...' : 'Create Wallet'}
            </button>
          </div>
        ) : (
          <>
            <div className="bg-white rounded-xl shadow-sm p-4 mb-4 border border-blue-100">
              <p className="text-xs text-gray-500">Your DID</p>
              <p className="text-sm font-mono text-blue-700 break-all">{identity.did}</p>
            </div>

            <div className="flex gap-2 mb-6">
              {(['wallet', 'scan', 'present'] as const).map(t => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`px-4 py-2 rounded-lg font-medium text-sm transition ${
                    tab === t ? 'bg-blue-600 text-white shadow-sm' : 'bg-white text-gray-600 hover:bg-gray-50 border'
                  }`}
                >
                  {t === 'wallet' ? `Credentials (${credentials.length})` : t === 'scan' ? 'Claim Credential' : 'Present'}
                </button>
              ))}
            </div>

            {status && (
              <div className={`mb-4 p-3 rounded-lg text-sm ${
                status.startsWith('Error') ? 'bg-red-50 text-red-700 border border-red-200' :
                status.includes('ACCEPTED') ? 'bg-green-50 text-green-700 border border-green-200' :
                status.includes('REJECTED') ? 'bg-red-50 text-red-700 border border-red-200' :
                'bg-blue-50 text-blue-700 border border-blue-200'
              }`}>
                {status}
              </div>
            )}

            {tab === 'wallet' && (
              <div className="space-y-3">
                {credentials.length === 0 ? (
                  <div className="text-center py-12 bg-white rounded-xl border border-dashed border-gray-300">
                    <p className="text-gray-500">No credentials yet. Claim one from an issuer.</p>
                  </div>
                ) : (
                  credentials.map((cred, i) => (
                    <div key={i} className="bg-white rounded-xl shadow-sm border border-gray-200 p-4">
                      <div className="flex items-start justify-between mb-2">
                        <div>
                          <span className="inline-block bg-blue-100 text-blue-800 text-xs font-semibold px-2 py-1 rounded-full mb-1">
                            {cred.types.find(t => t !== 'VerifiableCredential') || 'Credential'}
                          </span>
                          <p className="text-xs text-gray-500 font-mono">{cred.issuer.slice(0, 40)}...</p>
                        </div>
                        <button onClick={() => removeCredential(i)} className="text-gray-400 hover:text-red-500 text-xs">Remove</button>
                      </div>
                      <div className="grid grid-cols-2 gap-2 mt-3">
                        {Object.entries(cred.claims).filter(([k]) => k !== 'id').map(([key, val]) => (
                          <div key={key} className="text-xs">
                            <span className="text-gray-500">{key}:</span>
                            <span className="ml-1 text-gray-900 font-medium">{String(val)}</span>
                          </div>
                        ))}
                      </div>
                      <div className="mt-3 flex gap-4 text-xs text-gray-500">
                        <span>Issued: {new Date(cred.issuanceDate).toLocaleDateString()}</span>
                        {cred.expirationDate && <span>Expires: {new Date(cred.expirationDate).toLocaleDateString()}</span>}
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}

            {tab === 'scan' && (
              <div className="bg-white rounded-xl shadow-sm border p-6">
                <h3 className="font-semibold text-lg mb-4">Claim a Credential</h3>
                <div className="space-y-4">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Offer URL or ID</label>
                    <input
                      type="text"
                      value={offerInput}
                      onChange={e => setOfferInput(e.target.value)}
                      placeholder="Paste offer URL, QR data, or offer ID"
                      className="w-full px-4 py-2 border rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">PIN (if required)</label>
                    <input
                      type="text"
                      value={pinInput}
                      onChange={e => setPinInput(e.target.value)}
                      placeholder="Enter PIN"
                      className="w-full px-4 py-2 border rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                    />
                  </div>
                  <button
                    onClick={claimCredential}
                    disabled={loading || !offerInput}
                    className="w-full bg-blue-600 text-white py-2 rounded-lg font-semibold hover:bg-blue-700 disabled:opacity-50 transition"
                  >
                    {loading ? 'Claiming...' : 'Claim Credential'}
                  </button>
                </div>
              </div>
            )}

            {tab === 'present' && (
              <div className="space-y-4">
                <div className="bg-white rounded-xl shadow-sm border p-6">
                  <h3 className="font-semibold text-lg mb-4">Present Credentials</h3>
                  <div className="space-y-4">
                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-1">Verification Session ID</label>
                      <input
                        type="text"
                        value={sessionInput}
                        onChange={e => setSessionInput(e.target.value)}
                        placeholder="Enter session ID from verifier"
                        className="w-full px-4 py-2 border rounded-lg text-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500"
                      />
                    </div>

                    <div>
                      <label className="block text-sm font-medium text-gray-700 mb-2">Select Credentials to Present</label>
                      {credentials.length === 0 ? (
                        <p className="text-sm text-gray-500">No credentials available</p>
                      ) : (
                        <div className="space-y-2">
                          {credentials.map((cred, i) => (
                            <label key={i} className={`flex items-center gap-3 p-3 border rounded-lg cursor-pointer transition ${
                              selectedCreds.has(i) ? 'border-blue-500 bg-blue-50' : 'border-gray-200 hover:border-gray-300'
                            }`}>
                              <input
                                type="checkbox"
                                checked={selectedCreds.has(i)}
                                onChange={() => toggleCredSelection(i)}
                                className="rounded text-blue-600"
                              />
                              <div className="text-sm">
                                <span className="font-medium">{cred.types.find(t => t !== 'VerifiableCredential')}</span>
                                <span className="text-gray-500 ml-2">from {cred.issuer.slice(0, 30)}...</span>
                              </div>
                            </label>
                          ))}
                        </div>
                      )}
                    </div>

                    <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3">
                      <p className="text-xs text-yellow-800">
                        <strong>Consent:</strong> By presenting, you authorize sharing the selected credentials with the verifier. Only the claims in the selected credentials will be shared.
                      </p>
                    </div>

                    <button
                      onClick={presentCredentials}
                      disabled={loading || !sessionInput || selectedCreds.size === 0}
                      className="w-full bg-green-600 text-white py-2 rounded-lg font-semibold hover:bg-green-700 disabled:opacity-50 transition"
                    >
                      {loading ? 'Submitting...' : `Present ${selectedCreds.size} Credential(s)`}
                    </button>
                  </div>
                </div>

                {presentResult && (
                  <div className={`rounded-xl p-4 ${
                    presentResult.decision === 'accepted' ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'
                  }`}>
                    <h4 className="font-semibold mb-2">{presentResult.decision === 'accepted' ? 'Verification Passed' : 'Verification Failed'}</h4>
                    {presentResult.failReason && <p className="text-sm text-red-700 mb-2">{String(presentResult.failReason)}</p>}
                    {(presentResult.checks as Array<Record<string, unknown>>)?.map((check, i) => (
                      <div key={i} className="flex items-center gap-2 text-xs py-1">
                        <span className={check.passed === true ? 'text-green-600' : check.passed === false ? 'text-red-600' : 'text-gray-400'}>
                          {check.passed === true ? 'PASS' : check.passed === false ? 'FAIL' : 'SKIP'}
                        </span>
                        <span className="text-gray-700">{String(check.checkName)}: {String(check.detail)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}

export default App;
