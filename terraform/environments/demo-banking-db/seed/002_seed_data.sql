--------------------------------------------------------------------------------
-- Banking Demo — Seed Data
--
-- Populates the banking schema with realistic reference data, customers,
-- accounts, transactions, employees, loans, and credit cards.
--------------------------------------------------------------------------------

SET search_path TO banking, public;

--------------------------------------------------------------------------------
-- Reference Data
--------------------------------------------------------------------------------

INSERT INTO banking.branches (branch_code, branch_name, address, city, state, zip_code, phone, opened_date) VALUES
('HQ001',  'Downtown Manhattan',     '100 Wall Street',         'New York',      'NY', '10005', '212-555-0100', '2018-01-15'),
('BR002',  'Midtown East',           '350 Park Avenue',         'New York',      'NY', '10022', '212-555-0200', '2019-03-01'),
('BR003',  'Financial District SF',  '555 California Street',   'San Francisco', 'CA', '94104', '415-555-0300', '2019-06-15'),
('BR004',  'Loop District',          '233 S Wacker Drive',      'Chicago',       'IL', '60606', '312-555-0400', '2020-01-10'),
('BR005',  'Back Bay',               '200 Clarendon Street',    'Boston',        'MA', '02116', '617-555-0500', '2020-09-01'),
('BR006',  'Buckhead',               '3344 Peachtree Road NE',  'Atlanta',       'GA', '30326', '404-555-0600', '2021-02-15'),
('BR007',  'Downtown Austin',        '100 Congress Avenue',     'Austin',        'TX', '78701', '512-555-0700', '2021-07-01'),
('BR008',  'Capitol Hill',           '1700 Pennsylvania Ave',   'Washington',    'DC', '20006', '202-555-0800', '2022-01-15');

INSERT INTO banking.account_types (type_code, type_name, description, min_balance, interest_rate) VALUES
('CHECKING',    'Checking Account',            'Standard checking with debit card access',     0.00,    0.0010),
('SAVINGS',     'Savings Account',             'Interest-bearing savings account',              100.00,  0.0425),
('MONEY_MKT',   'Money Market Account',        'High-yield money market with limited checks',   2500.00, 0.0475),
('CD_6MO',      '6-Month CD',                  'Certificate of deposit — 6 month term',         1000.00, 0.0500),
('CD_12MO',     '12-Month CD',                 'Certificate of deposit — 12 month term',        1000.00, 0.0525),
('PREMIUM',     'Premium Checking',            'High-balance checking with perks',              25000.00,0.0150),
('STUDENT',     'Student Checking',            'No-fee checking for students under 25',         0.00,    0.0005),
('BUSINESS',    'Business Checking',           'Commercial checking with higher limits',        0.00,    0.0010);

INSERT INTO banking.transaction_types (type_code, type_name, direction) VALUES
('DEP',      'Deposit',                'CREDIT'),
('WDR',      'Withdrawal',             'DEBIT'),
('XFER_IN',  'Transfer In',            'CREDIT'),
('XFER_OUT', 'Transfer Out',           'DEBIT'),
('ACH_IN',   'ACH Credit',             'CREDIT'),
('ACH_OUT',  'ACH Debit',              'DEBIT'),
('WIRE_IN',  'Wire Transfer In',       'CREDIT'),
('WIRE_OUT', 'Wire Transfer Out',      'DEBIT'),
('FEE',      'Service Fee',            'DEBIT'),
('INT',      'Interest Payment',       'CREDIT'),
('ATM_WDR',  'ATM Withdrawal',         'DEBIT'),
('ATM_DEP',  'ATM Deposit',            'CREDIT'),
('POS',      'Point of Sale',          'DEBIT'),
('CHK',      'Check Payment',          'DEBIT'),
('DD',       'Direct Deposit',         'CREDIT'),
('REFUND',   'Refund',                 'CREDIT');

--------------------------------------------------------------------------------
-- Customers
--------------------------------------------------------------------------------

