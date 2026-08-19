import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_shell_script_parses():
    scripts = [
        ROOT / "Install.command",
        ROOT / "Start.command",
        ROOT / "Download Model.command",
        ROOT / "Doctor.command",
        *sorted((ROOT / "scripts").glob("*.sh")),
    ]

    for script in scripts:
        subprocess.run(["bash", "-n", str(script)], check=True)
