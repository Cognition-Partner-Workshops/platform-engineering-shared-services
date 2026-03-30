import Fastify from 'fastify';
import cors from '@fastify/cors';
import { authRoutes } from './routes/auth.js';
import { issuerRoutes } from './routes/issuers.js';
import { schemaRoutes } from './routes/schemas.js';

const fastify = Fastify({ logger: true });

await fastify.register(cors, { origin: true });

// Register routes
await fastify.register(authRoutes);
await fastify.register(issuerRoutes);
await fastify.register(schemaRoutes);

// Health check
fastify.get('/health', async () => ({ status: 'ok', service: 'trust-registry' }));

const PORT = Number(process.env.PORT) || 3001;

try {
  await fastify.listen({ port: PORT, host: '0.0.0.0' });
  console.log(`Trust Registry running on http://localhost:${PORT}`);
} catch (err) {
  fastify.log.error(err);
  process.exit(1);
}
