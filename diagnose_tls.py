#!/usr/bin/env python3
"""
Diagnose the TLS trust failure.

curl.exe succeeded and Python failed on the same machine, which means
the server is fine and the two are consulting different trust stores.
curl on Windows uses the OS store. Python uses certifi's bundle.

Two causes produce the identical "unable to get local issuer certificate"
message, and they have different consequences for deployment:

  A) The server sends an incomplete chain, omitting an intermediate.
     Windows silently fetches the missing cert via the AIA extension.
     OpenSSL does not. This would fail on a GitHub Actions runner too.

  B) Something local is intercepting TLS: a corporate proxy, or
     antivirus with HTTPS scanning. Its root sits in the Windows store
     but not in certifi. This affects only this machine, and CI is fine.

Writes nothing, sends no application data.
"""

import socket
import ssl
import sys

HOST = "www.bccourts.ca"
PORT = 443


def name_of(cert_der):
    try:
        from cryptography import x509
        c = x509.load_der_x509_certificate(cert_der)
        def field(n, oid):
            try:
                return n.get_attributes_for_oid(oid)[0].value
            except (IndexError, AttributeError):
                return "?"
        from cryptography.x509.oid import NameOID
        return (field(c.subject, NameOID.COMMON_NAME),
                field(c.issuer, NameOID.COMMON_NAME),
                field(c.issuer, NameOID.ORGANIZATION_NAME),
                c.not_valid_after_utc.date().isoformat())
    except Exception as e:
        return ("<unparsed: %s>" % e, "?", "?", "?")


def chain():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((HOST, PORT), timeout=20) as raw:
        with ctx.wrap_socket(raw, server_hostname=HOST) as s:
            try:
                return s.get_unverified_chain() or []
            except AttributeError:
                der = s.getpeercert(binary_form=True)
                return [der] if der else []


def main():
    print("host: %s" % HOST)
    print()

    try:
        import certifi
        print("certifi bundle: %s" % certifi.where())
    except ImportError:
        print("certifi not installed")
    print("openssl       : %s" % ssl.OPENSSL_VERSION)
    print()

    print("chain presented by the server")
    try:
        certs = chain()
    except Exception as e:
        print("  could not connect at all: %s" % e)
        print("  This is a network problem, not a trust problem.")
        return 1

    if not certs:
        print("  none returned")
        return 1

    for i, der in enumerate(certs):
        cn, icn, iorg, exp = name_of(der)
        role = "leaf" if i == 0 else "intermediate %d" % i
        print("  [%d] %-14s subject CN : %s" % (i, role, cn))
        print("      %-14s issuer  CN : %s" % ("", icn))
        print("      %-14s issuer  O  : %s" % ("", iorg))
        print("      %-14s expires    : %s" % ("", exp))
    print()

    # Verdict
    leaf_issuer_org = name_of(certs[0])[2]
    print("verdict")
    if len(certs) == 1:
        print("  Server sent ONE certificate and no intermediate.")
        print("  Cause A: incomplete chain. Windows fixes this by fetching")
        print("  the intermediate itself; OpenSSL will not. A Linux CI")
        print("  runner would hit the same error.")
    else:
        print("  Server sent %d certificates, so the chain looks complete."
              % len(certs))
        print("  That points at cause B: local TLS interception.")
        print("  Issuing organisation is %r." % leaf_issuer_org)
        print("  If that is not a public CA, it is your proxy or antivirus.")
    print()

    # Does the OS store accept it?
    print("does the Windows store accept this chain?")
    try:
        import truststore
        tctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with socket.create_connection((HOST, PORT), timeout=20) as raw:
            with tctx.wrap_socket(raw, server_hostname=HOST):
                pass
        print("  YES. Using the OS trust store fixes this.")
    except ImportError:
        print("  unknown, truststore not installed. Run:")
        print("      python -m pip install truststore")
        print("  then run this script again.")
    except Exception as e:
        print("  NO: %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
