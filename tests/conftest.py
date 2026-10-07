import sys
import os

# Add project root and src/nl2infra to sys.path
root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
src_dir = os.path.join(root_dir, "src", "nl2infra")

for p in [root_dir, src_dir]:
    if p not in sys.path:
        sys.path.insert(0, p)
