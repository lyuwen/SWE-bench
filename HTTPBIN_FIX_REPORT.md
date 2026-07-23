# Fixing `psf/requests` httpbin.org flakiness in SWE-bench evaluation

## Problem

The `psf__requests-*` instances in `swe-bench-verified.jsonl` run tests that make live
HTTP/HTTPS requests to `httpbin.org`. That service is an external dependency and is
frequently slow or unreachable, so these instances fail or time out for reasons that
have nothing to do with the model's patch. This produces false negatives.

## Solution (summary)

Stand up a **local** httpbin server inside the evaluation container and redirect
`httpbin.org` to `127.0.0.1`, so the tests hit a local, deterministic server instead of
the public internet. The starting point was
[microsoft/debug-gym](https://github.com/microsoft/debug-gym/blob/cc3fe3ef4ce08919e522eb00ea1bea5689f3b53e/debug_gym/gym/envs/swe_bench.py#L109-L122),
but that reference has two latent TLS bugs that only surface when the tests actually
verify HTTPS against the `httpbin.org` hostname (see "Why each line matters"). This
implementation fixes both.

The fix is applied only for `repo == "psf/requests"`; all other repos are unaffected.

## Result (empirical, gold patches, 8 psf/requests instances)

| Setup | Resolved |
|---|---|
| debug-gym-style (pytest_httpbin cert + `REQUESTS_CA_BUNDLE`) | 4 / 8 |
| **This fix (httpbin.org-SAN cert + vendored-bundle append)** | **7 / 8** |

The one remaining unresolved instance, `psf__requests-2317`, fails on a single
PASS_TO_PASS test, `test_auth_is_stripped_on_redirect_off_host`, which issues a real
request to `http://www.google.co.uk`. That test needs external internet egress the eval
container does not have; it is unrelated to httpbin and no local-httpbin fix can address
it. All FAIL_TO_PASS tests pass for all 8 instances, and every httpbin-dependent test
passes.

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
mkdir -p /tmp/swebench_httpbin_certs
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout /tmp/swebench_httpbin_certs/key.pem -out /tmp/swebench_httpbin_certs/cert.pem -days 3650 \
  -subj '/CN=httpbin.org' \
  -addext 'subjectAltName=DNS:httpbin.org,DNS:localhost,IP:127.0.0.1'
export REQUESTS_CA_BUNDLE=/tmp/swebench_httpbin_certs/cert.pem
export CURL_CA_BUNDLE=/tmp/swebench_httpbin_certs/cert.pem
cat /tmp/swebench_httpbin_certs/cert.pem >> "$(python -c "import requests; print(requests.certs.where())")"
(nohup gunicorn -b 127.0.0.1:80 -k gevent httpbin:app > /dev/null 2>&1 &)
(nohup gunicorn -b 127.0.0.1:443 --certfile=/tmp/swebench_httpbin_certs/cert.pem --keyfile=/tmp/swebench_httpbin_certs/key.pem -k gevent httpbin:app > /dev/null 2>&1 &)
sleep 2
echo "127.0.0.1    httpbin.org" >> /etc/hosts
```

## Why each line matters (this is the part to port into your inference logic)

The central difficulty is **TLS hostname verification**. The tests connect to
`https://httpbin.org/...`, and after the `/etc/hosts` redirect that connection lands on
the local server — but the server's certificate must be valid for the hostname
`httpbin.org`, and the client must trust it. Two independent trust paths exist in the
`requests` codebase, and both must be satisfied.

1. **Generate a cert with `subjectAltName=DNS:httpbin.org`** (the `openssl req` line)
   pytest_httpbin's bundled cert is issued for `localhost`/`127.0.0.1` only. If you
   serve it and connect to `https://httpbin.org`, verification fails with:
   ```
   ssl.SSLCertVerificationError: hostname 'httpbin.org' doesn't match either of
   'localhost', '127.0.0.1', ...
   ```
   This is the bug that caps the debug-gym approach at 4/8. Generating a self-signed
   cert whose SAN includes `httpbin.org` is what makes HTTPS hostname verification pass.
   (SAN, not just CN — modern OpenSSL ignores CN for hostname matching.)

2. **`export REQUESTS_CA_BUNDLE=<cert>`** (+ mirror to `CURL_CA_BUNDLE`)
   This makes the client trust the self-signed cert for requests that go through
   `Session.request()` / the module-level `requests.get()` etc., because those call
   `merge_environment_settings`, which reads `REQUESTS_CA_BUNDLE` (then `CURL_CA_BUNDLE`)
   from the environment. We point it at our generated cert (which is both the server cert
   and, being self-signed, its own CA).

   > The debug-gym reference used `export CURL_CA_BUNDLE=""`. For `requests` that is a
   > **no-op**: it reads `REQUESTS_CA_BUNDLE or CURL_CA_BUNDLE`, an empty string is falsy
   > and ignored, so verification falls back to the *system* CA bundle (which lacks the
   > cert). It neither disables verification nor adds the CA.

3. **`cat <cert> >> $(python -c "import requests; print(requests.certs.where())")`**
   Tests that call `Session.send()` **directly** (e.g.
   `test_mixed_case_scheme_acceptable`) bypass `merge_environment_settings` entirely, so
   `REQUESTS_CA_BUNDLE` is never consulted — they verify against requests' **bundled**
   CA file (in this vintage of requests, the vendored `requests/cacert.pem`). Appending
   our cert to that file is what makes those tests pass. This is the second fix beyond
   debug-gym, and without it instances like 1921/2317 keep a residual SSL failure.

4. **`pip install 'httpbin[mainapp]==0.10.2' 'pytest-httpbin==2.1.0'`**
   - `httpbin[mainapp]` provides the WSGI `httpbin:app` and pulls in `gunicorn` +
     `gevent` (the `mainapp` extra).
   - `pytest-httpbin` is still installed because it is what the requests test-suite
     expects to be importable in some versions; we no longer use its cert, but keep it
     for compatibility.
   - Versions pinned to a known-good combination.

5. **Two `gunicorn` workers, ports 80 and 443**
   - Port 80 serves plain HTTP; port 443 serves HTTPS with the generated cert/key.
   - `-k gevent` gives an async worker so a test that makes a request to the same server
     it's running against doesn't deadlock on a single sync worker.
   - `(nohup ... &)` in a subshell fully detaches the process so the eval script's shell
     moves on immediately (important in non-TTY container exec).

6. **`sleep 2`**
   Gives gunicorn time to bind before tests start; without a small wait, fast test
   startup can race the server bind.

7. **`echo "127.0.0.1    httpbin.org" >> /etc/hosts`**
   Redirects all `httpbin.org` traffic to the local server. Test code is unchanged; DNS
   resolution is what's diverted. Note the tests read `HTTPBIN_URL` (default
   `http://httpbin.org/`) but many also hard-code `https://httpbin.org`, so the hosts
   redirect (rather than setting `HTTPBIN_URL`) is what covers all cases.

