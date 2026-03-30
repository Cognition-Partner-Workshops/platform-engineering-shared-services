import { useState, useEffect } from 'react';

const ISSUER_URL = 'http://localhost:3002';
const TRUST_REGISTRY_URL = 'http://localhost:3001';

interface IssuerInfo {
  did: string;
  name: string;
  type: string;
  publicKeyJwk: Record<string, string>;
  createdAt: string;
}

interface Credential {
  id: string;
  issuerDid: string;
  subjectDid: string;
  schemaName: string;
  claims: Record<string, unknown>;
  status: string;
  createdAt: number;
  expirationDate?: string;
}

interface OfferInfo {
  offerId: string;
  offerUrl: string;
  qrData: string;
  pin?: string;
  pinRequired: boolean;
  expiresAt: string;
}

interface SchemaInfo {
  id: string;
  name: string;
  version: string;
  description?: string;
}

function App() {
  const [tab, setTab] = useState<'onboard' | 'issue' | 'credentials' | 'schemas' | 'auth'>('auth');
  const [token, setToken] = useState('');
  const [issuers, setIssuers] = useState<IssuerInfo[]>([]);
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [schemas, setSchemas] = useState<SchemaInfo[]>([]);
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(false);
  const [currentOffer, setCurrentOffer] = useState<OfferInfo | null>(null);

  // Auth
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  // Onboard
  const [onboardName, setOnboardName] = useState('');
  const [onboardType, setOnboardType] = useState('employer');
  const [onboardSchemas, setOnboardSchemas] = useState('EmploymentCredential');

  // Issue
  const [selectedIssuer, setSelectedIssuer] = useState('');
  const [schemaName, setSchemaName] = useState('EmploymentCredential');
  const [subjectDid, setSubjectDid] = useState('');
  const [claimsJson, setClaimsJson] = useState('{}');
  const [expirationDate, setExpirationDate] = useState('');
  const [requirePin, setRequirePin] = useState(false);

  // Schema registration
  const [newSchemaName, setNewSchemaName] = useState('');
  const [newSchemaVersion, setNewSchemaVersion] = useState('1.0');
  const [newSchemaDesc, setNewSchemaDesc] = useState('');
  const [newSchemaJson, setNewSchemaJson] = useState('{"type":"object","properties":{},"required":[]}');

  const authHeaders: Record<string, string> = token ? { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' } : { 'Content-Type': 'application/json' };

  const login = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${ISSUER_URL}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) throw new Error('Login failed');
      const data = await res.json();
      setToken(data.token);
      setStatus('Logged in!');
      setTab('onboard');
      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const register = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${ISSUER_URL}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, role: 'issuer' }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || 'Registration failed');
      }
      const data = await res.json();
      setToken(data.token);
      setStatus('Registered and logged in!');
      setTab('onboard');
      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const loadData = async () => {
    try {
      const [issuersRes, credsRes, schemasRes] = await Promise.all([
        fetch(`${ISSUER_URL}/issuers`),
        fetch(`${ISSUER_URL}/credentials`),
        fetch(`${TRUST_REGISTRY_URL}/registry/schemas`),
      ]);
      if (issuersRes.ok) setIssuers(await issuersRes.json());
      if (credsRes.ok) setCredentials(await credsRes.json());
      if (schemasRes.ok) setSchemas(await schemasRes.json());
    } catch {}
  };

  useEffect(() => { loadData(); }, []);

  const onboard = async () => {
    setLoading(true);
    setStatus('');
    try {
      const res = await fetch(`${ISSUER_URL}/onboard`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({
          name: onboardName,
          type: onboardType,
          authorizedSchemas: onboardSchemas.split(',').map(s => s.trim()),
        }),
      });
      if (!res.ok) throw new Error('Onboard failed');
      const data = await res.json();
      setStatus(`Issuer onboarded! DID: ${data.did}`);
      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const issueCredential = async () => {
    setLoading(true);
    setStatus('');
    setCurrentOffer(null);
    try {
      let claims;
      try { claims = JSON.parse(claimsJson); } catch { throw new Error('Invalid JSON in claims'); }

      const body: Record<string, unknown> = {
        schemaName,
        subjectDid: subjectDid || 'did:key:placeholder',
        claims,
      };
      if (selectedIssuer) body.issuerDid = selectedIssuer;
      if (expirationDate) body.expirationDate = expirationDate;

      const res = await fetch(`${ISSUER_URL}/credentials`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('Issue failed');
      const data = await res.json();
      setStatus(`Credential issued! ID: ${data.credential.id}`);

      // Generate offer
      const credId = data.credential.id.replace('urn:uuid:', '');
      const offerRes = await fetch(`${ISSUER_URL}/credentials/${credId}/offer`, {
        method: 'POST',
        headers: authHeaders,
        body: JSON.stringify({ requirePin }),
      });
      if (offerRes.ok) {
        const offer = await offerRes.json();
        setCurrentOffer(offer);
        setStatus(`Credential issued and offer created!`);
      }

      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const revokeCredential = async (id: string) => {
    const credId = id.replace('urn:uuid:', '');
    const res = await fetch(`${ISSUER_URL}/credentials/${credId}/revoke`, { method: 'POST', headers: authHeaders });
    if (res.ok) {
      setStatus('Credential revoked');
      loadData();
    }
  };

  const suspendCredential = async (id: string) => {
    const credId = id.replace('urn:uuid:', '');
    const res = await fetch(`${ISSUER_URL}/credentials/${credId}/suspend`, { method: 'POST', headers: authHeaders });
    if (res.ok) {
      setStatus('Credential suspended');
      loadData();
    }
  };

  const activateCredential = async (id: string) => {
    const credId = id.replace('urn:uuid:', '');
    const res = await fetch(`${ISSUER_URL}/credentials/${credId}/activate`, { method: 'POST', headers: authHeaders });
    if (res.ok) {
      setStatus('Credential activated');
      loadData();
    }
  };

  const generateOffer = async (credId: string) => {
    const id = credId.replace('urn:uuid:', '');
    const res = await fetch(`${ISSUER_URL}/credentials/${id}/offer`, {
      method: 'POST',
      headers: authHeaders,
      body: JSON.stringify({ requirePin }),
    });
    if (res.ok) {
      const offer = await res.json();
      setCurrentOffer(offer);
      setStatus('Offer generated!');
    }
  };

  const registerSchema = async () => {
    setLoading(true);
    try {
      let schemaJson;
      try { schemaJson = JSON.parse(newSchemaJson); } catch { throw new Error('Invalid JSON schema'); }

      const res = await fetch(`${TRUST_REGISTRY_URL}/registry/schemas`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: newSchemaName,
          version: newSchemaVersion,
          description: newSchemaDesc,
          schemaJson,
        }),
      });
      if (!res.ok) throw new Error('Schema registration failed');
      setStatus('Schema registered!');
      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const claimTemplates: Record<string, string> = {
    EmploymentCredential: JSON.stringify({ name: "John Doe", employeeId: "EMP001", employer: "Acme Corp", position: "Software Engineer", department: "Engineering", startDate: "2023-01-15", employmentStatus: "active" }, null, 2),
    EducationCredential: JSON.stringify({ name: "John Doe", institution: "MIT", degree: "Bachelor of Science", fieldOfStudy: "Computer Science", graduationDate: "2022-06-15", gpa: "3.8", registrationNumber: "REG12345" }, null, 2),
    KYCCredential: JSON.stringify({ name: "John Doe", dateOfBirth: "1995-03-15", nationality: "Indian", documentType: "Aadhaar", documentNumber: "1234-5678-9012", address: "Mumbai, India" }, null, 2),
    IncomeCredential: JSON.stringify({ name: "John Doe", annualIncome: 1200000, currency: "INR", employer: "Acme Corp", financialYear: "2025-2026" }, null, 2),
    ProfessionalCertificationCredential: JSON.stringify({ name: "John Doe", certificationName: "AWS Solutions Architect", issuingBody: "Amazon Web Services", certificationId: "AWS-SA-001", issueDate: "2024-01-01", expiryDate: "2027-01-01", domain: "Cloud Computing" }, null, 2),
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-emerald-50 to-teal-100">
      <header className="bg-white shadow-sm border-b border-emerald-200">
        <div className="max-w-5xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-emerald-600 rounded-xl flex items-center justify-center text-white font-bold text-lg">I</div>
            <div>
              <h1 className="text-xl font-bold text-gray-900">TrustVault Issuer Portal</h1>
              <p className="text-xs text-gray-500">Issue &amp; Manage Verifiable Credentials</p>
            </div>
          </div>
          {token && <span className="text-xs text-green-600 font-medium">Authenticated</span>}
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex gap-2 mb-6 flex-wrap">
          {(['auth', 'onboard', 'issue', 'credentials', 'schemas'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 rounded-lg font-medium text-sm transition ${
                tab === t ? 'bg-emerald-600 text-white shadow-sm' : 'bg-white text-gray-600 hover:bg-gray-50 border'
              }`}
            >
              {t === 'auth' ? 'Login' : t === 'onboard' ? 'Onboard Issuer' : t === 'issue' ? 'Issue Credential' : t === 'credentials' ? `Credentials (${credentials.length})` : `Schemas (${schemas.length})`}
            </button>
          ))}
        </div>

        {status && (
          <div className={`mb-4 p-3 rounded-lg text-sm ${status.startsWith('Error') ? 'bg-red-50 text-red-700 border border-red-200' : 'bg-green-50 text-green-700 border border-green-200'}`}>
            {status}
          </div>
        )}

        {tab === 'auth' && (
          <div className="bg-white rounded-xl shadow-sm border p-6 max-w-md">
            <h3 className="font-semibold text-lg mb-4">Login / Register</h3>
            <div className="space-y-3">
              <input value={username} onChange={e => setUsername(e.target.value)} placeholder="Username" className="w-full px-4 py-2 border rounded-lg text-sm" />
              <input value={password} onChange={e => setPassword(e.target.value)} type="password" placeholder="Password" className="w-full px-4 py-2 border rounded-lg text-sm" />
              <div className="flex gap-2">
                <button onClick={login} disabled={loading} className="flex-1 bg-emerald-600 text-white py-2 rounded-lg font-semibold hover:bg-emerald-700 disabled:opacity-50">Login</button>
                <button onClick={register} disabled={loading} className="flex-1 bg-gray-600 text-white py-2 rounded-lg font-semibold hover:bg-gray-700 disabled:opacity-50">Register</button>
              </div>
            </div>
          </div>
        )}

        {tab === 'onboard' && (
          <div className="space-y-4">
            <div className="bg-white rounded-xl shadow-sm border p-6">
              <h3 className="font-semibold text-lg mb-4">Onboard New Issuer</h3>
              <div className="space-y-3">
                <input value={onboardName} onChange={e => setOnboardName(e.target.value)} placeholder="Organization Name" className="w-full px-4 py-2 border rounded-lg text-sm" />
                <select value={onboardType} onChange={e => setOnboardType(e.target.value)} className="w-full px-4 py-2 border rounded-lg text-sm">
                  <option value="employer">Employer</option>
                  <option value="university">University</option>
                  <option value="government">Government</option>
                  <option value="bank">Bank</option>
                  <option value="enterprise">Enterprise</option>
                  <option value="other">Other</option>
                </select>
                <input value={onboardSchemas} onChange={e => setOnboardSchemas(e.target.value)} placeholder="Authorized Schemas (comma-separated)" className="w-full px-4 py-2 border rounded-lg text-sm" />
                <button onClick={onboard} disabled={loading || !onboardName} className="w-full bg-emerald-600 text-white py-2 rounded-lg font-semibold hover:bg-emerald-700 disabled:opacity-50">
                  {loading ? 'Onboarding...' : 'Onboard Issuer'}
                </button>
              </div>
            </div>
            {issuers.length > 0 && (
              <div className="bg-white rounded-xl shadow-sm border p-6">
                <h3 className="font-semibold text-lg mb-3">Onboarded Issuers</h3>
                {issuers.map((iss, i) => (
                  <div key={i} className="border rounded-lg p-3 mb-2">
                    <p className="font-medium">{iss.name} <span className="text-xs text-gray-500">({iss.type})</span></p>
                    <p className="text-xs text-gray-500 font-mono break-all">{iss.did}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === 'issue' && (
          <div className="space-y-4">
            <div className="bg-white rounded-xl shadow-sm border p-6">
              <h3 className="font-semibold text-lg mb-4">Issue a Credential</h3>
              <div className="space-y-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Issuer</label>
                  <select value={selectedIssuer} onChange={e => setSelectedIssuer(e.target.value)} className="w-full px-4 py-2 border rounded-lg text-sm">
                    <option value="">Auto-select first issuer</option>
                    {issuers.map(iss => (
                      <option key={iss.did} value={iss.did}>{iss.name} ({iss.did.slice(0, 30)}...)</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Schema</label>
                  <select value={schemaName} onChange={e => { setSchemaName(e.target.value); setClaimsJson(claimTemplates[e.target.value] || '{}'); }} className="w-full px-4 py-2 border rounded-lg text-sm">
                    <option value="EmploymentCredential">Employment Credential</option>
                    <option value="EducationCredential">Education Credential</option>
                    <option value="KYCCredential">KYC Credential</option>
                    <option value="IncomeCredential">Income Credential</option>
                    <option value="ProfessionalCertificationCredential">Professional Certification</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Subject DID (holder)</label>
                  <input value={subjectDid} onChange={e => setSubjectDid(e.target.value)} placeholder="did:key:z6Mk... (or leave blank)" className="w-full px-4 py-2 border rounded-lg text-sm" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Claims (JSON)</label>
                  <textarea value={claimsJson} onChange={e => setClaimsJson(e.target.value)} rows={6} className="w-full px-4 py-2 border rounded-lg text-sm font-mono" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Expiration Date (optional)</label>
                  <input type="datetime-local" value={expirationDate} onChange={e => setExpirationDate(e.target.value)} className="w-full px-4 py-2 border rounded-lg text-sm" />
                </div>
                <label className="flex items-center gap-2">
                  <input type="checkbox" checked={requirePin} onChange={e => setRequirePin(e.target.checked)} />
                  <span className="text-sm">Require PIN for claiming</span>
                </label>
                <button onClick={issueCredential} disabled={loading} className="w-full bg-emerald-600 text-white py-2 rounded-lg font-semibold hover:bg-emerald-700 disabled:opacity-50">
                  {loading ? 'Issuing...' : 'Issue Credential & Generate Offer'}
                </button>
              </div>
            </div>
            {currentOffer && (
              <div className="bg-white rounded-xl shadow-sm border p-6 border-emerald-300">
                <h3 className="font-semibold text-lg mb-3 text-emerald-700">Credential Offer</h3>
                <div className="space-y-2 text-sm">
                  <div><span className="text-gray-500">Offer ID:</span> <span className="font-mono">{currentOffer.offerId}</span></div>
                  <div><span className="text-gray-500">QR Data / Offer URL:</span> <span className="font-mono break-all text-xs">{currentOffer.qrData}</span></div>
                  {currentOffer.pin && (
                    <div className="bg-yellow-50 border border-yellow-200 rounded-lg p-3">
                      <span className="text-yellow-800 font-semibold">PIN: {currentOffer.pin}</span>
                      <p className="text-xs text-yellow-700 mt-1">Share this PIN with the holder separately</p>
                    </div>
                  )}
                  <div><span className="text-gray-500">Expires:</span> {currentOffer.expiresAt}</div>
                </div>
              </div>
            )}
          </div>
        )}

        {tab === 'credentials' && (
          <div className="space-y-3">
            <div className="flex justify-between items-center">
              <h3 className="font-semibold text-lg">Issued Credentials</h3>
              <button onClick={loadData} className="text-sm text-emerald-600 hover:text-emerald-700">Refresh</button>
            </div>
            {credentials.length === 0 ? (
              <div className="text-center py-12 bg-white rounded-xl border border-dashed border-gray-300">
                <p className="text-gray-500">No credentials issued yet.</p>
              </div>
            ) : (
              credentials.map((cred, i) => (
                <div key={i} className="bg-white rounded-xl shadow-sm border p-4">
                  <div className="flex items-start justify-between">
                    <div>
                      <span className={`inline-block text-xs font-semibold px-2 py-1 rounded-full ${
                        cred.status === 'active' ? 'bg-green-100 text-green-800' :
                        cred.status === 'revoked' ? 'bg-red-100 text-red-800' :
                        'bg-yellow-100 text-yellow-800'
                      }`}>{cred.status}</span>
                      <span className="ml-2 text-sm font-medium">{cred.schemaName}</span>
                    </div>
                    <div className="flex gap-1">
                      <button onClick={() => generateOffer(cred.id)} className="text-xs bg-blue-50 text-blue-700 px-2 py-1 rounded hover:bg-blue-100">Offer</button>
                      {cred.status === 'active' && (
                        <>
                          <button onClick={() => suspendCredential(cred.id)} className="text-xs bg-yellow-50 text-yellow-700 px-2 py-1 rounded hover:bg-yellow-100">Suspend</button>
                          <button onClick={() => revokeCredential(cred.id)} className="text-xs bg-red-50 text-red-700 px-2 py-1 rounded hover:bg-red-100">Revoke</button>
                        </>
                      )}
                      {cred.status === 'suspended' && (
                        <button onClick={() => activateCredential(cred.id)} className="text-xs bg-green-50 text-green-700 px-2 py-1 rounded hover:bg-green-100">Activate</button>
                      )}
                    </div>
                  </div>
                  <p className="text-xs text-gray-500 font-mono mt-1">{cred.id}</p>
                  <div className="grid grid-cols-2 gap-1 mt-2">
                    {Object.entries(cred.claims).filter(([k]) => k !== 'id').map(([k, v]) => (
                      <div key={k} className="text-xs"><span className="text-gray-500">{k}:</span> <span className="font-medium">{String(v)}</span></div>
                    ))}
                  </div>
                </div>
              ))
            )}
          </div>
        )}

        {tab === 'schemas' && (
          <div className="space-y-4">
            <div className="bg-white rounded-xl shadow-sm border p-6">
              <h3 className="font-semibold text-lg mb-4">Register New Schema</h3>
              <div className="space-y-3">
                <input value={newSchemaName} onChange={e => setNewSchemaName(e.target.value)} placeholder="Schema Name (e.g. EmploymentCredential)" className="w-full px-4 py-2 border rounded-lg text-sm" />
                <input value={newSchemaVersion} onChange={e => setNewSchemaVersion(e.target.value)} placeholder="Version" className="w-full px-4 py-2 border rounded-lg text-sm" />
                <input value={newSchemaDesc} onChange={e => setNewSchemaDesc(e.target.value)} placeholder="Description" className="w-full px-4 py-2 border rounded-lg text-sm" />
                <textarea value={newSchemaJson} onChange={e => setNewSchemaJson(e.target.value)} rows={4} placeholder="JSON Schema" className="w-full px-4 py-2 border rounded-lg text-sm font-mono" />
                <button onClick={registerSchema} disabled={loading || !newSchemaName} className="w-full bg-emerald-600 text-white py-2 rounded-lg font-semibold hover:bg-emerald-700 disabled:opacity-50">Register Schema</button>
              </div>
            </div>
            <div className="bg-white rounded-xl shadow-sm border p-6">
              <h3 className="font-semibold text-lg mb-3">Registered Schemas</h3>
              {schemas.length === 0 ? (
                <p className="text-gray-500 text-sm">No schemas registered yet.</p>
              ) : (
                schemas.map((s, i) => (
                  <div key={i} className="border rounded-lg p-3 mb-2">
                    <p className="font-medium">{s.name} <span className="text-xs text-gray-500">v{s.version}</span></p>
                    {s.description && <p className="text-xs text-gray-500">{s.description}</p>}
                  </div>
                ))
              )}
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