INSERT INTO banking.customers (first_name, last_name, email, phone, date_of_birth, address, city, state, zip_code, credit_score, created_at) VALUES
('James',    'Morrison',   'james.morrison@email.com',      '212-555-1001', '1985-03-15', '45 East 78th Street',      'New York',      'NY', '10075', 780, '2019-01-15 09:00:00'),
('Sarah',    'Chen',       'sarah.chen@email.com',          '415-555-1002', '1990-07-22', '1200 Pacific Avenue',      'San Francisco', 'CA', '94109', 810, '2019-02-20 10:30:00'),
('Michael',  'Thompson',   'michael.t@email.com',           '312-555-1003', '1978-11-03', '680 N Lake Shore Drive',   'Chicago',       'IL', '60611', 720, '2019-05-10 14:00:00'),
('Emily',    'Rodriguez',  'emily.r@email.com',             '617-555-1004', '1992-01-28', '15 Beacon Street',         'Boston',        'MA', '02108', 750, '2020-01-05 11:00:00'),
('David',    'Kim',        'david.kim@email.com',           '212-555-1005', '1988-09-12', '200 Central Park South',   'New York',      'NY', '10019', 800, '2020-03-15 09:30:00'),
('Lisa',     'Patel',      'lisa.patel@email.com',          '404-555-1006', '1995-04-18', '2500 Peachtree Road',      'Atlanta',       'GA', '30305', 690, '2020-06-20 13:00:00'),
('Robert',   'Williams',   'robert.w@email.com',            '512-555-1007', '1982-08-05', '500 West 5th Street',      'Austin',        'TX', '78703', 740, '2020-09-01 10:00:00'),
('Amanda',   'Foster',     'amanda.f@email.com',            '202-555-1008', '1991-12-30', '1600 K Street NW',         'Washington',    'DC', '20006', 770, '2021-01-10 15:00:00'),
('Thomas',   'Jackson',    'thomas.j@email.com',            '212-555-1009', '1975-06-14', '88 Greenwich Street',      'New York',      'NY', '10006', 830, '2021-04-05 09:00:00'),
('Jennifer', 'Lee',        'jennifer.lee@email.com',        '415-555-1010', '1993-10-25', '450 Sutter Street',        'San Francisco', 'CA', '94108', 760, '2021-07-20 11:30:00'),
('Christopher','Davis',    'chris.davis@email.com',         '312-555-1011', '1987-02-08', '100 E Walton Place',       'Chicago',       'IL', '60611', 710, '2021-09-15 14:00:00'),
('Maria',    'Garcia',     'maria.garcia@email.com',        '617-555-1012', '1994-05-19', '300 Atlantic Avenue',      'Boston',        'MA', '02210', 790, '2022-01-05 10:00:00'),
('William',  'Taylor',     'william.t@email.com',           '404-555-1013', '1980-03-22', '100 Centennial Olympic',   'Atlanta',       'GA', '30313', 680, '2022-03-20 09:30:00'),
('Rachel',   'Nguyen',     'rachel.n@email.com',            '512-555-1014', '1996-08-11', '200 Barton Springs Road',  'Austin',        'TX', '78704', 730, '2022-06-15 13:00:00'),
('Daniel',   'Brown',      'daniel.brown@email.com',        '202-555-1015', '1983-01-07', '900 F Street NW',          'Washington',    'DC', '20004', 820, '2022-09-01 11:00:00'),
('Jessica',  'Martinez',   'jessica.m@email.com',           '212-555-1016', '1989-11-15', '55 Water Street',          'New York',      'NY', '10041', 700, '2023-01-10 10:00:00'),
('Andrew',   'Wilson',     'andrew.w@email.com',            '415-555-1017', '1986-07-03', '101 Market Street',        'San Francisco', 'CA', '94105', 760, '2023-03-15 14:30:00'),
('Catherine','White',      'catherine.w@email.com',         '312-555-1018', '1997-09-28', '875 N Michigan Avenue',    'Chicago',       'IL', '60611', 740, '2023-06-01 09:00:00'),
('Steven',   'Anderson',   'steven.a@email.com',            '617-555-1019', '1981-04-12', '1 Federal Street',         'Boston',        'MA', '02110', 800, '2023-08-20 11:00:00'),
('Nicole',   'Thomas',     'nicole.t@email.com',            '404-555-1020', '1993-12-01', '191 Peachtree Tower',      'Atlanta',       'GA', '30303', 770, '2024-01-05 10:30:00');

