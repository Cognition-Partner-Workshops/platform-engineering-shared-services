import { useState, useEffect } from 'react';

const VERIFIER_URL = 'http://localhost:3003';
const TRUST_REGISTRY_URL = 'http://localhost:3001';

interface Policy {
  id: string;
  name: string;
  acceptedSchemas: string[];
  trustedRegistries: string[];
  trustedIssuers: string | string[];
  checkRevocation: boolean;
  requireNonExpired: boolean;
}

interface Session {
  id: string;
  policyId: string;
  purpose: string;
  nonce: string;
  status: string;
  decision: string | null;
  failReason: string | null;
  holderDid: string | null;
  credentialsVerified: number | null;
  createdAt: string;
  updatedAt: string;
}

interface AuditEntry {
  sessionId: string;
  holderDid: string | null;
  decision: string | null;
  policyId: string;
  purpose: string;
  failReason: string | null;
  credentialsVerified: number | null;
  verifiedAt: string | null;
}

interface CheckEntry {
  checkName: string;
  passed: boolean | null;
  detail: string;
  timestamp: string | null;
}

function App() {
  const [tab, setTab] = useState<'auth' | 'policies' | 'verify' | 'sessions' | 'audit'>('auth');
  const [token, setToken] = useState('');
  const [status, setStatus] = useState('');
  const [loading, setLoading] = useState(false);

  // Auth
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  // Policies
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [policyName, setPolicyName] = useState('');
  const [acceptedSchemas, setAcceptedSchemas] = useState('EmploymentCredential,EducationCredential');
  const [trustedRegistries, setTrustedRegistries] = useState('http://localhost:3001');
  const [trustedIssuers, setTrustedIssuers] = useState('*');
  const [checkRevocation, setCheckRevocation] = useState(true);
  const [requireNonExpired, setRequireNonExpired] = useState(true);

  // Verification
  const [selectedPolicy, setSelectedPolicy] = useState('');
  const [purpose, setPurpose] = useState('Employment/Education Verification');
  const [currentSession, setCurrentSession] = useState<{ sessionId: string; qrData: string; requestUrl: string } | null>(null);
  const [sessionResult, setSessionResult] = useState<{ decision: string; checks: CheckEntry[]; failReason?: string } | null>(null);

  // Sessions & Audit
  const [sessions, setSessions] = useState<Session[]>([]);
  const [auditItems, setAuditItems] = useState<AuditEntry[]>([]);
  const [auditDetail, setAuditDetail] = useState<{ sessionId: string; checks: CheckEntry[] } | null>(null);

  const login = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${VERIFIER_URL}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!res.ok) throw new Error('Login failed');
      const data = await res.json();
      setToken(data.token);
      setStatus('Logged in!');
      setTab('policies');
      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const register = async () => {
    setLoading(true);
    try {
      const res = await fetch(`${VERIFIER_URL}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password, role: 'verifier' }),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.error || 'Registration failed');
      }
      const data = await res.json();
      setToken(data.token);
      setStatus('Registered and logged in!');
      setTab('policies');
      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const loadData = async () => {
    try {
      const [policiesRes, sessionsRes, auditRes] = await Promise.all([
        fetch(`${VERIFIER_URL}/policies`),
        fetch(`${VERIFIER_URL}/presentations/sessions`),
        fetch(`${VERIFIER_URL}/audit?limit=50`),
      ]);
      if (policiesRes.ok) setPolicies(await policiesRes.json());
      if (sessionsRes.ok) setSessions(await sessionsRes.json());
      if (auditRes.ok) {
        const data = await auditRes.json();
        setAuditItems(data.items || []);
      }
    } catch {}
  };

  useEffect(() => { loadData(); }, []);

  const createPolicy = async () => {
    setLoading(true);
    setStatus('');
    try {
      const issuers = trustedIssuers.trim() === '*' ? '*' : trustedIssuers.split(',').map(s => s.trim());
      const res = await fetch(`${VERIFIER_URL}/policies`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: policyName,
          acceptedSchemas: acceptedSchemas.split(',').map(s => s.trim()),
          trustedRegistries: trustedRegistries.split(',').map(s => s.trim()),
          trustedIssuers: issuers,
          checkRevocation,
          requireNonExpired,
        }),
      });
      if (!res.ok) throw new Error('Policy creation failed');
      setStatus('Policy created!');
      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const createVerificationRequest = async () => {
    setLoading(true);
    setStatus('');
    setCurrentSession(null);
    setSessionResult(null);
    try {
      if (!selectedPolicy) throw new Error('Select a policy');
      const res = await fetch(`${VERIFIER_URL}/presentations/request`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ policyId: selectedPolicy, purpose }),
      });
      if (!res.ok) throw new Error('Failed to create request');
      const data = await res.json();
      setCurrentSession(data);
      setStatus(`Session created: ${data.sessionId}`);
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const pollSessionStatus = async () => {
    if (!currentSession) return;
    setLoading(true);
    try {
      const res = await fetch(`${VERIFIER_URL}/presentations/status/${currentSession.sessionId}`);
      if (!res.ok) throw new Error('Failed to get status');
      const data = await res.json();
      if (data.decision) {
        // Get detailed audit
        const auditRes = await fetch(`${VERIFIER_URL}/audit/${currentSession.sessionId}`);
        if (auditRes.ok) {
          const audit = await auditRes.json();
          setSessionResult({ decision: data.decision, checks: audit.checks, failReason: data.failReason });
        }
        setStatus(`Decision: ${data.decision}${data.failReason ? ` — ${data.failReason}` : ''}`);
      } else {
        setStatus(`Status: ${data.status} — waiting for wallet submission...`);
      }
      loadData();
    } catch (e: any) {
      setStatus(`Error: ${e.message}`);
    }
    setLoading(false);
  };

  const viewAuditDetail = async (sessionId: string) => {
    try {
      const res = await fetch(`${VERIFIER_URL}/audit/${sessionId}`);
      if (res.ok) {
        const data = await res.json();
        setAuditDetail({ sessionId, checks: data.checks || [] });
      }
    } catch {}
  };

  const deletePolicy = async (id: string) => {
    const res = await fetch(`${VERIFIER_URL}/policies/${id}`, { method: 'DELETE' });
    if (res.ok) {
      setStatus('Policy deleted');
      loadData();
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-purple-50 to-violet-100">
      <header className="bg-white shadow-sm border-b border-purple-200">
        <div className="max-w-5xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 bg-purple-600 rounded-xl flex items-center justify-center text-white font-bold text-lg">V</div>
            <div>
              <h1 className="text-xl font-bold text-gray-900">TrustVault Verifier Portal</h1>
              <p className="text-xs text-gray-500">Verify Credentials &amp; Manage Trust Policies</p>
            </div>
          </div>
          {token && <span className="text-xs text-green-600 font-medium">Authenticated</span>}
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-6">
        <div className="flex gap-2 mb-6 flex-wrap">
          {(['auth', 'policies', 'verify', 'sessions', 'audit'] as const).map(t => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-4 py-2 rounded-lg font-medium text-sm transition ${
                tab === t ? 'bg-purple-600 text-white shadow-sm' : 'bg-white text-gray-600 hover:bg-gray-50 border'
              }`}
            >
              {t === 'auth' ? 'Login' : t === 'policies' ? `Policies (${policies.length})` : t === 'verify' ? 'Verify' : t === 'sessions' ? `Sessions (${sessions.length})` : 'Audit Trail'}
            </button>
          ))}
        </div>

        {status && (
          <div className={`mb-4 p-3 rounded-lg text-sm ${
            status.startsWith('Error') ? 'bg-red-50 text-red-700 border border-red-200' :
            status.includes('accepted') ? 'bg-green-50 text-green-700 border border-green-200' :
            status.includes('rejected') ? 'bg-red-50 text-red-700 border border-red-200' :
            'bg-blue-50 text-blue-700 border border-blue-200'
          }`}>
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
                <button onClick={login} disabled={loading} className="flex-1 bg-purple-600 text-white py-2 rounded-lg font-semibold hover:bg-purple-700 disabled:opacity-50">Login</button>
                <button onClick={register} disabled={loading} className="flex-1 bg-gray-600 text-white py-2 rounded-lg font-semibold hover:bg-gray-700 disabled:opacity-50">Register</button>
              </div>
            </div>
          </div>
        )}

        {tab === 'policies' && (
          <div className="space-y-4">
            <div className="bg-white rounded-xl shadow-sm border p-6">
              <h3 className="font-semibold text-lg mb-4">Create Trust Policy</h3>
              <div className="space-y-3">
                <input value={policyName} onChange={e => setPolicyName(e.target.value)} placeholder="Policy Name" className="w-full px-4 py-2 border rounded-lg text-sm" />
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Accepted Schemas (comma-separated)</label>
                  <input value={acceptedSchemas} onChange={e => setAcceptedSchemas(e.target.value)} className="w-full px-4 py-2 border rounded-lg text-sm" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Trusted Registries (comma-separated)</label>
                  <input value={trustedRegistries} onChange={e => setTrustedRegistries(e.target.value)} className="w-full px-4 py-2 border rounded-lg text-sm" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Trusted Issuers (* = all, or comma-separated DIDs)</label>
                  <input value={trustedIssuers} onChange={e => setTrustedIssuers(e.target.value)} className="w-full px-4 py-2 border rounded-lg text-sm" />
                </div>
                <div className="flex gap-4">
                  <label className="flex items-center gap-2">
                    <input type="checkbox" checked={checkRevocation} onChange={e => setCheckRevocation(e.target.checked)} />
                    <span className="text-sm">Check Revocation</span>
                  </label>
                  <label className="flex items-center gap-2">
                    <input type="checkbox" checked={requireNonExpired} onChange={e => setRequireNonExpired(e.target.checked)} />
                    <span className="text-sm">Require Non-Expired</span>
                  </label>
                </div>
                <button onClick={createPolicy} disabled={loading || !policyName} className="w-full bg-purple-600 text-white py-2 rounded-lg font-semibold hover:bg-purple-700 disabled:opacity-50">Create Policy</button>
              </div>
            </div>
            {policies.length > 0 && (
              <div className="bg-white rounded-xl shadow-sm border p-6">
                <h3 className="font-semibold text-lg mb-3">Existing Policies</h3>
                {policies.map((p, i) => (
                  <div key={i} className="border rounded-lg p-3 mb-2">
                    <div className="flex justify-between items-start">
                      <div>
                        <p className="font-medium">{p.name}</p>
                        <p className="text-xs text-gray-500">Schemas: {p.acceptedSchemas.join(', ')}</p>
                        <p className="text-xs text-gray-500">Issuers: {typeof p.trustedIssuers === 'string' ? p.trustedIssuers : p.trustedIssuers.join(', ')}</p>
                        <p className="text-xs text-gray-500">Revocation: {p.checkRevocation ? 'Yes' : 'No'} | Expiry: {p.requireNonExpired ? 'Yes' : 'No'}</p>
                      </div>
                      <div className="flex gap-1">
                        <button onClick={() => { setSelectedPolicy(p.id); setTab('verify'); }} className="text-xs bg-purple-50 text-purple-700 px-2 py-1 rounded hover:bg-purple-100">Use</button>
                        <button onClick={() => deletePolicy(p.id)} className="text-xs bg-red-50 text-red-700 px-2 py-1 rounded hover:bg-red-100">Delete</button>
                      </div>
                    </div>
                    <p className="text-xs text-gray-400 font-mono mt-1">ID: {p.id}</p>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {tab === 'verify' && (
          <div className="space-y-4">
            <div className="bg-white rounded-xl shadow-sm border p-6">
              <h3 className="font-semibold text-lg mb-4">Create Verification Request</h3>
              <div className="space-y-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Policy</label>
                  <select value={selectedPolicy} onChange={e => setSelectedPolicy(e.target.value)} className="w-full px-4 py-2 border rounded-lg text-sm">
                    <option value="">Select a policy</option>
                    {policies.map(p => (
                      <option key={p.id} value={p.id}>{p.name} ({p.acceptedSchemas.join(', ')})</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Purpose</label>
                  <input value={purpose} onChange={e => setPurpose(e.target.value)} className="w-full px-4 py-2 border rounded-lg text-sm" />
                </div>
                <button onClick={createVerificationRequest} disabled={loading || !selectedPolicy} className="w-full bg-purple-600 text-white py-2 rounded-lg font-semibold hover:bg-purple-700 disabled:opacity-50">
                  Create Verification Request
                </button>
              </div>
            </div>

            {currentSession && (
              <div className="bg-white rounded-xl shadow-sm border p-6 border-purple-300">
                <h3 className="font-semibold text-lg mb-3 text-purple-700">Active Verification Session</h3>
                <div className="space-y-2 text-sm">
                  <div><span className="text-gray-500">Session ID:</span> <span className="font-mono font-bold">{currentSession.sessionId}</span></div>
                  <div><span className="text-gray-500">QR Data:</span> <span className="font-mono text-xs break-all">{currentSession.qrData}</span></div>
                  <p className="text-xs text-gray-500 mt-2">Share the Session ID with the wallet holder, then click "Check Status" below.</p>
                  <button onClick={pollSessionStatus} disabled={loading} className="w-full bg-purple-100 text-purple-700 py-2 rounded-lg font-semibold hover:bg-purple-200 disabled:opacity-50 mt-2">
                    {loading ? 'Checking...' : 'Check Status'}
                  </button>
                </div>
              </div>
            )}

            {sessionResult && (
              <div className={`rounded-xl p-4 ${
                sessionResult.decision === 'accepted' ? 'bg-green-50 border border-green-200' : 'bg-red-50 border border-red-200'
              }`}>
                <h4 className="font-semibold mb-2 text-lg">
                  {sessionResult.decision === 'accepted' ? 'ACCEPTED' : 'REJECTED'}
                </h4>
                {sessionResult.failReason && <p className="text-sm text-red-700 mb-3">{sessionResult.failReason}</p>}
                <div className="space-y-1">
                  {sessionResult.checks.map((check, i) => (
                    <div key={i} className="flex items-center gap-2 text-sm py-1 border-t border-gray-100">
                      <span className={`w-12 text-center text-xs font-bold rounded px-1 py-0.5 ${
                        check.passed === true ? 'bg-green-200 text-green-800' :
                        check.passed === false ? 'bg-red-200 text-red-800' :
                        'bg-gray-200 text-gray-600'
                      }`}>
                        {check.passed === true ? 'PASS' : check.passed === false ? 'FAIL' : 'SKIP'}
                      </span>
                      <span className="font-medium text-gray-700">{check.checkName}</span>
                      <span className="text-gray-500 text-xs">{check.detail}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {tab === 'sessions' && (
          <div className="space-y-3">
            <div className="flex justify-between items-center">
              <h3 className="font-semibold text-lg">Verification Sessions</h3>
              <button onClick={loadData} className="text-sm text-purple-600 hover:text-purple-700">Refresh</button>
            </div>
            {sessions.length === 0 ? (
              <div className="text-center py-12 bg-white rounded-xl border border-dashed border-gray-300">
                <p className="text-gray-500">No verification sessions yet.</p>
              </div>
            ) : (
              sessions.map((s, i) => (
                <div key={i} className="bg-white rounded-xl shadow-sm border p-4">
                  <div className="flex items-start justify-between">
                    <div>
                      <span className={`inline-block text-xs font-semibold px-2 py-1 rounded-full ${
                        s.decision === 'accepted' ? 'bg-green-100 text-green-800' :
                        s.decision === 'rejected' ? 'bg-red-100 text-red-800' :
                        s.status === 'pending' ? 'bg-yellow-100 text-yellow-800' :
                        'bg-blue-100 text-blue-800'
                      }`}>{s.decision || s.status}</span>
                      <span className="ml-2 text-sm">{s.purpose}</span>
                    </div>
                    <button onClick={() => viewAuditDetail(s.id)} className="text-xs bg-gray-50 text-gray-700 px-2 py-1 rounded hover:bg-gray-100">Details</button>
                  </div>
                  <p className="text-xs text-gray-500 font-mono mt-1">Session: {s.id}</p>
                  {s.holderDid && <p className="text-xs text-gray-500">Holder: {s.holderDid.slice(0, 40)}...</p>}
                  {s.failReason && <p className="text-xs text-red-600 mt-1">{s.failReason}</p>}
                  <p className="text-xs text-gray-400 mt-1">{s.createdAt}</p>
                </div>
              ))
            )}
          </div>
        )}

        {tab === 'audit' && (
          <div className="space-y-4">
            <div className="flex justify-between items-center">
              <h3 className="font-semibold text-lg">Audit Trail</h3>
              <button onClick={loadData} className="text-sm text-purple-600 hover:text-purple-700">Refresh</button>
            </div>
            <div className="bg-white rounded-xl shadow-sm border overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-gray-50 border-b">
                  <tr>
                    <th className="px-4 py-2 text-left text-gray-600">Session</th>
                    <th className="px-4 py-2 text-left text-gray-600">Decision</th>
                    <th className="px-4 py-2 text-left text-gray-600">Purpose</th>
                    <th className="px-4 py-2 text-left text-gray-600">VCs</th>
                    <th className="px-4 py-2 text-left text-gray-600">Time</th>
                    <th className="px-4 py-2"></th>
                  </tr>
                </thead>
                <tbody>
                  {auditItems.map((a, i) => (
                    <tr key={i} className="border-b hover:bg-gray-50">
                      <td className="px-4 py-2 font-mono text-xs">{a.sessionId.slice(0, 8)}...</td>
                      <td className="px-4 py-2">
                        <span className={`text-xs font-semibold px-2 py-0.5 rounded-full ${
                          a.decision === 'accepted' ? 'bg-green-100 text-green-800' :
                          a.decision === 'rejected' ? 'bg-red-100 text-red-800' :
                          'bg-gray-100 text-gray-600'
                        }`}>{a.decision || 'pending'}</span>
                      </td>
                      <td className="px-4 py-2 text-gray-700">{a.purpose}</td>
                      <td className="px-4 py-2 text-gray-700">{a.credentialsVerified ?? '-'}</td>
                      <td className="px-4 py-2 text-xs text-gray-500">{a.verifiedAt ? new Date(a.verifiedAt).toLocaleString() : '-'}</td>
                      <td className="px-4 py-2">
                        <button onClick={() => viewAuditDetail(a.sessionId)} className="text-xs text-purple-600 hover:text-purple-700">View</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {auditDetail && (
              <div className="bg-white rounded-xl shadow-sm border p-6">
                <h4 className="font-semibold mb-3">Session: {auditDetail.sessionId}</h4>
                <div className="space-y-1">
                  {auditDetail.checks.map((check, i) => (
                    <div key={i} className="flex items-center gap-2 text-sm py-1 border-t border-gray-100">
                      <span className={`w-12 text-center text-xs font-bold rounded px-1 py-0.5 ${
                        check.passed === true ? 'bg-green-200 text-green-800' :
                        check.passed === false ? 'bg-red-200 text-red-800' :
                        'bg-gray-200 text-gray-600'
                      }`}>
                        {check.passed === true ? 'PASS' : check.passed === false ? 'FAIL' : 'SKIP'}
                      </span>
                      <span className="font-medium text-gray-700">{check.checkName}</span>
                      <span className="text-gray-500 text-xs">{check.detail}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
