"""Safe Sigma REST client with browser-first refresh and one 401 retry.

Vendored and adapted for this standalone plugin from
``sigma-migration-skills/shared/lib/sigma_rest.py`` at
1106989164fec827ab44796e24ae65eb71490a4d. The local adaptation keeps the
SSRS publisher's stricter no-redirect transport and typed HTTP errors needed
for bounded export polling.

Credential precedence is:
  1. a caller-provided ``SIGMA_API_TOKEN``;
  2. ``auth.json`` in ``SIGMA_WORKDIR`` or the current directory;
  3. the vendored browser-first provider, which uses the OS keychain and then
     falls back to client credentials in ``auto`` mode.

Known token ages are refreshed proactively after 50 minutes. An age-unknown
token is honored until a 401. Every request refreshes and retries at most once.
"""

import datetime as _dt
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


NEUTRAL_ENV = os.path.expanduser("~/.sigma-migration/env")
TOKEN_TTL_SECONDS = 50 * 60
SIGMA_DOMAIN = "sigma" "computing.com"
PUBLISHED_API_HOSTS = frozenset(
    (
        f"aws-api.{SIGMA_DOMAIN}",
        f"api.us-a.aws.{SIGMA_DOMAIN}",
        f"api.ca.aws.{SIGMA_DOMAIN}",
        f"api.eu.aws.{SIGMA_DOMAIN}",
        f"api.au.aws.{SIGMA_DOMAIN}",
        f"api.uk.aws.{SIGMA_DOMAIN}",
        f"api.us.azure.{SIGMA_DOMAIN}",
        f"api.eu.azure.{SIGMA_DOMAIN}",
        f"api.ca.azure.{SIGMA_DOMAIN}",
        f"api.uk.azure.{SIGMA_DOMAIN}",
        f"api.au.azure.{SIGMA_DOMAIN}",
        f"api.{SIGMA_DOMAIN}",
        f"api.sa.gcp.{SIGMA_DOMAIN}",
    )
)
_PROVIDER_EXPORT = re.compile(
    r"\Aexport (SIGMA_API_TOKEN|SIGMA_TOKEN_MINTED_AT|SIGMA_AUTH_METHOD)="
    r"([A-Za-z0-9._~+/=:-]+)\Z"
)


class SigmaError(RuntimeError):
    """Base error for Sigma API and response failures."""


class SigmaAuthError(SigmaError):
    """No usable Sigma authentication could be obtained."""


class SigmaSecurityError(SigmaError):
    """A URL or redirect violated the credential-boundary contract."""


class SigmaHttpError(SigmaError):
    """A non-success response, with status retained for bounded polling."""

    def __init__(self, method, path, status, reason="", body=b""):
        self.method = method.upper()
        self.path = path
        self.status = status
        self.reason = reason
        self.body = bytes(body or b"")
        detail = self.body.decode("utf-8", "replace")
        suffix = f"\n{detail[:1000]}" if detail else ""
        super().__init__(
            f"{self.method} {self.path} -> {self.status} {self.reason}{suffix}"
        )


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward Basic or bearer credentials through an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            f"redirect refused ({req.full_url} -> {newurl})",
            headers,
            fp,
        )


class _Resp:
    __slots__ = ("status", "body", "reason")

    def __init__(self, status, body, reason=""):
        self.status = status
        self.body = (
            body
            if isinstance(body, (bytes, bytearray))
            else str(body or "").encode("utf-8")
        )
        self.reason = reason


_token_mutex = threading.Lock()
_token_override = None
_minted_at = None
_refresh_inflight = False
_validated_bases = set()


