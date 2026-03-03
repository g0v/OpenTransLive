# MongoDB Query Input Sanitization

## Summary
Added comprehensive input validation to prevent NoSQL injection attacks across all MongoDB queries in the application.

## Problem
User input was being used directly in MongoDB queries without sanitization, creating potential NoSQL injection vulnerabilities at multiple locations:
- URL path parameters (sid, id, token)
- WebSocket event data (session_id, secret_key, realtime_token)

## Solution
Implemented two sanitization functions that validate all user inputs before using them in MongoDB queries:

### 1. `sanitize_query_param(value: str, param_name: str) -> str`
- Used in HTTP endpoints (raises HTTPException on invalid input)
- Validates input is a string
- Rejects inputs containing MongoDB operator characters: `$` and `.`
- Rejects empty or whitespace-only strings

### 2. `validate_query_param(value: str, param_name: str) -> tuple[bool, str]`
- Used in WebSocket events (returns validation status)
- Same validation logic as sanitize_query_param
- Returns tuple: (is_valid, error_message)

## Protected Endpoints

### HTTP Endpoints
1. `/download/{id}` - Sanitizes session ID
2. `/yt/{id}` - Sanitizes session ID
3. `/rt/{id}` - Sanitizes session ID
4. `/panel/{sid}` - Sanitizes session ID
5. `/heartbeat/{sid}` - Sanitizes session ID
6. `/release-admin/{sid}` - Sanitizes session ID
7. `/api/tokens/{token}` - Sanitizes token parameter

### WebSocket Events
1. `join_session` - Validates session_id and secret_key
2. `sync` - Validates session ID from data

### Internal Functions
1. `is_realtime_authorized` - Validates realtime tokens

## Testing
A comprehensive test suite (`test_sanitization.py`) verifies the sanitization:
- Valid inputs: alphanumeric, hyphens, underscores
- Invalid inputs: MongoDB operators ($, .), empty strings, non-strings
- All test cases passed successfully

## Security Impact
These changes prevent attackers from:
- Injecting MongoDB query operators (e.g., `$ne`, `$gt`, `$where`)
- Using dot notation to access nested fields
- Bypassing authentication or authorization checks
- Accessing or modifying unauthorized data

## Files Modified
- `live_server/app/__init__.py` - Added sanitization functions and applied them to all vulnerable locations

## Backward Compatibility
These changes maintain full backward compatibility:
- Valid session IDs, tokens, and other parameters continue to work
- Only malicious inputs containing `$` or `.` are rejected
- Error messages are clear and informative
