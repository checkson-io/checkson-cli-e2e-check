# Checkson CLI end-to-end check

Can a user find the Checkson CLI, download it, sign in, and see their checks?
This monitoring check follows that journey from the public website through to
an authenticated CLI command. It is designed to run periodically on Checkson.

## What it checks

Each run starts with a fresh browser session and temporary CLI configuration:

1. **Find the CLI:** open [checkson.io](https://checkson.io/) and follow its **CLI**
   link to the official GitHub repository.
2. **Download the latest release:** follow the repository's installation link,
   select the latest release, and download the Linux binary for the container's
   architecture. Verify its SHA-256 checksum and run `checkson version`.
3. **Sign in through the browser:** run `checkson login`, open the URL it prints,
   sign in with email and password, and approve the CLI authorization.
4. **List checks:** run `checkson list` and confirm that the expected check name
   appears. The health of that check does not affect this assertion.
5. **Create a personal access token:** use the signed-in web UI to create a
   temporary token.
6. **Test token authentication:** use the new token to sign in to a separate,
   empty CLI configuration directory. Run `checkson list` and verify the same name.
7. **Clean up:** delete the temporary token through the web UI and verify its
   removal. Cleanup is also attempted if token login or list verification fails.

The browser creates the token, so a browser-login failure prevents the token
route from being tested. A passing run verifies the entire journey, including
cleanup. It does not create, edit, or delete your monitoring checks.

## Set it up on Checkson

Create a check named **checkson-cli-e2e** and configure this Docker image:

```text
ghcr.io/checkson-io/checkson-cli-e2e:main
```

`latest` points to the same build. For a fixed version, use a published commit tag
(`sha-<full commit SHA>`) or image digest. If the package is private, provide GHCR
pull credentials in the check's registry configuration.

Add these environment variables to the check:

| Variable | Value |
| --- | --- |
| `user` | Email address of the Checkson account used for this test |
| `pw` | Password for that account |
| `EXPECTED_CHECK` | Optional: exact check name to find; defaults to `checkson-cli-e2e` |

The account must support email/password login and be able to see the expected
check. The monitoring check can verify its own presence, so no additional fixture
is required. No permanent personal access token is needed.

Keep the password in the check's environment configuration, outside the image and
source code. Google and GitHub sign-in are not automated.

## Run it locally

Set the credentials without putting the password into shell history:

```sh
export user='your-monitoring-account@example.com'
read -r -s -p 'Checkson password: ' pw; echo  # Bash
export pw
```

For Zsh, use `read -r -s 'pw?Checkson password: '` followed by `export pw` instead.
Then run:

```sh
docker run --rm --pull=always --platform linux/amd64 --init --ipc=host \
  -e user -e pw \
  -e CHECKSON_DIR=/results \
  -v checkson-cli-e2e-check-results:/results \
  ghcr.io/checkson-io/checkson-cli-e2e:main
```

The published image is Linux AMD64. On an ARM Mac, Docker runs it through
emulation. `--pull=always` ensures you receive the current image rather than an
older cached copy. The named volume preserves cleanup information between runs.

## Understand the result

A successful run ends with:

```text
Both authentication routes verified
Delete temporary token in web UI
OK: both CLI login routes and list verified
```

Exit code **0** means the complete journey passed. Exit code **1** means a step
failed, including token cleanup. Timestamped stage names show where it failed.
Checkson receives a short result message and a `cli-e2e.json` attachment containing
the stage, selected release, download URL, elapsed time, and cleanup status.
If both the journey and cleanup fail, `journey_failure_stage` retains the original
failure. Passwords, token values, raw CLI output, screenshots, and browser traces
are not attached or logged by this check.

An empty version field is reported as a warning because release v1.0.18 has
missing build metadata. Its archive checksum is still verified. A nonempty version
that disagrees with the selected release fails the check.

### Timing and interrupted runs

The journey has a 45-second budget, followed by up to 8 seconds for token cleanup,
leaving room within Checkson's 60-second limit for browser startup and shutdown.
Slow networks can still cause timeouts. `JOURNEY_TIMEOUT` overrides the journey
budget; increase it only if your Checkson plan permits a longer total run.

The temporary token's ID is saved under
`$CHECKSON_DIR/persistent/cli-e2e-token.json`. After an interrupted run, the next
run attempts to remove that token before creating another. Only the ID is saved,
not the token value or login session. Preserve this directory locally with the
volume above; Checkson provides persistence for its check runs.

A hard stop between token creation and saving its ID can leave a token requiring
manual removal under **Access Tokens**. Run only one copy against an account at a
time: tokens have no names, so the check identifies its token by comparing IDs
before and after creation. Other tokens are preserved, even when their displayed
three-character prefixes match.

## Develop and publish

Build the image from the repository root:

```sh
docker build --platform linux/amd64 -t checkson-cli-e2e-check:local .
```

Run the unit tests:

```sh
python3 -m unittest discover -v
```

Run the complete browser journey against fixture pages and a fixture CLI, without
real credentials or changes to a live account:

```sh
docker run --rm --platform linux/amd64 --init --ipc=host --entrypoint python \
  -v "$PWD:/tests:ro" -w /tests \
  checkson-cli-e2e-check:local tests/browser_fixture.py
```

The browser scenarios test both success and token-list failure, including token
cleanup with tooltip-labelled Delete buttons and overlapping token prefixes.

GitHub Actions runs the unit tests, builds the Linux image, and runs the browser
regressions. After tests pass, pushes to `main` publish `main`, `latest`, and a
commit-specific tag to GHCR. Pull requests run the same checks without publishing.
A manual workflow run can also rebuild and publish the image. Live account
credentials are not needed by CI; live monitoring happens in Checkson.
