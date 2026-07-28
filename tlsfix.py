#!/usr/bin/env python3
"""
Repair an incomplete TLS certificate chain.

Some servers send only their leaf certificate and leave the client to
find the intermediate. Windows and most browsers do this silently by
following the Authority Information Access (AIA) extension. OpenSSL
does not, so Python fails with "unable to get local issuer certificate"
against a site that works perfectly in a browser.

This module follows the AIA extension the way a browser would, fetches
the missing intermediate, and writes a bundle combining it with the
normal trusted roots.

What this does NOT do, deliberately: it never disables verification.
The fetched intermediate still has to chain to a root already trusted
on this machine. If it does not, verification fails as it should. All
we are doing is supplying a certificate the server should have sent.

Falls back silently to normal verification if anything goes wrong, so
it can never make the situation worse than not using it.
"""

import os
import socket
import ssl
import tempfile

CACHE = os.path.join(tempfile.gettempdir(), "courtready-ca-bundle.pem")
TIMEOUT = 20
MAX_DEPTH = 4


def _presented_chain(host, port):
    """Every certificate the server actually sends, in DER form."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            try:
                return list(s.get_unverified_chain() or [])
            except AttributeError:
                der = s.getpeercert(binary_form=True)
                return [der] if der else []


def _aia_urls(cert):
    from cryptography import x509
    from cryptography.x509.oid import ExtensionOID, AuthorityInformationAccessOID
    try:
        ext = cert.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    except x509.ExtensionNotFound:
        return []
    return [d.access_location.value for d in ext
            if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]


def _fetch(url):
    """Download an issuer certificate. May be DER, PEM, or PKCS7."""
    import urllib.request
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs7

    req = urllib.request.Request(url, headers={"User-Agent": "CourtreadyBot"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()

    for loader in (x509.load_der_x509_certificate,
                   x509.load_pem_x509_certificate):
        try:
            return [loader(data)]
        except Exception:
            pass
    for loader in (pkcs7.load_der_pkcs7_certificates,
                   pkcs7.load_pem_pkcs7_certificates):
        try:
            certs = loader(data)
            if certs:
                return list(certs)
        except Exception:
            pass
    return []


def build_bundle(host, port=443, cache=CACHE, force=False):
    """
    Return a path to a CA bundle able to verify `host`, or None if the
    normal bundle already works or the repair could not be made.
    """
    if not force and os.path.exists(cache):
        return cache

    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
        import certifi
    except ImportError:
        return None

    try:
        chain = [x509.load_der_x509_certificate(d)
                 for d in _presented_chain(host, port)]
    except Exception:
        return None

    if not chain:
        return None

    # Walk upward from whatever the server sent, fetching each issuer
    # until we reach something self-signed or run out of AIA pointers.
    extra, current, seen = [], chain[-1], set()
    for _ in range(MAX_DEPTH):
        if current.issuer == current.subject:
            break
        urls = _aia_urls(current)
        if not urls:
            break
        got = []
        for u in urls:
            try:
                got = _fetch(u)
            except Exception:
                got = []
            if got:
                break
        if not got:
            break
        issuer = got[0]
        fp = issuer.fingerprint(issuer.signature_hash_algorithm)
        if fp in seen:
            break
        seen.add(fp)
        extra.append(issuer)
        current = issuer

    if not extra:
        return None

    try:
        with open(certifi.where(), "rb") as f:
            roots = f.read()
        with open(cache, "wb") as f:
            f.write(roots)
            if not roots.endswith(b"\n"):
                f.write(b"\n")
            for c in extra:
                f.write(c.public_bytes(Encoding.PEM))
    except Exception:
        return None

    return cache


def verify_works(host, port=443, bundle=None):
    """Does a real verified handshake succeed?"""
    ctx = ssl.create_default_context(cafile=bundle)
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
            with ctx.wrap_socket(raw, server_hostname=host):
                return True
    except Exception:
        return False


def ensure(host, port=443):
    """
    Make verification of `host` work if it currently does not.

    Returns a description of what happened, for logging. The bundle
    path, if one was built, is exported through REQUESTS_CA_BUNDLE so
    requests picks it up automatically.
    """
    if verify_works(host, port):
        return "ok: chain verifies normally"

    bundle = build_bundle(host, port, force=True)
    if not bundle:
        return "FAILED: chain incomplete and no issuer could be fetched"

    if verify_works(host, port, bundle):
        os.environ["REQUESTS_CA_BUNDLE"] = bundle
        os.environ["SSL_CERT_FILE"] = bundle
        return "repaired: fetched missing intermediate via AIA"

    return "FAILED: fetched an intermediate but the chain still fails"


if __name__ == "__main__":
    import sys
    h = sys.argv[1] if len(sys.argv) > 1 else "www.bccourts.ca"
    print("%s: %s" % (h, ensure(h)))
