import Fastify from 'fastify';
import cors from '@fastify/cors';
import { authRoutes } from './routes/auth.js';
import { policyRoutes } from './routes/policies.js';
import { oid4vpRoutes } from './routes/oid4vp.js';
import { auditRoutes } from './routes/audit.js';

const fastify = Fastify({ logger: true });

await fastify.register(cors, { origin: true });

// Register all routes
await fastify.register(authRoutes);
await fastify.register(policyRoutes);
await fastify.register(oid4vpRoutes);
await fastify.register(auditRoutes);

// Health check
fastify.get('/health', async () => ({ status: 'ok', service: 'verifier-service' }));

const PORT = Number(process.env.PORT) || 3003;

try {
  await fastify.listen({ port: PORT, host: '0.0.0.0' });
  console.log(`Verifier Service running on http://localhost:${PORT}`);
} catch (err) {
  fastify.log.error(err);
  process.exit(1);
}
