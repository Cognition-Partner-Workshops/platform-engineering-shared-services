import { type FastifyInstance } from 'fastify';
import { v4 as uuid } from 'uuid';
import { eq } from 'drizzle-orm';
import { generateAuthToken, hashPassword, LoginSchema, RegisterSchema } from '@trustvault/shared';
import { db, schema } from '../db/index.js';

export async function authRoutes(fastify: FastifyInstance) {
  fastify.post('/auth/register', async (req, reply) => {
    const parsed = RegisterSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: 'Invalid input', details: parsed.error.flatten() });
    }
    const { username, password, role } = parsed.data;

    const existing = db.select().from(schema.users).where(eq(schema.users.username, username)).get();
    if (existing) {
      return reply.status(409).send({ error: 'Username already exists' });
    }

    const id = uuid();
    const passwordHash = hashPassword(password);
    db.insert(schema.users).values({
      id,
      username,
      passwordHash,
      role,
      createdAt: Date.now(),
    }).run();

    const token = await generateAuthToken({ sub: id, username, role });
    return { id, username, role, token };
  });

  fastify.post('/auth/login', async (req, reply) => {
    const parsed = LoginSchema.safeParse(req.body);
    if (!parsed.success) {
      return reply.status(400).send({ error: 'Invalid input', details: parsed.error.flatten() });
    }
    const { username, password } = parsed.data;

    const user = db.select().from(schema.users).where(eq(schema.users.username, username)).get();
    if (!user || user.passwordHash !== hashPassword(password)) {
      return reply.status(401).send({ error: 'Invalid credentials' });
    }

    const token = await generateAuthToken({ sub: user.id, username: user.username, role: user.role });
    return { id: user.id, username: user.username, role: user.role, token };
  });
}
