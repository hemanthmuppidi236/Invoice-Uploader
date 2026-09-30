"""
Supabase clients.

Two clients, same split as the pay app:
  - `service_client`: service-role key, bypasses RLS, used for every backend
    write and for any read that needs to see across users.
  - `anon_client`:    anon key, respects RLS, used only to verify user tokens.
"""

from typing import Optional

from supabase import Client, create_client

from .config import settings


_service_client: Optional[Client] = None
_anon_client: Optional[Client] = None


def get_service_client() -> Client:
    global _service_client
    if _service_client is None:
        _service_client = create_client(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
    return _service_client


def get_anon_client() -> Client:
    global _anon_client
    if _anon_client is None:
        _anon_client = create_client(
            settings.supabase_url,
            settings.supabase_anon_key,
        )
    return _anon_client
