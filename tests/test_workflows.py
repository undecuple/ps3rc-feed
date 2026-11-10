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
REFRESH_TOKEN_STEPS = {("collect", "Smoke check code search"), ("collect", "Collect"), ("publish-state", None)}


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
                if job != "publish-state":
                    problems.append("git push in " + job)
                if not re.search(r"\sHEAD:refs/heads/feed-state$", line.rstrip()):
                    problems.append("push target is not refs/heads/feed-state: " + line.strip())
                if re.search(r"\bmain\b|--all|--mirror|--tags", line):
                    problems.append("push may reach another ref: " + line.strip())
            if "contents: write" in line and job != "publish-state":
                problems.append("contents: write in " + job)
            if "@" in line and "https://" in line and "github.com" in line:
                problems.append("credential-looking URL in " + job)
    return problems


def check_jobs_gated(text, gate=True):
    problems = []
    for job, lines in jobs(text).items():
        head, _ = split_steps(lines)
        if gate and ("    " + GATE) not in head:
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
    )


class WorkflowFiles(unittest.TestCase):
    def setUp(self):
        self.refresh = read(REFRESH)

    def test_refresh_passes_every_check(self):
        self.assertEqual(refresh_problems(self.refresh), [])

    def test_refresh_jobs(self):
        self.assertEqual(sorted(jobs(self.refresh)), ["collect", "deploy", "publish-state"])

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
        self.assertEqual(self.refresh.count("curl "), 1)
        readme = read(os.path.join(REPO_ROOT, "README.md"))
        self.assertTrue("\n## Scheduled refresh\n" in readme, "README has no Scheduled refresh section")
        self.assertTrue("REFRESH_ENABLED" in readme, "README does not name REFRESH_ENABLED")

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
    ("ungated job", "    needs: [collect, publish-state]\n    " + GATE + "\n", "    needs: [collect, publish-state]\n"),
    ("no timeout", "    timeout-minutes: 10\n", ""),
    ("zero minute", '"17 */6 * * *"', '"0 */6 * * *"'),
    ("every hour", '"17 */6 * * *"', '"17 * * * *"'),
    ("persisted credentials", "          ref: main\n          persist-credentials: false\n", "          ref: main\n"),
    ("cancel in progress", "  group: refresh\n  cancel-in-progress: false", "  group: refresh\n  cancel-in-progress: true"),
    ("dispatch input", "  workflow_dispatch:\n", "  workflow_dispatch:\n    inputs:\n      budget:\n        default: '900'\n"),
    ("push trigger", "  workflow_dispatch:\n", "  workflow_dispatch:\n  push:\n"),
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
        for name in ("feed.json", "manifest.json"):
            with open(os.path.join(self.dest, name), "w", encoding="utf-8") as handle:
                handle.write("committed\n")
        self.feed = b'{"generated_at": "2026-11-10T06:17:00Z", "repos": [], "events": []}\n'
        self.version = hashlib.sha256(self.feed).hexdigest()[:16]
        self.write_source(self.feed, {"feed": "feed.json", "generated_at": "2026-11-10T06:17:00Z", "version": self.version})

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
        self.assertEqual(self.dest_feed(), b"committed\n")

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
                self.assertEqual(self.dest_feed(), b"committed\n")
                self.assertEqual(self.run_main("--required", self.src, self.dest)[0], 1)

    def test_missing_source(self):
        missing = os.path.join(self.tmp.name, "absent")
        self.assertEqual(self.run_main(missing, self.dest)[0], 0)
        self.assertEqual(self.run_main("--required", missing, self.dest)[0], 1)
        self.assertEqual(self.dest_feed(), b"committed\n")

    def test_symlink_is_refused(self):
        os.remove(os.path.join(self.src, "feed.json"))
        target = os.path.join(self.tmp.name, "elsewhere.json")
        with open(target, "wb") as handle:
            handle.write(self.feed)
        os.symlink(target, os.path.join(self.src, "feed.json"))
        self.assertEqual(self.run_main("--required", self.src, self.dest)[0], 1)
        self.assertEqual(self.dest_feed(), b"committed\n")

    def test_committed_site_data_passes(self):
        site_data = os.path.join(REPO_ROOT, "site", "data")
        feed, manifest, version = self.overlay.check(site_data)
        self.assertEqual(len(version), 16)


if __name__ == "__main__":
    unittest.main()
