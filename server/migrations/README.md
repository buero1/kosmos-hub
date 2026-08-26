# Migrations

Phase 1 aktiviert `AUTO_CREATE_TABLES` fuer schnelles lokales Bootstrapping.
Kleine additive Spaltenaenderungen werden beim Start des Hubs abgesichert, damit
ein bestehendes Phase-1-Deployment nicht manuell migriert werden muss.

Sobald das erste echte Deployment fuer `kosmos-hub` steht, sollen die Tabellen
ueber Alembic-Revisionen statt ueber `create_all()` verwaltet werden.
