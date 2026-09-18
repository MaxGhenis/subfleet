"""subfleet enroll --mint: capture a setup-token from a pty-run `claude setup-token`."""
import os
import stat
import sys
import textwrap

import pytest

from subfleet import enroll_mint

FAKE_TOKEN = "sk-ant-oat01-" + "Ab3_-" * 14            # 70 chars after the prefix
REAL_URL = ("https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
            "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
            "&scope=user%3Ainference&code_challenge=05yBELgHkLgxjjDHZO_mwq3t09y858VnPkLLfvk7lUQ"
            "&code_challenge_method=S256")


def _fake_claude(tmp_path, body):
    """A stand-in for `claude` whose `setup-token` subcommand runs `body`."""
    script = tmp_path / "claude"
    script.write_text("#!%s\n" % sys.executable + textwrap.dedent(body))
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


HAPPY = """
    import sys
    assert sys.argv[1:] == ["setup-token"], sys.argv
    print("\\x1b7\\x1b8Welcome to Claude Code v2.1.273")
    print("\\u00b7 Opening browser to sign in\\u2026")
    print("Browser didn't open? Use the url below to sign in (c to copy)")
    print("%s")
    sys.stdout.flush()
    sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code != "CODE-123":
        print("\\nInvalid code"); sys.exit(1)
    print("\\n\\u2713 Long-lived authentication token created successfully!")
    print("Your OAuth token (valid for 1 year):")
    print("\\x1b[33m%s\\x1b[0m")
    print("Store this token securely. You won't be able to see it again.")
""" % (REAL_URL, FAKE_TOKEN)


def test_scan_and_mask_on_real_shapes():
    # The CLI's TUI can drop spaces between words when rendered through a pty.
    text = "Browser didn't open? Use the url below to sign in (c to copy)\n" + REAL_URL + "\nPastecode here if prompted >"
    seen = enroll_mint.scan(text)
    assert seen["url"] == REAL_URL
    assert seen["paste_prompt"] is True
    assert seen["token"] is None
    done = enroll_mint.clean(("Your OAuth token (valid for 1 year):\r\n\x1b[33m%s\x1b[0m\r\n" % FAKE_TOKEN).encode())
    assert enroll_mint.scan(done)["token"] == FAKE_TOKEN
    masked = enroll_mint.mask(done, FAKE_TOKEN)
    assert FAKE_TOKEN not in masked and enroll_mint.MASK in masked


def test_mint_captures_token_and_never_exposes_it(tmp_path):
    urls, prompts = [], []
    res = enroll_mint.mint(
        _fake_claude(tmp_path, HAPPY),
        on_url=urls.append,
        code_prompt=lambda: (prompts.append(1), "CODE-123")[1],
        paste=True,
        timeout_s=20,
    )
    assert res.token == FAKE_TOKEN
    assert res.exit_code == 0
    assert res.error is None
    assert urls == [REAL_URL]
    assert prompts == [1]                       # asked exactly once
    assert res.code_sent is True
    assert FAKE_TOKEN not in res.transcript
    assert enroll_mint.MASK in res.transcript
    assert res.events == ["url", "code", "token"]


def test_mint_wrong_code_reports_failure(tmp_path):
    res = enroll_mint.mint(_fake_claude(tmp_path, HAPPY), code_prompt=lambda: "nope", paste=True, timeout_s=20)
    assert res.token is None
    assert res.exit_code is not None                       # child reaped either way
    assert "Invalid code" in res.error                      # the CLI's complaint is surfaced, fast


def test_mint_aborts_when_no_code_available(tmp_path):
    res = enroll_mint.mint(_fake_claude(tmp_path, HAPPY), code_prompt=lambda: None, paste=True, timeout_s=20)
    assert res.token is None
    assert "none was supplied" in res.error


def test_mint_times_out_and_kills_child(tmp_path):
    hang = """
        import time
        print("Opening browser to sign in...")
        time.sleep(30)
    """
    res = enroll_mint.mint(_fake_claude(tmp_path, hang), paste=True, timeout_s=1.5)
    assert res.token is None
    assert "timed out" in res.error
    assert res.exit_code is not None            # child reaped, not left running


def test_child_env_drops_inherited_credentials():
    env = enroll_mint._child_env({"CLAUDE_CODE_OAUTH_TOKEN": "x", "ANTHROPIC_API_KEY": "y", "PATH": "/bin"})
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and "ANTHROPIC_API_KEY" not in env
    assert env["PATH"] == "/bin" and env["COLUMNS"] == str(enroll_mint.COLUMNS)


