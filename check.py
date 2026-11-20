"""App login -> app CLI link -> released CLI -> browser login -> list -> UI token -> token login -> list."""

import asyncio
import hashlib
import io
import json
import os
import platform
import re
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class CheckFailure(Exception):
    """Safe, deliberately credential-free diagnostic."""


def has_check(output, expected):
    # Names occupy one column; a substring could accidentally match a different check.
    for line in ANSI.sub("", output).splitlines():
        columns = re.split(r"\s{2,}|\s*\|\s*", line.strip())
        if expected in columns:
            return True
    return False


def extract_binary(data, destination):
    # Never extract arbitrary paths or links from an externally downloaded archive.
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        matches = [
            m
            for m in archive.getmembers()
            if m.isfile() and Path(m.name).name == "checkson"
        ]
        if len(matches) != 1 or matches[0].size > 100_000_000:
            raise CheckFailure("Release must contain exactly one checkson executable")
        with archive.extractfile(matches[0]) as source:
            destination.write_bytes(source.read())
    destination.chmod(0o700)


class Check:
    def __init__(self, root):
        self.root = Path(root)
        self.binary = self.root / "checkson"
        self.expected = os.getenv("EXPECTED_CHECK", "checkson-cli-e2e")
        self.stage = "configuration"
        self.started = time.monotonic()
        self.processes = set()
        self.context = None
        self.page = None
        self.token_id = None
        self.release = None
        self.download_url = None
        self.reported_version = None
        self.journey_failure_stage = None
        self.check_dir = Path(os.getenv("CHECKSON_DIR", str(self.root / "results")))
        self.ledger = self.check_dir / "persistent" / "cli-e2e-token.json"

    def step(self, stage):
        self.stage = stage
        print(f"[{time.monotonic() - self.started:.1f}s] {stage}", flush=True)

    async def start_cli(self, home, *args):
        home.mkdir(mode=0o700, exist_ok=True)
        # Do not pass the user's password or unrelated container secrets to the CLI.
        env = {
            k: os.environ[k]
            for k in ("PATH", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if k in os.environ
        }
        env.update(HOME=str(home), NO_COLOR="1")
        proc = await asyncio.create_subprocess_exec(
            str(self.binary),
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.processes.add(proc)
        return proc

    async def cli(self, home, *args):
        proc = await self.start_cli(home, *args)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        self.processes.discard(proc)
        if proc.returncode != 0:
            # CLI errors may contain credentials or server response bodies.
            raise CheckFailure(f"CLI {args[0]} exited with status {proc.returncode}")
        return stdout.decode(errors="replace")

    async def response_after(self, endpoint, action):
        # Own the future so cancellation during a UI action cannot leave an
        # unobserved Playwright expect_response exception when the browser closes.
        future = asyncio.get_running_loop().create_future()

        def receive(response):
            if (
                not future.done()
                and urlparse(response.url).path.endswith("/" + endpoint)
                and response.request.method == "POST"
            ):
                future.set_result(response)

        self.page.on("response", receive)
        try:
            await action()
            return await asyncio.wait_for(future, timeout=7)
        finally:
            self.page.remove_listener("response", receive)
            if not future.done():
                future.cancel()

    async def read_tokens(self, response):
        if not response.ok:
            raise CheckFailure("Could not load token list")
        body = await response.json()
        tokens = body.get("result", body.get("data"))
        if not isinstance(tokens, list):
            raise CheckFailure("Invalid token list response")
        return sorted(tokens, key=lambda t: t["created"])

    async def token_table(self):
        response = await self.response_after(
            "listpersonalaccesstokens",
            lambda: self.page.goto("https://app.checkson.io/personal-access-tokens"),
        )
        return await self.read_tokens(response)

    async def delete_token(self, token_id):
        tokens = await self.token_table()
        ids = [t["id"] for t in tokens]
        if token_id not in ids:
            return  # Already removed by a previous cleanup attempt.
        # The UI sorts by creation time; use the exact ID's position, never its short prefix.
        # Joy Tooltip gives this button the accessible name from its title,
        # although the visible button text is just "Delete".
        delete_name = re.compile(r"^Delete(?: personal access token)?$")
        rows = self.page.locator("tbody tr").filter(
            has=self.page.get_by_role("button", name=delete_name)
        )
        await self.page.wait_for_function(
            "count => document.querySelectorAll('tbody tr button').length === count",
            arg=len(tokens),
        )

        async def click_delete():
            response = await self.response_after(
                "deletepersonalaccesstoken",
                lambda: (
                    rows.nth(ids.index(token_id))
                    .get_by_role("button", name=delete_name)
                    .click()
                ),
            )
            if not response.ok or "error" in await response.json():
                raise CheckFailure("Temporary token deletion failed")

        # Deletion invalidates the UI query. Verify its refreshed response rather
        # than reloading the whole app a second time inside the cleanup budget.
        refreshed = await self.response_after("listpersonalaccesstokens", click_delete)
        if token_id in [t["id"] for t in await self.read_tokens(refreshed)]:
            raise CheckFailure("Temporary token still exists after deletion")

    async def run(self):
        if not os.getenv("user") or not os.getenv("pw"):
            raise CheckFailure("Set the user and pw environment variables")
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            self.context = await browser.new_context(accept_downloads=True)
            self.context.set_default_timeout(7000)
            self.context.set_default_navigation_timeout(10000)
            self.page = await self.context.new_page()
            try:
                await asyncio.wait_for(
                    self.journey(), timeout=float(os.getenv("JOURNEY_TIMEOUT", "45"))
                )
            except Exception:
                self.journey_failure_stage = self.stage
                raise
            finally:
                # A separate budget leaves time before Checkson terminates the container.
                try:
                    if self.token_id:
                        self.step("Delete temporary token in web UI")
                        await asyncio.wait_for(
                            self.delete_token(self.token_id), timeout=8
                        )
                        self.ledger.unlink(missing_ok=True)
                        self.token_id = None
                        if self.journey_failure_stage:
                            self.stage = self.journey_failure_stage
                finally:
                    for proc in self.processes:
                        if proc.returncode is None:
                            proc.kill()
                        await proc.communicate()
                    await browser.close()

    async def sign_in_app(self):
        self.step("Sign in to Checkson app with email/password")
        await self.page.goto("https://app.checkson.io/signin")
        await self.page.locator('input[name="email"]').fill(os.environ["user"])
        await self.page.locator('input[name="password"]').fill(os.environ["pw"])
        await self.page.get_by_role("button", name="Login", exact=True).click()
        await self.page.get_by_role("link", name="CLI Tooling", exact=True).wait_for()

    async def download_cli(self):
        self.step("Discover CLI in Checkson app")
        await self.page.get_by_role("link", name="CLI Tooling", exact=True).click()
        await self.page.get_by_role(
            "heading", name="Command line tooling", exact=True
        ).wait_for()
        installation_link = self.page.get_by_role("link", name="GitHub", exact=True)
        await installation_link.wait_for()
        async with self.page.expect_popup() as popup:
            await installation_link.click()
        self.page = await popup.value
        await self.page.wait_for_url("https://github.com/checkson-io/checkson-cli")
        repo = urlparse(self.page.url)
        if (
            repo.hostname != "github.com"
            or repo.path.rstrip("/") != "/checkson-io/checkson-cli"
        ):
            raise CheckFailure("App CLI link did not reach the official repository")

        self.step("Follow installation instructions to latest release")
        await self.page.get_by_role(
            "link", name="releases page", exact=True
        ).first.click()
        # Release lists are newest first; GitHub's Latest badge identifies the stable release.
        await self.page.get_by_role("link", name="Latest", exact=True).first.click()
        await self.page.wait_for_url("**/releases/tag/**")
        match = re.search(r"/releases/tag/(v?\d+\.\d+\.\d+[^/]*)", self.page.url)
        if not match:
            raise CheckFailure("Could not identify latest release version")
        self.release = match[1]
        print(f"Release: {self.release}", flush=True)
        arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
        if not arch or platform.system() != "Linux":
            raise CheckFailure("Run this check in its Linux Docker image")
        assets = self.page.locator("details").filter(
            has=self.page.locator("summary").filter(has_text=re.compile(r"^\s*Assets"))
        )
        if await assets.count() and await assets.first.get_attribute("open") is None:
            await assets.first.locator("summary").click()
        link = self.page.locator(f'a[href$="_linux_{arch}.tar.gz"]').first
        await link.wait_for(state="attached")
        self.download_url = await link.get_attribute("href")
        from urllib.parse import urljoin

        self.download_url = urljoin(self.page.url, self.download_url)
        if not self.download_url.startswith(
            "https://github.com/checkson-io/checkson-cli/releases/download/"
        ):
            raise CheckFailure("Unexpected CLI download location")

        self.step("Download and execute released CLI")
        response = await self.context.request.get(self.download_url, timeout=15000)
        if not response.ok:
            raise CheckFailure(f"Download returned HTTP {response.status}")
        archive = await response.body()
        checksum_link = self.page.locator('a[href$="/checksums.txt"]').first
        checksum_url = urljoin(self.page.url, await checksum_link.get_attribute("href"))
        checksum_response = await self.context.request.get(checksum_url, timeout=7000)
        if not checksum_response.ok:
            raise CheckFailure("Release checksum download failed")
        filename = self.download_url.rsplit("/", 1)[-1]
        checksums = {
            parts[1].lstrip("*"): parts[0]
            for line in (await checksum_response.text()).splitlines()
            if len(parts := line.split()) == 2
        }
        if checksums.get(filename) != hashlib.sha256(archive).hexdigest():
            raise CheckFailure(
                "CLI archive does not match the published release checksum"
            )
        extract_binary(archive, self.binary)
        version = await self.cli(self.root / "version-home", "version")
        reported = re.search(r'version:"([^"]*)"', version)
        self.reported_version = reported[1] if reported else None
        if not version.strip():
            raise CheckFailure("CLI version command returned no output")
        if self.reported_version == "":
            print(
                "Warning: released CLI has empty version metadata; archive checksum verified",
                flush=True,
            )
        elif not re.search(
            rf"(?<![\w.])v?{re.escape(self.release.lstrip('v'))}(?![\w.])", version
        ):
            raise CheckFailure("CLI version does not match the selected release")

    async def journey(self):
        await self.sign_in_app()
        await self.download_cli()
        self.step("Authorize CLI using signed-in browser")
        home = self.root / "browser-home"
        proc = await self.start_cli(home, "login")
        auth_url = None

        async def read_url():
            while line := await proc.stdout.readline():
                found = re.search(
                    rb"https://app\.checkson\.io/signin-device-code\?deviceCode=[A-Za-z0-9_-]+",
                    line,
                )
                if found:
                    return found[0].decode()
            raise CheckFailure("CLI did not print an authorization URL")

        auth_url = await asyncio.wait_for(read_url(), timeout=10)
        await self.page.goto(auth_url)
        await self.page.get_by_role("button", name="Yes", exact=True).click()
        await self.page.get_by_text(
            "The console application has been authorized.", exact=False
        ).wait_for()
        await asyncio.wait_for(proc.communicate(), timeout=10)
        self.processes.discard(proc)
        if proc.returncode != 0:
            raise CheckFailure("Browser CLI login failed")
        self.step("Verify list using browser authentication")
        if not has_check(await self.cli(home, "list"), self.expected):
            raise CheckFailure("Expected check missing from browser-authenticated list")

        if self.ledger.exists():
            self.step("Clean up token left by interrupted previous run")
            stale_id = json.loads(self.ledger.read_text())["id"]
            await self.delete_token(stale_id)
            self.ledger.unlink()

        self.step("Create temporary token in web UI")
        previous_ids = {t["id"] for t in await self.token_table()}
        response = await self.response_after(
            "listpersonalaccesstokens",
            lambda: self.page.get_by_role(
                "button", name="Create new personal access token", exact=True
            ).click(),
        )
        tokens = await self.read_tokens(response)
        created = [t for t in tokens if t["id"] not in previous_ids]
        if len(created) != 1:
            raise CheckFailure(
                "Cannot uniquely identify the token created by this run; avoid concurrent runs"
            )
        self.token_id = created[0]["id"]
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps({"id": self.token_id}))
        dialog = self.page.get_by_role("dialog")
        token = await dialog.locator("strong").inner_text()
        await dialog.get_by_role(
            "button", name="Okay, I have saved it", exact=True
        ).click()
        self.step("Token CLI login in isolated configuration")
        token_home = self.root / "token-home"
        await self.cli(token_home, "login", "--personal-access-token", token)
        self.step("Verify list using token authentication")
        if not has_check(await self.cli(token_home, "list"), self.expected):
            raise CheckFailure("Expected check missing from token-authenticated list")
        self.step("Both authentication routes verified")


def main():
    with tempfile.TemporaryDirectory(prefix="checkson-cli-e2e-") as root:
        check = Check(root)
        success = False
        try:
            asyncio.run(check.run())
            success = True
        except Exception as error:  # noqa: BLE001 - redact all failures at the process boundary
            # Playwright exceptions and CLI output can contain credentials. Report only safe metadata.
            print(f"FAIL: {check.stage} ({type(error).__name__})", flush=True)
            if isinstance(error, CheckFailure):
                print(str(error), flush=True)
            if check.token_id or check.ledger.exists():
                print(
                    "Temporary token cleanup incomplete; saved ID for next run",
                    flush=True,
                )
        message = (
            "OK: both CLI login routes and list verified"
            if success
            else f"CRITICAL: {check.stage}"
        )
        check.check_dir.mkdir(parents=True, exist_ok=True)
        (check.check_dir / "message").write_text(message[:150])
        attachments = check.check_dir / "attachments"
        attachments.mkdir(exist_ok=True)
        (attachments / "cli-e2e.json").write_text(
            json.dumps(
                {
                    "success": success,
                    "step": check.stage,
                    "journey_failure_stage": check.journey_failure_stage,
                    "release": check.release,
                    "reported_version": check.reported_version,
                    "download_url": check.download_url,
                    "elapsed_seconds": round(time.monotonic() - check.started, 2),
                    "token_cleanup_pending": check.token_id is not None
                    or check.ledger.exists(),
                },
                indent=2,
            )
        )
        print(message, flush=True)
        return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
