# Fixing `psf/requests` httpbin.org flakiness in SWE-bench evaluation

## Problem

The `psf__requests-*` instances in `swe-bench-verified.jsonl` run tests that make live
HTTP/HTTPS requests to `httpbin.org`. That service is an external dependency and is
frequently slow or unreachable, so these instances fail or time out for reasons that
have nothing to do with the model's patch. This produces false negatives.

## Solution (summary)

Stand up a **local** httpbin server inside the evaluation container and redirect
`httpbin.org` to `127.0.0.1`, so the tests hit a local, deterministic server instead of
the public internet. This mirrors the approach in
[microsoft/debug-gym](https://github.com/microsoft/debug-gym/blob/cc3fe3ef4ce08919e522eb00ea1bea5689f3b53e/debug_gym/gym/envs/swe_bench.py#L109-L122).

The fix is applied only for `repo == "psf/requests"`; all other repos are unaffected.

## Where the change lives

File: `swebench/harness/test_spec/python.py`

Two edits:

1. New helper `make_httpbin_setup_commands(env_name)` returning the shell commands.
2. In `make_eval_script_list_py(...)`, after the `install` step and before the
   test-reset/apply-patch/run block, inject those commands when the instance repo is
   `psf/requests`.

### The injected commands (in order)

```bash
python -m pip install 'httpbin[mainapp]==0.10.2' 'pytest-httpbin==2.1.0'
HTTPBIN_CERT_DIR=$(python -c "import os, pytest_httpbin; print(os.path.join(os.path.dirname(pytest_httpbin.__file__), 'certs'))")
export REQUESTS_CA_BUNDLE=$(python -m pytest_httpbin.certs)
export CURL_CA_BUNDLE=$REQUESTS_CA_BUNDLE
(nohup gunicorn -b 127.0.0.1:80 -k gevent httpbin:app > /dev/null 2>&1 &)
(nohup gunicorn -b 127.0.0.1:443 --certfile="$HTTPBIN_CERT_DIR/server.pem" --keyfile="$HTTPBIN_CERT_DIR/server.key" -k gevent httpbin:app > /dev/null 2>&1 &)
sleep 2
echo "127.0.0.1    httpbin.org" >> /etc/hosts
```

## Why each line matters (this is the part to port into your inference logic)

1. **`export REQUESTS_CA_BUNDLE=$(python -m pytest_httpbin.certs)`** (+ mirror to
   `CURL_CA_BUNDLE`)
   The local HTTPS server presents a self-signed cert. The `requests` client verifies
   against a CA bundle, so it must be pointed at pytest_httpbin's CA. This is the
   method documented in the pytest-httpbin README:
   `python -m pytest_httpbin.certs` prints the **client-side CA bundle** path (distinct
   from the server `server.pem`/`server.key` used by gunicorn). Without this, HTTPS
   tests fail with SSL verification errors.

   > Note: the debug-gym reference instead used `export CURL_CA_BUNDLE=""`. For the
   > `requests` library that is effectively a **no-op** — `requests` reads
   > `REQUESTS_CA_BUNDLE or CURL_CA_BUNDLE`, an empty string is falsy and ignored, so
   > verification falls back to the *system* CA bundle (which lacks the self-signed
   > cert). It neither disables verification nor adds the CA. We set
   > `REQUESTS_CA_BUNDLE` (the documented, correct variable) and mirror it into
   > `CURL_CA_BUNDLE` for any subprocess/curl-based checks.

2. **`pip install 'httpbin[mainapp]==0.10.2' 'pytest-httpbin==2.1.0'`**
   - `httpbin[mainapp]` provides the WSGI `httpbin:app` and pulls in `gunicorn` +
     `gevent` (the `mainapp` extra).
   - `pytest-httpbin` ships the self-signed cert/key pair (`server.pem`, `server.key`)
     we point gunicorn at for HTTPS.
   - The versions are pinned to a known-good combination.

3. **`HTTPBIN_CERT_DIR=$(...)`**
   Resolves the cert directory at runtime instead of hard-coding a `site-packages`
   path. The debug-gym reference hard-codes the conda `site-packages` path; deriving it
   from `pytest_httpbin.__file__` is more robust across Python/conda layouts.

4. **Two `gunicorn` workers, ports 80 and 443**
   - Port 80 serves plain HTTP; port 443 serves HTTPS with the bundled cert.
   - `-k gevent` gives an async worker so a test that makes a request to the same server
     it's running against doesn't deadlock on a single sync worker.
   - `(nohup ... &)` in a subshell fully detaches the process so the eval script's
     shell moves on immediately (important in non-TTY container exec).

5. **`sleep 2`**
   Gives gunicorn time to bind before tests start. (Added beyond the debug-gym
   reference; without a small wait, fast test startup can race the server bind.)

6. **`echo "127.0.0.1    httpbin.org" >> /etc/hosts`**
   Redirects all `httpbin.org` traffic to the local server. The test code is unchanged;
   DNS resolution is what's diverted.

## Placement in the eval script

The commands are inserted **after `conda activate` + package install** and **before**
the test-file reset / test-patch apply / test run. This matters because:

- The correct Python env must be active so `pip install` and the `python -c` cert lookup
  target the testbed env.
- The server must be up before the test command runs.

Generated script order for a `psf/requests` instance:
```
source activate → conda activate testbed → cd /testbed
git config / status / show / diff
source activate → conda activate testbed
<install>
<httpbin setup: 7 commands above>
reset test files → apply test patch
: 'START_TEST_OUTPUT' → pytest ... → : 'END_TEST_OUTPUT'
reset test files
```

## Requirements / assumptions for your inference environment

- **Root / port binding**: binding to ports 80 and 443 requires root (or
  `CAP_NET_BIND_SERVICE`). SWE-bench eval scripts run as root inside the container, so
  this works there. If your inference sandbox runs unprivileged, either grant the
  capability or bind to high ports (e.g. 8080/8443) and adjust `/etc/hosts` +
  test config accordingly — note the tests expect the standard ports, so high ports
  need extra redirection (iptables) rather than a plain hosts entry.
- **`/etc/hosts` must be writable** (true for root in the container).
- **Network egress to PyPI** is needed once, to install the two packages. If your
  environment is offline, pre-bake these into the image instead.
- **gunicorn/gevent** come transitively from `httpbin[mainapp]`; no separate install.

## Scope / safety

- Guarded by `instance["repo"] == "psf/requests"`; verified that other repos (e.g.
  `django/django`) receive none of these commands.
- Purely additive to the eval script; no change to grading, patch application, or the
  test command itself.

## Verification done

- `python -m py_compile swebench/harness/test_spec/python.py` — passes.
- Rendered the eval command list for a `psf/requests` instance and confirmed the seven
  setup lines appear in the correct position; confirmed a `django/django` instance
  contains no `httpbin` commands.
- Note: a full end-to-end run against actual `psf__requests-*` instances (building the
  image and executing the container) was **not** run here — recommend validating one
  instance end-to-end before a full sweep.

## Suggested next step for a real run

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name <path-to>/swe-bench-verified.jsonl \
  --predictions_path gold \
  --run_id httpbin-smoke \
  --instance_ids psf__requests-2317 \
  --max_workers 1
```
Then inspect `logs/run_evaluation/httpbin-smoke/.../psf__requests-2317/test_output.txt`
to confirm tests hit the local server (no `httpbin.org` connection errors).
