#!/usr/bin/env python3
"""
Test script to verify session ID validation functionality.
This demonstrates the security improvements for session ID validation.
"""

import re
from typing import Tuple


def validate_session_id(value: str, param_name: str = "session ID") -> Tuple[bool, str]:
    """
    Validate session ID to prevent NoSQL injection and enumeration attacks.

    Enforces:
    - Alphanumeric characters, hyphens, and underscores only
    - Length between 4 and 64 characters
    - No MongoDB special characters ($ and .)

    Args:
        value: The input string to validate
        param_name: Name of the parameter for error messages

    Returns:
        Tuple of (is_valid, error_message)
    """
    if not isinstance(value, str):
        return False, f"Invalid {param_name}: must be a string"

    if not value.strip():
        return False, f"Invalid {param_name}: cannot be empty"

    if len(value) < 4 or len(value) > 64:
        return False, f"Invalid {param_name}: must be between 4 and 64 characters"

    if not re.match(r'^[a-zA-Z0-9_-]+$', value):
        return False, f"Invalid {param_name}: must contain only alphanumeric characters, hyphens, and underscores"

    return True, ""


def run_tests():
    """Run validation tests and display results."""
    test_cases = [
        # Valid cases
        ("valid-session-123", True, "Valid alphanumeric with hyphens"),
        ("session_id_456", True, "Valid with underscores"),
        ("ABC123xyz", True, "Valid alphanumeric"),
        ("test", True, "Valid minimum length (4 chars)"),
        ("a" * 64, True, "Valid maximum length (64 chars)"),
        ("video-id_123-abc", True, "Valid YouTube-style ID"),

        # Invalid cases - Special characters
        ("session$ne", False, "MongoDB operator ($)"),
        ("session.field", False, "MongoDB dot notation"),
        ("test@session", False, "Special character (@)"),
        ("session#123", False, "Special character (#)"),
        ("test session", False, "Contains space"),
        ("session!123", False, "Special character (!)"),

        # Invalid cases - Length
        ("abc", False, "Too short (3 chars)"),
        ("a" * 65, False, "Too long (65 chars)"),
        ("", False, "Empty string"),
        ("   ", False, "Whitespace only"),

        # Invalid cases - Type
        (123, False, "Non-string (integer)"),
        (None, False, "None value"),
    ]

    print("=" * 80)
    print("SESSION ID VALIDATION TEST RESULTS")
    print("=" * 80)
    print()

    passed = 0
    failed = 0

    for test_input, expected_valid, description in test_cases:
        is_valid, error_msg = validate_session_id(test_input)
        status = "PASS" if is_valid == expected_valid else "FAIL"

        if is_valid == expected_valid:
            passed += 1
            icon = "✓"
        else:
            failed += 1
            icon = "✗"

        print(f"{icon} {status}: {description}")
        print(f"  Input: {repr(test_input)}")
        print(f"  Expected: {'Valid' if expected_valid else 'Invalid'}")
        print(f"  Result: {'Valid' if is_valid else 'Invalid'}")
        if error_msg:
            print(f"  Error: {error_msg}")
        print()

    print("=" * 80)
    print(f"SUMMARY: {passed} passed, {failed} failed out of {len(test_cases)} tests")
    print("=" * 80)

    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    exit(0 if success else 1)
