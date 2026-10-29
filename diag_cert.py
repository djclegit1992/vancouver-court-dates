"""Fetch the court's missing intermediate certificate and print it as PEM,
with enough detail to verify it independently."""
import ssl, socket, time
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.hazmat.primitives import hashes
import tlsfix

HOST = "www.bccourts.ca"
leaf = None
for attempt in range(8):
    try:
        leaf = x509.load_der_x509_certificate(tlsfix._presented_chain(HOST, 443)[0])
        break
    except Exception as e:
        print("attempt %d: %s" % (attempt + 1, e)); time.sleep(5)
print("leaf subject :", leaf.subject.rfc4514_string())
print("leaf issuer  :", leaf.issuer.rfc4514_string())
print("leaf expires :", leaf.not_valid_after_utc)
urls = tlsfix._aia_urls(leaf)
print("AIA urls     :", urls)
certs = []
for u in urls:
    certs = tlsfix._fetch(u)
    if certs: break
for c in certs:
    print("---")
    print("subject :", c.subject.rfc4514_string())
    print("issuer  :", c.issuer.rfc4514_string())
    print("valid   :", c.not_valid_before_utc, "->", c.not_valid_after_utc)
    print("sha256  :", c.fingerprint(hashes.SHA256()).hex())
    print("signs leaf:", c.subject == leaf.issuer)
    print(c.public_bytes(Encoding.PEM).decode())
