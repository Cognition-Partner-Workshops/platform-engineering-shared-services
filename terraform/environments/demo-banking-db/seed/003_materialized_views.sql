--------------------------------------------------------------------------------
-- Banking Demo — Materialized Views
--
-- Pre-computed analytics views suitable for dashboard UIs. These demonstrate
-- how materialized views can be queried via JDBC/MCP connectors without
-- hitting expensive joins or aggregations at runtime.
--------------------------------------------------------------------------------

SET search_path TO banking, public;

--------------------------------------------------------------------------------
-- 1. Customer Portfolio Summary
--    One row per customer with total balances across products.
--------------------------------------------------------------------------------

CREATE MATERIALIZED VIEW banking.mv_customer_portfolio AS
SELECT
    c.customer_id,
    c.first_name || ' ' || c.last_name AS customer_name,
    c.email,
    c.city || ', ' || c.state AS location,
    c.credit_score,
    COUNT(DISTINCT a.account_id) AS total_accounts,
    COALESCE(SUM(a.balance) FILTER (WHERE at.type_code IN ('CHECKING', 'PREMIUM', 'STUDENT', 'BUSINESS')), 0) AS checking_balance,
    COALESCE(SUM(a.balance) FILTER (WHERE at.type_code = 'SAVINGS'), 0) AS savings_balance,
    COALESCE(SUM(a.balance) FILTER (WHERE at.type_code IN ('MONEY_MKT', 'CD_6MO', 'CD_12MO')), 0) AS investment_balance,
    COALESCE(SUM(a.balance), 0) AS total_deposit_balance,
    COALESCE((SELECT SUM(l.outstanding_balance) FROM banking.loans l WHERE l.customer_id = c.customer_id AND l.status != 'PAID_OFF'), 0) AS total_loan_balance,
    COALESCE((SELECT SUM(cc.current_balance) FROM banking.credit_cards cc WHERE cc.customer_id = c.customer_id AND cc.status = 'ACTIVE'), 0) AS total_card_balance,
    c.created_at AS customer_since
FROM banking.customers c
LEFT JOIN banking.accounts a ON a.customer_id = c.customer_id AND a.status = 'ACTIVE'
LEFT JOIN banking.account_types at ON at.type_id = a.account_type
WHERE c.is_active = TRUE
GROUP BY c.customer_id, c.first_name, c.last_name, c.email, c.city, c.state, c.credit_score, c.created_at
ORDER BY total_deposit_balance DESC
WITH DATA;

CREATE UNIQUE INDEX idx_mv_customer_portfolio_id ON banking.mv_customer_portfolio (customer_id);

--------------------------------------------------------------------------------
-- 2. Branch Performance Dashboard
--    Aggregated metrics per branch.
--------------------------------------------------------------------------------

CREATE MATERIALIZED VIEW banking.mv_branch_performance AS
SELECT
    b.branch_id,
    b.branch_code,
    b.branch_name,
    b.city || ', ' || b.state AS location,
    COALESCE(accts.total_accounts, 0) AS total_accounts,
    COALESCE(accts.total_customers, 0) AS total_customers,
    COALESCE(accts.total_deposits, 0) AS total_deposits,
    ROUND(COALESCE(accts.avg_account_balance, 0), 2) AS avg_account_balance,
    COALESCE(emps.employee_count, 0) AS employee_count,
    COALESCE(emps.total_payroll, 0) AS total_payroll,
    COALESCE(lns.active_loans, 0) AS active_loans,
    COALESCE(lns.total_loan_portfolio, 0) AS total_loan_portfolio,
    b.opened_date
FROM banking.branches b
LEFT JOIN (
    SELECT branch_id, COUNT(*) AS total_accounts, COUNT(DISTINCT customer_id) AS total_customers,
           SUM(balance) AS total_deposits, AVG(balance) AS avg_account_balance
    FROM banking.accounts WHERE status = 'ACTIVE' GROUP BY branch_id
) accts ON accts.branch_id = b.branch_id
LEFT JOIN (
    SELECT branch_id, COUNT(*) AS employee_count, SUM(salary) AS total_payroll
    FROM banking.employees WHERE is_active = TRUE GROUP BY branch_id
) emps ON emps.branch_id = b.branch_id
LEFT JOIN (
    SELECT branch_id, COUNT(*) AS active_loans, SUM(outstanding_balance) AS total_loan_portfolio
    FROM banking.loans WHERE status IN ('CURRENT', 'DELINQUENT') GROUP BY branch_id
) lns ON lns.branch_id = b.branch_id
WHERE b.is_active = TRUE
ORDER BY total_deposits DESC
WITH DATA;

