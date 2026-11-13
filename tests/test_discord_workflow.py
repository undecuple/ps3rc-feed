"""Text checks on .github/workflows/discord.yml, the workflow that posts to Discord after a refresh.

Read as plain text like the refresh workflow (tests/test_workflows.py, whose helpers are reused). The rules: it
starts only from a successful refresh on main, is off unless DISCORD_ENABLED is 'true', the webhook secret reaches
exactly one step and that step has no repository token, the only push is a plain push to feed-discord, and the
refresh workflow never mentions the webhook. Every check is also run on a deliberately broken copy.
"""

import os
import re
import unittest

from tests import test_workflows as tw

DISCORD = os.path.join(tw.WORKFLOWS, "discord.yml")
CONDITION = (
    "vars.DISCORD_ENABLED == 'true' && github.event.workflow_run.conclusion == 'success' "
    "&& github.event.workflow_run.head_branch == 'main'"
)


def check_trigger(text):
    on = tw.top_block(text, "on")
    problems = []
    if on != ["workflow_run:", "workflows: [refresh]", "types: [completed]"]:
        problems.append("trigger is %r" % on)
    if tw.top_block(text, "concurrency") != ["group: discord", "cancel-in-progress: false"]:
        problems.append("concurrency is not the discord group without cancelling")
    return problems


def check_job(text):
    all_jobs = tw.jobs(text)
    if list(all_jobs) != ["notify"]:
        return ["jobs are %r" % list(all_jobs)]
    head, steps = tw.split_steps(all_jobs["notify"])
    problems = []
    if tw.job_if(head) != CONDITION:
        problems.append("job condition is %r" % tw.job_if(head))
    if "    permissions:" not in head or "      contents: write" not in head:
        problems.append("job permissions are not contents: write")
    if not any(re.match(r"^    timeout-minutes: [1-9][0-9]*$", line) for line in head):
        problems.append("no timeout-minutes")
    if any("DISCORD_WEBHOOK_URL" in line or "secrets." in line or "GITHUB_TOKEN" in line or "github.token" in line for line in head):
        problems.append("secret or token at job level")
    return problems


def check_secret_scope(text):
    """The webhook secret is an env value of the Send step only, and that step has no token; the token is in the push step only."""
    problems = []
    secret_lines = [line for line in text.splitlines() if "secrets." in line]
    if secret_lines != ["          DISCORD_WEBHOOK_URL: ${{ secrets.DISCORD_WEBHOOK_URL }}"]:
        problems.append("secret lines are %r" % secret_lines)
    for job_lines in tw.jobs(text).values():
        _, steps = tw.split_steps(job_lines)
        for step in steps:
            body = "\n".join(step)
            name = tw.step_name(step)
            if "secrets." in body and name != "Send":
                problems.append("secret in step %s" % name)
            has_token = "github.token" in body or "GITHUB_TOKEN" in body
            if name == "Send" and has_token:
                problems.append("Send step holds the token")
            if has_token and name != "Push the delivery state":
                problems.append("token in step %s" % name)
            if "DISCORD_WEBHOOK_URL" in body and name != "Send":
                problems.append("webhook variable in step %s" % name)
            if "secrets." in body or "DISCORD_WEBHOOK_URL" in body:
                if re.search(r"\becho\b.*DISCORD|\bprintenv\b|\benv\b\s*$|set\s+-x", body):
                    problems.append("webhook may be printed in %s" % name)
    return problems


def check_push(text):
    pushes = [line.strip() for line in text.splitlines() if re.search(r"\bgit\b.*\bpush\b", line)]
    problems = []
    if pushes != ['git push -q "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY.git" HEAD:refs/heads/feed-discord']:
        problems.append("pushes are %r" % pushes)
    for line in text.splitlines():
        if re.search(r"--force|--mirror|--all|--tags|\s\+HEAD", line):
            problems.append("force or broad push: " + line.strip())
    return problems


def check_send_step(text):
    _, steps = tw.split_steps(tw.jobs(text).get("notify", []))
    send = [s for s in steps if tw.step_name(s) == "Send"]
    if len(send) != 1:
        return ["no single Send step"]
    body = "\n".join(send[0])
    needed = [
        "python3 -B -m collector discord --feed data/feed-state/feed.json",
        "--send --delivery-state-out data/discord/discord-state.json",
        'state=(--delivery-state data/discord/discord-state.json)',
        "3) echo",
    ]
    problems = ["Send lacks: " + n for n in needed if n not in body]
    if any(line.strip().startswith(("continue-on-error", "if:")) for line in send[0]):
        problems.append("Send is conditional or may fail silently")
    names = [tw.step_name(s) for s in steps]
    order = ["Look for the feed-discord branch", "Check the inputs", "Send", "Push the delivery state", "Fail when messages were not delivered"]
    positions = [names.index(n) if n in names else -1 for n in order]
    if -1 in positions or positions != sorted(positions):
        problems.append("step order is %r" % names)
    return problems


