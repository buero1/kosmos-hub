"""Shared, non-production settings for the server test suite."""

import os


# Tests must never require or inherit the Hub's live deployment credentials.
os.environ["APP_SECRET_KEY"] = "test-only-secret-key-with-at-least-32-characters"
os.environ["DATABASE_URL"] = "mysql+pymysql://kosmos_test:local-test-password@127.0.0.1/kosmos_hub_test"
