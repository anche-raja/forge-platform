import os
from pathlib import Path
from typing import List


def scan_java_files(source_dir: str, phase: str = "java21") -> List[str]:
    """Return relative paths of all .java files eligible for migration."""
    source_path = Path(source_dir).resolve()
    results = []

    for root, _dirs, files in os.walk(source_path):
        for fname in files:
            if not fname.endswith(".java"):
                continue
            abs_path = Path(root) / fname
            rel_path = str(abs_path.relative_to(source_path))

            # Skip test files
            if "src/test" in rel_path.replace("\\", "/"):
                continue

            # Skip generated files
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                if "DO NOT EDIT" in content[:500]:
                    continue
            except OSError:
                continue

            results.append(str(abs_path))

    return sorted(results)
