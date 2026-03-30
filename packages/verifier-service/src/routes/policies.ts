import { type FastifyInstance } from 'fastify';
import { v4 as uuid } from 'uuid';
import { eq } from 'drizzle-orm';
import { CreatePolicySchema } from '@trustvault/shared';
import { db, schema } from '../db/index.js';

export async function policyRoutes(fastify: FastifyInstance) {
  // Create a trust policy
  fastify.post('/policies', async (req, reply) => {
    const parsed = CreatePolicySchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: 'Invalid input', details: parsed.error.flatten() });
    }
    const { name, acceptedSchemas, trustedRegistries, trustedIssuers, checkRevocation, requireNonExpired } = parsed.data;

    const id = uuid();
    const now = Date.now();
    db.insert(schema.trustPolicies).values({
      id,
      name,
      acceptedSchemas: JSON.stringify(acceptedSchemas),
      trustedRegistries: JSON.stringify(trustedRegistries),
      trustedIssuers: typeof trustedIssuers === 'string' ? trustedIssuers : JSON.stringify(trustedIssuers),
      checkRevocation,
      requireNonExpired,
      createdAt: now,
    }).run();

    return {
      id,
      name,
      acceptedSchemas,
      trustedRegistries,
      trustedIssuers,
      checkRevocation,
      requireNonExpired,
      createdAt: new Date(now).toISOString(),
    };
  });

  // List all policies
  fastify.get('/policies', async (_req, _reply) => {
    const all = db.select().from(schema.trustPolicies).all();
    return all.map(p => ({
      ...p,
      acceptedSchemas: JSON.parse(p.acceptedSchemas),
      trustedRegistries: JSON.parse(p.trustedRegistries),
      trustedIssuers: p.trustedIssuers === '*' ? '*' : JSON.parse(p.trustedIssuers),
    }));
  });

  // Get policy by id
  fastify.get<{ Params: { id: string } }>('/policies/:id', async (req, reply) => {
    const { id } = req.params;
    const p = db.select().from(schema.trustPolicies).where(eq(schema.trustPolicies.id, id)).get();
    if (!p) {
      return reply.status(404).send({ error: 'Policy not found' });
    }
    return {
      ...p,
      acceptedSchemas: JSON.parse(p.acceptedSchemas),
      trustedRegistries: JSON.parse(p.trustedRegistries),
      trustedIssuers: p.trustedIssuers === '*' ? '*' : JSON.parse(p.trustedIssuers),
    };
  });

  // Delete policy
  fastify.delete<{ Params: { id: string } }>('/policies/:id', async (req, reply) => {
    const { id } = req.params;
    const p = db.select().from(schema.trustPolicies).where(eq(schema.trustPolicies.id, id)).get();
    if (!p) {
      return reply.status(404).send({ error: 'Policy not found' });
    }
    db.delete(schema.trustPolicies).where(eq(schema.trustPolicies.id, id)).run();
    return { message: 'Policy deleted', id };
  });
}
