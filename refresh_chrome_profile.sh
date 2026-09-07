#!/bin/bash
set -euo pipefail
cat <<'MSG'
Sessions now live in data/profiles/shared and are reused by every browser workflow.
This command no longer deletes or copies Chrome profiles.
Use Platforms → Log in for an expired session. Finish the current browser task first.
If an older automation window is open, close it once before the first new managed run.
MSG
