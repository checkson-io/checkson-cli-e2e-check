# checkson.io CLI end-to-end check

A synthetic monitor for the [checkson.io CLI](https://github.com/checkson-io/checkson-cli), covering installation discovery, browser authorization, personal access tokens, and authenticated API access.

The check runs as a container on [checkson.io](https://checkson.io). It combines browser automation with execution of the latest released CLI to verify a complete user workflow across the web application, release distribution, authentication services, and backend.

## Coverage

Each run uses a fresh browser session and isolated CLI configuration directories.

1. Sign in to the checkson.io web application using email and password.
2. Navigate to **CLI Tooling** and follow the installation link to GitHub.
3. Download the latest Linux release, verify its published SHA-256 checksum, and execute `checkson version`.
4. Run `checkson login` and approve the authorization request in the authenticated browser session.
5. Run `checkson list` and assert that the configured check name appears.
6. Create a temporary personal access token through the web application.
7. Authenticate a separate CLI session with that token and repeat the list assertion.
8. Delete the temporary token through the web application and verify its removal.

Following the installation link from the application also exercises the link delivered by the deployed frontend. Separate CLI configurations prevent either authentication route from reusing credentials established by the other.

The list assertion checks for an exact name match, independently of the monitored check's health.

## Configuration

Container image:

```text
ghcr.io/checkson-io/checkson-cli-e2e:latest
```

The image is published for `linux/amd64`. Tags `main` and `latest` track successful builds from the default branch. Commit-specific tags use `sha-<full commit SHA>`.

| Variable | Required | Description |
| --- | --- | --- |
| `user` | Yes | checkson.io account email |
| `pw` | Yes | checkson.io account password |
| `EXPECTED_CHECK` | No | Exact check name expected in CLI output. Default: `checkson-cli-e2e` |
| `JOURNEY_TIMEOUT` | No | Workflow timeout in seconds, excluding cleanup. Default: `45` |
| `CHECKSON_DIR` | Provided by checkson.io | Directory for result messages, attachments, and persistent cleanup state |

The account must support email/password authentication and have access to the expected check. The monitor can assert its own presence; a separate fixture check is optional.

Configure credentials through the execution environment. No permanent personal access token is required.

## Local execution

With `user` and `pw` exported:

```sh
docker run --rm --pull=always --platform linux/amd64 --init --ipc=host \
  -e user -e pw \
  -e CHECKSON_DIR=/results \
  -v checkson-cli-e2e-results:/results \
  ghcr.io/checkson-io/checkson-cli-e2e:latest
```

The volume preserves cleanup state across runs. Private GHCR packages require registry authentication.

## Results and failure handling

Exit code `0` indicates that the complete workflow, including token cleanup, succeeded. Any failed assertion, timeout, or cleanup failure produces exit code `1`.

Timestamped log entries identify each stage. When running on checkson.io, the check also writes:

- A short result message to `$CHECKSON_DIR/message`.
- A diagnostic attachment to `$CHECKSON_DIR/attachments/cli-e2e.json`.

The attachment records the selected release, download URL, execution time, failure stage, and pending token cleanup. If cleanup fails after an earlier workflow failure, both stages are retained.

Credentials, token values, browser traces, and screenshots are excluded from diagnostic output.

## Operational considerations

**Execution budget.** The default workflow timeout is 45 seconds, with an additional 8-second cleanup budget. Account for browser startup and shutdown when selecting the container's overall execution limit.

**Token lifecycle.** Cleanup is attempted after success or failure. The temporary token's ID is persisted so a subsequent run can recover from an interrupted execution. A termination between token creation and recording its ID may still require manual cleanup.

**Concurrency.** Run one instance per account at a time. The check identifies its temporary token by comparing token IDs before and after creation; concurrent token creation can make that identification ambiguous.

**Authentication dependencies.** Token authentication is tested after browser authentication because the browser session creates the temporary token. Earlier failures stop the remaining workflow.

**Version metadata.** A reported version that differs from the selected release fails the check. Empty version metadata produces a warning to accommodate releases with missing build metadata; archive checksum verification remains mandatory.

## Development and CI

The implementation uses Python and Playwright with Chromium Headless Shell.

```sh
python -m unittest discover -v
python tests/browser_fixture.py
```

Browser tests require Playwright and its Chromium installation. They intercept network requests and use a fixture CLI, exercising the workflow without live account credentials. Scenarios cover successful execution, token-list failure, and a missing installation link in the application.

GitHub Actions runs the unit tests and containerized browser regression tests before publishing images to GHCR. Pull requests run validation without publishing.
