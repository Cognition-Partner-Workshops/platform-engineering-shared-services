import { type FastifyInstance } from 'fastify';
import { eq, desc, sql } from 'drizzle-orm';
import { db, schema } from '../db/index.js';

export async function auditRoutes(fastify: FastifyInstance) {
  // Full audit log (paginated)
  fastify.get('/audit', async (req, _reply) => {
    const query = req.query as { limit?: string; offset?: string };
    const limit = Math.min(parseInt(query.limit || '20', 10), 100);
    const offset = parseInt(query.offset || '0', 10);

    const total = db.select({ count: sql<number>`count(*)` }).from(schema.verificationSessions).get();
    const sessions = db.select().from(schema.verificationSessions)
      .orderBy(desc(schema.verificationSessions.createdAt))
      .limit(limit)
      .offset(offset)
      .all();

    return {
      total: total?.count || 0,
      items: sessions.map(s => ({
        sessionId: s.id,
        holderDid: s.holderDid,
        decision: s.decision,
        policyId: s.policyId,
        purpose: s.purpose,
        failReason: s.failReason,
        credentialsVerified: s.credentialsVerified,
        verifiedAt: s.updatedAt ? new Date(s.updatedAt).toISOString() : null,
      })),
    };
  });

  // Per-session audit log
  fastify.get<{ Params: { sessionId: string } }>('/audit/:sessionId', async (req, reply) => {
    const { sessionId } = req.params;
    const session = db.select().from(schema.verificationSessions).where(eq(schema.verificationSessions.id, sessionId)).get();
    if (!session) {
      return reply.status(404).send({ error: 'Session not found' });
    }

    const logs = db.select().from(schema.verificationLogs)
      .where(eq(schema.verificationLogs.sessionId, sessionId))
      .all();

    return {
      sessionId,
      decision: session.decision,
      holderDid: session.holderDid,
      purpose: session.purpose,
      failReason: session.failReason,
      credentialsVerified: session.credentialsVerified,
      checks: logs.map(l => ({
        checkName: l.checkName,
        passed: l.passed,
        detail: l.detail,
        timestamp: l.timestamp ? new Date(l.timestamp).toISOString() : null,
      })),
    };
  });
}
