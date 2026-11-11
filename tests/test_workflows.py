"""Text checks on the GitHub Actions workflows, and the feed overlay script they share.

The workflow files are read as plain text with the standard library (no YAML
parser): jobs are the two-space keys under `jobs:`, steps start at
`      - `. Every check returns a list of problems, and every check is also run
once on a deliberately broken copy of the refresh workflow to show it can fail.
"""

import hashlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from contextlib import redirect_stdout

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(REPO_ROOT, ".github", "workflows")
REFRESH = os.path.join(WORKFLOWS, "refresh.yml")
OVERLAY = os.path.join(REPO_ROOT, ".github", "scripts", "overlay_feed.py")

GATE = "if: vars.REFRESH_ENABLED == 'true'"
# Steps that may hold the token, as (job, step name); None as the step name allows the whole job.
REFRESH_TOKEN_STEPS = {
    ("collect", "Collect"),
    ("publish-state", None),
    ("publish-history", "Download the feed"),
    ("publish-history", "Push to the feed-history branch"),
}
# The jobs that may push, and the one branch each may push to; only feed-state is ever force-pushed.
PUSH_TARGETS = {"publish-state": "feed-state", "publish-history": "feed-history"}
FORCE_PUSH_JOBS = {"publish-state"}


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def top_block(text, key):
    """Lines of a top-level key's block (indented lines after `key:`), comments and blanks dropped."""
    lines = text.splitlines()
    out = []
    inside = False
    for line in lines:
        if inside:
            if line and not line[0].isspace() and not line.startswith("#"):
                break
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                out.append(stripped)
        elif line.rstrip() == key + ":":
            inside = True
    return out


def jobs(text):
    """{job name: [lines]} for the keys under `jobs:`."""
    lines = text.splitlines()
    start = lines.index("jobs:")
    result = {}
    name = None
    for line in lines[start + 1:]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            name = match.group(1)
            result[name] = []
        elif name is not None:
            result[name].append(line)
    return result


def split_steps(job_lines):
    """(job-level lines, [step lines]) of one job."""
    head = []
    steps = []
    for line in job_lines:
        if line.startswith("      - "):
            steps.append([line])
        elif steps and (line.startswith("        ") or not line.strip()):
            steps[-1].append(line)
        elif steps and line.strip().startswith("#"):
            steps[-1].append(line)
        else:
            head.append(line)
    return head, steps


def step_name(step):
    for line in step:
        match = re.match(r"^      (?:- |  )name: (.+)$", line)
        if match:
            return match.group(1).strip()
    return None


# --- checks: each returns a list of problems ---------------------------------------------------------------


def check_pinned(text):
    problems = []
    for line in text.splitlines():
        if re.match(r"^\s*(- )?uses:", line) and not re.search(r"uses: [A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}(\s+#.*)?$", line):
            problems.append("not pinned to a 40-hex sha: " + line.strip())
    return problems


def check_top_permissions(text):
    block = top_block(text, "permissions")
    return [] if block == ["contents: read"] else ["top-level permissions are %r" % block]


def check_no_pull_request_target(text):
    return ["uses pull_request_target"] if "pull_request_target" in text else []


def check_no_secrets(text):
    return ["mentions secrets."] if "secrets." in text else []


def check_no_trace(text):
    problems = []
    for line in text.splitlines():
        if re.search(r"set\s+-[A-Za-z]*x|bash\s+-[A-Za-z]*x|GIT_TRACE|GIT_CURL_VERBOSE|curl\s.*(-v\b|--verbose)", line):
            problems.append("tracing: " + line.strip())
    return problems


def check_token_scope(text, allowed):
    """The token may appear only in the allowed steps, only as an env value, and never inside a run script."""
    problems = []
    for job, lines in jobs(text).items():
        head, steps = split_steps(lines)
        if any("github.token" in line or "GITHUB_TOKEN" in line for line in head):
            problems.append("token at job level in " + job)
        for step in steps:
            body = "\n".join(step)
            if "github.token" not in body and "GITHUB_TOKEN" not in body:
                continue
            name = step_name(step)
            if (job, name) not in allowed and (job, None) not in allowed:
                problems.append("token in %s / %s" % (job, name))
            for line in step:
                if "github.token" in line and not re.match(r"^\s+GITHUB_TOKEN: \$\{\{ github\.token \}\}$", line):
                    problems.append("token expression outside an env value in %s" % job)
    return problems