def _iso_z(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _parse_iso_epoch(value):
    try:
        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def _load_neutral_env(env=None, path=None):
    """Load missing Sigma settings from the agent-neutral environment file."""
    env = os.environ if env is None else env
    env_path = path or NEUTRAL_ENV
    if not os.path.isfile(env_path):
        return
    with open(env_path, encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not re.fullmatch(r"SIGMA_[A-Z0-9_]+", key) or key in env:
                continue
            try:
                values = shlex.split(raw_value, posix=True)
            except ValueError:
                continue
            if len(values) == 1:
                env[key] = values[0]


def _load_auth_json(env=None, cwd=None):
    """Load a shell-neutral token handoff when no explicit env token exists."""
    env = os.environ if env is None else env
    if env.get("SIGMA_API_TOKEN"):
        return
    cwd = os.getcwd() if cwd is None else cwd
    candidates = []
    for directory in (env.get("SIGMA_WORKDIR"), cwd):
        if directory and directory not in candidates:
            candidates.append(directory)
    for directory in candidates:
        path = os.path.join(directory, "auth.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as handle:
                auth = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(auth, dict):
            return
        token = auth.get("SIGMA_API_TOKEN")
        if token:
            env.setdefault("SIGMA_API_TOKEN", token)
            minted_at = auth.get("SIGMA_TOKEN_MINTED_AT")
            if not minted_at:
                try:
                    minted_at = _iso_z(os.path.getmtime(path))
                except OSError:
                    minted_at = None
            if minted_at:
                env.setdefault("SIGMA_TOKEN_MINTED_AT", minted_at)
        for key in ("SIGMA_BASE_URL", "SIGMA_AUTH_METHOD"):
            if auth.get(key):
                env.setdefault(key, auth[key])
        return


def bootstrap_credentials(env=None, cwd=None):
    _load_neutral_env(env)
    _load_auth_json(env, cwd)


def validate_base_url(value, allow_insecure=None):
    """Validate and normalize the API origin before transmitting credentials."""
    if not value:
        raise SigmaSecurityError("SIGMA_BASE_URL is not set")
    allow_insecure = (
        os.environ.get("SIGMA_ALLOW_INSECURE_BASE_URL") == "1"
        if allow_insecure is None
        else allow_insecure
    )
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise SigmaSecurityError("SIGMA_BASE_URL is not a valid URL") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        not host
        or not parsed.scheme
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise SigmaSecurityError(
            "SIGMA_BASE_URL must be an API origin without credentials, path, "
            "query, or fragment"
        )
    if allow_insecure:
        if parsed.scheme.lower() not in ("http", "https"):
            raise SigmaSecurityError("SIGMA_BASE_URL must use http:// or https://")
        netloc = host if port is None else f"{host}:{port}"
        return f"{parsed.scheme.lower()}://{netloc}"
    if parsed.scheme.lower() != "https" or port not in (None, 443):
        raise SigmaSecurityError("SIGMA_BASE_URL must use HTTPS on the default port")
    if host not in PUBLISHED_API_HOSTS:
        raise SigmaSecurityError(
            f"SIGMA_BASE_URL host is not a published Sigma API host: {host}"
        )
    return f"https://{host}"


def base_url():
    base = validate_base_url(os.environ.get("SIGMA_BASE_URL"))
    key = (base, os.environ.get("SIGMA_ALLOW_INSECURE_BASE_URL") == "1")
    _validated_bases.add(key)
    return base


def token_minted_at():
    with _token_mutex:
        minted_at = _minted_at
    if minted_at is not None:
        return minted_at
    return _parse_iso_epoch(os.environ.get("SIGMA_TOKEN_MINTED_AT"))


def _token_stale():
    minted_at = token_minted_at()
    return (
        minted_at is not None
        and time.time() - minted_at > TOKEN_TTL_SECONDS
    )


def auth_token():
    """Return the caller token, proactively refreshing when its age is stale."""
    with _token_mutex:
        token = _token_override
    token = token or os.environ.get("SIGMA_API_TOKEN")
    if not token or _token_stale():
        return refresh_token()
    return token


def token_provider_result():
    """Invoke the vendored provider and parse exports as data, never shell."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.environ.get("SIGMA_TOKEN_PROVIDER"),
        os.path.abspath(os.path.join(here, "..", "get_token.py")),
    )
    provider = next(
        (path for path in candidates if path and os.path.isfile(path)),
        None,
    )
    if provider is None:
        raise SigmaAuthError("vendored Sigma get_token.py provider not found")

    commands = []
    configured = os.environ.get("SIGMA_PYTHON")
    if configured:
        commands.append(shlex.split(configured, posix=os.name != "nt"))
    commands.extend(([sys.executable], ["python3"], ["python"], ["py", "-3"]))

    completed = None
    for command in commands:
        if not command:
            continue
        try:
            completed = subprocess.run(
                command + [provider, "--print-export"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            continue
        break
    if completed is None:
        raise SigmaAuthError("Python is unavailable; cannot refresh the Sigma token")
    if completed.returncode != 0:
        detail = completed.stderr.strip()
        suffix = f": {detail}" if detail else ""
        raise SigmaAuthError(f"Sigma token provider failed{suffix}")

    values = {}
    for line in completed.stdout.splitlines():
        match = _PROVIDER_EXPORT.fullmatch(line)
        if match:
            values[match.group(1)] = match.group(2)
    required = ("SIGMA_API_TOKEN", "SIGMA_TOKEN_MINTED_AT", "SIGMA_AUTH_METHOD")
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise SigmaAuthError(f"Sigma token provider omitted {', '.join(missing)}")
    return values


def refresh_token():
    """Refresh through the dual provider and cache the result in this process."""
    global _refresh_inflight, _token_override, _minted_at
    with _token_mutex:
        if _refresh_inflight:
            if _token_override:
                return _token_override
            raise SigmaAuthError("Sigma token refresh is already in progress")
        _refresh_inflight = True
    try:
        values = token_provider_result()
        minted_at = _parse_iso_epoch(values["SIGMA_TOKEN_MINTED_AT"])
        if minted_at is None:
            raise SigmaAuthError(
                "Sigma token provider returned an invalid mint timestamp"
            )
        with _token_mutex:
            _token_override = values["SIGMA_API_TOKEN"]
            _minted_at = minted_at
        os.environ.update(values)
        return values["SIGMA_API_TOKEN"]
    finally:
        with _token_mutex:
            _refresh_inflight = False


def _ssl_context():
    if os.environ.get("SIGMA_INSECURE_TLS"):
        print(
            "WARNING: SIGMA_INSECURE_TLS set — TLS certificate verification is "
            "DISABLED for Sigma requests.",
            file=sys.stderr,
        )
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    try:
        import truststore

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        pass
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _http_opener():
    """Build an HTTP(S)-only opener that refuses every redirect."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.HTTPSHandler(context=_ssl_context()),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        RejectRedirectHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


def _send(method, url, headers, body, timeout):
    """Low-level transport seam; tests replace this without network access."""
    parsed = urllib.parse.urlsplit(url)
    insecure = os.environ.get("SIGMA_ALLOW_INSECURE_BASE_URL") == "1"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and insecure):
        raise SigmaSecurityError(
            f"refusing non-HTTPS request URL with scheme {parsed.scheme!r}"
        )
    data = body.encode("utf-8") if isinstance(body, str) else body
    request = urllib.request.Request(
        url, data=data, method=method.upper(), headers=headers
    )
    try:
        with _http_opener().open(request, timeout=timeout) as response:
            return _Resp(
                getattr(response, "status", 200),
                response.read(),
                getattr(response, "reason", ""),
            )
    except urllib.error.HTTPError as exc:
        body = exc.read() if exc.fp is not None else b""
        return _Resp(exc.code, body, exc.reason)
    except urllib.error.URLError as exc:
        raise SigmaError(f"{method.upper()} {url} failed: {exc.reason}") from exc


def request(
    method,
    path,
    body=None,
    content_type="application/json",
    accept="application/json",
    binary=False,
    allow_statuses=(),
    timeout=120,
):
    """Send one API request, refreshing once on 401 and parsing the response."""
    if method.lower() not in ("get", "post", "put", "patch", "delete"):
        raise ValueError(f"unsupported method {method}")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise SigmaSecurityError("Sigma request path must be origin-relative")
    base = base_url()
    url = f"{base}{path}"
    if isinstance(body, (dict, list)):
        body = json.dumps(body).encode("utf-8")

    for attempt in range(2):
        headers = {"Authorization": f"Bearer {auth_token()}", "Accept": accept}
        if body is not None:
            headers["Content-Type"] = content_type
        response = _send(method, url, headers, body, timeout)
        if response.status == 401 and attempt == 0:
            refresh_token()
            continue
        if response.status in allow_statuses:
            return None
        if not 200 <= response.status < 300:
            raise SigmaHttpError(
                method,
                path,
                response.status,
                response.reason,
                response.body,
            )
        if binary:
            return None if response.status == 204 else bytes(response.body)
        text = response.body.decode("utf-8", "replace")
        if accept != "application/json":
            return text
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise SigmaError(
                f"{method.upper()} {path} returned non-JSON content"
            ) from exc

    raise AssertionError("request retry loop exhausted")


def reset_runtime_state():
    """Reset process-local caches; intended for credential-free tests."""
    global _token_override, _minted_at, _refresh_inflight
    with _token_mutex:
        _token_override = None
        _minted_at = None
        _refresh_inflight = False
    _validated_bases.clear()


bootstrap_credentials()
