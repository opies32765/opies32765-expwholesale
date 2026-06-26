#!/bin/bash
# DealerPrice read-only export (operator-authorized 2026-06-26). No writes, no LLM, no iPacket.
exec python3 /usr/local/bin/dp_export.py
