"""Neutral settings for every test, set before vader.config reads .env.vader (which never overrides them),
so the tests don't depend on the local setup and never touch a real GitHub repo or save files. A test that
needs a setting turns it on itself."""
import os

for name, value in {"FAILED_REPLIES_DIR": "", "VERIFY_SEVERITIES": "", "AUTO_MERGE_BRANCH": "",
                    "CLOSE_LINKED_ISSUES": "false", "MODEL_RUNTIME": "generic", "VALIDATOR_ASKS": "",
                    "NEEDS_PERSON_PATHS": ""}.items():
    os.environ.setdefault(name, value)
