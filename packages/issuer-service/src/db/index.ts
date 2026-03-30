import Database from 'better-sqlite3';
import { drizzle } from 'drizzle-orm/better-sqlite3';
import * as schema from './schema.js';

const sqlite = new Database('issuer-service.db');
sqlite.pragma('journal_mode = WAL');

export const db = drizzle(sqlite, { schema });

sqlite.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'issuer',
    created_at INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS issuer_keys (
    issuer_did TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    public_key_jwk TEXT NOT NULL,
    private_key_jwk TEXT NOT NULL,
    created_at INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    issuer_did TEXT NOT NULL,
    subject_did TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    claims TEXT NOT NULL,
    jwt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    status_list_index INTEGER,
    expiration_date TEXT,
    created_at INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS credential_offers (
    id TEXT PRIMARY KEY,
    credential_id TEXT NOT NULL,
    pre_authorized_code TEXT NOT NULL,
    offer_url TEXT NOT NULL,
    pin TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS status_list (
    id TEXT PRIMARY KEY,
    issuer_did TEXT NOT NULL,
    encoded_list TEXT NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL
  );
`);

export { schema };
