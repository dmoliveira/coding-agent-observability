"""Print documented Make targets, grouped by their owning file."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GROUPS = (
    ("Setup", "setup.mk"),
    ("Server", "stack.mk"),
    ("Integrations", "integrations.mk"),
    ("Backup", "backup.mk"),
    ("Development", "../Makefile"),
)

print("AI Observatory commands\n")
for heading, filename in GROUPS:
    path = ROOT / "make" / filename
    if not path.exists():
        continue
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        before, sep, description = line.partition("##")
        if sep and ":" in before:
            name = before.split(":", 1)[0].strip()
            if name and not any(char in name for char in " $%"):
                entries.append((name, description.strip()))
    if entries:
        print(f"{heading}:")
        for name, description in entries:
            print(f"  {name:22} {description}")
        print()
print("  help                   Show this help")