def check_push_targets(text):
    problems = []
    for job, lines in jobs(text).items():
        for line in lines:
            if re.search(r"\bgit\b.*\bpush\b", line):
                target = PUSH_TARGETS.get(job)
                if target is None:
                    problems.append("git push in " + job)
                elif not re.search(r"\sHEAD:refs/heads/%s$" % re.escape(target), line.rstrip()):
                    problems.append("push target is not refs/heads/%s: %s" % (target, line.strip()))
                if re.search(r"\bmain\b|--all|--mirror|--tags", line):
                    problems.append("push may reach another ref: " + line.strip())
                if job not in FORCE_PUSH_JOBS and re.search(r"--force|\s-[A-Za-z]*f\b|\s\+[A-Za-z]*:?|--mirror", line):
                    problems.append("force push in " + job)
            if "contents: write" in line and job not in PUSH_TARGETS:
                problems.append("contents: write in " + job)
            if "@" in line and "https://" in line and "github.com" in line:
                problems.append("credential-looking URL in " + job)
    return problems


def job_if(head):
    """The job-level `if:` expression, or None."""
    for line in head:
        match = re.match(r"^    if: (.+)$", line)
        if match:
            return match.group(1).strip()
    return None


def top_level_or(expression):
    """True when `||` appears outside parentheses (it could bypass a leading conjunct)."""
    depth = 0
    for index, ch in enumerate(expression):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and expression.startswith("||", index):
            return True
    return False


def gated(expression):
    """The REFRESH_ENABLED gate is the whole condition, or its first conjunct joined by && only."""
    gate = GATE[len("if: "):]
    if expression == gate:
        return True
    if expression is None or not expression.startswith(gate + " && "):
        return False
    return not top_level_or(expression) and not re.search(r"\b(always|failure|cancelled|success)\(", expression)


def check_jobs_gated(text, gate=True):
    problems = []
    for job, lines in jobs(text).items():
        head, _ = split_steps(lines)
        if gate and not gated(job_if(head)):
            problems.append("job %s is not gated on REFRESH_ENABLED" % job)
        if not any(re.match(r"^    timeout-minutes: [1-9][0-9]*$", line) for line in head):
            problems.append("job %s has no timeout-minutes" % job)
        if not any(re.match(r"^    permissions:\s*$", line) for line in head) and gate:
            problems.append("job %s declares no permissions" % job)
    return problems


def check_schedule(text):
    crons = re.findall(r"cron:\s*\"([^\"]+)\"", text)
    if len(crons) != 1:
        return ["expected one cron, found %d" % len(crons)]
    fields = crons[0].split()
    problems = []
    if len(fields) != 5:
        return ["cron does not have five fields"]
    if not re.fullmatch(r"[0-9]{1,2}", fields[0]) or not 1 <= int(fields[0]) <= 59:
        problems.append("cron minute is not a fixed nonzero minute")
    if fields[1:] != ["*/6", "*", "*", "*"]:
        problems.append("cron is not once every 6 hours")
    return problems


def check_checkout_credentials(text):
    problems = []
    for job, lines in jobs(text).items():
        _, steps = split_steps(lines)
        for step in steps:
            body = "\n".join(step)
            if "actions/checkout@" in body and "persist-credentials: false" not in body:
                problems.append("checkout without persist-credentials: false in " + job)
    return problems


def check_triggers(text):
    on = top_block(text, "on")
    problems = []
    if "workflow_dispatch:" not in on or "schedule:" not in on:
        problems.append("triggers are not schedule and workflow_dispatch")
    if any(line.startswith(("push", "pull_request", "inputs", "workflow_run")) for line in on):
        problems.append("unexpected trigger or input: %r" % on)
    concurrency = top_block(text, "concurrency")
    if concurrency != ["group: refresh", "cancel-in-progress: false"]:
        problems.append("concurrency is %r" % concurrency)
    return problems


PUBLISHABLE = "(needs.collect.outputs.stopped == '' || needs.collect.outputs.stopped == 'budget') && needs.collect.outputs.repos != '' && needs.collect.outputs.repos != '0'"
COLLECT_OUTPUTS = [
    "      stopped: ${{ steps.guard.outputs.stopped }}",
    "      repos: ${{ steps.guard.outputs.repos }}",
    "      first_run: ${{ steps.state.outputs.exists == 'false' }}",
]
GUARD_LINES = [
    "        id: guard",
    """          stopped=$(python3 -B -c 'import json,sys; s=json.load(open(sys.argv[1]))["collection_report"]["stopped"]; print("" if s is None else s)' data/run.json)""",
    "            *[!A-Za-z_]*) echo \"stop reason is not a plain word\"; exit 1 ;;",
    """          repos=$(python3 -B -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["repos"]))' data/site/feed.json)""",
    "            *[!0-9]*|\"\") echo \"repository count is not a number\"; exit 1 ;;",
    "          echo \"stopped=$stopped\" >> \"$GITHUB_OUTPUT\"",
    "          echo \"repos=$repos\" >> \"$GITHUB_OUTPUT\"",
    "          echo \"stop reason: ${stopped:-none}; repositories in the feed: $repos\"",
    "            \"\"|budget) ;;",
    "            *) echo \"the run stopped abnormally ($stopped): nothing is published or deployed\"; exit 1 ;;",
    "          if [ \"$repos\" -eq 0 ]; then",
]