--------------------------------------------------------------------------------
-- Accounts (2-3 per customer, ~50 total)
--------------------------------------------------------------------------------

INSERT INTO banking.accounts (account_number, customer_id, account_type, branch_id, balance, opened_date) VALUES
-- Customer 1 - James Morrison
('1001-0001-CHK', 1, 1, 1, 15420.50, '2019-01-15'),
('1001-0001-SAV', 1, 2, 1, 85000.00, '2019-01-15'),
('1001-0001-MMK', 1, 3, 1, 150000.00, '2020-06-01'),
-- Customer 2 - Sarah Chen
('1002-0002-CHK', 2, 1, 3, 28750.00, '2019-02-20'),
('1002-0002-SAV', 2, 2, 3, 120000.00, '2019-02-20'),
-- Customer 3 - Michael Thompson
('1003-0003-CHK', 3, 1, 4, 8320.75, '2019-05-10'),
('1003-0003-SAV', 3, 2, 4, 45000.00, '2019-05-10'),
('1003-0003-CD6', 3, 4, 4, 25000.00, '2024-01-15'),
-- Customer 4 - Emily Rodriguez
('1004-0004-CHK', 4, 1, 5, 12100.30, '2020-01-05'),
('1004-0004-SAV', 4, 2, 5, 38000.00, '2020-01-05'),
-- Customer 5 - David Kim
('1005-0005-PRM', 5, 6, 2, 250000.00, '2020-03-15'),
('1005-0005-SAV', 5, 2, 2, 500000.00, '2020-03-15'),
('1005-0005-MMK', 5, 3, 2, 350000.00, '2021-01-10'),
-- Customer 6 - Lisa Patel
('1006-0006-CHK', 6, 1, 6, 4250.00, '2020-06-20'),
('1006-0006-SAV', 6, 2, 6, 15000.00, '2020-06-20'),
-- Customer 7 - Robert Williams
('1007-0007-CHK', 7, 1, 7, 18900.00, '2020-09-01'),
('1007-0007-SAV', 7, 2, 7, 72000.00, '2020-09-01'),
('1007-0007-BIZ', 7, 8, 7, 145000.00, '2021-03-01'),
-- Customer 8 - Amanda Foster
('1008-0008-CHK', 8, 1, 8, 22400.00, '2021-01-10'),
('1008-0008-SAV', 8, 2, 8, 95000.00, '2021-01-10'),
-- Customer 9 - Thomas Jackson
('1009-0009-PRM', 9, 6, 1, 180000.00, '2021-04-05'),
('1009-0009-MMK', 9, 3, 1, 420000.00, '2021-04-05'),
('1009-0009-CD12',9, 5, 1, 100000.00, '2024-06-01'),
-- Customer 10 - Jennifer Lee
('1010-0010-CHK', 10, 1, 3, 9800.00, '2021-07-20'),
('1010-0010-SAV', 10, 2, 3, 32000.00, '2021-07-20'),
-- Customer 11 - Christopher Davis
('1011-0011-CHK', 11, 1, 4, 6540.00, '2021-09-15'),
('1011-0011-SAV', 11, 2, 4, 18500.00, '2021-09-15'),
-- Customer 12 - Maria Garcia
('1012-0012-CHK', 12, 1, 5, 14200.00, '2022-01-05'),
('1012-0012-SAV', 12, 2, 5, 55000.00, '2022-01-05'),
('1012-0012-CD6', 12, 4, 5, 30000.00, '2024-03-01'),
-- Customer 13 - William Taylor
('1013-0013-CHK', 13, 1, 6, 3100.00, '2022-03-20'),
('1013-0013-SAV', 13, 2, 6, 8000.00, '2022-03-20'),
-- Customer 14 - Rachel Nguyen
('1014-0014-STU', 14, 7, 7, 2850.00, '2022-06-15'),
('1014-0014-SAV', 14, 2, 7, 12000.00, '2022-06-15'),
-- Customer 15 - Daniel Brown
('1015-0015-PRM', 15, 6, 8, 320000.00, '2022-09-01'),
('1015-0015-SAV', 15, 2, 8, 200000.00, '2022-09-01'),
('1015-0015-MMK', 15, 3, 8, 175000.00, '2023-01-15'),
-- Customer 16 - Jessica Martinez
('1016-0016-CHK', 16, 1, 1, 7650.00, '2023-01-10'),
('1016-0016-SAV', 16, 2, 1, 22000.00, '2023-01-10'),
-- Customer 17 - Andrew Wilson
('1017-0017-CHK', 17, 1, 3, 19500.00, '2023-03-15'),
('1017-0017-SAV', 17, 2, 3, 68000.00, '2023-03-15'),
-- Customer 18 - Catherine White
('1018-0018-STU', 18, 7, 4, 3200.00, '2023-06-01'),
('1018-0018-SAV', 18, 2, 4, 9500.00, '2023-06-01'),
-- Customer 19 - Steven Anderson
('1019-0019-CHK', 19, 1, 5, 45000.00, '2023-08-20'),
('1019-0019-SAV', 19, 2, 5, 180000.00, '2023-08-20'),
('1019-0019-BIZ', 19, 8, 5, 290000.00, '2023-10-01'),
-- Customer 20 - Nicole Thomas
('1020-0020-CHK', 20, 1, 6, 11300.00, '2024-01-05'),
('1020-0020-SAV', 20, 2, 6, 42000.00, '2024-01-05');

