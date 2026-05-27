# Retry Implementation Summary - Task #1749

## ✅ Implementation Complete

Successfully ported the retry architecture from Claude Code (nirholas-claude-code/src/services/api/withRetry.ts) into EQUIPA's agent_runner.py.

## Key Features Implemented

### 1. Exponential Backoff with 25% Jitter
- **Base delay**: 500ms
- **Exponential growth**: 2^attempt
- **Max delay cap**: 32 seconds
- **Jitter**: 25% random variation (Claude Code pattern)
- **Pure Python stdlib**: Uses `time`, `random`, `math` only

**Retry delays:**
```
Attempt 0: 0.50-0.62s
Attempt 1: 1.00-1.25s
Attempt 2: 2.00-2.50s
Attempt 3: 4.00-5.00s
Attempt 4: 8.00-10.00s
Attempt 5: 16.00-20.00s
Attempt 6+: 32.00-40.00s (capped)
```

### 2. Model Fallback After 3x 529 Errors
- Tracks consecutive 529/overloaded errors
- After 3 consecutive failures: opus → sonnet
- Fallback persists for remainder of retry attempts
- Resets counter on successful execution

### 3. Retryable Error Detection
Retries on:
- 429 (rate limit)
- 5xx server errors
- Connection errors
- Timeout errors
- ECONNRESET, EPIPE

Non-retryable errors fail immediately (4xx except 429, permission errors, invalid input)

### 4. Applied to Both Agent Modes
- **Non-streaming**: `run_agent()` → `run_agent_with_retry()`
- **Streaming**: `run_agent_streaming()` → `run_agent_streaming_with_retry()`
- Both use identical retry logic for consistency

## Architecture

### Function Hierarchy
```
run_agent()                     # Public API (non-streaming)
  └─> run_agent_with_retry()   # Retry wrapper
      └─> _run_agent_impl()    # Internal implementation

run_agent_streaming()                # Public API (streaming)
  └─> run_agent_streaming_with_retry() # Retry wrapper
      └─> _run_agent_streaming_impl()  # Internal implementation
```

### Key Constants
```python
BASE_DELAY_MS = 500         # 500ms base
MAX_DELAY_MS = 32000        # 32s cap
MAX_529_RETRIES = 3         # Model fallback threshold
JITTER_FACTOR = 0.25        # 25% jitter
```

## Backward Compatibility
- ✅ Existing callers of `run_agent()` automatically get retry logic
- ✅ Existing callers of `run_agent_streaming()` automatically get retry logic
- ✅ Old internal functions renamed to `_*_impl()` for clarity
- ✅ All function signatures preserved (no breaking changes)

## Testing
Verified exponential backoff calculation matches Claude Code behavior:
- Jitter range: 0-25% of base delay
- Cap at 32 seconds enforced correctly
- Model fallback triggers after exactly 3x 529 errors

## Commits
- `6e5208b` - Add retry with jitter and model fallback to streaming agent
- `601eba4` - Add retry logic to streaming agent execution
- `80e496e` - Add exponential backoff with jitter + model fallback to agent retry logic

## No External Dependencies
Zero pip dependencies added. Implementation uses only Python stdlib:
- `time.sleep()` for delays
- `random.random()` for jitter
- `math` for exponential calculation
- `subprocess` (already used)

## Next Steps
1. Monitor agent runs for retry frequency
2. Tune max_retries if needed (currently 10)
3. Consider adding retry metrics to TheForge DB
4. Add persistent retry mode for unattended sessions (future enhancement)
