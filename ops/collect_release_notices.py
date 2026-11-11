"""Stream complete installed license/notice files from the exact image to stdout."""
from pathlib import Path
import sys
import tarfile

roots = ("/usr/share/doc", "/usr/share/licenses", "/usr/local", "/app", "/opt", "/root/.cache", "/home", "/cache")
seen = set()
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as output:
    for root in roots:
        path = Path(root)
        if not path.exists():
            continue
        for member in path.rglob("*"):
            name = member.name.lower()
            # Entire distro documentation includes compressed copyrights and component notices.
            selected = root.startswith("/usr/share/") or any(word in name for word in ("license", "licence", "copying", "copyright", "notice")) or name == "metadata"
            if selected and member.is_file() and not member.is_symlink() and str(member) not in seen:
                output.add(member, arcname=str(member).lstrip("/"), recursive=False)
                seen.add(str(member))
