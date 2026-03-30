import Database from 'better-sqlite3';
import { drizzle } from 'drizzle-orm/better-sqlite3';
import * as schema from './schema.js';

const sqlite = new Database('verifier-service.db');
sqlite.pragma('journal_mode = WAL');

export const db = drizzle(sqlite, { schema });

sqlite.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'verifier',
    created_at INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS trust_policies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    accepted_schemas TEXT NOT NULL,
    trusted_registries TEXT NOT NULL,
    trusted_issuers TEXT NOT NULL,
    check_revocation INTEGER NOT NULL DEFAULT 1,
    require_non_expired INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS verification_sessions (
    id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    purpose TEXT,
    nonce TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    holder_did TEXT,
    vp_jwt TEXT,
    decision TEXT,
    fail_reason TEXT,
    credentials_verified INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
  );

  CREATE TABLE IF NOT EXISTS verification_logs (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    passed INTEGER,
    detail TEXT NOT NULL,
    timestamp INTEGER
  );
`);

export { schema };
