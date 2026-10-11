"""Conservative publication allowlist and credential scan; never prints matches."""
import argparse
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOT_FILES = {"README.md", ".gitignore", ".env.example", "server.py", "run_live.py", "evals.py"}
DOCS = {"WORKFLOW.md", "EXTERNAL_SETUP.md", "RESUME.md", "IMPROVEMENTS.md", "VALIDATION.md", "SCALING.md"}
PATTERNS = {
    "Google API key": r"AIza[0-9A-Za-z_-]{30,}",
    "OpenAI key": r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})",
    "AWS access key": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "private key": r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    "personal filesystem path": r"/(?:Users|home)/[A-Za-z0-9_.-]+/",
}


def allowed(path):
    p = Path(path)
    if path in ROOT_FILES or path == ".github/workflows/ci.yml":
        return True
    if len(p.parts) != 2:
        return False
    directory, name = p.parts
    return ((directory in {"supportops", "tests", "scripts"} and p.suffix == ".py")
            or (directory == "docs" and name in DOCS)
            or (directory == "data" and name in {"accounts.json", "knowledge.json", "services.json"})
            or (directory == "web" and name in {"index.html", "styles.css", "app.js"}))


def inspect(path, content):
    problems = []
    if not allowed(path):
        problems.append("not on publication allowlist")
    try:
        value = content.decode("utf-8")
    except UnicodeDecodeError:
        return problems + ["binary content is not allowed"]
    for label, pattern in PATTERNS.items():
        if re.search(pattern, value):
            problems.append(label)
    for match in re.finditer(r"(?m)^[ \t]*(?:export[ \t]+)?(?:GEMINI_API_KEY|OPENAI_API_KEY|GH_TOKEN|GITHUB_TOKEN)[ \t]*=[ \t]*([^\r\n]*)$", value):
        candidate = match[1].strip().strip("\"'")
        if candidate not in {"", "your-key", "your-api-key"}:
            problems.append("nonempty credential assignment")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracked", action="store_true", help="Audit index contents of all Git-tracked files")
    args = parser.parse_args()
    if args.tracked:
        names = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
        files = [(name, subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)) for name in names if name]
    else:
        files = [(str(p.relative_to(ROOT)), p.read_bytes()) for p in ROOT.rglob("*") if p.is_file() and allowed(str(p.relative_to(ROOT)))]
    failures = []
    for name, content in files:
        failures.extend((name, reason) for reason in inspect(name, content))
    if not files:
        failures.append(("publication", "no files found"))
    for name, reason in failures:
        print(f"BLOCKED {name}: {reason}")
    if not failures:
        print(f"Publication checks passed for {len(files)} text files; no disallowed files or detected credentials.")
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
