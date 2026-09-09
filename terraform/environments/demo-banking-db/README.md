# Banking Demo Database

A Terraform-managed AWS RDS PostgreSQL instance seeded with a realistic banking schema — customers, accounts, transactions, loans, credit cards, and **7 materialized views** for analytics dashboards.

Built for quick demos showing how Devin can connect to a data layer via JDBC or MCP connector, query DDL, and expose materialized views in a UI application.

## Architecture

- **RDS PostgreSQL 16** on `db.t4g.micro` (single-AZ, private by default)
- Dedicated VPC with public subnets and internet gateway
- Security group on port 5432 restricted to the VPC CIDR by default; trusted operator CIDRs can be allow-listed (wildcards such as `0.0.0.0/0` are rejected)
- S3 remote state backend with DynamoDB locking

## Quick Start

### Prerequisites

- AWS CLI configured with credentials
- Terraform >= 1.5.0
- `psql` client (for seeding)

### Deploy

```bash
cd terraform/environments/demo-banking-db

terraform init
terraform apply
```

By default the instance has no public IP and only accepts connections from inside its VPC (e.g. via a bastion, VPN, or SSM port-forward). To connect directly from a workstation, opt in explicitly and allow-list only your egress IP:

```bash
terraform apply \
  -var 'publicly_accessible=true' \
  -var 'allowed_cidr_blocks=["203.0.113.10/32"]'
```

`publicly_accessible=true` without `allowed_cidr_blocks`, or any wildcard CIDR (`0.0.0.0/0`, `::/0`), is rejected at plan time.

### Seed the Database

```bash
# Get connection details from Terraform outputs
export DB_HOST=$(terraform output -raw db_hostname)
export DB_NAME=$(terraform output -raw db_name)
export DB_USER=$(terraform output -raw db_username)

# Set the database credential (same value as db_password in terraform.tfvars)
read -sp "DB Password: " DB_PW && echo

# Run seed scripts in order
for f in seed/001_schema.sql seed/002_seed_data.sql seed/003_materialized_views.sql; do
  PGPASSWORD="$DB_PW" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -f "$f"
done
```

### Connection Details

| Property | Value |
|----------|-------|
| **JDBC URL** | `jdbc:postgresql://<endpoint>:5432/banking` |
| **Database** | `banking` |
| **Schema** | `banking` |
| **Username** | `bankadmin` |
| **Password** | *(set in `terraform.tfvars`)* |

## Schema Overview

### Core Tables

| Table | Description |
|-------|-------------|
| `banking.customers` | 20 customers across 6 US cities |
| `banking.accounts` | ~50 accounts (checking, savings, money market, CDs, premium, student, business) |
| `banking.transactions` | ~250+ transactions (deposits, POS, ACH, wire, ATM, fees, interest) |
| `banking.branches` | 8 branches across major US cities |
| `banking.employees` | 18 employees with roles and salaries |
| `banking.loans` | 14 loans (mortgage, auto, personal, student, business) |
| `banking.credit_cards` | 16 credit cards (Visa, Mastercard, Amex) |
| `banking.account_types` | Reference: 8 account type definitions |
| `banking.transaction_types` | Reference: 16 transaction type definitions |

### Materialized Views

| View | Description |
|------|-------------|
| `mv_customer_portfolio` | Customer-level portfolio summary with deposits, loans, and card balances |
| `mv_branch_performance` | Branch-level KPIs: deposits, customers, employees, loan portfolio |
| `mv_monthly_transaction_volume` | Monthly aggregates by channel and direction |
| `mv_loan_risk_summary` | Loan portfolio risk segmentation by type and status |
| `mv_credit_card_utilization` | Card portfolio health metrics by card type |
| `mv_daily_balance_trend` | Daily credit/debit/net flow for sparkline charts |
| `mv_top_customers_aum` | High-value customers ranked by net position with tier assignment |

All views can be refreshed on demand via `SELECT banking.refresh_all_views();`

## Teardown

```bash
cd terraform/environments/demo-banking-db
terraform destroy -auto-approve
```

This removes the RDS instance, VPC, subnets, security groups, and all associated resources.