def check_publish_guard(text):
    """The collect job exposes the stop reason, repository count and first-run flag, and fails on an abnormal run;
    publish-state and deploy run only for a publishable run; deploy never runs after a first run."""
    problems = []
    all_jobs = jobs(text)
    collect = all_jobs.get("collect", [])
    for line in COLLECT_OUTPUTS:
        if line not in collect:
            problems.append("collect output missing: " + line.strip())
    _, steps = split_steps(collect)
    names = [step_name(step) for step in steps]
    guard = [step for step in steps if step_name(step) == "Check the run before anything is published"]
    if len(guard) != 1:
        problems.append("collect has no single guard step")
    else:
        body = guard[0]
        for line in GUARD_LINES:
            if line not in body:
                problems.append("guard step lacks: " + line.strip())
        if any(line.strip().startswith(("if:", "continue-on-error")) for line in body):
            problems.append("guard step is conditional or may fail silently")
        # The reason is printed before any exit that refuses the run.
        joined = "\n".join(body)
        printed = joined.find("echo \"stop reason:")
        refusal = joined.find("the run stopped abnormally")
        if printed < 0 or refusal < 0 or printed > refusal:
            problems.append("guard step does not print the reason before refusing")
        uploads = [i for i, step in enumerate(steps) if "actions/upload-pages-artifact@" in "\n".join(step)]
        if not uploads or steps.index(guard[0]) > min(uploads) or names.index("Export") > steps.index(guard[0]):
            problems.append("guard step is not between Export and the artifact upload")
    gate = GATE[len("if: "):]
    expected = {
        "publish-state": gate + " && " + PUBLISHABLE,
        "publish-history": gate + " && " + PUBLISHABLE,
        "deploy": gate + " && " + PUBLISHABLE + " && needs.collect.outputs.first_run == 'false'",
    }
    for job, condition in expected.items():
        head, _ = split_steps(all_jobs.get(job, []))
        if job_if(head) != condition:
            problems.append("job %s condition is %r" % (job, job_if(head)))
    if 'cp site/data/feed.json "$work/check/feed.json"' not in "\n".join(all_jobs.get("publish-state", [])):
        problems.append("publish-state does not compare with the committed feed")
    return problems


NEEDS = {
    "publish-state": "    needs: collect",
    "publish-history": "    needs: collect",
    "deploy": "    needs: [collect, publish-state]",
}
HISTORY_LINES = [
    "          git ls-remote --exit-code --heads origin refs/heads/feed-history > /dev/null || rc=$?",
    "          elif [ \"$rc\" -eq 2 ]; then",
    "            echo \"could not list the remote branches (exit $rc)\"",
    "          ref: feed-history",
    "          path: data/history",
    "          git init -q -b feed-history data/history",
    'cp site/data/feed.json "$work/check/feed.json"',
    '          python3 -B .github/scripts/overlay_feed.py --required --expect-version "$EXPECT_VERSION" \\',
    "        run: python3 -B -m collector history --feed data/feed/feed.json --history-dir data/history",
    "          if git diff --cached --quiet; then",
]


def check_history_job(text):
    """publish-history and deploy stay independent; publish-history finds its branch by exit code, checks the feed
    like publish-state, runs the history command and commits only a change."""
    problems = []
    all_jobs = jobs(text)
    for job, line in NEEDS.items():
        head, _ = split_steps(all_jobs.get(job, []))
        needs = [l for l in head if l.startswith("    needs:")]
        if needs != [line]:
            problems.append("job %s needs %r" % (job, needs))
    body = "\n".join(all_jobs.get("publish-history", []))
    for line in HISTORY_LINES:
        if line not in body:
            problems.append("publish-history lacks: " + line.strip())
    _, steps = split_steps(all_jobs.get("publish-history", []))
    for step in steps:
        if any(line.strip().startswith("continue-on-error") for line in step):
            problems.append("publish-history step may fail silently: %s" % step_name(step))
    return problems