def check_common(text):
    return (
        tw.check_pinned(text)
        + tw.check_top_permissions(text)
        + tw.check_no_pull_request_target(text)
        + tw.check_no_trace(text)
        + tw.check_checkout_credentials(text)
    )


def discord_problems(text):
    return check_common(text) + check_trigger(text) + check_job(text) + check_secret_scope(text) + check_push(text) + check_send_step(text)


class DiscordWorkflow(unittest.TestCase):
    def setUp(self):
        self.text = tw.read(DISCORD)

    def test_passes_every_check(self):
        self.assertEqual(discord_problems(self.text), [])

    def test_the_refresh_workflow_never_sees_the_webhook(self):
        refresh = tw.read(tw.REFRESH)
        self.assertNotIn("DISCORD", refresh)
        self.assertNotIn("feed-discord", refresh)

    def test_pins_are_the_ones_already_used(self):
        pins = lambda t: set(re.findall(r"uses: ([A-Za-z0-9_./-]+@[0-9a-f]{40})", t))
        others = set()
        for name in ("pages.yml", "tests.yml", "refresh.yml"):
            others |= pins(tw.read(os.path.join(tw.WORKFLOWS, name)))
        self.assertLessEqual(pins(self.text), others)

    def test_branch_start_needs_the_exit_code(self):
        self.assertIn("git ls-remote --exit-code --heads origin refs/heads/feed-discord > /dev/null || rc=$?", self.text)
        self.assertIn('elif [ "$rc" -eq 2 ]; then', self.text)

    def test_no_request_outside_the_collector(self):
        self.assertEqual(self.text.count("curl "), 0)

    def test_checks_fail_on_broken_copies(self):
        mutations = [
            ("unpinned", tw.pinned("checkout", self.text), "actions/checkout@v7"),
            ("trigger", "  workflow_run:\n", "  workflow_run:\n    branches: [x]\n"),
            ("push trigger", "on:\n", "on:\n  push:\n"),
            ("pull_request_target", "on:\n", "on:\n  pull_request_target:\n"),
            ("gate", "vars.DISCORD_ENABLED == 'true' && ", ""),
            ("conclusion", "github.event.workflow_run.conclusion == 'success'", "always()"),
            ("branch", " && github.event.workflow_run.head_branch == 'main'", ""),
            ("top permissions", "permissions:\n  contents: read\n\nconcurrency", "permissions:\n  contents: write\n\nconcurrency"),
            ("cancel", "cancel-in-progress: false", "cancel-in-progress: true"),
            ("force push", 'git push -q "$GITHUB_SERVER_URL', 'git push -q --force "$GITHUB_SERVER_URL'),
            ("push target", "HEAD:refs/heads/feed-discord", "HEAD:refs/heads/main"),
            ("secret in push", "          GITHUB_TOKEN: ${{ github.token }}", "          GITHUB_TOKEN: ${{ github.token }}\n          X: ${{ secrets.DISCORD_WEBHOOK_URL }}"),
            ("token in send", "          STATE_EXISTS: ${{ steps.delivery.outputs.exists }}\n        run: |\n          state=()", "          STATE_EXISTS: ${{ steps.delivery.outputs.exists }}\n          GITHUB_TOKEN: ${{ github.token }}\n        run: |\n          state=()"),
            ("secret at job level", "    timeout-minutes: 10\n", "    timeout-minutes: 10\n    env:\n      X: ${{ secrets.DISCORD_WEBHOOK_URL }}\n"),
            ("silent send", "        id: send\n", "        id: send\n        continue-on-error: true\n"),
            ("persist credentials", "          ref: feed-state\n          path: data/feed-state\n          persist-credentials: false", "          ref: feed-state\n          path: feed-state"),
            ("trace", "          rc=0\n          python3", "          set -x\n          rc=0\n          python3"),
        ]
        for name, old, new in mutations:
            with self.subTest(mutation=name):
                self.assertEqual(self.text.count(old) >= 1, True, "mutation target missing")
                self.assertNotEqual(discord_problems(self.text.replace(old, new, 1)), [])


if __name__ == "__main__":
    unittest.main()