def test_clean_strips_osc_hyperlinks_and_private_csi():
    raw = (b"\x1b]8;;https://example.test/x\x1b\\link text\x1b]8;;\x1b\\ \x1b[>0q\x1b]0;title\x07"
           b"\x1b[33m" + FAKE_TOKEN.encode() + b"\x1b[39m")
    text = enroll_mint.clean(raw)
    assert "\x1b" not in text
    assert enroll_mint.scan(text)["token"] == FAKE_TOKEN


def test_well_formed_rejects_short_or_dirty_tokens():
    assert enroll_mint.well_formed(FAKE_TOKEN)
    assert not enroll_mint.well_formed("sk-ant-oat01-" + "A" * 40)          # too short: a wrapped line
    assert not enroll_mint.well_formed(FAKE_TOKEN + "\x1b[0m")
    assert not enroll_mint.well_formed(None)


def test_timeout_does_not_count_time_at_the_code_prompt(tmp_path):
    import time
    def slow_prompt():
        time.sleep(4.0)                              # human takes longer than the whole timeout
        return "CODE-123"
    res = enroll_mint.mint(_fake_claude(tmp_path, HAPPY), code_prompt=slow_prompt, paste=True, timeout_s=3.0)
    assert res.token == FAKE_TOKEN, res.error


def test_diagnostic_is_masked_and_written_0600(tmp_path):
    res = enroll_mint.mint(_fake_claude(tmp_path, HAPPY), code_prompt=lambda: "CODE-123", paste=True, timeout_s=20)
    rec = enroll_mint.diagnostic(res, {"status": "network-error", "error": "boom", "checked_at": "t"})
    import json
    blob = json.dumps(rec)
    assert FAKE_TOKEN not in blob
    assert rec["token_len"] == len(FAKE_TOKEN) and rec["well_formed"] is True
    assert rec["probe"] == {"status": "network-error", "error": "boom", "checked_at": "t"}
    out = tmp_path / "state" / "enroll-mint-last.json"
    enroll_mint.write_diagnostic(out, rec)
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    assert FAKE_TOKEN not in out.read_text()


# ---------------------------------------------------------------- native mode (the CLI's own callback)

STATE = "un51h67m48C7BsuWOuebauLCK4cTH9uGkDJawuZdJec"
NATIVE_URL = REAL_URL.replace("https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback",
                              "http%3A%2F%2Flocalhost%3A55828%2Fcallback") + "&state=" + STATE

# Mimics the real CLI: launches `open <url>` (subfleet's stub records it and
# forwards to $SUBFLEET_MINT_REAL_OPEN), shows the paste line as a fallback,
# then receives the code on its own callback and prints the token. No paste.
NATIVE = """
    import subprocess, sys, time
    assert sys.argv[1:] == ["setup-token"], sys.argv
    print("\\u00b7 Opening browser to sign in\\u2026"); sys.stdout.flush()
    rc = subprocess.call(["open", "%s"])
    if rc != 0:
        print("Browser didn't open? Use the url below to sign in (c to copy)")
        print("%s")
    sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
    time.sleep(0.8)                                   # the browser approval arrives on the CLI's own port
    print("\\n\\u2713 Long-lived authentication token created successfully!")
    print("Your OAuth token (valid for 1 year):")
    print("%s")
""" % (NATIVE_URL, NATIVE_URL, FAKE_TOKEN)

OAUTH_FAIL = """
    import subprocess, sys, time
    subprocess.call(["open", "%s"])
    sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
    time.sleep(0.5)
    print("\\nOAuth error: Request failed with status code 400 Press Enter to retry.")
    sys.stdout.flush()
    time.sleep(30)                                    # the real CLI waits here forever
""" % NATIVE_URL

NO_BROWSER = {"SUBFLEET_MINT_REAL_OPEN": "/usr/bin/true", "PATH": os.environ.get("PATH", "")}


def test_native_mode_never_prompts_and_captures_the_token(tmp_path):
    relayed, prompts = [], []
    res = enroll_mint.mint(_fake_claude(tmp_path, NATIVE), on_url=relayed.append,
                           code_prompt=lambda: (prompts.append(1), "unused")[1], env=NO_BROWSER, timeout_s=20)
    assert res.token == FAKE_TOKEN, (res.error, res.transcript[-300:])
    assert prompts == []                                    # the paste line is a fallback, not a request
    assert res.code_sent is False and res.code_source is None
    assert relayed == [NATIVE_URL]                          # recorded by the stub, forwarded to `open`
    assert "Browser didn't open" not in res.transcript      # the CLI saw its `open` succeed
    assert res.events == ["url", "token"]
    assert FAKE_TOKEN not in res.transcript


