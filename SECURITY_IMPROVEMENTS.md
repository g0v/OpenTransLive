# MongoDB Query Input Sanitization and Session ID Validation

## Summary
Added comprehensive input validation to prevent NoSQL injection attacks and session enumeration across all MongoDB queries in the application. Enhanced with strict session ID validation using regex patterns and length limits.

## Problem
User input was being used directly in MongoDB queries without sanitization, creating potential NoSQL injection vulnerabilities at multiple locations:
- URL path parameters (sid, id, token)
- WebSocket event data (session_id, secret_key, realtime_token)

Additional security risks identified:
- No format validation for session IDs (allowed special characters)
- No length limits on session IDs
- Potential for session enumeration and session fixation attacks

## Solution
Implemented two sanitization functions that validate all user inputs before using them in MongoDB queries:

### 1. `sanitize_query_param(value: str, param_name: str) -> str`
- Used in HTTP endpoints (raises HTTPException on invalid input)
- Validates input is a string
- Rejects empty or whitespace-only strings
- **For session IDs**: Enforces strict validation:
  - Alphanumeric characters, hyphens, and underscores only (regex: `^[a-zA-Z0-9_-]+$`)
  - Length between 4 and 64 characters
  - Prevents MongoDB operators and special characters
- **For other parameters**: Rejects MongoDB operator characters (`$` and `.`)

### 2. `validate_query_param(value: str, param_name: str) -> tuple[bool, str]`
- Used in WebSocket events (returns validation status)
- Same validation logic as sanitize_query_param
- Returns tuple: (is_valid, error_message)
- **Enhanced with session ID-specific validation** (same rules as above)

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
- **Enumerating session IDs** through malformed input
- **Session fixation attacks** using specially crafted session IDs
- **Special character injection** that could bypass sanitization

## Implementation Details

### Session ID Detection
The enhanced validation automatically detects session ID parameters by checking if:
- The parameter name contains "session" (case-insensitive), OR
- The parameter name equals "sid" (case-insensitive)

This ensures all session-related parameters receive strict validation without requiring code changes at every call site.

### Validation Rules for Session IDs
1. **Type check**: Must be a string
2. **Non-empty check**: Cannot be empty or whitespace-only
3. **Length validation**: 4-64 characters (prevents both too-short guessable IDs and excessively long inputs)
4. **Format validation**: Only alphanumeric characters (a-z, A-Z, 0-9), hyphens (-), and underscores (_)
5. **Special character prevention**: Rejects all other characters including MongoDB operators

## Files Modified
- `live_server/app/__init__.py` - Enhanced sanitization functions with strict session ID validation and applied them to all vulnerable locations

## Backward Compatibility
These changes maintain backward compatibility for valid session IDs:
- Session IDs using alphanumeric characters, hyphens, and underscores continue to work
- YouTube video IDs (alphanumeric with hyphens/underscores) remain compatible
- UUIDs with hyphens are supported
- Only malicious or malformed inputs are rejected
- Error messages are clear and informative, specifying exact validation requirements
