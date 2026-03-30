import { type FastifyInstance } from 'fastify';
import { v4 as uuid } from 'uuid';
import { eq } from 'drizzle-orm';
import { randomBytes } from 'node:crypto';
import { db, schema } from '../db/index.js';

const ISSUER_URL = process.env.ISSUER_SERVICE_URL || 'http://localhost:3002';

function generatePin(): string {
  return String(Math.floor(1000 + Math.random() * 9000));
}

function generatePreAuthCode(): string {
  return randomBytes(24).toString('base64url');
}

export async function oid4vciRoutes(fastify: FastifyInstance) {
  // OpenID Credential Issuer metadata
  fastify.get('/.well-known/openid-credential-issuer', async () => {
    const issuers = db.select().from(schema.issuerKeys).all();
    return {
      credential_issuer: ISSUER_URL,
      credential_endpoint: `${ISSUER_URL}/oid4vci/credential`,
      token_endpoint: `${ISSUER_URL}/oid4vci/token`,
      credentials_supported: [
        {
          format: 'jwt_vc_json',
          types: ['VerifiableCredential', 'EmploymentCredential'],
          display: [{ name: 'Employment Credential', locale: 'en-US' }],
        },
        {
          format: 'jwt_vc_json',
          types: ['VerifiableCredential', 'EducationCredential'],
          display: [{ name: 'Education Credential', locale: 'en-US' }],
        },
        {
          format: 'jwt_vc_json',
          types: ['VerifiableCredential', 'KYCCredential'],
          display: [{ name: 'KYC Credential', locale: 'en-US' }],
        },
        {
          format: 'jwt_vc_json',
          types: ['VerifiableCredential', 'IncomeCredential'],
          display: [{ name: 'Income Credential', locale: 'en-US' }],
        },
        {
          format: 'jwt_vc_json',
          types: ['VerifiableCredential', 'ProfessionalCertificationCredential'],
          display: [{ name: 'Professional Certification Credential', locale: 'en-US' }],
        },
      ],
      display: issuers.map(i => ({ name: i.name })),
    };
  });

  // Generate credential offer
  fastify.post<{ Params: { credentialId: string } }>('/credentials/:credentialId/offer', async (req, reply) => {
    const { credentialId } = req.params;
    const fullId = credentialId.startsWith('urn:uuid:') ? credentialId : `urn:uuid:${credentialId}`;
    const cred = db.select().from(schema.credentials).where(eq(schema.credentials.id, fullId)).get();
    if (!cred) {
      return reply.status(404).send({ error: 'Credential not found' });
    }

    const body = (req.body || {}) as { requirePin?: boolean };
    const offerId = uuid();
    const preAuthCode = generatePreAuthCode();
    const pin = body.requirePin ? generatePin() : null;

    const offerUrl = `openid-credential-offer://?credential_offer_uri=${encodeURIComponent(`${ISSUER_URL}/oid4vci/offers/${offerId}`)}`;

    const now = Date.now();
    db.insert(schema.credentialOffers).values({
      id: offerId,
      credentialId: fullId,
      offerUrl,
      preAuthorizedCode: preAuthCode,
      pin,
      status: 'pending',
      createdAt: now,
      expiresAt: now + 15 * 60 * 1000, // 15 minutes
    }).run();

    const response: Record<string, unknown> = {
      offerId,
      offerUrl,
      qrData: `${ISSUER_URL}/oid4vci/offers/${offerId}`,
      pinRequired: !!pin,
      expiresAt: new Date(now + 15 * 60 * 1000).toISOString(),
    };
    if (pin) {
      response.pin = pin;
    }
    return response;
  });

  // Get credential offer (wallet fetches this)
  fastify.get<{ Params: { offerId: string } }>('/oid4vci/offers/:offerId', async (req, reply) => {
    const { offerId } = req.params;
    const offer = db.select().from(schema.credentialOffers).where(eq(schema.credentialOffers.id, offerId)).get();
    if (!offer) {
      return reply.status(404).send({ error: 'Offer not found' });
    }

    if (offer.status !== 'pending') {
      return reply.status(410).send({ error: `Offer already ${offer.status}` });
    }

    if (Date.now() > offer.expiresAt) {
      db.update(schema.credentialOffers).set({ status: 'expired' }).where(eq(schema.credentialOffers.id, offerId)).run();
      return reply.status(410).send({ error: 'Offer expired' });
    }

    const cred = db.select().from(schema.credentials).where(eq(schema.credentials.id, offer.credentialId)).get();

    return {
      credential_issuer: ISSUER_URL,
      credentials: [cred?.schemaName || 'VerifiableCredential'],
      grants: {
        'urn:ietf:params:oauth:grant-type:pre-authorized_code': {
          'pre-authorized_code': offer.preAuthorizedCode,
          user_pin_required: !!offer.pin,
        },
      },
    };
  });

  // Token endpoint (wallet exchanges pre-auth code for access token)
  fastify.post('/oid4vci/token', async (req, reply) => {
    const body = req.body as Record<string, string>;
    const preAuthCode = body['pre-authorized_code'];
    const userPin = body['user_pin'];

    if (!preAuthCode) {
      return reply.status(400).send({ error: 'Missing pre-authorized_code' });
    }

    const offer = db.select().from(schema.credentialOffers)
      .where(eq(schema.credentialOffers.preAuthorizedCode, preAuthCode)).get();

    if (!offer) {
      return reply.status(400).send({ error: 'Invalid pre-authorized_code' });
    }

    if (offer.status !== 'pending') {
      return reply.status(400).send({ error: `Offer already ${offer.status}` });
    }

    if (Date.now() > offer.expiresAt) {
      db.update(schema.credentialOffers).set({ status: 'expired' }).where(eq(schema.credentialOffers.id, offer.id)).run();
      return reply.status(400).send({ error: 'Offer expired' });
    }

    // Validate PIN if required
    if (offer.pin) {
      if (!userPin || userPin !== offer.pin) {
        return reply.status(401).send({ error: 'invalid_pin' });
      }
    }

    // Generate access token (simple for demo)
    const accessToken = `${offer.id}:${randomBytes(16).toString('hex')}`;

    return {
      access_token: accessToken,
      token_type: 'bearer',
      expires_in: 900,
      c_nonce: randomBytes(16).toString('base64url'),
    };
  });

  // Credential endpoint (wallet requests the actual credential)
  fastify.post('/oid4vci/credential', async (req, reply) => {
    const authHeader = req.headers.authorization;
    if (!authHeader || !authHeader.startsWith('Bearer ')) {
      return reply.status(401).send({ error: 'Missing authorization' });
    }

    const token = authHeader.slice(7);
    const offerId = token.split(':')[0];

    const offer = db.select().from(schema.credentialOffers)
      .where(eq(schema.credentialOffers.id, offerId)).get();

    if (!offer) {
      return reply.status(401).send({ error: 'Invalid token' });
    }

    // Mark offer as claimed
    db.update(schema.credentialOffers).set({ status: 'claimed' }).where(eq(schema.credentialOffers.id, offerId)).run();

    // Return the credential
    const cred = db.select().from(schema.credentials).where(eq(schema.credentials.id, offer.credentialId)).get();
    if (!cred) {
      return reply.status(404).send({ error: 'Credential not found' });
    }

    return {
      format: 'jwt_vc_json',
      credential: cred.jwt,
    };
  });

  // List all offers
  fastify.get('/offers', async (_req, _reply) => {
    const all = db.select().from(schema.credentialOffers).all();
    return all.map(o => ({
      ...o,
      createdAt: new Date(o.createdAt).toISOString(),
      expiresAt: new Date(o.expiresAt).toISOString(),
    }));
  });
}