--------------------------------------------------------------------------------
-- Employees
--------------------------------------------------------------------------------

INSERT INTO banking.employees (first_name, last_name, email, role, branch_id, hire_date, salary) VALUES
('Margaret', 'Hayes',     'margaret.h@bank.internal',  'Branch Manager',      1, '2018-01-15', 145000.00),
('Richard',  'Clarke',    'richard.c@bank.internal',   'Assistant Manager',   1, '2018-06-01', 105000.00),
('Patricia', 'Sullivan',  'patricia.s@bank.internal',  'Teller',              1, '2019-03-15',  52000.00),
('George',   'Nakamura',  'george.n@bank.internal',    'Branch Manager',      2, '2019-03-01', 140000.00),
('Sandra',   'Lopez',     'sandra.l@bank.internal',    'Loan Officer',        2, '2020-01-10',  88000.00),
('Kevin',    'O''Brien',  'kevin.ob@bank.internal',    'Branch Manager',      3, '2019-06-15', 155000.00),
('Diana',    'Chang',     'diana.c@bank.internal',     'Wealth Advisor',      3, '2020-03-01', 120000.00),
('Frank',    'Mueller',   'frank.m@bank.internal',     'Branch Manager',      4, '2020-01-10', 135000.00),
('Helen',    'Russo',     'helen.r@bank.internal',     'Teller',              4, '2021-06-15',  50000.00),
('Brian',    'Cooper',    'brian.c@bank.internal',      'Branch Manager',      5, '2020-09-01', 138000.00),
('Laura',    'Bennett',   'laura.b@bank.internal',     'Loan Officer',        5, '2021-04-01',  85000.00),
('Marcus',   'Reed',      'marcus.r@bank.internal',    'Branch Manager',      6, '2021-02-15', 132000.00),
('Angela',   'Price',     'angela.p@bank.internal',    'Teller',              6, '2022-01-10',  48000.00),
('Derek',    'Simmons',   'derek.s@bank.internal',     'Branch Manager',      7, '2021-07-01', 130000.00),
('Megan',    'Foster',    'megan.f@bank.internal',     'Financial Analyst',   7, '2022-06-01',  92000.00),
('Charles',  'Webb',      'charles.w@bank.internal',   'Branch Manager',      8, '2022-01-15', 142000.00),
('Valerie',  'Cruz',      'valerie.c@bank.internal',   'Compliance Officer',  8, '2022-09-01', 110000.00),
('Arthur',   'Stone',     'arthur.s@bank.internal',    'VP Operations',       1, '2018-01-15', 185000.00);

--------------------------------------------------------------------------------
-- Transactions (~200 across accounts, spanning 2023-2025)
--------------------------------------------------------------------------------

