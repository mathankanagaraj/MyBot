import ast
import sys

def check_syntax(filename):
    try:
        with open(filename, "r") as f:
            source = f.read()
        ast.parse(source)
        print(f"✅ Syntax OK: {filename}")
        return True
    except SyntaxError as e:
        print(f"❌ Syntax Error in {filename}: {e}")
        return False
    except Exception as e:
        print(f"❌ Error reading {filename}: {e}")
        return False

files = [
    "src/core/angel_client.py",
    "src/core/worker.py"
]

success = True
for f in files:
    if not check_syntax(f):
        success = False

if not success:
    sys.exit(1)
