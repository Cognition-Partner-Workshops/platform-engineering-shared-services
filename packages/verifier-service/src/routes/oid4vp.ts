import { type FastifyInstance } from 'fastify';
import { v4 as uuid } from 'uuid';
import { eq } from 'drizzle-orm';
import { randomBytes } from 'node:crypto';
import {
  verifyPresentationJwt,
  verifyCredentialJwt,
  decodeCredentialJwt,
  decodePresentationJwt,
  StatusList,
} from '@trustvault/shared';
import { db, schema } from '../db/index.js';

const VERIFIER_URL = process.env.VERIFIER_SERVICE_URL || 'http://localhost:3003';

function addLog(sessionId: string, checkName: string, passed: boolean | null, detail: string) {
  db.insert(schema.verificationLogs).values({
    id: uuid(),
    sessionId,
    checkName,
    passed,
    detail,
    timestamp: Date.now(),
  }).run();
}

export async function oid4vpRoutes(fastify: FastifyInstance) {
  // Create a presentation request (verifier initiates)
  fastify.post('/presentations/request', async (req, reply) => {
    const body = req.body as { policyId: string; purpose?: string };
    if (!body.policyId) {
      return reply.status(400).send({ error: 'policyId is required' });
    }

    const policy = db.select().from(schema.trustPolicies).where(eq(schema.trustPolicies.id, body.policyId)).get();
    if (!policy) {
      return reply.status(404).send({ error: 'Policy not found' });
    }

    const sessionId = uuid();
    const nonce = randomBytes(16).toString('base64url');
    const now = Date.now();

    db.insert(schema.verificationSessions).values({
      id: sessionId,
      policyId: body.policyId,
      purpose: body.purpose || 'Verification Request',
      nonce,
      status: 'pending',
      createdAt: now,
      updatedAt: now,
    }).run();

    const acceptedSchemas = JSON.parse(policy.acceptedSchemas) as string[];

    return {
      sessionId,
      requestUrl: `${VERIFIER_URL}/oid4vp/request/${sessionId}`,
      qrData: `openid4vp://?request_uri=${encodeURIComponent(`${VERIFIER_URL}/oid4vp/request/${sessionId}`)}`,
      purpose: body.purpose || 'Verification Request',
      requestedSchemas: acceptedSchemas,
      nonce,
      expiresAt: new Date(now + 10 * 60 * 1000).toISOString(),
    };
  });

  // OID4VP Authorization Request (wallet fetches this)
  fastify.get<{ Params: { sessionId: string } }>('/oid4vp/request/:sessionId', async (req, reply) => {
    const { sessionId } = req.params;
    const session = db.select().from(schema.verificationSessions).where(eq(schema.verificationSessions.id, sessionId)).get();
    if (!session) {
      return reply.status(404).send({ error: 'Session not found' });
    }

    const policy = db.select().from(schema.trustPolicies).where(eq(schema.trustPolicies.id, session.policyId)).get();
    if (!policy) {
      return reply.status(404).send({ error: 'Policy not found' });
    }

    const acceptedSchemas = JSON.parse(policy.acceptedSchemas) as string[];

    return {
      response_type: 'vp_token',
      client_id: VERIFIER_URL,
      response_uri: `${VERIFIER_URL}/oid4vp/response`,
      nonce: session.nonce,
      presentation_definition: {
        id: sessionId,
        input_descriptors: acceptedSchemas.map((s, i) => ({
          id: `descriptor_${i}`,
          name: s,
          constraints: {
            fields: [
              {
                path: ['$.type'],
                filter: { type: 'array', contains: { const: s } },
              },
            ],
          },
        })),
      },
      state: sessionId,
    };
  });

  // Wallet polls this to check request status and metadata
  fastify.get<{ Params: { sessionId: string } }>('/presentations/request/:sessionId', async (req, reply) => {
    const { sessionId } = req.params;
    const session = db.select().from(schema.verificationSessions).where(eq(schema.verificationSessions.id, sessionId)).get();
    if (!session) {
      return reply.status(404).send({ error: 'Session not found' });
    }

    const policy = db.select().from(schema.trustPolicies).where(eq(schema.trustPolicies.id, session.policyId)).get();
    const acceptedSchemas = policy ? JSON.parse(policy.acceptedSchemas) : [];

    return {
      sessionId,
      status: session.status,
      requestedSchemas: acceptedSchemas,
      verifierName: policy?.name || 'Unknown',
      purpose: session.purpose,
      nonce: session.nonce,
      decision: session.decision,
      failReason: session.failReason,
      expiresAt: new Date(session.createdAt + 10 * 60 * 1000).toISOString(),
    };
  });

  // OID4VP Response (wallet submits VP here)
  fastify.post('/oid4vp/response', async (req, reply) => {
    const body = req.body as { vp_token: string; state: string; presentation_submission?: unknown };
    if (!body.vp_token || !body.state) {
      return reply.status(400).send({ error: 'Missing vp_token or state' });
    }

    const sessionId = body.state;
    return processVpSubmission(sessionId, body.vp_token, reply);
  });

  // Alias for wallet same-device flow
  fastify.post('/presentations/submit', async (req, reply) => {
    const body = req.body as { vp_token: string; sessionId: string };
    if (!body.vp_token || !body.sessionId) {
      return reply.status(400).send({ error: 'Missing vp_token or sessionId' });
    }

    return processVpSubmission(body.sessionId, body.vp_token, reply);
  });

  // Get session status
  fastify.get<{ Params: { sessionId: string } }>('/presentations/status/:sessionId', async (req, reply) => {
    const { sessionId } = req.params;
    const session = db.select().from(schema.verificationSessions).where(eq(schema.verificationSessions.id, sessionId)).get();
    if (!session) {
      return reply.status(404).send({ error: 'Session not found' });
    }

    return {
      sessionId,
      status: session.status,
      decision: session.decision,
      failReason: session.failReason,
      credentialsVerified: session.credentialsVerified,
      holderDid: session.holderDid,
    };
  });

  // List all sessions
  fastify.get('/presentations/sessions', async (_req, _reply) => {
    const all = db.select().from(schema.verificationSessions).all();
    return all.map(s => ({
      ...s,
      createdAt: new Date(s.createdAt).toISOString(),
      updatedAt: new Date(s.updatedAt).toISOString(),
    }));
  });
}