def refresh_problems(text):
    return (
        check_pinned(text)
        + check_top_permissions(text)
        + check_no_pull_request_target(text)
        + check_no_secrets(text)
        + check_no_trace(text)
        + check_token_scope(text, REFRESH_TOKEN_STEPS)
        + check_push_targets(text)
        + check_jobs_gated(text)
        + check_schedule(text)
        + check_checkout_credentials(text)
        + check_triggers(text)
        + check_publish_guard(text)
        + check_history_job(text)
    )


README_PHRASES = (
    "An abnormal stop publishes nothing",
    "After the first run, review `feed.json` on the `feed-state` branch",
    "deploy it by starting the `pages` workflow by hand",
    "observed, not documented",
)


def readme_problems(readme):
    """The Scheduled refresh section names the publishing guard, the manual first deploy and what is observed rather than documented."""
    start = readme.find("\n## Scheduled refresh\n")
    if start < 0:
        return ["README has no Scheduled refresh section"]
    end = readme.find("\n## ", start + 1)
    section = readme[start : end if end >= 0 else len(readme)]
    return ["Scheduled refresh does not say %r" % phrase for phrase in README_PHRASES if phrase not in " ".join(section.split())]


class WorkflowFiles(unittest.TestCase):
    def setUp(self):
        self.refresh = read(REFRESH)

    def test_refresh_passes_every_check(self):
        self.assertEqual(refresh_problems(self.refresh), [])

    def test_refresh_jobs(self):
        self.assertEqual(sorted(jobs(self.refresh)), ["collect", "deploy", "publish-history", "publish-state"])

    def test_other_workflows(self):
        for name in sorted(os.listdir(WORKFLOWS)):
            if name == "refresh.yml" or not name.endswith(".yml"):
                continue
            with self.subTest(name=name):
                text = read(os.path.join(WORKFLOWS, name))
                problems = (
                    check_pinned(text)
                    + check_top_permissions(text)
                    + check_no_pull_request_target(text)
                    + check_no_secrets(text)
                    + check_no_trace(text)
                    + check_token_scope(text, set())
                    + check_jobs_gated(text, gate=False)
                    + check_checkout_credentials(text)
                )
                self.assertEqual(problems, [])
                self.assertNotRegex(text, r"\bgit\b.*\bpush\b")

    def test_pinned_shas_are_the_ones_already_used(self):
        def pins(text):
            return set(re.findall(r"uses: ([A-Za-z0-9_./-]+@[0-9a-f]{40})", text))

        others = set()
        for name in ("pages.yml", "tests.yml"):
            others |= pins(read(os.path.join(WORKFLOWS, name)))
        self.assertLessEqual(pins(self.refresh), others)

    def test_budget(self):
        config = json.loads(read(os.path.join(REPO_ROOT, "config", "sources.json")))
        self.assertEqual(config["max_requests"], 800)
        self.assertIn("--max-requests 800", self.refresh)
        self.assertEqual(self.refresh.count("curl "), 0)  # no request outside the collector
        readme = read(os.path.join(REPO_ROOT, "README.md"))
        self.assertTrue("\n## Scheduled refresh\n" in readme, "README has no Scheduled refresh section")
        self.assertTrue("REFRESH_ENABLED" in readme, "README does not name REFRESH_ENABLED")

    def test_readme_explains_the_publishing_guard(self):
        self.assertEqual(readme_problems(read(os.path.join(REPO_ROOT, "README.md"))), [])

    def test_pages_overlays_the_state_branch(self):
        pages = read(os.path.join(WORKFLOWS, "pages.yml"))
        self.assertIn("ref: feed-state", pages)
        self.assertIn("python3 -B .github/scripts/overlay_feed.py feed-state out/data", pages)
        self.assertNotIn("--required", pages)


