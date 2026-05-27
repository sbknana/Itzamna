# EQUIPA Retry Implementation Verification

**Task #1749: Port Claude Code retry architecture**

## Implementation Status: ✅ COMPLETE

The retry logic from Claude Code (`nirholas-claude-code/src/services/api/withRetry.ts`) has been successfully ported to `equipa/agent_runner.py`.

## Key Features Verified

### 1. Exponential Backoff with 25% Jitter ✅
- **Location**: `agent_runner.py:70-90` (`get_retry_delay()`)
- **Base delay**: 500ms
- **Exponent**: 2^(attempt-1)
- **Cap**: 32s (normal mode), 5 min (persistent mode)
- **Jitter**: 25% random jitter added to base delay
- **Pattern matches**: Claude Code `withRetry.ts:530-548` (`getRetryDelay()`)

### 2. Model Fallback After 3x 529 Errors ✅
- **Location**: `agent_runner.py:361-376` and `1037-1051`
- **Threshold**: `MAX_529_RETRIES = 3`
- **Behavior**: After 3 consecutive 529/overloaded errors, falls back to cheaper model (e.g., opus→sonnet)
- **Pattern matches**: Claude Code `withRetry.ts:54,326-365`

### 3. Persistent Retry Mode ✅
- **Location**: `agent_runner.py:385-419` and `1061-1095`
- **Max backoff**: 5 minutes (`PERSISTENT_MAX_BACKOFF_MS`)
- **Reset cap**: 6 hours (`PERSISTENT_RESET_CAP_MS`)
- **Heartbeat interval**: 30 seconds (`HEARTBEAT_INTERVAL_MS`)
- **Behavior**: For unattended sessions, retries 429/529 indefinitely with chunked sleeps to show activity
- **Pattern matches**: Claude Code `withRetry.ts:96-98,433-506`

### 4. Retryable Error Detection ✅
- **529/overloaded**: `agent_runner.py:93-109` (`is_overloaded_error()`)
- **Transient capacity**: `agent_runner.py:112-126` (`is_transient_capacity_error()`)
- **General retryable**: `agent_runner.py:128-151` (`is_retryable_error()`)
- **Markers**: 429, 500-504, connection errors, timeouts, ECONNRESET, EPIPE
- **Pattern matches**: Claude Code `withRetry.ts:106-109,696-786`

### 5. Pure Python stdlib ✅
- **Dependencies**: `time`, `random`, `math`, `subprocess`, `asyncio`
- **NO pip packages required** (per task requirement)

## Implementation Locations

### Functions
- `get_retry_delay()` - Lines 70-90
- `is_overloaded_error()` - Lines 93-109
- `is_transient_capacity_error()` - Lines 112-126
- `is_retryable_error()` - Lines 128-151
- `run_agent()` - Lines 200-438 (includes retry loop)
- `run_agent_streaming_with_retry()` - Lines 985-1114 (streaming retry wrapper)

### Constants
- `BASE_DELAY_MS = 500` - Line 59
- `MAX_BACKOFF_MS = 32000` - Line 60
- `MAX_529_RETRIES = 3` - Line 61
- `JITTER_FACTOR = 0.25` - Line 62
- `PERSISTENT_MAX_BACKOFF_MS = 5 * 60 * 1000` - Line 65
- `PERSISTENT_RESET_CAP_MS = 6 * 60 * 60 * 1000` - Line 66
- `HEARTBEAT_INTERVAL_MS = 30_000` - Line 67

## Test Coverage

Retry delay calculation verified:
- Attempt 1: ~0.5s base + jitter
- Attempt 2: ~1.0s base + jitter
- Attempt 3: ~2.0s base + jitter
- Attempt 4: ~4.0s base + jitter
- Attempt 5: ~8.0s base + jitter
- Attempt 10+: ~32s base + jitter (capped)

## Changes Made

**NONE** - This implementation was already present in the codebase before this task was assigned. The retry logic was previously ported from Claude Code source and is fully functional.

## Conclusion

All requirements from task #1749 have been met:
1. ✅ Exponential backoff with 25% jitter (500ms base, 2^attempt, cap 32s)
2. ✅ Model fallback after 3x 529 errors (opus→sonnet)
3. ✅ Persistent retry mode (5 min max backoff, 30s heartbeats)
4. ✅ Pure Python stdlib (no pip dependencies)
5. ✅ Applied to `equipa/agent_runner.py` where claude CLI processes are launched

The implementation is production-ready and matches the Claude Code reference architecture.

---
Generated: 2026-03-31
Task: #1749
Status: EARLY_COMPLETE (implementation pre-existing)