def test_native_mode_fails_fast_on_cli_oauth_error(tmp_path):
    res = enroll_mint.mint(_fake_claude(tmp_path, OAUTH_FAIL), env=NO_BROWSER, timeout_s=20)
    assert res.token is None
    assert "OAuth failure" in res.error and "400" in res.error
    assert res.exit_code is not None                        # child terminated, not left waiting


def test_native_mode_timeout_names_the_browser_approval(tmp_path):
    hang = """
        import subprocess, sys, time
        subprocess.call(["open", "%s"])
        sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
        time.sleep(30)
    """ % NATIVE_URL
    res = enroll_mint.mint(_fake_claude(tmp_path, hang), env=NO_BROWSER, timeout_s=2.0)
    assert res.token is None and "waiting for the browser approval" in res.error


def test_stub_open_records_url_and_forwards(tmp_path):
    import subprocess
    url_file = str(tmp_path / "url"); marker = tmp_path / "forwarded"
    fake_real_open = tmp_path / "real-open"
    fake_real_open.write_text("#!/bin/sh\nprintf '%%s' \"$*\" > '%s'\n" % marker)
    fake_real_open.chmod(0o755)
    d = enroll_mint._stub_open_dir(url_file)
    rc = subprocess.call([os.path.join(d, "open"), "-a", "Safari", NATIVE_URL],
                         env={"SUBFLEET_MINT_URL_FILE": url_file, "SUBFLEET_MINT_REAL_OPEN": str(fake_real_open)})
    assert rc == 0
    assert enroll_mint._read_captured_url(url_file) == NATIVE_URL
    assert marker.read_text() == "-a Safari " + NATIVE_URL   # the real browser launch still happened


def test_scan_detects_the_cli_oauth_error_even_with_dropped_spaces():
    assert enroll_mint.scan("OAuth error: Requstfailed withstatus code 400 Press Enter to retry.")["oauth_error"]
    assert enroll_mint.scan("PressEntertoretry")["oauth_error"]
    assert enroll_mint.scan("Your OAuth token (valid for 1 year):")["oauth_error"] is None


def test_token_is_bounded_by_the_cli_escape_codes_not_by_cleaned_text():
    # Real failure 2026-09-15: the TUI positioned the next line with cursor
    # moves, cleaning erased them, and the text regex captured token+"Store".
    raw = (b"Your OAuth token (valid for 1 year):\x1b[1B\x1b[0G\x1b[33m" + FAKE_TOKEN.encode() + b"\x1b[39m"
           b"\x1b[1B\x1b[0GStore this token securely. You won't be able to see it again.")
    text = enroll_mint.clean(raw)
    assert enroll_mint.scan(text)["token"] == FAKE_TOKEN + "Store"         # the old, wrong answer
    assert enroll_mint.scan(text, raw)["token"] == FAKE_TOKEN               # bounded by \x1b[39m
    assert enroll_mint.token_from_raw(b"no token here") is None


GLUED = """
    import subprocess, sys, time
    subprocess.call(["open", "%s"])
    sys.stdout.write("Paste code here if prompted > "); sys.stdout.flush()
    time.sleep(0.5)
    sys.stdout.write("\\x1b[1B\\x1b[0G\\u2713 Long-lived authentication token created successfully!")
    sys.stdout.write("\\x1b[1B\\x1b[0GYour OAuth token (valid for 1 year):")
    sys.stdout.write("\\x1b[1B\\x1b[0G\\x1b[33m%s\\x1b[39m")
    sys.stdout.write("\\x1b[1B\\x1b[0GStore this token securely. You won't be able to see it again.\\n")
    sys.stdout.flush()
""" % (NATIVE_URL, FAKE_TOKEN)


def test_native_mode_survives_cursor_positioned_output(tmp_path):
    res = enroll_mint.mint(_fake_claude(tmp_path, GLUED), env=NO_BROWSER, timeout_s=20)
    assert res.token == FAKE_TOKEN, (res.error, res.transcript[-200:])
    assert enroll_mint.well_formed(res.token) and len(res.token) == len(FAKE_TOKEN)
