"""Evaluator definitions for coding agent sessions.

Each CRITERIA_* constant is a Python function body that HoneyHive executes
server-side. The function receives an `event` dict with keys:
  inputs, outputs, feedback, metadata, event_type, event_name

These evaluators are agent-agnostic: they normalize tool names across
Claude Code (PascalCase: Bash, Read, Write) and other frameworks
(snake_case: bash, file_read, file_write).
"""

# ---------------------------------------------------------------------------
# Helpers shared across evaluators (inlined into each criteria string since
# HoneyHive evaluators are self-contained functions).
# ---------------------------------------------------------------------------

_TOOL_NORMALIZER = """
def normalize_tool(name):
    \"\"\"Map tool event names to canonical categories.\"\"\"
    if not name or not name.startswith("tool."):
        return None
    t = name[5:]
    low = t.lower()
    BASH = {"bash"}
    FILE_READ = {"read", "file_read"}
    FILE_WRITE = {"write", "file_write", "file_create"}
    FILE_EDIT = {"edit", "file_edit"}
    FILE_SEARCH = {"glob", "grep", "file_search"}
    AGENT = {"agent"}
    WEB = {"webfetch", "websearch", "web_search"}
    if low in BASH:
        return "bash"
    if low in FILE_READ:
        return "file_read"
    if low in FILE_WRITE:
        return "file_write"
    if low in FILE_EDIT:
        return "file_edit"
    if low in FILE_SEARCH:
        return "file_search"
    if low in AGENT:
        return "agent"
    if low in WEB:
        return "web"
    if low.startswith("mcp__"):
        return "mcp"
    return "other"

def extract_events_from_artifact(event):
    \"\"\"Extract event-like records from session artifact transcript.\"\"\"
    outputs = event.get("outputs") or {}
    artifact = outputs.get("artifact") or {}
    content = artifact.get("content") or []
    if not isinstance(content, list):
        return []
    return content

def tool_calls_from_record(record):
    \"\"\"Return one call per tool invocation in raw or normalized records.\"\"\"
    ename = record.get("event_name", "")
    if ename.startswith("tool."):
        return [{
            "name": ename[5:],
            "input": record.get("tool_input") or record.get("input") or {},
        }]

    etype = record.get("type", "")
    tool_name = record.get("tool_name") or record.get("toolName") or record.get("name")
    if etype in ("tool_use", "tool_result") and tool_name:
        return [{
            "name": str(tool_name),
            "input": record.get("tool_input") or record.get("input") or {},
        }]

    message = record.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []

    calls = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            calls.append({
                "name": str(block.get("name") or "other"),
                "input": block.get("input") or {},
            })
    return calls

def tool_names_from_record(record):
    \"\"\"Return one tool name per tool invocation in raw or normalized records.\"\"\"
    return [call["name"] for call in tool_calls_from_record(record)]

def record_is_tool(record):
    \"\"\"True when the record represents at least one tool event.\"\"\"
    if record.get("event_name", "").startswith("tool."):
        return True
    if record.get("type", "") in ("tool_use", "tool_result"):
        return True
    return bool(tool_names_from_record(record))

def record_is_model(record):
    \"\"\"True when the record represents model/user turn activity.\"\"\"
    ename = record.get("event_name", "")
    hook = record.get("hook_event_name", "")
    if ename.startswith("turn.") or hook in ("UserPromptSubmit", "Stop"):
        return True
    if record.get("type", "") in ("text", "thinking", "assistant"):
        return True
    message = record.get("message")
    return isinstance(message, dict) and message.get("role") == "assistant"
"""

# ---------------------------------------------------------------------------
# 1a. Bash Ratio (neutral metric — not inherently bad)
# ---------------------------------------------------------------------------
# High bash for discovery (rg, fd, jq, yq) is ENCOURAGED.
# This is a descriptive metric, not a quality judgment.