async function processVpSubmission(sessionId: string, vpToken: string, reply: any) {
  const session = db.select().from(schema.verificationSessions).where(eq(schema.verificationSessions.id, sessionId)).get();
  if (!session) {
    return reply.status(404).send({ error: 'Session not found' });
  }

  // Mark as received
  db.update(schema.verificationSessions)
    .set({ status: 'received', vpJwt: vpToken, updatedAt: Date.now() })
    .where(eq(schema.verificationSessions.id, sessionId))
    .run();

  const policy = db.select().from(schema.trustPolicies).where(eq(schema.trustPolicies.id, session.policyId)).get();
  if (!policy) {
    return reply.status(404).send({ error: 'Policy not found' });
  }

  const acceptedSchemas = JSON.parse(policy.acceptedSchemas) as string[];
  const trustedRegistries = JSON.parse(policy.trustedRegistries) as string[];
  const trustedIssuers = policy.trustedIssuers;

  // Step 1: Verify VP signature
  const vpResult = await verifyPresentationJwt(vpToken);
  if (!vpResult.valid) {
    addLog(sessionId, 'vp_signature', false, `VP signature invalid: ${vpResult.error}`);
    return finishSession(sessionId, 'rejected', `VP signature invalid: ${vpResult.error}`, 0, null);
  }
  addLog(sessionId, 'vp_signature', true, 'VP signature valid');

  const vpPayload = vpResult.payload!;
  const holderDid = vpPayload.iss as string;
  const vpData = vpPayload.vp as { verifiableCredential: string[] };
  const vcJwts = vpData?.verifiableCredential || [];

  if (vcJwts.length === 0) {
    addLog(sessionId, 'vc_count', false, 'No credentials in presentation');
    return finishSession(sessionId, 'rejected', 'No credentials in presentation', 0, holderDid);
  }

  let credentialsVerified = 0;

  // Step 2: Verify each VC
  for (const vcJwt of vcJwts) {
    // Step 2a: Verify VC signature
    const vcResult = await verifyCredentialJwt(vcJwt);
    if (!vcResult.valid) {
      addLog(sessionId, 'vc_signature', false, `VC signature invalid: ${vcResult.error}`);
      return finishSession(sessionId, 'rejected', `VC signature invalid: ${vcResult.error}`, credentialsVerified, holderDid);
    }

    const vcPayload = vcResult.payload!;
    const vc = vcPayload.vc as Record<string, unknown>;
    const issuerDid = vcPayload.iss as string;
    const vcTypes = (vc.type as string[]) || [];
    const schemaType = vcTypes.find(t => t !== 'VerifiableCredential') || 'Unknown';

    addLog(sessionId, 'vc_signature', true, `VC signature valid (${schemaType})`);

    // Step 2b: Schema check
    const schemaMatch = acceptedSchemas.some(s => vcTypes.includes(s));
    if (!schemaMatch) {
      addLog(sessionId, 'schema_check', false, `Schema ${schemaType} not in accepted schemas: ${acceptedSchemas.join(', ')}`);
      return finishSession(sessionId, 'rejected', `Schema ${schemaType} not accepted by policy`, credentialsVerified, holderDid);
    }
    addLog(sessionId, 'schema_check', true, `Schema ${schemaType} accepted`);

    // Step 2c: Trust registry check
    let issuerTrusted = false;
    for (const registryUrl of trustedRegistries) {
      try {
        const res = await fetch(`${registryUrl}/registry/verify/${encodeURIComponent(issuerDid)}`);
        if (res.ok) {
          const data = await res.json() as { trusted: boolean };
          if (data.trusted) {
            issuerTrusted = true;
            break;
          }
        }
      } catch {
        // Registry unreachable
      }
    }

    if (!issuerTrusted) {
      addLog(sessionId, 'trust_registry', false, `Issuer ${issuerDid} not found in trust registry`);
      return finishSession(sessionId, 'rejected', `Issuer ${issuerDid} not found in trust registry`, credentialsVerified, holderDid);
    }
    addLog(sessionId, 'trust_registry', true, `Issuer ${issuerDid} is trusted`);

    // Step 2d: Issuer allowlist check
    if (trustedIssuers !== '*') {
      const allowedIssuers = JSON.parse(trustedIssuers) as string[];
      if (!allowedIssuers.includes(issuerDid)) {
        addLog(sessionId, 'issuer_allowlist', false, `Issuer ${issuerDid} not in policy's explicit issuer allowlist`);
        return finishSession(sessionId, 'rejected', `Issuer ${issuerDid} not in policy's explicit issuer allowlist`, credentialsVerified, holderDid);
      }
      addLog(sessionId, 'issuer_allowlist', true, `Issuer ${issuerDid} in allowlist`);
    } else {
      addLog(sessionId, 'issuer_allowlist', true, 'Wildcard (*) — all registry-listed issuers accepted');
    }

    // Step 2e: Expiry check (conditional)
    if (policy.requireNonExpired) {
      const exp = vcPayload.exp as number | undefined;
      if (exp && Date.now() / 1000 > exp) {
        addLog(sessionId, 'expiry_check', false, `Credential expired at ${new Date(exp * 1000).toISOString()}`);
        return finishSession(sessionId, 'rejected', 'Credential has expired', credentialsVerified, holderDid);
      }
      addLog(sessionId, 'expiry_check', true, exp ? `Valid until ${new Date(exp * 1000).toISOString()}` : 'No expiry set');
    } else {
      addLog(sessionId, 'expiry_check', null, 'Expiry check disabled by policy');
    }

    // Step 2f: Revocation check (conditional)
    if (policy.checkRevocation) {
      const credStatus = (vc.credentialStatus as Record<string, string>) || null;
      if (credStatus && credStatus.statusListCredential) {
        try {
          const slRes = await fetch(credStatus.statusListCredential);
          if (slRes.ok) {
            const slData = await slRes.json() as { credentialSubject: { encodedList: string } };
            const sl = StatusList.fromEncoded(slData.credentialSubject.encodedList);
            const index = parseInt(credStatus.statusListIndex, 10);
            const isRevoked = sl.getStatus(index);
            if (isRevoked) {
              addLog(sessionId, 'revocation_check', false, `Credential at index ${index} is revoked`);
              return finishSession(sessionId, 'rejected', 'Credential has been revoked', credentialsVerified, holderDid);
            }
            addLog(sessionId, 'revocation_check', true, 'Credential is not revoked');
          } else {
            addLog(sessionId, 'revocation_check', null, 'Status list unavailable — skipped');
          }
        } catch {
          addLog(sessionId, 'revocation_check', null, 'Status list unreachable — skipped');
        }
      } else {
        addLog(sessionId, 'revocation_check', null, 'No credentialStatus in VC — skipped');
      }
    } else {
      addLog(sessionId, 'revocation_check', null, 'Revocation check disabled by policy');
    }

    credentialsVerified++;
  }

  // All checks passed
  return finishSession(sessionId, 'accepted', null, credentialsVerified, holderDid);
}

function finishSession(sessionId: string, decision: string, failReason: string | null, credentialsVerified: number, holderDid: string | null) {
  const status = decision === 'accepted' ? 'verified' : 'rejected';
  db.update(schema.verificationSessions)
    .set({
      status,
      decision,
      failReason,
      credentialsVerified,
      holderDid,
      updatedAt: Date.now(),
    })
    .where(eq(schema.verificationSessions.id, sessionId))
    .run();

  const session = db.select().from(schema.verificationSessions).where(eq(schema.verificationSessions.id, sessionId)).get();
  const logs = db.select().from(schema.verificationLogs).where(eq(schema.verificationLogs.sessionId, sessionId)).all();

  return {
    sessionId,
    decision,
    failReason,
    credentialsVerified,
    holderDid,
    checks: logs.map(l => ({
      checkName: l.checkName,
      passed: l.passed,
      detail: l.detail,
      timestamp: l.timestamp ? new Date(l.timestamp).toISOString() : null,
    })),
  };
}
