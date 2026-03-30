import { sqliteTable, text, integer } from 'drizzle-orm/sqlite-core';

export const users = sqliteTable('users', {
  id: text('id').primaryKey(),
  username: text('username').notNull().unique(),
  passwordHash: text('password_hash').notNull(),
  role: text('role').notNull().default('issuer'),
  createdAt: integer('created_at').notNull(),
});

export const issuerKeys = sqliteTable('issuer_keys', {
  issuerDid: text('issuer_did').primaryKey(),
  name: text('name').notNull(),
  type: text('type').notNull(),
  publicKeyJwk: text('public_key_jwk').notNull(),
  privateKeyJwk: text('private_key_jwk').notNull(),
  createdAt: integer('created_at').notNull(),
});

export const credentials = sqliteTable('credentials', {
  id: text('id').primaryKey(),
  issuerDid: text('issuer_did').notNull(),
  subjectDid: text('subject_did').notNull(),
  schemaName: text('schema_name').notNull(),
  claims: text('claims').notNull(), // JSON
  jwt: text('jwt').notNull(),
  status: text('status').notNull().default('active'), // active | suspended | revoked
  statusListIndex: integer('status_list_index'),
  expirationDate: text('expiration_date'),
  createdAt: integer('created_at').notNull(),
});

export const credentialOffers = sqliteTable('credential_offers', {
  id: text('id').primaryKey(),
  credentialId: text('credential_id').notNull(),
  offerUrl: text('offer_url').notNull(),
  preAuthorizedCode: text('pre_authorized_code').notNull(),
  pin: text('pin'), // Optional 4-6 digit PIN
  status: text('status').notNull().default('pending'), // pending | claimed | expired
  createdAt: integer('created_at').notNull(),
  expiresAt: integer('expires_at').notNull(),
});

export const statusList = sqliteTable('status_list', {
  id: text('id').primaryKey(),
  issuerDid: text('issuer_did').notNull(),
  encodedList: text('encoded_list').notNull(),
  currentIndex: integer('current_index').notNull().default(0),
  updatedAt: integer('updated_at').notNull(),
});