# Each mutation breaks one rule in the refresh workflow; the checks must then report a problem.
MUTATIONS = [
    ("unpinned action", "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", "actions/checkout@v7"),
    ("short sha", "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97", "actions/setup-python@5fda3b9"),
    ("top permissions", "permissions:\n  contents: read\n\nconcurrency", "permissions:\n  contents: write\n\nconcurrency"),
    ("top permissions extra", "permissions:\n  contents: read\n\n", "permissions:\n  contents: read\n  pages: write\n\n"),
    ("pull_request_target", "  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request_target:\n"),
    ("secrets", "GITHUB_TOKEN: ${{ github.token }}\n          STATE_EXISTS", "GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n          STATE_EXISTS"),
    ("token in export step", "          STATE_EXISTS: ${{ steps.state.outputs.exists }}\n        run: |\n          if",
     "          STATE_EXISTS: ${{ steps.state.outputs.exists }}\n          GITHUB_TOKEN: ${{ github.token }}\n        run: |\n          if"),
    ("token in deploy", "          EXPECT_VERSION: ${{ needs.collect.outputs.version }}\n        run: |\n          mkdir out",
     "          EXPECT_VERSION: ${{ needs.collect.outputs.version }}\n          GITHUB_TOKEN: ${{ github.token }}\n        run: |\n          mkdir out"),
    ("token inline", 'echo "feed-state updated"', 'echo "${{ github.token }}"'),
    ("set -x", "          work=\"$RUNNER_TEMP/feed-state\"", "          set -x\n          work=\"$RUNNER_TEMP/feed-state\""),
    ("push to main", "HEAD:refs/heads/feed-state", "HEAD:refs/heads/main"),
    ("push extra ref", "HEAD:refs/heads/feed-state", "HEAD:refs/heads/feed-state main"),
    ("push in deploy", "          cd out\n", "          git push origin HEAD:refs/heads/feed-state\n          cd out\n"),
    ("contents write in collect", "    permissions:\n      contents: read\n    outputs:", "    permissions:\n      contents: write\n    outputs:"),
    ("ungated job", "    needs: collect\n    " + GATE + " && ", "    needs: collect\n    if: "),
    ("gate bypassed by or", "    needs: collect\n    " + GATE + " && ", "    needs: collect\n    " + GATE + " || "),
    ("deploy runs always", " && needs.collect.outputs.first_run == 'false'\n", " && needs.collect.outputs.first_run == 'false' && always()\n"),
    ("deploy after a first run", " && needs.collect.outputs.first_run == 'false'\n", "\n"),
    ("deploy on a first run only", "needs.collect.outputs.first_run == 'false'\n", "needs.collect.outputs.first_run != 'false'\n"),
    ("publish-state skips first runs", "needs.collect.outputs.repos != '0'\n    runs-on", "needs.collect.outputs.repos != '0' && needs.collect.outputs.first_run == 'false'\n    runs-on"),
    ("publish-state after rate_limited", "needs.collect.outputs.stopped == 'budget')", "needs.collect.outputs.stopped == 'budget' || needs.collect.outputs.stopped == 'rate_limited')"),
    ("publish-state with no repos", "needs.collect.outputs.repos != '' && needs.collect.outputs.repos != '0'\n    runs-on", "needs.collect.outputs.repos != ''\n    runs-on"),
    ("deploy with no repos", "needs.collect.outputs.repos != '0' && needs.collect.outputs.first_run", "needs.collect.outputs.first_run"),
    ("no stopped output", "      stopped: ${{ steps.guard.outputs.stopped }}\n", ""),
    ("no repos output", "      repos: ${{ steps.guard.outputs.repos }}\n", ""),
    ("first_run inverted", "first_run: ${{ steps.state.outputs.exists == 'false' }}", "first_run: ${{ steps.state.outputs.exists == 'true' }}"),
    ("guard allows rate_limited", '            ""|budget) ;;', '            ""|budget|rate_limited) ;;'),
    ("guard does not fail", "nothing is published or deployed\"; exit 1 ;;", "nothing is published or deployed\" ;;"),
    ("guard may fail silently", "        id: guard\n", "        id: guard\n        continue-on-error: true\n"),
    ("guard unvalidated reason", '            *[!A-Za-z_]*) echo "stop reason is not a plain word"; exit 1 ;;\n', ""),
    ("guard unvalidated count", '            *[!0-9]*|"") echo "repository count is not a number"; exit 1 ;;\n', ""),
    ("guard ignores empty feed", '          if [ "$repos" -eq 0 ]; then', '          if [ "$repos" -lt 0 ]; then'),
    ("guard reason unprinted", '          echo "stop reason: ${stopped:-none}; repositories in the feed: $repos"\n', ""),
    ("guard after upload", "      - name: Check the run before anything is published\n",
     "      - uses: actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9 # v5.0.0\n        with:\n          path: data/artifact\n      - name: Check the run before anything is published\n"),
    ("guard reads the wrong file", '["stopped"]; print("" if s is None else s)\' data/run.json', '["stopped"]; print("" if s is None else s)\' data/old.json'),
    ("publish-state without size check", 'cp site/data/feed.json "$work/check/feed.json"', 'true'),
    ("no timeout", "    timeout-minutes: 10\n", ""),
    ("zero minute", '"17 */6 * * *"', '"0 */6 * * *"'),
    ("every hour", '"17 */6 * * *"', '"17 * * * *"'),
    ("persisted credentials", "          ref: main\n          persist-credentials: false\n", "          ref: main\n"),
    ("cancel in progress", "  group: refresh\n  cancel-in-progress: false", "  group: refresh\n  cancel-in-progress: true"),
    ("dispatch input", "  workflow_dispatch:\n", "  workflow_dispatch:\n    inputs:\n      budget:\n        default: '900'\n"),
    ("push trigger", "  workflow_dispatch:\n", "  workflow_dispatch:\n  push:\n"),
    # publish-history
    ("history force push", 'git push -q "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY.git" HEAD:refs/heads/feed-history',
     'git push -q --force "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY.git" HEAD:refs/heads/feed-history'),
    ("history short force push", 'git push -q "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY.git" HEAD:refs/heads/feed-history',
     'git push -qf "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY.git" HEAD:refs/heads/feed-history'),
    ("history plus refspec", "HEAD:refs/heads/feed-history", "+HEAD:refs/heads/feed-history"),
    ("history pushes feed-state", "HEAD:refs/heads/feed-history", "HEAD:refs/heads/feed-state"),
    ("history pushes main", "HEAD:refs/heads/feed-history", "HEAD:refs/heads/main"),
    ("contents write in deploy", "    permissions:\n      contents: read\n      pages: write", "    permissions:\n      contents: write\n      pages: write"),
    ("history ungated", "    needs: collect\n    if: vars.REFRESH_ENABLED == 'true' && (needs.collect.outputs.stopped == '' || needs.collect.outputs.stopped == 'budget') && needs.collect.outputs.repos != '' && needs.collect.outputs.repos != '0'\n    runs-on: ubuntu-latest\n    timeout-minutes: 15",
     "    needs: collect\n    if: always()\n    runs-on: ubuntu-latest\n    timeout-minutes: 15"),
    ("history skips first runs", "needs.collect.outputs.repos != '0'\n    runs-on: ubuntu-latest\n    timeout-minutes: 15",
     "needs.collect.outputs.repos != '0' && needs.collect.outputs.first_run == 'false'\n    runs-on: ubuntu-latest\n    timeout-minutes: 15"),
    ("deploy waits for history", "    needs: [collect, publish-state]", "    needs: [collect, publish-state, publish-history]"),
    ("publish-state waits for history", "    needs: collect\n    if: vars.REFRESH_ENABLED == 'true' && (needs.collect.outputs.stopped == '' || needs.collect.outputs.stopped == 'budget') && needs.collect.outputs.repos != '' && needs.collect.outputs.repos != '0'\n    runs-on: ubuntu-latest\n    timeout-minutes: 10",
     "    needs: [collect, publish-history]\n    if: vars.REFRESH_ENABLED == 'true' && (needs.collect.outputs.stopped == '' || needs.collect.outputs.stopped == 'budget') && needs.collect.outputs.repos != '' && needs.collect.outputs.repos != '0'\n    runs-on: ubuntu-latest\n    timeout-minutes: 10"),
    ("token in history update", "      - name: Update the history\n", "      - name: Update the history\n        env:\n          GITHUB_TOKEN: ${{ github.token }}\n"),
    ("history checkout keeps credentials", "          ref: feed-history\n          path: data/history\n          persist-credentials: false\n", "          ref: feed-history\n          path: data/history\n"),
    ("history branch probe ignores errors", '          elif [ "$rc" -eq 2 ]; then\n            echo "exists=false" >> "$GITHUB_OUTPUT"\n            echo "no feed-history',
     '          elif true; then\n            echo "exists=false" >> "$GITHUB_OUTPUT"\n            echo "no feed-history'),
    ("history without version check", '          python3 -B .github/scripts/overlay_feed.py --required --expect-version "$EXPECT_VERSION" \\\n            data/feed', "          true \\\n            data/feed"),
    ("history without size check", 'cp site/data/feed.json "$work/check/feed.json"\n          python3 -B .github/scripts/overlay_feed.py --required --expect-version "$EXPECT_VERSION" \\\n            data/feed',
     'true\n          python3 -B .github/scripts/overlay_feed.py --required --expect-version "$EXPECT_VERSION" \\\n            data/feed'),
    ("history command missing", "        run: python3 -B -m collector history --feed data/feed/feed.json --history-dir data/history", "        run: true"),
    ("history commits without a change", "          if git diff --cached --quiet; then", "          if false; then"),
    ("history may fail silently", "      - name: Update the history\n", "      - name: Update the history\n        continue-on-error: true\n"),
    ("history no timeout", "    timeout-minutes: 15\n    permissions:\n      contents: write", "    permissions:\n      contents: write"),
]


