# Migrations

Phase 1 aktiviert `AUTO_CREATE_TABLES` fuer schnelles lokales Bootstrapping.

Sobald das erste echte Deployment fuer `kosmos-hub` steht, sollen die Tabellen
ueber Alembic-Revisionen statt ueber `create_all()` verwaltet werden.

