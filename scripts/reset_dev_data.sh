#!/usr/bin/env bash
# Wipe every account and its data from a *development* deployment.
#
# Verifying multi-tenancy means creating real accounts, and a few dozen of them
# accumulate quickly - each with its own directory and vector index. This clears
# them. It refuses to run against anything that does not look local, because
# "the script that deletes every customer" is not one you want to be one
# mistyped DATABASE_URL away from.
set -euo pipefail

cd "$(dirname "$0")/.."
[ -f .env ] && set -a && . ./.env && set +a

URL="${DATABASE_URL:-sqlite}"
case "$URL" in
  *127.0.0.1*|*localhost*|sqlite*) ;;
  *) echo "Refusing to reset: DATABASE_URL does not look local ($URL)"; exit 1 ;;
esac

read -r -p "Delete EVERY account and all tenant data at ${URL}? [y/N] " reply
[ "$reply" = "y" ] || { echo "Nothing done."; exit 0; }

# Containers first: a running worker would otherwise keep writing to a
# directory that is about to disappear.
docker ps -aq --filter "label=deleg8.role=worker" | xargs -r docker rm -f >/dev/null 2>&1 || true

# A heredoc has no __file__, which python-dotenv's find_dotenv() asserts on, so
# the environment is exported above and read from there instead.
.venv/bin/python - <<'PY'
from src.db.base import init_engine, session_scope
from src.db.models import User
from sqlalchemy import select

init_engine()
with session_scope() as session:
    users = session.scalars(select(User)).all()
    for user in users:
        session.delete(user)
    print(f"deleted {len(users)} account(s)")
PY

rm -rf data/tenants
echo "removed data/tenants"
