import ast
import subprocess

def validate_python_syntax(code_string):
    try:
        ast.parse(code_string)
        return True, "Syntax valid."
    except SyntaxError as e:
        return False, str(e)

def run_pytest_sandbox(test_dir="tests/"):
    result = subprocess.run(["pytest", test_dir], capture_output=True, text=True)
    if result.returncode == 0:
        return True, result.stdout
    return False, result.stderr
