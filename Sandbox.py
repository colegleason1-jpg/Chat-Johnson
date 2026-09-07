import subprocess
import os

def create_worktree(branch_name="staging-agent-branch", path="../sandbox_workspace"):
    if os.path.exists(path):
        subprocess.run(["git", "worktree", "remove", "--force", path], check=False)
    result = subprocess.run(["git", "worktree", "add", path, "-b", branch_name], capture_output=True, text=True)
    if result.returncode == 0:
        return True, path
    return False, result.stderr