CREATE UNIQUE INDEX idx_mv_branch_performance_id ON banking.mv_branch_performance (branch_id);

--------------------------------------------------------------------------------
-- 3. Monthly Transaction Volume
--    Aggregated transaction metrics by month and channel.
--------------------------------------------------------------------------------

CREATE MATERIALIZED VIEW banking.mv_monthly_transaction_volume AS
SELECT
    DATE_TRUNC('month', t.transaction_date)::DATE AS month,
    t.channel,
    tt.direction,
    COUNT(*) AS transaction_count,
    SUM(t.amount) AS total_amount,
    ROUND(AVG(t.amount), 2) AS avg_amount,
    MIN(t.amount) AS min_amount,
    MAX(t.amount) AS max_amount
FROM banking.transactions t
JOIN banking.transaction_types tt ON tt.type_id = t.transaction_type
GROUP BY DATE_TRUNC('month', t.transaction_date)::DATE, t.channel, tt.direction
ORDER BY month DESC, total_amount DESC
WITH DATA;

CREATE INDEX idx_mv_monthly_txn_month ON banking.mv_monthly_transaction_volume (month);

--------------------------------------------------------------------------------
-- 4. Loan Risk Summary
--    Risk segmentation across the loan portfolio.
--------------------------------------------------------------------------------

CREATE MATERIALIZED VIEW banking.mv_loan_risk_summary AS
SELECT
    l.loan_type,
    l.status,
    COUNT(*) AS loan_count,
    SUM(l.principal_amount) AS total_principal,
    SUM(l.outstanding_balance) AS total_outstanding,
    ROUND(AVG(l.interest_rate) * 100, 2) AS avg_interest_rate_pct,
    ROUND(AVG(l.term_months), 0) AS avg_term_months,
    ROUND(AVG(c.credit_score), 0) AS avg_borrower_credit_score,
    MIN(c.credit_score) AS min_credit_score,
    MAX(c.credit_score) AS max_credit_score,
    ROUND(
        SUM(l.outstanding_balance) * 100.0 /
        NULLIF(SUM(l.principal_amount), 0), 2
    ) AS outstanding_to_principal_pct
FROM banking.loans l
JOIN banking.customers c ON c.customer_id = l.customer_id
GROUP BY l.loan_type, l.status
ORDER BY l.loan_type, l.status
WITH DATA;

--------------------------------------------------------------------------------
-- 5. Credit Card Utilization
--    Card portfolio health and utilization metrics.
--------------------------------------------------------------------------------

CREATE MATERIALIZED VIEW banking.mv_credit_card_utilization AS
SELECT
    cc.card_type,
    COUNT(*) AS card_count,
    SUM(cc.credit_limit) AS total_credit_limit,
    SUM(cc.current_balance) AS total_balance,
    ROUND(SUM(cc.current_balance) * 100.0 / NULLIF(SUM(cc.credit_limit), 0), 2) AS utilization_pct,
    ROUND(AVG(cc.apr) * 100, 2) AS avg_apr_pct,
    SUM(cc.rewards_points) AS total_rewards_points,
    ROUND(AVG(cc.current_balance), 2) AS avg_balance,
    COUNT(*) FILTER (WHERE cc.current_balance > cc.credit_limit * 0.9) AS near_limit_count,
    COUNT(*) FILTER (WHERE cc.current_balance = 0) AS zero_balance_count
FROM banking.credit_cards cc
WHERE cc.status = 'ACTIVE'
GROUP BY cc.card_type
ORDER BY total_balance DESC
WITH DATA;

--------------------------------------------------------------------------------
-- 6. Daily Balance Trend (last 30 days of transaction activity)
--    Shows net flow per day for dashboard sparklines.
--------------------------------------------------------------------------------

