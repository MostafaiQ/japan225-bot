"""
Applies a unified diff to a project file, commits, and pushes.
Security:
  - Path must be inside project root
  - Only .py / .json / .md extensions
  - Never .env or *.db
  - patch --dry-run before applying
  - git stash <file> for rollback if patch fails
"""
import os
import subprocess
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
ALLOWED_EXTS = {".py", ".json", ".md"}
BLOCKED_NAMES = {".env", ".env.example"}


def _run(cmd: list[str], cwd: str = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd or str(PROJECT_ROOT),
        capture_output=True, text=True, check=check
    )


def apply_fix(target: str, diff: str) -> dict:
    """
    Apply a unified diff to target file.
    Returns {"success": True, "message": "..."} or raises RuntimeError.
    """
    # ── Validate path ─────────────────────────────────────────────────────────
    target_path = (PROJECT_ROOT / target).resolve()
    if not str(target_path).startswith(str(PROJECT_ROOT)):
        raise RuntimeError("Path traversal rejected")
    if target_path.suffix not in ALLOWED_EXTS:
        raise RuntimeError(f"Extension '{target_path.suffix}' not allowed (only .py .json .md)")
    if target_path.name in BLOCKED_NAMES or target_path.suffix == ".db":
        raise RuntimeError("Blocked file")
    if not target_path.exists():
        raise RuntimeError(f"File not found: {target}")

    # ── Write diff to temp file ───────────────────────────────────────────────
    with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False) as f:
        f.write(diff)
        patch_file = f.name

    try:
        # ── Dry run first ─────────────────────────────────────────────────────
        dry = _run(
            ["patch", "--dry-run", "-p1", "-i", patch_file],
            check=False
        )
        if dry.returncode != 0:
            raise RuntimeError(f"Patch dry-run failed:\n{dry.stdout}\n{dry.stderr}")

        # ── Stash the file for rollback ───────────────────────────────────────
        _run(["git", "stash", "push", "--", str(target_path.relative_to(PROJECT_ROOT))], check=False)

        # ── Apply patch ───────────────────────────────────────────────────────
        apply = _run(["patch", "-p1", "-i", patch_file], check=False)
        if apply.returncode != 0:
            # Rollback via stash
            _run(["git", "stash", "pop"], check=False)
            raise RuntimeError(f"Patch apply failed:\n{apply.stdout}\n{apply.stderr}")

        # ── Git add + commit + push ───────────────────────────────────────────
        rel = str(target_path.relative_to(PROJECT_ROOT))
        _run(["git", "add", rel])
        _run(["git", "commit", "-m", f"fix: apply dashboard patch to {rel}"])
        push = _run(["git", "push", "origin", "main"], check=False)
        push_msg = "pushed" if push.returncode == 0 else f"push failed: {push.stderr.strip()}"

        return {"success": True, "message": f"Patch applied and committed ({push_msg})."}

    finally:
        os.unlink(patch_file)