CRITERIA_BASH_RATIO = _TOOL_NORMALIZER + """
def evaluate(event):
    records = extract_events_from_artifact(event)
    tool_count = 0
    bash_count = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        names = tool_names_from_record(r)
        if names:
            for tool_name in names:
                tool_count += 1
                if normalize_tool("tool." + tool_name) == "bash":
                    bash_count += 1
        elif record_is_tool(r):
            tool_count += 1
    if tool_count == 0:
        return 0.0
    return round(bash_count / tool_count, 3)

result = evaluate(event)
"""

# ---------------------------------------------------------------------------
# 1b. Bash Edit Misuse
# ---------------------------------------------------------------------------
# Only flags bash commands doing in-place file editing (sed -i, awk -i)
# instead of the native Edit tool, which is more accurate and parallel.

CRITERIA_BASH_EDIT_MISUSE = _TOOL_NORMALIZER + """
import re

def evaluate(event):
    records = extract_events_from_artifact(event)
    edit_tool_count = 0
    bash_edit_count = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        calls = tool_calls_from_record(r)
        categories = [normalize_tool("tool." + call["name"]) for call in calls]
        if not categories and record_is_tool(r):
            categories = [normalize_tool(r.get("event_name", ""))]
        if "file_edit" in categories:
            edit_tool_count += 1
        for call, category in zip(calls, categories):
            if category != "bash":
                continue
            cmd = ""
            tool_input = call.get("input") or {}
            if isinstance(tool_input, dict):
                cmd = str(tool_input.get("command", ""))
            elif isinstance(tool_input, str):
                cmd = tool_input
            if re.search(r'\\b(sed|awk)\\b.*-i', cmd):
                bash_edit_count += 1
    total_edits = edit_tool_count + bash_edit_count
    if total_edits == 0:
        return 0.0
    return round(bash_edit_count / total_edits, 3)

result = evaluate(event)
"""

# ---------------------------------------------------------------------------
# 2. File Search Spam
# ---------------------------------------------------------------------------

CRITERIA_FILE_SEARCH_SPAM = _TOOL_NORMALIZER + """
def evaluate(event):
    records = extract_events_from_artifact(event)
    tool_count = 0
    search_count = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        names = tool_names_from_record(r)
        if names:
            for tool_name in names:
                tool_count += 1
                if normalize_tool("tool." + tool_name) == "file_search":
                    search_count += 1
        elif record_is_tool(r):
            tool_count += 1
    if tool_count == 0:
        return 0.0
    return round(search_count / tool_count, 3)

result = evaluate(event)
"""

# ---------------------------------------------------------------------------
# 3. Permission Bottleneck
# ---------------------------------------------------------------------------

CRITERIA_PERMISSION_BOTTLENECK = _TOOL_NORMALIZER + """
def evaluate(event):
    records = extract_events_from_artifact(event)
    total = 0
    permission_count = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        total += 1
        # Check for permission notifications
        ntype = r.get("notification_type", "")
        msg = str(r.get("message", ""))
        etype = r.get("type", "")
        if ntype == "permission_prompt" or "permission" in msg.lower():
            permission_count += 1
        elif etype == "tool_use":
            # Check for PermissionRequest hook events
            hook = r.get("hook_event_name", "")
            if hook == "PermissionRequest":
                permission_count += 1
    if total == 0:
        return 0.0
    return round(permission_count / total, 3)

result = evaluate(event)
"""

# ---------------------------------------------------------------------------
# 4. Subagent Lifecycle Health
# ---------------------------------------------------------------------------

CRITERIA_SUBAGENT_LIFECYCLE = """
def evaluate(event):
    outputs = event.get("outputs") or {}
    artifact = outputs.get("artifact") or {}
    content = artifact.get("content") or []
    if not isinstance(content, list):
        return True
    starts = 0
    stops = 0
    for r in content:
        if not isinstance(r, dict):
            continue
        ename = r.get("event_name", "")
        etype = r.get("type", "")
        hook = r.get("hook_event_name", "")
        if ename == "chain.subagent.start" or hook == "SubagentStart":
            starts += 1
        elif ename == "chain.subagent.stop" or hook == "SubagentStop":
            stops += 1
    if starts == 0:
        return True
    return starts == stops

result = evaluate(event)
"""

