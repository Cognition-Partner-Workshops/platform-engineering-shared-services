import { type FastifyInstance } from 'fastify';
import { v4 as uuid } from 'uuid';
import { eq } from 'drizzle-orm';
import { CredentialSchemaRegistration } from '@trustvault/shared';
import { db, schema } from '../db/index.js';

export async function schemaRoutes(fastify: FastifyInstance) {
  // Register a new schema
  fastify.post('/registry/schemas', async (req, reply) => {
    const parsed = CredentialSchemaRegistration.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: 'Invalid input', details: parsed.error.flatten() });
    }
    const { name, version, description, schemaJson } = parsed.data;

    const existing = db.select().from(schema.schemas).where(eq(schema.schemas.name, name)).get();
    if (existing) {
      return reply.status(409).send({ error: 'Schema with this name already exists' });
    }

    const id = uuid();
    const now = Date.now();
    db.insert(schema.schemas).values({
      id,
      name,
      version,
      description: description ?? null,
      schemaJson: JSON.stringify(schemaJson),
      createdAt: now,
    }).run();

    return { id, name, version, description, createdAt: new Date(now).toISOString() };
  });

  // List all schemas
  fastify.get('/registry/schemas', async (_req, _reply) => {
    const all = db.select().from(schema.schemas).all();
    return all.map(s => ({
      ...s,
      schemaJson: JSON.parse(s.schemaJson),
    }));
  });

  // Get schema by name
  fastify.get<{ Params: { name: string } }>('/registry/schemas/:name', async (req, reply) => {
    const { name } = req.params;
    const s = db.select().from(schema.schemas).where(eq(schema.schemas.name, name)).get();
    if (!s) {
      return reply.status(404).send({ error: 'Schema not found' });
    }
    return { ...s, schemaJson: JSON.parse(s.schemaJson) };
  });

  // Delete schema
  fastify.delete<{ Params: { name: string } }>('/registry/schemas/:name', async (req, reply) => {
    const { name } = req.params;
    const s = db.select().from(schema.schemas).where(eq(schema.schemas.name, name)).get();
    if (!s) {
      return reply.status(404).send({ error: 'Schema not found' });
    }
    db.delete(schema.schemas).where(eq(schema.schemas.name, name)).run();
    return { message: 'Schema removed', name };
  });
}