-- Helper: Generate transactions with realistic patterns
DO $$
DECLARE
    v_acct RECORD;
    v_bal NUMERIC(15,2);
    v_amt NUMERIC(15,2);
    v_date TIMESTAMP;
    v_ref_counter INTEGER := 10000;
    v_month INTEGER;
    v_day INTEGER;
BEGIN
    FOR v_acct IN
        SELECT account_id, balance, account_type, opened_date
        FROM banking.accounts
        WHERE account_type IN (1, 6, 7, 8) -- checking, premium, student, business
    LOOP
        v_bal := v_acct.balance;

        -- Generate 8-12 transactions per account
        FOR i IN 1..10 LOOP
            v_month := (i * 2) % 24 + 1;
            v_day := (i * 3) % 28 + 1;
            v_date := ('2024-01-01'::DATE + ((v_month - 1) || ' months')::INTERVAL + ((v_day - 1) || ' days')::INTERVAL)::TIMESTAMP;
            v_ref_counter := v_ref_counter + 1;

            -- Direct deposit (credit)
            v_amt := ROUND((RANDOM() * 4000 + 2000)::NUMERIC, 2);
            v_bal := v_bal + v_amt;
            INSERT INTO banking.transactions (account_id, transaction_type, amount, balance_after, description, reference_number, counterparty, channel, transaction_date, posted_date)
            VALUES (v_acct.account_id, 15, v_amt, v_bal, 'Payroll direct deposit', 'DD-' || v_ref_counter, 'Employer Corp', 'ACH', v_date, v_date::DATE);
            v_ref_counter := v_ref_counter + 1;

            -- POS purchase (debit)
            v_amt := ROUND((RANDOM() * 200 + 20)::NUMERIC, 2);
            v_bal := v_bal - v_amt;
            INSERT INTO banking.transactions (account_id, transaction_type, amount, balance_after, description, reference_number, counterparty, channel, transaction_date, posted_date)
            VALUES (v_acct.account_id, 13, v_amt, v_bal, 'Purchase', 'POS-' || v_ref_counter, 'Merchant #' || (i * 100 + v_acct.account_id), 'ONLINE', v_date + INTERVAL '2 days', (v_date + INTERVAL '2 days')::DATE);
            v_ref_counter := v_ref_counter + 1;

            -- ACH bill payment (debit) every other iteration
            IF i % 2 = 0 THEN
                v_amt := ROUND((RANDOM() * 500 + 100)::NUMERIC, 2);
                v_bal := v_bal - v_amt;
                INSERT INTO banking.transactions (account_id, transaction_type, amount, balance_after, description, reference_number, counterparty, channel, transaction_date, posted_date)
                VALUES (v_acct.account_id, 6, v_amt, v_bal, 'Bill payment', 'ACH-' || v_ref_counter, 'Utility Co', 'ONLINE', v_date + INTERVAL '5 days', (v_date + INTERVAL '5 days')::DATE);
                v_ref_counter := v_ref_counter + 1;
            END IF;

            -- ATM withdrawal every 3rd iteration
            IF i % 3 = 0 THEN
                v_amt := ROUND((RANDOM() * 300 + 40)::NUMERIC / 20, 0) * 20;
                v_bal := v_bal - v_amt;
                INSERT INTO banking.transactions (account_id, transaction_type, amount, balance_after, description, reference_number, counterparty, channel, transaction_date, posted_date)
                VALUES (v_acct.account_id, 11, v_amt, v_bal, 'ATM cash withdrawal', 'ATM-' || v_ref_counter, 'ATM #' || (1000 + v_acct.account_id), 'ATM', v_date + INTERVAL '10 days', (v_date + INTERVAL '10 days')::DATE);
                v_ref_counter := v_ref_counter + 1;
            END IF;
        END LOOP;
    END LOOP;
END $$;