def mutate(text, mutation):
    label, old, new = mutation
    if old not in text:
        raise AssertionError("mutation %r does not apply" % label)
    return text.replace(old, new, 1)


class BrokenWorkflows(unittest.TestCase):
    def test_every_mutation_is_caught(self):
        original = read(REFRESH)
        for mutation in MUTATIONS:
            with self.subTest(mutation=mutation[0]):
                broken = mutate(original, mutation)
                self.assertNotEqual(broken, original)
                self.assertNotEqual(refresh_problems(broken), [], mutation[0])


def feed_bytes(count, stopped=None, collection=True, repos=None):
    """A feed.json body with `count` repository records; `collection=False` leaves the collection object out."""
    feed = {"generated_at": "2026-11-10T06:17:00Z", "repos": [{"id": "r%d" % (n + 1)} for n in range(count)] if repos is None else repos, "events": []}
    if collection is True:
        feed["collection"] = {"complete": stopped is None, "stopped": stopped, "not_fetched": 0, "error_count": 0, "last_run_at": "2026-11-10T06:17:00Z"}
    elif collection is not False:
        feed["collection"] = collection
    return (json.dumps(feed, sort_keys=True) + "\n").encode("utf-8")


class BrokenReadme(unittest.TestCase):
    def test_each_phrase_is_required(self):
        readme = read(os.path.join(REPO_ROOT, "README.md"))
        for phrase in README_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, " ".join(readme.split()))
                broken = " ".join(readme.split()).replace(phrase, "")
                self.assertNotEqual(readme_problems(broken), [])


