import { type FastifyInstance } from 'fastify';
import { eq } from 'drizzle-orm';
import { buildDidDocument } from '@trustvault/shared';
import { db, schema } from '../db/index.js';

export async function didDocRoutes(fastify: FastifyInstance) {
  fastify.get<{ Params: { issuerDid: string } }>('/did/:issuerDid/did.json', async (req, reply) => {
    const { issuerDid } = req.params;
    const decodedDid = decodeURIComponent(issuerDid);
    const keyRecord = db.select().from(schema.issuerKeys).where(eq(schema.issuerKeys.issuerDid, decodedDid)).get();

    if (!keyRecord) {
      return reply.status(404).send({ error: 'Issuer not found' });
    }

    const publicKeyJwk = JSON.parse(keyRecord.publicKeyJwk);
    return buildDidDocument(decodedDid, publicKeyJwk);
  });
}
