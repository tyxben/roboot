# Tests

Minimal pytest suite for Roboot's Python modules. Relay Worker tests live
separately in `relay/test/` (TypeScript).

## Run

```bash
pip install pytest pytest-asyncio
pytest tests/
```

## Coverage

- `test_chat_store.py` — SQLite chat history persistence (create, record,
  list, retention purge, session isolation).
- `test_chat_handler.py` — shared Arcana streaming loop (frame sequence,
  persistence plumbing, `include_sessions_on_done` toggle).
- `test_relay_crypto.py` — relay E2EE round-trip (ECDH + HKDF + AES-GCM),
  tamper detection.
