--------------------------------------------------------------------------------
-- Banking Demo — Schema
--
-- Creates the core banking schema: customers, accounts, transactions,
-- branches, employees, loans, and credit cards. Designed to demonstrate
-- DDL introspection, JDBC metadata queries, and materialized view support.
--------------------------------------------------------------------------------

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Schema
CREATE SCHEMA IF NOT EXISTS banking;
SET search_path TO banking, public;

--------------------------------------------------------------------------------
-- Reference Tables
--------------------------------------------------------------------------------

CREATE TABLE banking.branches (
    branch_id     SERIAL PRIMARY KEY,
    branch_code   VARCHAR(10) UNIQUE NOT NULL,
    branch_name   VARCHAR(100) NOT NULL,
    address       VARCHAR(255),
    city          VARCHAR(100) NOT NULL,
    state         VARCHAR(2) NOT NULL,
    zip_code      VARCHAR(10),
    phone         VARCHAR(20),
    opened_date   DATE NOT NULL DEFAULT CURRENT_DATE,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE banking.account_types (
    type_id       SERIAL PRIMARY KEY,
    type_code     VARCHAR(20) UNIQUE NOT NULL,
    type_name     VARCHAR(100) NOT NULL,
    description   TEXT,
    min_balance   NUMERIC(15,2) DEFAULT 0.00,
    interest_rate NUMERIC(5,4) DEFAULT 0.0000
);

CREATE TABLE banking.transaction_types (
    type_id   SERIAL PRIMARY KEY,
    type_code VARCHAR(20) UNIQUE NOT NULL,
    type_name VARCHAR(100) NOT NULL,
    direction VARCHAR(6) NOT NULL CHECK (direction IN ('CREDIT', 'DEBIT'))
);

--------------------------------------------------------------------------------
-- Core Tables
--------------------------------------------------------------------------------

CREATE TABLE banking.customers (
    customer_id   SERIAL PRIMARY KEY,
    first_name    VARCHAR(100) NOT NULL,
    last_name     VARCHAR(100) NOT NULL,
    email         VARCHAR(255) UNIQUE NOT NULL,
    phone         VARCHAR(20),
    date_of_birth DATE,
    ssn_hash      VARCHAR(128),
    address       VARCHAR(255),
    city          VARCHAR(100),
    state         VARCHAR(2),
    zip_code      VARCHAR(10),
    credit_score  INTEGER CHECK (credit_score BETWEEN 300 AND 850),
    created_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMP NOT NULL DEFAULT NOW(),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE INDEX idx_customers_name ON banking.customers (last_name, first_name);
CREATE INDEX idx_customers_state ON banking.customers (state);

CREATE TABLE banking.accounts (
    account_id     SERIAL PRIMARY KEY,
    account_number VARCHAR(20) UNIQUE NOT NULL,
    customer_id    INTEGER NOT NULL REFERENCES banking.customers(customer_id),
    account_type   INTEGER NOT NULL REFERENCES banking.account_types(type_id),
    branch_id      INTEGER NOT NULL REFERENCES banking.branches(branch_id),
    balance        NUMERIC(15,2) NOT NULL DEFAULT 0.00,
    currency       VARCHAR(3) NOT NULL DEFAULT 'USD',
    opened_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    closed_date    DATE,
    status         VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                   CHECK (status IN ('ACTIVE', 'CLOSED', 'FROZEN', 'DORMANT')),
    created_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_accounts_customer ON banking.accounts (customer_id);
CREATE INDEX idx_accounts_branch ON banking.accounts (branch_id);
CREATE INDEX idx_accounts_status ON banking.accounts (status);

CREATE TABLE banking.transactions (
    transaction_id   BIGSERIAL PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES banking.accounts(account_id),
    transaction_type INTEGER NOT NULL REFERENCES banking.transaction_types(type_id),
    amount           NUMERIC(15,2) NOT NULL CHECK (amount > 0),
    balance_after    NUMERIC(15,2) NOT NULL,
    description      VARCHAR(255),
    reference_number VARCHAR(50) UNIQUE NOT NULL,
    counterparty     VARCHAR(255),
    channel          VARCHAR(20) DEFAULT 'BRANCH'
                     CHECK (channel IN ('BRANCH', 'ATM', 'ONLINE', 'MOBILE', 'WIRE', 'ACH')),
    transaction_date TIMESTAMP NOT NULL DEFAULT NOW(),
    posted_date      DATE NOT NULL DEFAULT CURRENT_DATE,
    created_at       TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_transactions_account ON banking.transactions (account_id);
CREATE INDEX idx_transactions_date ON banking.transactions (transaction_date);
CREATE INDEX idx_transactions_posted ON banking.transactions (posted_date);
CREATE INDEX idx_transactions_type ON banking.transactions (transaction_type);

CREATE TABLE banking.employees (
    employee_id  SERIAL PRIMARY KEY,
    first_name   VARCHAR(100) NOT NULL,
    last_name    VARCHAR(100) NOT NULL,
    email        VARCHAR(255) UNIQUE NOT NULL,
    role         VARCHAR(50) NOT NULL,
    branch_id    INTEGER REFERENCES banking.branches(branch_id),
    hire_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    salary       NUMERIC(12,2),
    is_active    BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE banking.loans (
    loan_id          SERIAL PRIMARY KEY,
    loan_number      VARCHAR(20) UNIQUE NOT NULL,
    customer_id      INTEGER NOT NULL REFERENCES banking.customers(customer_id),
    branch_id        INTEGER NOT NULL REFERENCES banking.branches(branch_id),
    loan_type        VARCHAR(30) NOT NULL
                     CHECK (loan_type IN ('MORTGAGE', 'AUTO', 'PERSONAL', 'STUDENT', 'BUSINESS')),
    principal_amount NUMERIC(15,2) NOT NULL,
    interest_rate    NUMERIC(5,4) NOT NULL,
    term_months      INTEGER NOT NULL,
    monthly_payment  NUMERIC(12,2) NOT NULL,
    outstanding_balance NUMERIC(15,2) NOT NULL,
    origination_date DATE NOT NULL,
    maturity_date    DATE NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'CURRENT'
                     CHECK (status IN ('CURRENT', 'DELINQUENT', 'DEFAULT', 'PAID_OFF', 'CHARGED_OFF')),
    created_at       TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_loans_customer ON banking.loans (customer_id);
CREATE INDEX idx_loans_status ON banking.loans (status);

CREATE TABLE banking.credit_cards (
    card_id         SERIAL PRIMARY KEY,
    card_number     VARCHAR(128) NOT NULL,
    customer_id     INTEGER NOT NULL REFERENCES banking.customers(customer_id),
    card_type       VARCHAR(20) NOT NULL
                    CHECK (card_type IN ('VISA', 'MASTERCARD', 'AMEX')),
    credit_limit    NUMERIC(12,2) NOT NULL,
    current_balance NUMERIC(12,2) NOT NULL DEFAULT 0.00,
    apr             NUMERIC(5,4) NOT NULL,
    issued_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    expiry_date     DATE NOT NULL,
    status          VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
                    CHECK (status IN ('ACTIVE', 'CLOSED', 'SUSPENDED', 'EXPIRED')),
    rewards_points  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_cards_customer ON banking.credit_cards (customer_id);
