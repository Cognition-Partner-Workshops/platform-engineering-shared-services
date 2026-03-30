import { type FastifyInstance } from 'fastify';
import { generateES256KeyPair, deriveDidKey, OnboardIssuerSchema } from '@trustvault/shared';
import { db, schema } from '../db/index.js';

const TRUST_REGISTRY_URL = process.env.TRUST_REGISTRY_URL || 'http://localhost:3001';

export async function onboardRoutes(fastify: FastifyInstance) {
  // Onboard a new issuer: generate keys, derive DID, register in trust registry
  fastify.post('/onboard', async (req, reply) => {
    const parsed = OnboardIssuerSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: 'Invalid input', details: parsed.error.flatten() });
    }
    const { name, type, authorizedSchemas } = parsed.data;

    const { publicKeyJwk, privateKeyJwk } = await generateES256KeyPair();
    const did = deriveDidKey(publicKeyJwk);

    const now = Date.now();
    db.insert(schema.issuerKeys).values({
      issuerDid: did,
      name,
      type,
      publicKeyJwk: JSON.stringify(publicKeyJwk),
      privateKeyJwk: JSON.stringify(privateKeyJwk),
      createdAt: now,
    }).run();

    // Register in Trust Registry
    let registeredInTrustRegistry = false;
    try {
      const res = await fetch(`${TRUST_REGISTRY_URL}/registry/issuers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ did, name, type, authorizedSchemas, status: 'active' }),
      });
      registeredInTrustRegistry = res.ok;
    } catch {
      // Trust registry might not be running
      registeredInTrustRegistry = false;
    }

    return {
      did,
      name,
      type,
      publicKeyJwk,
      status: 'active',
      registeredInTrustRegistry,
      createdAt: new Date(now).toISOString(),
    };
  });

  // List onboarded issuers
  fastify.get('/issuers', async (_req, _reply) => {
    const all = db.select().from(schema.issuerKeys).all();
    return all.map(i => ({
      did: i.issuerDid,
      name: i.name,
      type: i.type,
      publicKeyJwk: JSON.parse(i.publicKeyJwk),
      createdAt: new Date(i.createdAt).toISOString(),
    }));
  });
}
