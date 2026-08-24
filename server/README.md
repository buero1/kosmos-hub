# kosmos-hub server

## Start

1. Python `3.12+` verwenden
2. Abhaengigkeiten installieren
3. `.env.example` nach `.env` uebernehmen und Werte setzen
4. `uvicorn app.main:app --reload` starten

## Phase-1-Endpunkte

- `GET /healthz`
- `POST /api/v1/registrations`
- `GET /api/v1/sites`
- `GET /api/v1/sites/{site_id}`
- `GET /`
- `GET /sites`
- `GET /sites/{site_id}`

