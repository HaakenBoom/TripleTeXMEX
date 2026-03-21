# Tripletex API Reference

This folder contains real API responses from the Tripletex sandbox.
Used to build the system prompt so Claude knows exactly what fields to send — no guessing, no 422 errors.

## Files
- `required_fields/` — Validation errors from empty POST requests (tells us what's required)
- `schemas/` — Full entity structures from GET requests (tells us what fields exist)
- `test_creates/` — Successful create responses (tells us what comes back)
