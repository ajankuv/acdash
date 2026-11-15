"""AC Dash — AC Infinity dashboard."""
import os

# Bump this, add a CHANGELOG.md section, push to main, then tag v<version> to release.
__version__ = "1.0.0"

# Commit the image was built from (Docker build arg GIT_SHA); "dev" outside a CI build.
REVISION = (os.environ.get("ACDASH_REVISION") or "dev").strip()[:7] or "dev"