# ---------------------------------------------------------------------------
# 5. Session Size Alert
# ---------------------------------------------------------------------------

CRITERIA_SESSION_SIZE = """
def evaluate(event):
    outputs = event.get("outputs") or {}
    artifact = outputs.get("artifact") or {}
    content = artifact.get("content") or []
    if not isinstance(content, list):
        return 0.0
    return float(len(content))

result = evaluate(event)
"""

# ---------------------------------------------------------------------------
# 6. Tool-to-Model Ratio
# ---------------------------------------------------------------------------

CRITERIA_TOOL_MODEL_RATIO = _TOOL_NORMALIZER + """
def evaluate(event):
    records = extract_events_from_artifact(event)
    tool_count = 0
    model_count = 0
    for r in records:
        if not isinstance(r, dict):
            continue
        names = tool_names_from_record(r)
        if names:
            tool_count += len(names)
        elif record_is_tool(r):
            tool_count += 1
        if record_is_model(r):
            model_count += 1
    if model_count == 0:
        return 0.0 if tool_count == 0 else float(tool_count)
    return round(tool_count / model_count, 2)

result = evaluate(event)
"""

# ---------------------------------------------------------------------------
# 7. Task Completion (LLM evaluator) — runs on session.start with chat_history
# ---------------------------------------------------------------------------

LLM_TASK_COMPLETION = """You are evaluating whether a coding AI agent successfully completed the user's task.

Review the conversation between the user and the AI coding agent below:

{{ outputs.chat_history }}

Evaluate whether the agent accomplished what the user asked for. Consider:
- Did the agent understand the user's intent correctly?
- Did the agent produce working code or complete the requested action?
- Were there unresolved errors or incomplete steps at the end?
- Did the agent get stuck in loops or go off-track?

Rate the task completion on a scale of 1-5:
1 = Failed completely or went off-track
2 = Partially attempted but left significant gaps
3 = Mostly complete but with notable issues
4 = Successfully completed with minor imperfections
5 = Fully and cleanly completed

Your rating: [[X]]"""

# ---------------------------------------------------------------------------
# 8. Approach Efficiency (LLM evaluator) — runs on session.end with artifact
# ---------------------------------------------------------------------------

LLM_APPROACH_EFFICIENCY = """You are evaluating the efficiency of a coding AI agent's approach to solving a task.

Review the full execution trajectory of the agent below. This includes all tool calls, thinking blocks, file operations, and decisions:

{{ outputs.artifact }}

Evaluate how efficiently the agent worked. Consider:
- Did it explore the codebase in a targeted way, or waste time with repetitive searches?
- Did it use the right tools (e.g. Grep/Glob instead of shelling out to grep/find)?
- Did it avoid unnecessary file reads, redundant bash commands, or circular approaches?
- Did it plan before acting, or did it trial-and-error excessively?
- Did it use subagents/parallelism effectively when appropriate?
- Was the number of steps proportionate to the task complexity?

Rate the approach efficiency on a scale of 1-5:
1 = Extremely wasteful — excessive repetition, wrong tools, circular exploration
2 = Inefficient — significant wasted effort but eventually productive
3 = Adequate — some inefficiency but generally reasonable
4 = Efficient — well-targeted with minor improvements possible
5 = Optimal — direct, well-planned, minimal wasted steps

Your rating: [[X]]"""


# ---------------------------------------------------------------------------
# Registry: all evaluator definitions for programmatic creation
# ---------------------------------------------------------------------------

