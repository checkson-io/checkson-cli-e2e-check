import asyncio
import io
import os
import tarfile
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from check import Check, CheckFailure, extract_binary, has_check


class OutputTests(unittest.TestCase):
    def test_exact_fixture_in_colored_borderless_output(self):
        self.assertTrue(
            has_check(
                "STATUS  NAME               SINCE\n"
                "\x1b[31m✖\x1b[0m       checkson-cli-e2e    10 minutes ago",
                "checkson-cli-e2e",
            )
        )

    def test_does_not_accept_substring(self):
        self.assertFalse(
            has_check("✔  checkson-cli-e2e-old  yesterday", "checkson-cli-e2e")
        )

    def test_accepts_bordered_table(self):
        self.assertTrue(
            has_check("| OK | checkson-cli-e2e | yesterday |", "checkson-cli-e2e")
        )

    def test_health_status_does_not_affect_presence(self):
        self.assertTrue(
            has_check("✖  checkson-cli-e2e  yesterday  1 / 1", "checkson-cli-e2e")
        )


class ArchiveTests(unittest.TestCase):
    def archive(self, entries):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, content, kind in entries:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                if kind == tarfile.REGTYPE:
                    entry.size = len(content)
                    archive.addfile(entry, io.BytesIO(content))
                else:
                    entry.linkname = "/etc/passwd"
                    archive.addfile(entry)
        return stream.getvalue()

    def test_only_binary_is_written_even_with_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "binary"
            data = self.archive(
                [
                    ("../../unwanted", b"bad", tarfile.REGTYPE),
                    ("nested/checkson", b"binary", tarfile.REGTYPE),
                ]
            )
            extract_binary(data, target)
            self.assertEqual(target.read_bytes(), b"binary")
            self.assertEqual(list(Path(root).iterdir()), [target])
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)

    def test_symlink_cannot_be_used_as_binary(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaises(CheckFailure):
            extract_binary(
                self.archive([("checkson", b"", tarfile.SYMTYPE)]),
                Path(root) / "binary",
            )

    def test_ambiguous_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as root, self.assertRaises(CheckFailure):
            extract_binary(
                self.archive(
                    [
                        ("checkson", b"one", tarfile.REGTYPE),
                        ("nested/checkson", b"two", tarfile.REGTYPE),
                    ]
                ),
                Path(root) / "binary",
            )


class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_login_has_its_own_home_and_no_password_in_environment(self):
        with tempfile.TemporaryDirectory() as root:
            check = Check(root)
            check.binary.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "from pathlib import Path\n"
                "assert 'pw' not in os.environ and 'user' not in os.environ\n"
                "auth = Path.home() / '.checkson'\n"
                "assert not auth.exists()\n"
                "auth.write_text('session')\n"
                "print(Path.home())\n"
            )
            check.binary.chmod(0o700)
            with patch.dict(
                os.environ, {"user": "test@example.invalid", "pw": "secret"}
            ):
                for route in ("browser", "token"):
                    home = Path(root) / route
                    self.assertEqual(
                        (await check.cli(home, "login")).strip(), str(home)
                    )

    async def test_failed_cli_does_not_expose_output_or_token_argument(self):
        with tempfile.TemporaryDirectory() as root:
            check = Check(root)
            check.binary.write_text("#!/bin/sh\necho very-secret >&2\nexit 1\n")
            check.binary.chmod(0o700)
            with self.assertRaises(CheckFailure) as caught:
                await check.cli(
                    Path(root) / "home",
                    "login",
                    "--personal-access-token",
                    "very-secret",
                )
            self.assertNotIn("very-secret", str(caught.exception))


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_deletion_identifies_token_by_id_despite_duplicate_prefixes(self):
        with tempfile.TemporaryDirectory() as root:
            check = Check(root)
            old = {"id": "existing", "firstChars": "abc", "created": "2026-01-01"}
            new = {"id": "new", "firstChars": "abc", "created": "2026-01-02"}
            check.token_table = AsyncMock(return_value=[old, new])
            page = MagicMock()
            page.wait_for_function = AsyncMock()
            rows = page.locator.return_value.filter.return_value
            rows.nth.return_value.get_by_role.return_value.click = AsyncMock()
            deleted = MagicMock(ok=True)
            deleted.json = AsyncMock(return_value={"result": None})
            refreshed = MagicMock(ok=True)
            refreshed.json = AsyncMock(return_value={"result": [old]})

            async def response_after(endpoint, action):
                await action()
                return deleted if endpoint == "deletepersonalaccesstoken" else refreshed

            check.response_after = response_after
            check.page = page
            await check.delete_token("new")
            rows.nth.assert_called_once_with(1)

    async def exercise_cleanup(self, journey_error=None, cleanup_error=None):
        with tempfile.TemporaryDirectory() as root:
            check = Check(root)
            check.stage = "Original journey step"
            check.token_id = "only-the-token-created-by-this-run"
            check.ledger.parent.mkdir(parents=True)
            check.ledger.write_text('{"id":"only-the-token-created-by-this-run"}')
            check.journey = AsyncMock(side_effect=journey_error)
            check.delete_token = AsyncMock(side_effect=cleanup_error)
            browser = MagicMock()
            browser.close = AsyncMock()
            context = MagicMock()
            context.new_page = AsyncMock()
            browser.new_context = AsyncMock(return_value=context)
            runtime = MagicMock()
            runtime.chromium.launch = AsyncMock(return_value=browser)
            manager = MagicMock()
            manager.__aenter__ = AsyncMock(return_value=runtime)
            manager.__aexit__ = AsyncMock(return_value=False)
            module = types.ModuleType("playwright.async_api")
            module.async_playwright = lambda: manager
            with (
                patch.dict("sys.modules", {"playwright.async_api": module}),
                patch.dict(
                    os.environ, {"user": "test@example.invalid", "pw": "secret"}
                ),
            ):
                expected_error = cleanup_error or journey_error
                if expected_error:
                    with self.assertRaises(type(expected_error)):
                        await check.run()
                else:
                    await check.run()
            check.delete_token.assert_awaited_once_with(
                "only-the-token-created-by-this-run"
            )
            browser.close.assert_awaited_once()
            self.assertEqual(check.ledger.exists(), cleanup_error is not None)
            self.assertEqual(check.token_id is not None, cleanup_error is not None)
            self.assertEqual(
                check.stage,
                "Delete temporary token in web UI"
                if cleanup_error or not journey_error
                else "Original journey step",
            )
            self.assertEqual(
                check.journey_failure_stage,
                "Original journey step" if journey_error else None,
            )

    async def test_cleanup_after_success(self):
        await self.exercise_cleanup()

    async def test_cleanup_after_assertion_failure(self):
        await self.exercise_cleanup(journey_error=CheckFailure("fixture missing"))

    async def test_cleanup_after_timeout(self):
        await self.exercise_cleanup(journey_error=TimeoutError())

    async def test_cleanup_failure_fails_check_and_preserves_token_id(self):
        await self.exercise_cleanup(cleanup_error=CheckFailure("deletion failed"))

    async def test_cleanup_failure_records_original_failure_separately(self):
        await self.exercise_cleanup(
            journey_error=CheckFailure("list failed"),
            cleanup_error=TimeoutError(),
        )


class ResponseWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_response_listener_registered_before_action_and_removed(self):
        with tempfile.TemporaryDirectory() as root:
            check = Check(root)
            check.page = MagicMock()
            response = types.SimpleNamespace(
                url="https://example.invalid/listpersonalaccesstokens",
                request=types.SimpleNamespace(method="POST"),
            )

            async def action():
                callback = check.page.on.call_args.args[1]
                callback(response)

            result = await check.response_after("listpersonalaccesstokens", action)
            self.assertIs(result, response)
            check.page.remove_listener.assert_called_once_with(
                "response", check.page.on.call_args.args[1]
            )

    async def test_cancelled_action_removes_listener_without_unobserved_exception(self):
        with tempfile.TemporaryDirectory() as root:
            check = Check(root)
            check.page = MagicMock()
            started = asyncio.Event()

            async def action():
                started.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(
                check.response_after("deletepersonalaccesstoken", action)
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            callback = check.page.on.call_args.args[1]
            check.page.remove_listener.assert_called_once_with("response", callback)
            # A late response cannot complete a cancelled future or raise a second error.
            callback(
                types.SimpleNamespace(
                    url="https://example.invalid/deletepersonalaccesstoken",
                    request=types.SimpleNamespace(method="POST"),
                )
            )


if __name__ == "__main__":
    unittest.main()
