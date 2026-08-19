import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_shell_script_parses():
    scripts = [
        *sorted(ROOT.glob("*.command")),
        *sorted((ROOT / "scripts").glob("*.sh")),
    ]

    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)
