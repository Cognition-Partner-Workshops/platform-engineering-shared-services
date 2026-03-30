import { type FastifyInstance } from 'fastify';
import { eq } from 'drizzle-orm';
import { CreateIssuerSchema } from '@trustvault/shared';
import { db, schema } from '../db/index.js';

export async function issuerRoutes(fastify: FastifyInstance) {
  // List all issuers
  fastify.get('/registry/issuers', async (_req, _reply) => {
    const all = db.select().from(schema.issuers).all();
    return all.map(i => ({
      ...i,
      authorizedSchemas: JSON.parse(i.authorizedSchemas),
    }));
  });

  // Get single issuer
  fastify.get<{ Params: { did: string } }>('/registry/issuers/:did', async (req, reply) => {
    const { did } = req.params;
    const decodedDid = decodeURIComponent(did);
    const issuer = db.select().from(schema.issuers).where(eq(schema.issuers.did, decodedDid)).get();
    if (!issuer) {
      return reply.status(404).send({ error: 'Issuer not found' });
    }
    return { ...issuer, authorizedSchemas: JSON.parse(issuer.authorizedSchemas) };
  });

  // Register an issuer
  fastify.post('/registry/issuers', async (req, reply) => {
    const parsed = CreateIssuerSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: 'Invalid input', details: parsed.error.flatten() });
    }
    const { did, name, type, authorizedSchemas, status } = parsed.data;

    const existing = db.select().from(schema.issuers).where(eq(schema.issuers.did, did)).get();
    if (existing) {
      return reply.status(409).send({ error: 'Issuer already registered' });
    }

    const now = Date.now();
    db.insert(schema.issuers).values({
      did,
      name,
      type,
      authorizedSchemas: JSON.stringify(authorizedSchemas),
      status,
      createdAt: now,
      updatedAt: now,
    }).run();

    return { did, name, type, authorizedSchemas, status, createdAt: new Date(now).toISOString() };
  });

  // Update issuer status
  fastify.patch<{ Params: { did: string } }>('/registry/issuers/:did', async (req, reply) => {
    const { did } = req.params;
    const decodedDid = decodeURIComponent(did);
    const body = req.body as Record<string, unknown>;

    const issuer = db.select().from(schema.issuers).where(eq(schema.issuers.did, decodedDid)).get();
    if (!issuer) {
      return reply.status(404).send({ error: 'Issuer not found' });
    }

    const updates: Record<string, unknown> = { updatedAt: Date.now() };
    if (body.status && ['active', 'suspended', 'revoked'].includes(body.status as string)) {
      updates.status = body.status;
    }
    if (body.name) updates.name = body.name;
    if (body.authorizedSchemas) updates.authorizedSchemas = JSON.stringify(body.authorizedSchemas);

    db.update(schema.issuers).set(updates).where(eq(schema.issuers.did, decodedDid)).run();

    const updated = db.select().from(schema.issuers).where(eq(schema.issuers.did, decodedDid)).get();
    return { ...updated, authorizedSchemas: JSON.parse(updated!.authorizedSchemas) };
  });

  // Delete issuer
  fastify.delete<{ Params: { did: string } }>('/registry/issuers/:did', async (req, reply) => {
    const { did } = req.params;
    const decodedDid = decodeURIComponent(did);
    const issuer = db.select().from(schema.issuers).where(eq(schema.issuers.did, decodedDid)).get();
    if (!issuer) {
      return reply.status(404).send({ error: 'Issuer not found' });
    }
    db.delete(schema.issuers).where(eq(schema.issuers.did, decodedDid)).run();
    return { message: 'Issuer removed', did: decodedDid };
  });

  // Verify issuer (public endpoint for verifiers)
  fastify.get('/registry/verify/*', async (req, reply) => {
    const rawUrl = req.url;
    const prefix = '/registry/verify/';
    const decodedDid = decodeURIComponent(rawUrl.slice(rawUrl.indexOf(prefix) + prefix.length));
    const issuer = db.select().from(schema.issuers).where(eq(schema.issuers.did, decodedDid)).get();
    if (!issuer) {
      return { trusted: false, reason: 'Issuer not found in registry' };
    }
    if (issuer.status !== 'active') {
      return { trusted: false, reason: `Issuer status is '${issuer.status}'` };
    }
    return {
      trusted: true,
      issuer: {
        did: issuer.did,
        name: issuer.name,
        type: issuer.type,
        authorizedSchemas: JSON.parse(issuer.authorizedSchemas),
        status: issuer.status,
      },
    };
  });
}
