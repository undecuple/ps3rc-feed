# Contributing

Thank you for helping. This project is a small, automated index of public GitHub data, so most useful changes are
small.

## Before you start

- To correct a game label or ask for a listing to be removed, do not open a pull request here. Use the forms on
  [undecuple/me](https://github.com/undecuple/me/issues/new/choose) or write to undecuple@proton.me.
- To report a security problem, follow [SECURITY.md](SECURITY.md). Do not use a public issue.
- For anything larger than a small fix, ask first through the same forms so we can agree on the idea.

## What fits

- A new search term, include entry or game alias in `config/sources.json` or `config/games.json`. The steps for
  reviewing such a change are in the [README](README.md#reviewing-a-change-to-configsourcesjson). A game alias must
  be a name or phrase a project really uses for that game.
- A fix to the collector, the exporter or the site, with a test that fails before the fix and passes after it.
- A fix to the documentation.

## What does not

- Game files, keys, decryption steps, links to pirated copies, or anything that helps get around copy protection.
- Code that reads private data, scrapes pages instead of using the API, or raises the request budget without a
  reason that fits GitHub's limits.
- Passwords, tokens or personal data in any file, issue or pull request.

## Run the tests

    python3 -B -m unittest discover -s tests

Python 3.14 and the standard library are all it needs. The tests run offline. The site tests need Node.js; see
`site/README.md`.

## Pull requests

- Keep one change per pull request, with a short reason. The template asks for the same things.
- Say what you ran and what you did not.
- If an AI tool helped, say so in general terms. You are still responsible for what you submit.
- The maintainer reviews each pull request. A pull request can wait a while; this is a one-person project.

By contributing you agree that your changes are released under the [MIT licence](LICENSE).
