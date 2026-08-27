"""Melbourne Agent Village — deterministic domain library.

Pure, unit-testable simulation logic. No AWS SDK calls live in the pure
engines; all external I/O (DynamoDB, Bedrock model invocation, memory writes)
is supplied through injected callables / interfaces so the logic stays
deterministic and testable.

Python 3.12+, standard library + boto3 only.
"""

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = [1]

# Melbourne map bounds (Requirement 3 / 8).
MAP_LAT_MIN = -38.00
MAP_LAT_MAX = -37.70
MAP_LON_MIN = 144.85
MAP_LON_MAX = 145.10

TIMEZONE = "Australia/Melbourne"

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "MAP_LAT_MIN",
    "MAP_LAT_MAX",
    "MAP_LON_MIN",
    "MAP_LON_MAX",
    "TIMEZONE",
]
