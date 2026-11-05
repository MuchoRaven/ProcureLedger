"""
worker.py
=========
Sandboxed execution worker for ProcureLedger tool calls.
Executes serialized requests with zero network access and restricted I/O.
"""

import sys
import json
import traceback

def main():
    try:
        # Read serialized input from stdin
        raw_input = sys.stdin.read()
        if not raw_input:
            print(json.dumps({"ok": False, "error": "Empty input received by worker."}))
            sys.exit(1)

        request = json.loads(raw_input)
        tool_name = request.get("tool")
        args = request.get("args", {})

        # Execute sandboxed logic based on tool name
        if tool_name == "ping":
            result = {"ok": True, "message": "pong", "args": args}
        else:
            result = {"ok": False, "error": f"Unknown or restricted tool in sandbox: {tool_name}"}

        print(json.dumps({"ok": True, "result": result}))
        sys.exit(0)

    except Exception as e:
        tb = traceback.format_exc()
        print(json.dumps({"ok": False, "error": str(e), "traceback": tb}))
        sys.exit(1)

if __name__ == "__main__":
    main()