EVALUATORS = [
    {
        "name": "Coding Agent - Bash Ratio",
        "description": "Fraction of tool calls that are bash/shell. This is a neutral descriptive metric — high bash for discovery (rg, fd, jq) is fine and encouraged.",
        "type": "PYTHON",
        "criteria": CRITERIA_BASH_RATIO,
        "return_type": "float",
        "scale": 1,
        "enabled_in_prod": True,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "chain", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.end", "type": "string"},
            ]
        },
    },
    {
        "name": "Coding Agent - Bash Edit Misuse",
        "description": "Fraction of file edits done via bash (sed -i, awk -i) instead of the native Edit tool. Edit is more accurate and supports parallel calls.",
        "type": "PYTHON",
        "criteria": CRITERIA_BASH_EDIT_MISUSE,
        "return_type": "float",
        "scale": 1,
        "threshold": {"min": 0, "max": 0.5, },
        "enabled_in_prod": True,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "chain", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.end", "type": "string"},
            ]
        },
    },
    {
        "name": "Coding Agent - File Search Spam",
        "description": "Fraction of tool calls that are file search/glob. Values >0.4 indicate brute-force exploration instead of targeted search.",
        "type": "PYTHON",
        "criteria": CRITERIA_FILE_SEARCH_SPAM,
        "return_type": "float",
        "scale": 1,
        "threshold": {"min": 0, "max": 0.4, },
        "enabled_in_prod": True,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "chain", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.end", "type": "string"},
            ]
        },
    },
    {
        "name": "Coding Agent - Permission Bottleneck",
        "description": "Fraction of events that are permission prompts. High values mean the agent was blocked waiting for human approval — configure auto-approve for safe tools.",
        "type": "PYTHON",
        "criteria": CRITERIA_PERMISSION_BOTTLENECK,
        "return_type": "float",
        "scale": 1,
        "threshold": {"min": 0, "max": 0.15, },
        "enabled_in_prod": True,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "chain", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.end", "type": "string"},
            ]
        },
    },
    {
        "name": "Coding Agent - Subagent Lifecycle",
        "description": "Whether all subagent starts have matching stops. False indicates orphaned subagents.",
        "type": "PYTHON",
        "criteria": CRITERIA_SUBAGENT_LIFECYCLE,
        "return_type": "boolean",
        "enabled_in_prod": True,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "chain", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.end", "type": "string"},
            ]
        },
    },
    {
        "name": "Coding Agent - Session Event Count",
        "description": "Total number of events in the session transcript. Values >500 may indicate stuck/looping behavior.",
        "type": "PYTHON",
        "criteria": CRITERIA_SESSION_SIZE,
        "return_type": "float",
        "scale": 10000,
        "threshold": {"min": 1, "max": 500, },
        "enabled_in_prod": True,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "chain", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.end", "type": "string"},
            ]
        },
    },
    {
        "name": "Coding Agent - Tool to Model Ratio",
        "description": "Ratio of tool calls to model/thinking events. Very high values (>30) suggest insufficient reasoning between actions.",
        "type": "PYTHON",
        "criteria": CRITERIA_TOOL_MODEL_RATIO,
        "return_type": "float",
        "scale": 100,
        "threshold": {"min": 0, "max": 30, },
        "enabled_in_prod": True,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "chain", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.end", "type": "string"},
            ]
        },
    },
    {
        "name": "Coding Agent - Task Completion",
        "description": "LLM judge rating (1-5) of whether the agent successfully completed the user's task, based on the chat history.",
        "type": "LLM",
        "criteria": LLM_TASK_COMPLETION,
        "return_type": "float",
        "scale": 5,
        "threshold": {"min": 3, "max": 5, },
        "model_provider": "openai",
        "model_name": "gpt-5.4-mini",
        "enabled_in_prod": False,
        "sampling_percentage": 20,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "session", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.start", "type": "string"},
            ]
        },
    },
    {
        "name": "Coding Agent - Approach Efficiency",
        "description": "LLM judge rating (1-5) of whether the agent took an efficient approach, based on the full execution trajectory.",
        "type": "LLM",
        "criteria": LLM_APPROACH_EFFICIENCY,
        "return_type": "float",
        "scale": 5,
        "threshold": {"min": 3, "max": 5, },
        "model_provider": "openai",
        "model_name": "gpt-5.4-mini",
        "enabled_in_prod": False,
        "sampling_percentage": 20,
        "filters": {
            "filterArray": [
                {"field": "event_type", "operator": "is", "value": "chain", "type": "string"},
                {"field": "event_name", "operator": "is", "value": "session.end", "type": "string"},
            ]
        },
    },
]
