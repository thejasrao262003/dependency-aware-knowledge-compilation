# PostgreSQL at Halcyon

PostgreSQL is the primary database for the Ledger service. PostgreSQL supports Transactions. PostgreSQL supports JSONB. PostgreSQL supports Indexing.

## Operations

PostgreSQL runs on three nodes managed by Patroni. Patroni elects the PostgreSQL leader through Etcd.

Nightly backups of PostgreSQL are written to S3 by pgBackRest.
