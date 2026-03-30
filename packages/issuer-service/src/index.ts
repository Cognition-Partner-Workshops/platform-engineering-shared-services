import Fastify from 'fastify';
import cors from '@fastify/cors';
import { authRoutes } from './routes/auth.js';
import { onboardRoutes } from './routes/onboard.js';
import { didDocRoutes } from './routes/did-doc.js';
import { credentialRoutes } from './routes/credentials.js';
import { oid4vciRoutes } from './routes/oid4vci.js';

const fastify = Fastify({ logger: true });

await fastify.register(cors, { origin: true });

// Register all routes
await fastify.register(authRoutes);
await fastify.register(onboardRoutes);
await fastify.register(didDocRoutes);
await fastify.register(credentialRoutes);
await fastify.register(oid4vciRoutes);

// Health check
fastify.get('/health', async () => ({ status: 'ok', service: 'issuer-service' }));

const PORT = Number(process.env.PORT) || 3002;

try {
  await fastify.listen({ port: PORT, host: '0.0.0.0' });
  console.log(`Issuer Service running on http://localhost:${PORT}`);
} catch (err) {
  fastify.log.error(err);
  process.exit(1);
}
