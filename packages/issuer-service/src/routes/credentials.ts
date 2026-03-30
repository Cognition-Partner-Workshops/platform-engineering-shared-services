import { type FastifyInstance } from 'fastify';
import { v4 as uuid } from 'uuid';
import { eq } from 'drizzle-orm';
import {
  createVerifiableCredential,
  CreateCredentialSchema,
  StatusList,
} from '@trustvault/shared';
import { db, schema } from '../db/index.js';

export async function credentialRoutes(fastify: FastifyInstance) {
  // Issue a credential
  fastify.post('/credentials', async (req, reply) => {
    const parsed = CreateCredentialSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: 'Invalid input', details: parsed.error.flatten() });
    }
    const { schemaName, subjectDid, claims, expirationDate } = parsed.data;

    // Get first issuer or specified one
    const issuerDid = (req.body as Record<string, string>).issuerDid;
    let keyRecord;
    if (issuerDid) {
      keyRecord = db.select().from(schema.issuerKeys).where(eq(schema.issuerKeys.issuerDid, issuerDid)).get();
    } else {
      keyRecord = db.select().from(schema.issuerKeys).limit(1).get();
    }

    if (!keyRecord) {
      return reply.status(400).send({ error: 'No issuer onboarded. Call POST /onboard first.' });
    }

    const privateKeyJwk = JSON.parse(keyRecord.privateKeyJwk);

    // Allocate status list index
    let statusListEntry = db.select().from(schema.statusList).where(eq(schema.statusList.issuerDid, keyRecord.issuerDid)).get();
    if (!statusListEntry) {
      const sl = new StatusList();
      statusListEntry = {
        id: uuid(),
        issuerDid: keyRecord.issuerDid,
        encodedList: sl.encode(),
        currentIndex: 0,
        updatedAt: Date.now(),
      };
      db.insert(schema.statusList).values(statusListEntry).run();
    }

    const statusListIndex = statusListEntry.currentIndex;
    db.update(schema.statusList)
      .set({ currentIndex: statusListIndex + 1, updatedAt: Date.now() })
      .where(eq(schema.statusList.id, statusListEntry.id))
      .run();

    const credId = `urn:uuid:${uuid()}`;
    const ISSUER_URL = process.env.ISSUER_SERVICE_URL || 'http://localhost:3002';

    const { credential, jwt } = await createVerifiableCredential({
      id: credId,
      issuerDid: keyRecord.issuerDid,
      subjectDid,
      types: [schemaName],
      claims,
      privateKeyJwk,
      expirationDate,
      credentialStatus: {
        id: `${ISSUER_URL}/credentials/status/${statusListEntry.id}#${statusListIndex}`,
        type: 'StatusList2021Entry',
        statusPurpose: 'revocation',
        statusListIndex: String(statusListIndex),
        statusListCredential: `${ISSUER_URL}/credentials/status/${statusListEntry.id}`,
      },
    });

    const now = Date.now();
    db.insert(schema.credentials).values({
      id: credId,
      issuerDid: keyRecord.issuerDid,
      subjectDid,
      schemaName,
      claims: JSON.stringify(claims),
      jwt,
      status: 'active',
      statusListIndex,
      expirationDate: expirationDate ?? null,
      createdAt: now,
    }).run();

    return { credential, jwt };
  });

  // List all credentials
  fastify.get('/credentials', async (_req, _reply) => {
    const all = db.select().from(schema.credentials).all();
    return all.map(c => ({
      ...c,
      claims: JSON.parse(c.claims),
    }));
  });

  // Get credential by id
  fastify.get<{ Params: { id: string } }>('/credentials/:id', async (req, reply) => {
    const { id } = req.params;
    const cred = db.select().from(schema.credentials).where(eq(schema.credentials.id, id)).get();
    if (!cred) {
      // Try with urn:uuid: prefix
      const fullId = id.startsWith('urn:uuid:') ? id : `urn:uuid:${id}`;
      const cred2 = db.select().from(schema.credentials).where(eq(schema.credentials.id, fullId)).get();
      if (!cred2) {
        return reply.status(404).send({ error: 'Credential not found' });
      }
      return { ...cred2, claims: JSON.parse(cred2.claims) };
    }
    return { ...cred, claims: JSON.parse(cred.claims) };
  });

  // Revoke credential
  fastify.post<{ Params: { id: string } }>('/credentials/:id/revoke', async (req, reply) => {
    const { id } = req.params;
    const fullId = id.startsWith('urn:uuid:') ? id : `urn:uuid:${id}`;
    const cred = db.select().from(schema.credentials).where(eq(schema.credentials.id, fullId)).get();
    if (!cred) {
      return reply.status(404).send({ error: 'Credential not found' });
    }

    // Update status list
    if (cred.statusListIndex !== null) {
      const slEntry = db.select().from(schema.statusList).where(eq(schema.statusList.issuerDid, cred.issuerDid)).get();
      if (slEntry) {
        const sl = StatusList.fromEncoded(slEntry.encodedList);
        sl.setStatus(cred.statusListIndex, true);
        db.update(schema.statusList)
          .set({ encodedList: sl.encode(), updatedAt: Date.now() })
          .where(eq(schema.statusList.id, slEntry.id))
          .run();
      }
    }

    db.update(schema.credentials)
      .set({ status: 'revoked' })
      .where(eq(schema.credentials.id, fullId))
      .run();

    return { id: fullId, status: 'revoked', message: 'Credential revoked successfully' };
  });

  // Suspend credential
  fastify.post<{ Params: { id: string } }>('/credentials/:id/suspend', async (req, reply) => {
    const { id } = req.params;
    const fullId = id.startsWith('urn:uuid:') ? id : `urn:uuid:${id}`;
    const cred = db.select().from(schema.credentials).where(eq(schema.credentials.id, fullId)).get();
    if (!cred) {
      return reply.status(404).send({ error: 'Credential not found' });
    }

    db.update(schema.credentials)
      .set({ status: 'suspended' })
      .where(eq(schema.credentials.id, fullId))
      .run();

    return { id: fullId, status: 'suspended', message: 'Credential suspended' };
  });

  // Reactivate credential
  fastify.post<{ Params: { id: string } }>('/credentials/:id/activate', async (req, reply) => {
    const { id } = req.params;
    const fullId = id.startsWith('urn:uuid:') ? id : `urn:uuid:${id}`;
    const cred = db.select().from(schema.credentials).where(eq(schema.credentials.id, fullId)).get();
    if (!cred) {
      return reply.status(404).send({ error: 'Credential not found' });
    }

    // Remove from status list if was revoked
    if (cred.statusListIndex !== null) {
      const slEntry = db.select().from(schema.statusList).where(eq(schema.statusList.issuerDid, cred.issuerDid)).get();
      if (slEntry) {
        const sl = StatusList.fromEncoded(slEntry.encodedList);
        sl.setStatus(cred.statusListIndex, false);
        db.update(schema.statusList)
          .set({ encodedList: sl.encode(), updatedAt: Date.now() })
          .where(eq(schema.statusList.id, slEntry.id))
          .run();
      }
    }

    db.update(schema.credentials)
      .set({ status: 'active' })
      .where(eq(schema.credentials.id, fullId))
      .run();

    return { id: fullId, status: 'active', message: 'Credential reactivated' };
  });

  // Status list endpoint (public)
  fastify.get<{ Params: { id: string } }>('/credentials/status/:id', async (req, reply) => {
    const { id } = req.params;
    const slEntry = db.select().from(schema.statusList).where(eq(schema.statusList.id, id)).get();
    if (!slEntry) {
      return reply.status(404).send({ error: 'Status list not found' });
    }
    return {
      '@context': ['https://www.w3.org/2018/credentials/v1'],
      type: ['VerifiableCredential', 'StatusList2021Credential'],
      issuer: slEntry.issuerDid,
      credentialSubject: {
        id: `${process.env.ISSUER_SERVICE_URL || 'http://localhost:3002'}/credentials/status/${id}`,
        type: 'StatusList2021',
        statusPurpose: 'revocation',
        encodedList: slEntry.encodedList,
      },
    };
  });
}
