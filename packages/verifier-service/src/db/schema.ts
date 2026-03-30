import { sqliteTable, text, integer } from 'drizzle-orm/sqlite-core';

export const users = sqliteTable('users', {
  id: text('id').primaryKey(),
  username: text('username').notNull().unique(),
  passwordHash: text('password_hash').notNull(),
  role: text('role').notNull().default('verifier'),
  createdAt: integer('created_at').notNull(),
});

export const trustPolicies = sqliteTable('trust_policies', {
  id: text('id').primaryKey(),
  name: text('name').notNull(),
  acceptedSchemas: text('accepted_schemas').notNull(), // JSON array
  trustedRegistries: text('trusted_registries').notNull(), // JSON array
  trustedIssuers: text('trusted_issuers').notNull(), // JSON array or "*"
  checkRevocation: integer('check_revocation', { mode: 'boolean' }).notNull().default(true),
  requireNonExpired: integer('require_non_expired', { mode: 'boolean' }).notNull().default(true),
  createdAt: integer('created_at').notNull(),
});

export const verificationSessions = sqliteTable('verification_sessions', {
  id: text('id').primaryKey(),
  policyId: text('policy_id').notNull(),
  purpose: text('purpose'),
  nonce: text('nonce').notNull(),
  status: text('status').notNull().default('pending'), // pending | received | verified | rejected
  holderDid: text('holder_did'),
  vpJwt: text('vp_jwt'),
  decision: text('decision'), // accepted | rejected
  failReason: text('fail_reason'),
  credentialsVerified: integer('credentials_verified'),
  createdAt: integer('created_at').notNull(),
  updatedAt: integer('updated_at').notNull(),
});

export const verificationLogs = sqliteTable('verification_logs', {
  id: text('id').primaryKey(),
  sessionId: text('session_id').notNull(),
  checkName: text('check_name').notNull(),
  passed: integer('passed', { mode: 'boolean' }),
  detail: text('detail').notNull(),
  timestamp: integer('timestamp'),
});