def load_overlay():
    spec = importlib.util.spec_from_file_location("overlay_feed", OVERLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OverlayScript(unittest.TestCase):
    def setUp(self):
        self.overlay = load_overlay()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = os.path.join(self.tmp.name, "src")
        self.dest = os.path.join(self.tmp.name, "dest")
        os.mkdir(self.src)
        os.mkdir(self.dest)
        # The committed feed being replaced lists four repositories.
        self.committed = feed_bytes(4, collection=False)
        with open(os.path.join(self.dest, "feed.json"), "wb") as handle:
            handle.write(self.committed)
        with open(os.path.join(self.dest, "manifest.json"), "w", encoding="utf-8") as handle:
            handle.write("committed\n")
        self.feed = feed_bytes(3)
        self.version = self.write_feed(self.feed)

    def write_feed(self, feed):
        version = hashlib.sha256(feed).hexdigest()[:16]
        self.write_source(feed, {"feed": "feed.json", "generated_at": "2026-11-10T06:17:00Z", "version": version})
        return version

    def write_source(self, feed, manifest):
        with open(os.path.join(self.src, "feed.json"), "wb") as handle:
            handle.write(feed)
        with open(os.path.join(self.src, "manifest.json"), "w", encoding="utf-8") as handle:
            if isinstance(manifest, str):
                handle.write(manifest)
            else:
                json.dump(manifest, handle)

    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = self.overlay.main(list(args))
        return code, out.getvalue()

    def dest_feed(self):
        with open(os.path.join(self.dest, "feed.json"), "rb") as handle:
            return handle.read()

    def test_matching_files_are_copied(self):
        code, out = self.run_main(self.src, self.dest)
        self.assertEqual(code, 0)
        self.assertEqual(self.dest_feed(), self.feed)
        self.assertIn(self.version, out)

    def test_expected_version(self):
        self.assertEqual(self.run_main("--required", "--expect-version", self.version, self.src, self.dest)[0], 0)
        self.assertEqual(self.dest_feed(), self.feed)

    def test_unexpected_version_keeps_committed_data(self):
        code, _ = self.run_main("--required", "--expect-version", "0" * 16, self.src, self.dest)
        self.assertEqual(code, 1)
        self.assertEqual(self.dest_feed(), self.committed)

    def test_mismatches_keep_committed_data(self):
        cases = [
            ("hash", self.feed + b" ", {"feed": "feed.json", "generated_at": "2026-11-10T06:17:00Z", "version": self.version}),
            ("bad manifest", self.feed, "{not json"),
            ("manifest list", self.feed, "[]"),
            ("short version", self.feed, {"feed": "feed.json", "generated_at": "2026-11-10T06:17:00Z", "version": self.version[:8]}),
            ("other feed name", self.feed, {"feed": "other.json", "generated_at": "2026-11-10T06:17:00Z", "version": self.version}),
            ("generated_at", self.feed, {"feed": "feed.json", "generated_at": "2026-11-10T00:00:00Z", "version": self.version}),
        ]
        for label, feed, manifest in cases:
            with self.subTest(case=label):
                self.write_source(feed, manifest)
                code, out = self.run_main(self.src, self.dest)
                self.assertEqual(code, 0)
                self.assertIn("kept the committed data", out)
                self.assertEqual(self.dest_feed(), self.committed)
                self.assertEqual(self.run_main("--required", self.src, self.dest)[0], 1)

    def test_missing_source(self):
        missing = os.path.join(self.tmp.name, "absent")
        self.assertEqual(self.run_main(missing, self.dest)[0], 0)
        self.assertEqual(self.run_main("--required", missing, self.dest)[0], 1)
        self.assertEqual(self.dest_feed(), self.committed)

    def test_symlink_is_refused(self):
        os.remove(os.path.join(self.src, "feed.json"))
        target = os.path.join(self.tmp.name, "elsewhere.json")
        with open(target, "wb") as handle:
            handle.write(self.feed)
        os.symlink(target, os.path.join(self.src, "feed.json"))
        self.assertEqual(self.run_main("--required", self.src, self.dest)[0], 1)
        self.assertEqual(self.dest_feed(), self.committed)

    def test_committed_site_data_passes(self):
        site_data = os.path.join(REPO_ROOT, "site", "data")
        feed, manifest, version = self.overlay.check(site_data)
        self.assertEqual(len(version), 16)
        # It has no collection object, and it may replace itself (same size).
        self.assertNotIn("collection", json.loads(feed))
        self.assertGreater(len(json.loads(feed)["repos"]), 0)
        self.overlay.check_replaces(feed, site_data)

    def assertRefused(self, feed, needle):
        self.write_feed(feed)
        code, out = self.run_main(self.src, self.dest)
        self.assertEqual(code, 0)
        self.assertIn("kept the committed data", out)
        self.assertIn(needle, out)
        self.assertEqual(self.dest_feed(), self.committed)
        self.assertEqual(self.run_main("--required", self.src, self.dest)[0], 1)
        self.assertEqual(self.dest_feed(), self.committed)

    def assertAccepted(self, feed):
        self.write_feed(feed)
        self.assertEqual(self.run_main("--required", self.src, self.dest)[0], 0)
        self.assertEqual(self.dest_feed(), feed)

    def test_an_abnormal_stop_is_refused(self):
        for stopped in ("rate_limited", "search_rate_limited", "code_search_rate_limited", "network", "unauthorized", "", 0, False):
            with self.subTest(stopped=stopped):
                self.assertRefused(feed_bytes(4, stopped=stopped), "stopped abnormally")
        self.assertRefused(feed_bytes(4, collection="text"), "collection is not an object")
        for stopped in (None, "budget"):
            with self.subTest(stopped=stopped):
                self.assertAccepted(feed_bytes(4, stopped=stopped))

    def test_a_missing_collection_is_not_a_refusal(self):
        self.assertAccepted(feed_bytes(4, collection=False))

    def test_an_empty_feed_is_refused(self):
        self.assertRefused(feed_bytes(0), "no repositories")
        self.assertRefused(feed_bytes(0, collection=False), "no repositories")
        self.assertRefused(feed_bytes(3, repos="none"), "no repos list")
        # Even with nothing to replace.
        os.remove(os.path.join(self.dest, "feed.json"))
        self.write_feed(feed_bytes(0))
        self.assertEqual(self.run_main("--required", self.src, self.dest)[0], 1)

    def test_a_feed_under_half_the_size_it_replaces_is_refused(self):
        self.assertRefused(feed_bytes(1), "under half of the 4")
        self.assertAccepted(feed_bytes(2))
        # The destination now holds 2: 1 is half, so it is accepted.
        self.assertAccepted(feed_bytes(1))

    def test_nothing_to_replace_skips_the_size_rule(self):
        os.remove(os.path.join(self.dest, "feed.json"))
        self.assertAccepted(feed_bytes(1))

    def test_an_unreadable_feed_to_replace_is_a_refusal(self):
        for content in (b"committed\n", b"[]", b'{"repos": 3}'):
            with self.subTest(content=content):
                with open(os.path.join(self.dest, "feed.json"), "wb") as handle:
                    handle.write(content)
                self.committed = content
                self.assertRefused(feed_bytes(4), "feed.json being replaced")


if __name__ == "__main__":
    unittest.main()