-- Wire transfers for premium / high-value accounts
INSERT INTO banking.transactions (account_id, transaction_type, amount, balance_after, description, reference_number, counterparty, channel, transaction_date, posted_date) VALUES
(11, 7, 50000.00, 300000.00, 'Investment proceeds',       'WIRE-90001', 'Brokerage LLC',      'WIRE', '2024-03-15 10:00:00', '2024-03-15'),
(11, 8, 25000.00, 275000.00, 'Property escrow payment',   'WIRE-90002', 'Title Company Inc',   'WIRE', '2024-04-20 14:00:00', '2024-04-20'),
(21, 7, 75000.00, 255000.00, 'Trust distribution',        'WIRE-90003', 'Family Trust',        'WIRE', '2024-05-10 11:00:00', '2024-05-10'),
(35, 7, 100000.00, 420000.00,'Sale of securities',        'WIRE-90004', 'Investment Bank Co',  'WIRE', '2024-06-01 09:30:00', '2024-06-01'),
(35, 8, 40000.00, 380000.00, 'Real estate deposit',       'WIRE-90005', 'RE Holdings LLC',     'WIRE', '2024-07-15 16:00:00', '2024-07-15');

-- Service fees
INSERT INTO banking.transactions (account_id, transaction_type, amount, balance_after, description, reference_number, counterparty, channel, transaction_date, posted_date)
SELECT a.account_id, 9, 12.00, a.balance - 12.00,
       'Monthly maintenance fee', 'FEE-' || a.account_id || '-2024Q4', 'Bank', 'BRANCH',
       '2024-12-01 00:00:00', '2024-12-01'
FROM banking.accounts a
WHERE a.account_type IN (1, 8) AND a.balance < 10000;

-- Interest credits for savings accounts
INSERT INTO banking.transactions (account_id, transaction_type, amount, balance_after, description, reference_number, counterparty, channel, transaction_date, posted_date)
SELECT a.account_id, 10,
       ROUND(a.balance * 0.0425 / 12, 2),
       a.balance + ROUND(a.balance * 0.0425 / 12, 2),
       'Monthly interest credit', 'INT-' || a.account_id || '-2024DEC', 'Bank', 'BRANCH',
       '2024-12-31 00:00:00', '2024-12-31'
FROM banking.accounts a
WHERE a.account_type = 2;

--------------------------------------------------------------------------------
-- Loans
--------------------------------------------------------------------------------

INSERT INTO banking.loans (loan_number, customer_id, branch_id, loan_type, principal_amount, interest_rate, term_months, monthly_payment, outstanding_balance, origination_date, maturity_date, status) VALUES
('LN-2020-001', 1,  1, 'MORTGAGE',  450000.00, 0.0375, 360, 2084.45, 398000.00, '2020-06-15', '2050-06-15', 'CURRENT'),
('LN-2020-002', 3,  4, 'AUTO',       35000.00, 0.0499, 60,   660.49,  12500.00, '2020-11-01', '2025-11-01', 'CURRENT'),
('LN-2021-001', 5,  2, 'MORTGAGE',  850000.00, 0.0325, 360, 3697.28, 780000.00, '2021-03-01', '2051-03-01', 'CURRENT'),
('LN-2021-002', 7,  7, 'BUSINESS',  200000.00, 0.0650, 120, 2268.44, 145000.00, '2021-09-15', '2031-09-15', 'CURRENT'),
('LN-2022-001', 4,  5, 'PERSONAL',   15000.00, 0.0899, 36,   476.90,   3200.00, '2022-04-01', '2025-04-01', 'CURRENT'),
('LN-2022-002', 9,  1, 'MORTGAGE', 1200000.00, 0.0350, 360, 5388.56,1120000.00, '2022-08-15', '2052-08-15', 'CURRENT'),
('LN-2022-003', 11, 4, 'AUTO',       28000.00, 0.0549, 72,   459.62,  18000.00, '2022-10-01', '2028-10-01', 'CURRENT'),
('LN-2023-001', 12, 5, 'STUDENT',    45000.00, 0.0450, 120,  465.73,  38000.00, '2023-01-15', '2033-01-15', 'CURRENT'),
('LN-2023-002', 15, 8, 'MORTGAGE',  650000.00, 0.0625, 360, 4003.87, 625000.00, '2023-06-01', '2053-06-01', 'CURRENT'),
('LN-2023-003', 6,  6, 'PERSONAL',   10000.00, 0.1099, 24,   467.74,   2800.00, '2023-08-01', '2025-08-01', 'CURRENT'),
('LN-2024-001', 17, 3, 'AUTO',       42000.00, 0.0475, 60,   787.91,  35000.00, '2024-02-01', '2029-02-01', 'CURRENT'),
('LN-2024-002', 19, 5, 'BUSINESS',  350000.00, 0.0700, 84,  5387.15, 320000.00, '2024-04-15', '2031-04-15', 'CURRENT'),
('LN-2020-003', 13, 6, 'AUTO',       22000.00, 0.0599, 60,   425.64,     0.00,  '2020-05-01', '2025-05-01', 'PAID_OFF'),
('LN-2021-003', 6,  6, 'PERSONAL',    8000.00, 0.1299, 24,   381.44,   1200.00, '2021-12-01', '2023-12-01', 'DELINQUENT');

