"""Read-only Solana JSON-RPC client: on-disk cache, one shared rate limit for all processes, retries.

Standard library only. Some Python builds ship without root certificates; then the request falls back to curl.
"""
import hashlib
import json
import os
import ssl
import subprocess
import time
import urllib.error
import urllib.request

try:
    import fcntl
except ImportError:                                    # Windows: the limit is per process, not shared
    fcntl = None

DEFAULT_URL = "https://api.mainnet-beta.solana.com"
RETRYABLE_CODES = {429, -32005, -32019, -32603}        # -32019: long-term storage busy, "try again"


class RpcError(RuntimeError):
    pass


def _curl(url, body=None, timeout=60):
    cmd = ["curl", "-s", "-m", str(timeout)]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", body]
    r = subprocess.run(cmd + [url], capture_output=True, text=True)
    if r.returncode != 0:
        raise RpcError(f"curl exit {r.returncode}")
    return r.stdout


def http(url, body=None, timeout=60):
    """POST (or GET when body is None) and return the response text. HTTP errors return their body."""
    data = body.encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                                          "User-Agent": "solana-fund-tracer"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode()
    except urllib.error.HTTPError as e:                # 429 and friends carry a JSON-RPC error body
        return e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        if isinstance(e.reason, ssl.SSLCertVerificationError):
            return _curl(url, body, timeout)
        raise


class Rpc:
    def __init__(self, url=None, cache_dir=None, rps=None, transport=None, sleep=time.sleep, fresh=False):
        self.url = url or os.environ.get("FUNDTRACE_RPC_URL") or DEFAULT_URL
        rps = float(rps if rps is not None else os.environ.get("FUNDTRACE_RPS") or 1)   # 0 = no limit
        self.min_interval = 0.0 if rps <= 0 else 1.05 / rps
        self.cache_dir = cache_dir or os.environ.get("FUNDTRACE_CACHE") or os.path.join(".fundtrace", "cache")
        self.transport = transport or http
        self.sleep = sleep
        self.fresh = fresh                             # re-read signature lists; transactions never change
        self.stats = {"net": 0, "cache": 0}
        self._last = 0.0

    # --- rate limit -------------------------------------------------------------------------------------------
    def _wait_turn(self):
        if self.min_interval <= 0:
            return
        if fcntl is None:
            self.sleep(max(0.0, self.min_interval - (time.time() - self._last)))
            self._last = time.time()
            return
        os.makedirs(self.cache_dir, exist_ok=True)
        with open(os.path.join(self.cache_dir, ".ratelock"), "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                last = float(f.read().strip() or 0)
            except ValueError:
                last = 0.0
            self.sleep(max(0.0, self.min_interval - (time.time() - last)))
            f.seek(0)
            f.truncate()
            f.write(str(time.time()))
            f.flush()
            fcntl.flock(f, fcntl.LOCK_UN)

    # --- calls ------------------------------------------------------------------------------------------------
    def call(self, method, params, retries=7):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, sort_keys=True)
        path = os.path.join(self.cache_dir, hashlib.sha1(body.encode()).hexdigest() + ".json")
        use_cache = not (self.fresh and method == "getSignaturesForAddress")
        if use_cache and os.path.exists(path):
            self.stats["cache"] += 1
            with open(path) as f:
                return json.load(f)
        for attempt in range(retries):
            self._wait_turn()
            self.stats["net"] += 1
            try:
                j = json.loads(self.transport(self.url, body))
            except (ValueError, OSError, RpcError):
                self.sleep(min(30, 3 * (attempt + 1)))
                continue
            err = j.get("error")
            if err:
                if err.get("code") in RETRYABLE_CODES or "rate" in str(err).lower() or "try again" in str(err).lower():
                    self.sleep(5 * (attempt + 1))
                    continue
                raise RpcError(f"{method}: {err}")
            os.makedirs(self.cache_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump(j, f)
            return j
        raise RpcError(f"{method}: no answer after {retries} attempts")

    def tx(self, sig):
        # Version 1 transactions exist on mainnet since Sept 2026; a lower maximum makes the node refuse them.
        return self.call("getTransaction",
                         [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}]).get("result")

    def sigs(self, addr, before=None, limit=1000, until=None):
        """Signatures of an address, newest first: older than `before`, newer than `until`."""
        opt = {"limit": limit}
        if before:
            opt["before"] = before
        if until:
            opt["until"] = until
        return self.call("getSignaturesForAddress", [addr, opt]).get("result") or []

    def all_sigs(self, addr, cap=20000):
        """Every signature of an address, newest first, in pages of 1000."""
        out, before = [], None
        while len(out) < cap:
            page = self.sigs(addr, before)
            if not page:
                break
            out += page
            if len(page) < 1000:
                break
            before = page[-1]["signature"]
        return out[:cap]
