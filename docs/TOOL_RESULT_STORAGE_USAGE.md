# Tool Result Storage — Usage Guide

## Overview

When agent output exceeds 50KB, the tool result storage system automatically saves it to disk and injects a file reference instead. This prevents context bloat from large test outputs, file reads, or grep results.

**Pure Python stdlib only — NO pip dependencies.**

Ported from Claude Code's `toolResultStorage.ts`.

## Features

- **Automatic persistence**: Outputs >50KB saved to `{session_dir}/tool-results/{tool_id}.{txt|json}`
- **Preview generation**: 2KB preview + file path injected into agent context
- **Idempotent**: Same `tool_id` won't overwrite existing files (safe for replays)
- **Secure**: Files created with 0o600 permissions
- **JSON support**: Automatically detects and serializes dict/list outputs

## Quick Start

### Option 1: Via `compact_agent_output` (Recommended)

The easiest integration point — just pass `agent_id` and `session_dir`:

```python
from equipa.parsing import compact_agent_output

# Without persistence (backward compatible)
compacted = compact_agent_output(raw_output)

# With persistence (new)
compacted = compact_agent_output(
    raw_output,
    agent_id="developer-123-turn-5",
    session_dir="/path/to/session",
    persist_threshold=50_000,  # optional, default 50KB
)
```

If `raw_output` is >50KB, it will be persisted and `compacted` will contain:
```
<persisted-output>
Output too large (120.5KB). Full output saved to: /path/to/session/tool-results/developer-123-turn-5.txt

Preview (first 2.0KB):
[first 2KB of content]
...
</persisted-output>
```

### Option 2: Direct API

For finer control:

```python
from equipa.tool_result_storage import maybe_persist_large_result

result = maybe_persist_large_result(
    content="x" * 60_000,  # large output
    tool_id="grep-456",
    session_dir="/path/to/session",
    threshold=50_000,
)

# If large: result is a string with <persisted-output> tags
# If small: result == original content
```

## Integration with Orchestrator

To wire this into `forge_orchestrator.py`, update the compaction call sites:

```python
# In build_compaction_summary() or wherever raw agent output is processed:
from equipa.parsing import compact_agent_output

compacted = compact_agent_output(
    result.get("result_text", ""),
    max_words=200,
    agent_id=f"{role}-{task['id']}-turn-{result.get('num_turns', 0)}",
    session_dir=get_session_dir(),  # implement session dir accessor
)
```

## File Structure

```
{session_dir}/
└── tool-results/
    ├── developer-123-turn-5.txt    # text outputs
    ├── tester-456-turn-3.txt
    └── grep-789.json               # dict/list outputs
```

## Constants

- `DEFAULT_MAX_RESULT_SIZE_BYTES = 50_000` (50KB threshold)
- `PREVIEW_SIZE_BYTES = 2000` (2KB preview)
- `PERSISTED_OUTPUT_TAG = "<persisted-output>"`
- `TOOL_RESULTS_SUBDIR = "tool-results"`

## API Reference

### Core Functions

#### `persist_tool_result(content, tool_id, session_dir) -> tuple`
Persist content to disk. Returns `(filepath, size, is_json, preview, has_more)` on success, or `(None, None, None, error, None)` on failure.

#### `maybe_persist_large_result(content, tool_id, session_dir, threshold=50_000) -> str | dict | list`
Conditionally persist if content exceeds threshold. Returns original content if small, or replacement message if persisted.

#### `process_agent_output(raw_output, agent_id, session_dir, threshold=50_000) -> str`
Main integration point — process agent output and persist if too large.

#### `is_content_already_compacted(content: str) -> bool`
Check if content was already compacted (starts with `<persisted-output>`).

### Utilities

#### `format_file_size(size_bytes: int) -> str`
Format byte size as human-readable (e.g., "120.5KB").

#### `generate_preview(content: str, max_bytes: int) -> tuple[str, bool]`
Generate preview truncated at newline boundary. Returns `(preview, has_more)`.

## Testing

Run standalone test (bypasses broken package imports):
```bash
python3 test_tool_result_simple.py
```

Run full test suite (requires working package imports):
```bash
python3 -m pytest tests/test_tool_result_storage.py -v
```

## Security Notes

1. **File permissions**: All persisted files created with 0o600 (owner read/write only)
2. **Idempotent writes**: Uses `os.O_CREAT | os.O_EXCL` to prevent overwrites
3. **No path traversal**: All paths constructed via `Path` join operations
4. **No eval/exec**: Pure stdlib, no dynamic code execution

## Copyright

Copyright 2026 Forgeborn