--------------------------------------------------------------------------------
-- Credit Cards
--------------------------------------------------------------------------------

INSERT INTO banking.credit_cards (card_number, customer_id, card_type, credit_limit, current_balance, apr, issued_date, expiry_date, status, rewards_points) VALUES
(encode(gen_random_bytes(16), 'hex'), 1,  'VISA',       15000.00,  3200.00, 0.1899, '2020-01-15', '2027-01-31', 'ACTIVE', 45200),
(encode(gen_random_bytes(16), 'hex'), 2,  'MASTERCARD', 25000.00,  8500.00, 0.1699, '2019-06-01', '2026-06-30', 'ACTIVE', 82100),
(encode(gen_random_bytes(16), 'hex'), 3,  'VISA',       10000.00,  4800.00, 0.2199, '2020-03-15', '2027-03-31', 'ACTIVE', 22500),
(encode(gen_random_bytes(16), 'hex'), 4,  'AMEX',       20000.00,  1200.00, 0.1799, '2021-01-01', '2028-01-31', 'ACTIVE', 67800),
(encode(gen_random_bytes(16), 'hex'), 5,  'VISA',       50000.00, 12000.00, 0.1499, '2020-06-15', '2027-06-30', 'ACTIVE', 156000),
(encode(gen_random_bytes(16), 'hex'), 6,  'MASTERCARD',  8000.00,  6200.00, 0.2499, '2021-03-01', '2028-03-31', 'ACTIVE', 8900),
(encode(gen_random_bytes(16), 'hex'), 7,  'VISA',       20000.00,  5400.00, 0.1899, '2021-06-01', '2028-06-30', 'ACTIVE', 38200),
(encode(gen_random_bytes(16), 'hex'), 8,  'AMEX',       30000.00,  2100.00, 0.1699, '2021-09-15', '2028-09-30', 'ACTIVE', 91500),
(encode(gen_random_bytes(16), 'hex'), 9,  'VISA',       75000.00, 18000.00, 0.1399, '2021-04-01', '2028-04-30', 'ACTIVE', 210000),
(encode(gen_random_bytes(16), 'hex'), 10, 'MASTERCARD', 12000.00,  3800.00, 0.2099, '2022-01-15', '2029-01-31', 'ACTIVE', 15600),
(encode(gen_random_bytes(16), 'hex'), 11, 'VISA',        8000.00,  7200.00, 0.2399, '2022-06-01', '2029-06-30', 'ACTIVE', 5400),
(encode(gen_random_bytes(16), 'hex'), 12, 'AMEX',       25000.00,  4500.00, 0.1799, '2022-03-01', '2029-03-31', 'ACTIVE', 52000),
(encode(gen_random_bytes(16), 'hex'), 15, 'VISA',       40000.00,  9800.00, 0.1599, '2023-01-01', '2030-01-31', 'ACTIVE', 78000),
(encode(gen_random_bytes(16), 'hex'), 17, 'MASTERCARD', 18000.00,  2900.00, 0.1899, '2023-06-15', '2030-06-30', 'ACTIVE', 24500),
(encode(gen_random_bytes(16), 'hex'), 19, 'AMEX',       35000.00,  6700.00, 0.1699, '2024-01-01', '2031-01-31', 'ACTIVE', 32000),
(encode(gen_random_bytes(16), 'hex'), 20, 'VISA',       15000.00,  1800.00, 0.1999, '2024-03-01', '2031-03-31', 'ACTIVE', 4200);
