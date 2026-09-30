"""
Test config. Settings is fail-fast, so the required Supabase vars must exist
before `app.core.config` is imported by anything under test.
"""

import os

os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-key")
os.environ.setdefault("APP_ENV", "test")