## Placement in the eval script

Inserted **after `conda activate` + package install** and **before** the test-file reset
/ test-patch apply / test run. This matters because:

- The correct Python env must be active so `pip install`, the cert append, and the
  `python -c` lookups target the testbed env.
- The server must be up before the test command runs.

Generated script order for a `psf/requests` instance:
```
source activate → conda activate testbed → cd /testbed
git config / status / show / diff
source activate → conda activate testbed
<install>
<httpbin setup: commands above>
reset test files → apply test patch
: 'START_TEST_OUTPUT' → pytest ... → : 'END_TEST_OUTPUT'
reset test files
```

## Requirements / assumptions for your inference environment

- **Root / port binding**: binding to ports 80 and 443 requires root (or
  `CAP_NET_BIND_SERVICE`). SWE-bench eval scripts run as root inside the container, so
  this works there. If your inference sandbox runs unprivileged, either grant the
  capability or bind to high ports (8080/8443) and redirect 80/443 to them with iptables
  — the tests expect the standard ports, so a plain hosts entry is not enough for high
  ports.
- **`openssl`** must be present (it is, in the requests testbed images — OpenSSL 3.0.x).
- **`/etc/hosts` must be writable** (true for root in the container).
- **Network egress to PyPI** is needed once, to install the two packages. If offline,
  pre-bake them into the image.
- **`requests.certs.where()`** is the correct bundle to append to for this era of
  requests (vendored urllib3). If you port this to a newer requests that uses the
  external `certifi`, `requests.certs.where()` still resolves to certifi's bundle, so the
  same line works — but verify the path is writable.
- **No control over external-egress tests**: a test that redirects to a real third-party
  host (e.g. `test_auth_is_stripped_on_redirect_off_host` → `www.google.co.uk`) will
  fail without outbound internet. This is orthogonal to the httpbin fix.

## Scope / safety

- Guarded by `instance["repo"] == "psf/requests"`; other repos (e.g. `django/django`)
  receive none of these commands.
- Purely additive to the eval script; no change to grading, patch application, or the
  test command itself.
- The harness **pulls** the pre-built image named in the dataset's `image_name` column
  (`is_remote_image` path) rather than building from scratch; the httpbin setup runs at
  eval time on top of the pulled image, so no image rebuild is required.

## Verification done

- `python -m py_compile swebench/harness/test_spec/python.py` — passes.
- Interactive container debugging on `psf__requests-2317`'s image reproduced each SSL
  failure mode and confirmed each fix line resolves it (hostname SAN → fixes
  `hostname doesn't match`; vendored-bundle append → fixes `Session.send()`
  self-signed-cert failure).
- Full harness run on gold patches for all 8 `psf__requests-*` instances:
  **7/8 resolved, 0 errors**; the sole remaining failure is the external-egress test
  described above. Run id `httpbin-gold-test2`.

## Reproduce

```bash
conda activate swebench
python -m swebench.harness.run_evaluation \
  --dataset_name swe-bench-verified.jsonl \
  --predictions_path gold \
  --run_id httpbin-gold-test2 \
  --instance_ids psf__requests-1142 psf__requests-1724 psf__requests-1766 \
    psf__requests-1921 psf__requests-2317 psf__requests-2931 psf__requests-5414 \
    psf__requests-6028 \
  --max_workers 8 --cache_level env
```