CREATE MATERIALIZED VIEW banking.mv_daily_balance_trend AS
SELECT
    t.posted_date,
    COUNT(*) AS transaction_count,
    SUM(CASE WHEN tt.direction = 'CREDIT' THEN t.amount ELSE 0 END) AS total_credits,
    SUM(CASE WHEN tt.direction = 'DEBIT' THEN t.amount ELSE 0 END) AS total_debits,
    SUM(CASE WHEN tt.direction = 'CREDIT' THEN t.amount ELSE -t.amount END) AS net_flow
FROM banking.transactions t
JOIN banking.transaction_types tt ON tt.type_id = t.transaction_type
GROUP BY t.posted_date
ORDER BY t.posted_date DESC
WITH DATA;

CREATE UNIQUE INDEX idx_mv_daily_balance_date ON banking.mv_daily_balance_trend (posted_date);

--------------------------------------------------------------------------------
-- 7. Top Customers by Assets Under Management
--    High-value customer view for relationship management.
--------------------------------------------------------------------------------

CREATE MATERIALIZED VIEW banking.mv_top_customers_aum AS
SELECT
    c.customer_id,
    c.first_name || ' ' || c.last_name AS customer_name,
    c.credit_score,
    b.branch_name AS primary_branch,
    COALESCE(dep.total_deposits, 0) AS total_deposits,
    COALESCE(loan.total_loans, 0) AS total_loans,
    COALESCE(card.total_card_balance, 0) AS total_card_balance,
    COALESCE(dep.total_deposits, 0) - COALESCE(loan.total_loans, 0) - COALESCE(card.total_card_balance, 0) AS net_position,
    COALESCE(dep.account_count, 0) AS account_count,
    COALESCE(loan.loan_count, 0) AS loan_count,
    COALESCE(card.card_count, 0) AS card_count,
    c.created_at AS customer_since,
    CASE
        WHEN COALESCE(dep.total_deposits, 0) >= 500000 THEN 'PLATINUM'
        WHEN COALESCE(dep.total_deposits, 0) >= 100000 THEN 'GOLD'
        WHEN COALESCE(dep.total_deposits, 0) >= 25000  THEN 'SILVER'
        ELSE 'STANDARD'
    END AS tier
FROM banking.customers c
LEFT JOIN LATERAL (
    SELECT SUM(a.balance) AS total_deposits, COUNT(*) AS account_count,
           MIN(a.branch_id) AS primary_branch_id
    FROM banking.accounts a WHERE a.customer_id = c.customer_id AND a.status = 'ACTIVE'
) dep ON TRUE
LEFT JOIN banking.branches b ON b.branch_id = dep.primary_branch_id
LEFT JOIN LATERAL (
    SELECT SUM(l.outstanding_balance) AS total_loans, COUNT(*) AS loan_count
    FROM banking.loans l WHERE l.customer_id = c.customer_id AND l.status NOT IN ('PAID_OFF', 'CHARGED_OFF')
) loan ON TRUE
LEFT JOIN LATERAL (
    SELECT SUM(cc.current_balance) AS total_card_balance, COUNT(*) AS card_count
    FROM banking.credit_cards cc WHERE cc.customer_id = c.customer_id AND cc.status = 'ACTIVE'
) card ON TRUE
WHERE c.is_active = TRUE
ORDER BY net_position DESC
WITH DATA;

CREATE UNIQUE INDEX idx_mv_top_customers_id ON banking.mv_top_customers_aum (customer_id);

--------------------------------------------------------------------------------
-- Refresh function (can be called on demand or via pg_cron)
--------------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION banking.refresh_all_views()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY banking.mv_customer_portfolio;
    REFRESH MATERIALIZED VIEW CONCURRENTLY banking.mv_branch_performance;
    REFRESH MATERIALIZED VIEW banking.mv_monthly_transaction_volume;
    REFRESH MATERIALIZED VIEW banking.mv_loan_risk_summary;
    REFRESH MATERIALIZED VIEW banking.mv_credit_card_utilization;
    REFRESH MATERIALIZED VIEW CONCURRENTLY banking.mv_daily_balance_trend;
    REFRESH MATERIALIZED VIEW CONCURRENTLY banking.mv_top_customers_aum;
END;
$$ LANGUAGE plpgsql;
