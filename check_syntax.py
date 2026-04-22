import ast
import sys

with open("sodex_bot_v2.py", "r") as f:
    source = f.read()

try:
    tree = ast.parse(source)
    print("Syntax check: PASSED")
    
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            print(f"  Class: {node.name}")
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    print(f"    Method: {item.name}")
    
    # Count lines
    lines = source.split("\n")
    print(f"\n  Total lines: {len(lines)}")
    
except SyntaxError as e:
    print(f"Syntax ERROR: {e}")
    sys.exit(1)
