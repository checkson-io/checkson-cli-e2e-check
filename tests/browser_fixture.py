import asyncio
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.async_api import async_playwright

from check import Check, CheckFailure


async def scenario(p, fail_token_list=False, broken_cli_link=False):
    with tempfile.TemporaryDirectory() as root:
        check = Check(root)
        source = b"""#!/usr/bin/env python3
import os,sys,time
from pathlib import Path
home=Path.home()
if sys.argv[1]=='version': print('cmd.info{version:"v1.0.18"}')
elif sys.argv[1]=='login':
 if '--personal-access-token' in sys.argv:
  (home/'authorized').write_text('token')
 else:
  print('Go to https://app.checkson.io/signin-device-code?deviceCode=test-code',flush=True)
  while not (home/'authorized').exists(): time.sleep(.02)
elif sys.argv[1]=='list':
 assert (home/'authorized').exists()
 print('OK  '+('wrong-check' if os.path.exists(str(home.parent/'fail-token')) and home.name=='token-home' else 'checkson-cli-e2e')+'  yesterday')
"""
        if fail_token_list:
            (Path(root) / "fail-token").touch()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as archive:
            member = tarfile.TarInfo("checkson")
            member.size = len(source)
            archive.addfile(member, io.BytesIO(source))
        data = buf.getvalue()

        class Response:
            ok = True

            async def body(self):
                return data

            async def text(self):
                return (
                    hashlib.sha256(data).hexdigest()
                    + "  checkson-cli_1.0.18_linux_amd64.tar.gz"
                )

        async def get(*args, **kwargs):
            return Response()

        browser = await p.chromium.launch(
            channel=os.getenv("BROWSER_CHANNEL"), headless=True
        )
        context = await browser.new_context()
        check.page = await context.new_page()
        check.page.set_default_timeout(3000)
        check.context = SimpleNamespace(request=SimpleNamespace(get=get))
        existing = {"id": "existing", "firstChars": "abc", "created": "2026-01-01"}
        tokens = [existing]

        async def route_handler(route):
            url = route.request.url
            path = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(url).path
            html = ""
            if path == "/signin":
                html = '<form onsubmit="event.preventDefault();document.cookie=\'signedin=yes;path=/\';location.href=\'/checks\'"><input name="email"><input name="password" type="password"><button>Login</button></form>'
            elif path == "/checks":
                assert "signedin=yes" in route.request.headers.get("cookie", "")
                html = '<a href="/cli">CLI Tooling</a>'
            elif path == "/cli":
                assert "signedin=yes" in route.request.headers.get("cookie", "")
                html = "<h1>Command line tooling</h1>"
                if not broken_cli_link:
                    html += '<a target="_blank" href="https://github.com/stefan-hudelmaier/checkson-cli">GitHub</a>'
            elif path == "/stefan-hudelmaier/checkson-cli":
                await route.fulfill(
                    content_type="text/html",
                    body='<script>location.replace("https://github.com/checkson-io/checkson-cli")</script>',
                )
                return
            elif path == "/checkson-io/checkson-cli":
                html = '<a href="/checkson-io/checkson-cli/releases">releases page</a>'
            elif path.endswith("/releases"):
                html = '<a href="/checkson-io/checkson-cli/releases/tag/v1.0.18">Latest</a>'
            elif "/releases/tag/" in path:
                html = '<a href="/checkson-io/checkson-cli/releases/download/v1.0.18/checkson-cli_1.0.18_linux_amd64.tar.gz">Download</a><a href="/checkson-io/checkson-cli/releases/download/v1.0.18/checksums.txt">checksums.txt</a>'
            elif path == "/signin-device-code":
                assert "signedin=yes" in route.request.headers.get("cookie", ""), (
                    "CLI authorization lost browser session"
                )
                html = """<button onclick="fetch('/confirm').then(()=>document.body.innerHTML='The console application has been authorized.')">Yes</button>"""
            elif path == "/confirm":
                (Path(root) / "browser-home" / "authorized").write_text("browser")
                await route.fulfill(json={})
                return
            elif path == "/listpersonalaccesstokens":
                await route.fulfill(json={"result": tokens})
                return
            elif path == "/createpersonalaccesstoken":
                tokens.append(
                    {
                        "id": "created-by-test",
                        "firstChars": "abc",
                        "created": "2026-01-02",
                    }
                )
                await route.fulfill(json={})
                return
            elif path == "/deletepersonalaccesstoken":
                token_id = json.loads(route.request.post_data)["patId"]
                assert token_id == "created-by-test", "Attempt to delete existing token"
                tokens[:] = [t for t in tokens if t["id"] != token_id]
                await route.fulfill(json={"result": None})
                return
            elif path == "/personal-access-tokens":
                html = """<table><tbody></tbody></table><button onclick="createToken()">Create new personal access token</button>
<script>
async function load(){const r=await fetch('/listpersonalaccesstokens',{method:'POST'});const b=await r.json();document.querySelector('tbody').innerHTML=b.result.map(t=>`<tr><td>${t.firstChars}</td><td><button aria-label="Delete personal access token" onclick="removeToken('${t.id}')">Delete</button></td></tr>`).join('');}
async function createToken(){await fetch('/createpersonalaccesstoken',{method:'POST'});const d=document.createElement('div');d.setAttribute('role','dialog');d.innerHTML='<strong>abc-secret</strong><button onclick="this.parentNode.remove()">Okay, I have saved it</button>';document.body.appendChild(d);await load();}
async function removeToken(id){await fetch('/deletepersonalaccesstoken',{method:'POST',body:JSON.stringify({patId:id})});await load();}
load();</script>"""
            else:
                raise AssertionError("Unexpected request " + url)
            await route.fulfill(content_type="text/html", body=html)

        await context.route("**/*", route_handler)
        try:
            with (
                patch.dict(
                    os.environ,
                    {"user": "fixture@example.invalid", "pw": "fixture-password"},
                ),
                patch("check.platform.system", return_value="Linux"),
                patch("check.platform.machine", return_value="x86_64"),
            ):
                try:
                    await check.journey()
                    assert not fail_token_list and not broken_cli_link
                except CheckFailure:
                    if not fail_token_list:
                        raise
                    assert check.stage == "Verify list using token authentication"
                except Exception:
                    if not broken_cli_link:
                        raise
                    assert check.stage == "Discover CLI in Checkson app"
                    assert not check.binary.exists()
                finally:
                    if check.token_id:
                        await check.delete_token(check.token_id)
            assert tokens == [existing]
            print(
                f"PASS: browser fixture, token_list_failure={fail_token_list}, missing_app_link={broken_cli_link}"
            )
        finally:
            await browser.close()


async def main():
    async with async_playwright() as p:
        await scenario(p)
        await scenario(p, True)
        await scenario(p, broken_cli_link=True)


asyncio.run(main())
