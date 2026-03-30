import { sqliteTable, text, integer } from 'drizzle-orm/sqlite-core';

export const users = sqliteTable('users', {
  id: text('id').primaryKey(),
  username: text('username').notNull().unique(),
  passwordHash: text('password_hash').notNull(),
  role: text('role').notNull().default('admin'),
  createdAt: integer('created_at').notNull(),
});

export const issuers = sqliteTable('issuers', {
  did: text('did').primaryKey(),
  name: text('name').notNull(),
  type: text('type').notNull(),
  authorizedSchemas: text('authorized_schemas').notNull(), // JSON array
  status: text('status').notNull().default('active'),
  createdAt: integer('created_at').notNull(),
  updatedAt: integer('updated_at').notNull(),
});

export const schemas = sqliteTable('schemas', {
  id: text('id').primaryKey(),
  name: text('name').notNull().unique(),
  version: text('version').notNull().default('1.0'),
  description: text('description'),
  schemaJson: text('schema_json').notNull(), // Stringified JSON-Schema
  createdAt: integer('created_at').notNull(),
});
